"""Stage 5 - purchase-order matching.

* Exact PO lookup by normalised reference; a PO is only accepted if it belongs to the verified vendor.
* If the reference is missing / unknown / belongs to someone else, candidates are suggested with scores
  and reasons - never auto-selected.
* Amount is compared with the PO's *remaining* balance (partial billing is normal); tolerance is
  "x% of PO value or an absolute cap, whichever is lower".
* Line items are matched by description similarity, then checked for quantity and unit price. A single
  invoice line that equals the sum of several unmatched PO lines is reconciled as a bundle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from .checks import FAIL, INFO, PASS, UNABLE, WARN, Check, EffectiveInvoice, fmt_money
from .vendor import VendorResult


@dataclass
class POResult:
    status: str                          # MATCHED / MISSING / NOT_FOUND / VENDOR_MISMATCH / UNVERIFIED_VENDOR
    po: Optional[dict] = None            # the referenced PO (shown even if not accepted)
    accepted: bool = False
    candidates: list[dict] = field(default_factory=list)
    line_matches: list[dict] = field(default_factory=list)
    amount: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)


def _norm_desc(s: str) -> str:
    return " ".join(s.lower().replace("-", " ").split())


def _ex_tax(amount: Optional[float], rate: Optional[float], inclusive: bool) -> Optional[float]:
    if amount is None:
        return None
    return round(amount / (1 + (rate or 0) / 100), 2) if inclusive else amount


def suggest_candidates(inv: EffectiveInvoice, pos: list[dict], vendor_id: Optional[str],
                       exclude: Optional[str] = None, near_ref: Optional[str] = None) -> list[dict]:
    total = inv.num("total_amount")
    out = []
    for po in pos:
        if po["po_number"] == exclude or po["status"] != "OPEN":
            continue
        if vendor_id and po["vendor_id"] != vendor_id:
            continue
        reasons, score = [], 0.0
        # description + price evidence from line items
        best_line = 0.0
        price_hit = False
        for li in inv.line_items:
            for pl in po["lines"]:
                s = fuzz.token_set_ratio(_norm_desc(li["description"]), _norm_desc(pl["description"]))
                if s > best_line:
                    best_line = s
                    up = _ex_tax(li.get("unit_price"), pl["tax_rate"], inv.tax_mode == "inclusive")
                    price_hit = up is not None and abs(up - pl["unit_price"]) <= max(0.01, pl["unit_price"] * 0.02)
        score += 0.55 * best_line / 100
        if best_line >= 70:
            reasons.append(f"line description {best_line:.0f}% similar")
        if price_hit:
            score += 0.2
            reasons.append("unit price matches")
        if total is not None:
            rem = po["remaining"]
            if rem > 0 and total <= rem * 1.02:
                score += 0.15
                reasons.append(f"fits remaining balance {fmt_money(rem, po['currency'])}")
            if abs(total - rem) <= 1:
                score += 0.1
                reasons.append("equals remaining balance")
        if near_ref and Levenshtein.distance(near_ref, po["po_number"]) == 1:
            score += 0.15
            reasons.append(f"PO number one character from '{near_ref}'")
        if vendor_id:
            reasons.insert(0, "same vendor")
        if score >= 0.35:
            out.append(dict(po_number=po["po_number"], vendor_id=po["vendor_id"], vendor_name=po["vendor_name"],
                            description=po["description"], remaining=po["remaining"], total=po["total_amount"],
                            currency=po["currency"], score=round(min(score, 1.0), 2), reasons=reasons))
    return sorted(out, key=lambda c: -c["score"])[:3]


def match_po(inv: EffectiveInvoice, vendor: VendorResult, pos: list[dict], rules: dict) -> POResult:
    tol = rules["tolerance"]
    thr = rules["extraction"]["confidence_threshold"]
    ref = inv.get("po_reference")
    po_by_no = {p["po_number"]: p for p in pos}
    vid = vendor.vendor_id
    ch: list[Check] = []
    res = POResult(status="MISSING")

    if not ref:
        f = inv.fields.get("po_reference")
        unreadable = f is not None and f.original is None and f.status == "MISSING"
        ch.append(Check("po", "PO_REFERENCE", "PO reference present", FAIL,
                        "PO reference could not be read on the document" if unreadable else
                        "No PO reference on the invoice", reason_code="PO_MISSING",
                        rule="Never auto-select a PO; suggest candidates for a human"))
        res.candidates = suggest_candidates(inv, pos, vid or (vendor.tentative or {}).get("id"))
        if res.candidates:
            ch.append(Check("po", "PO_CANDIDATES", "Candidate POs", INFO,
                            f"{len(res.candidates)} candidate PO(s) suggested for the reviewer: " +
                            ", ".join(f"{c['po_number']} ({c['score']:.0%})" for c in res.candidates),
                            evidence={"candidates": res.candidates}))
        res.checks = ch
        return res

    if inv.status("po_reference") == "UNCERTAIN":
        ch.append(Check("po", "PO_REFERENCE_CONFIDENCE", "PO reference read reliably", WARN,
                        f"PO reference '{ref}' was read with low confidence", rule=f"confidence >= {thr:.0%}",
                        reason_code="PO_UNCERTAIN"))

    po = po_by_no.get(ref)
    if not po:
        res.status = "NOT_FOUND"
        ch.append(Check("po", "PO_EXISTS", "PO exists", FAIL, f"{ref} does not exist in the PO register",
                        actual=ref, reason_code="PO_NOT_FOUND"))
        res.candidates = suggest_candidates(inv, pos, vid, near_ref=ref)
        res.checks = ch
        return res

    res.po = po
    ch.append(Check("po", "PO_EXISTS", "PO exists", PASS,
                    f"{ref} found: {po['description']} ({po['vendor_id']} · {po['vendor_name']})", actual=ref))

    # --- vendor on PO must be the verified invoice vendor
    inv_tax = (inv.get("vendor_tax_id") or "").upper()
    if vid and po["vendor_id"] == vid:
        ch.append(Check("po", "PO_VENDOR", "PO belongs to invoice vendor", PASS,
                        f"{ref} was issued to {po['vendor_name']}", expected=po["vendor_name"],
                        actual=vendor.vendor["name"]))
    else:
        if vid:
            msg = (f"{ref} was issued to {po['vendor_id']} · {po['vendor_name']} (tax ID {po['vendor_tax_id']}), "
                   f"but this invoice is from {vid} · {vendor.vendor['name']} (tax ID {inv_tax}). "
                   f"PO match NOT accepted.")
            actual = f"{vendor.vendor['name']} ({inv_tax})"
        elif inv_tax and inv_tax != (po["vendor_tax_id"] or "").upper():
            msg = (f"{ref} was issued to {po['vendor_name']} (tax ID {po['vendor_tax_id']}), but the invoice was "
                   f"issued under tax ID {inv_tax} ('{inv.get('vendor_name')}'). PO match NOT accepted.")
            actual = f"{inv.get('vendor_name')} ({inv_tax})"
        else:
            msg = f"Cannot confirm the invoice vendor owns {ref} - vendor identity not verified."
            actual = inv.get("vendor_name")
        ch.append(Check("po", "PO_VENDOR", "PO belongs to invoice vendor", FAIL, msg,
                        expected=f"{po['vendor_name']} ({po['vendor_tax_id']})", actual=actual,
                        rule="An invoice may only be matched to a PO issued to the same vendor",
                        reason_code="VENDOR_PO_MISMATCH",
                        evidence={"po_vendor": {"id": po["vendor_id"], "name": po["vendor_name"],
                                                "tax_id": po["vendor_tax_id"]},
                                  "invoice_vendor": {"id": vid, "name": inv.get("vendor_name"), "tax_id": inv_tax}}))
        res.status = "VENDOR_MISMATCH" if vid or inv_tax else "UNVERIFIED_VENDOR"
        res.candidates = suggest_candidates(inv, pos, vid, exclude=ref) if vid else []
        if res.candidates:
            ch.append(Check("po", "PO_CANDIDATES", "Candidate POs", INFO,
                            "Open POs of the invoicing vendor that fit this invoice: " +
                            ", ".join(f"{c['po_number']} ({c['score']:.0%})" for c in res.candidates),
                            evidence={"candidates": res.candidates}))
        for name in ("PO status", "Currency", "Amount vs PO", "Line items"):
            ch.append(Check("po", "SKIPPED_" + name.upper().replace(" ", "_"), name, UNABLE,
                            "Not evaluated - PO match was not accepted"))
        res.checks = ch
        return res

    res.status, res.accepted = "MATCHED", True

    # --- PO status
    if po["status"] != "OPEN":
        ch.append(Check("po", "PO_STATUS", "PO is open", FAIL, f"{ref} is {po['status']}. {po['description']}",
                        expected="OPEN", actual=po["status"], reason_code="PO_CLOSED"))
    else:
        ch.append(Check("po", "PO_STATUS", "PO is open", PASS, f"{ref} is OPEN", expected="OPEN", actual="OPEN"))

    # --- currency
    cur = inv.get("currency")
    if cur and cur != po["currency"]:
        ch.append(Check("po", "CURRENCY", "Currency matches PO", FAIL, f"Invoice in {cur}, PO in {po['currency']}",
                        expected=po["currency"], actual=cur, reason_code="CURRENCY_MISMATCH"))
    elif not cur:
        ch.append(Check("po", "CURRENCY", "Currency matches PO", UNABLE, "Invoice currency unknown"))
    else:
        ch.append(Check("po", "CURRENCY", "Currency matches PO", PASS, f"Both in {cur}", expected=po["currency"],
                        actual=cur))

    # --- amount vs remaining balance
    total = inv.num("total_amount")
    c = po["currency"]
    consumed = po["consumed"]
    remaining = po["remaining"]
    amt = dict(po_total=po["total_amount"], already_invoiced=consumed, remaining_before=remaining,
               invoice_total=total, currency=c)
    if total is None:
        amt["classification"] = "UNABLE"
        ch.append(Check("po", "AMOUNT_VS_PO", "Invoice amount within PO", UNABLE,
                        "Invoice total not available - cannot compare with PO"))
    else:
        allowed = min(po["total_amount"] * tol["amount_pct"] / 100, tol["amount_abs"])
        diff = round(total - remaining, 2)
        pct = diff / po["total_amount"] * 100 if po["total_amount"] else 0
        amt.update(difference=diff, difference_pct=round(pct, 2), allowed_excess=round(allowed, 2),
                   rule=f"Excess over remaining balance allowed up to {tol['amount_pct']:g}% of PO value or "
                        f"{fmt_money(tol['amount_abs'], c)}, whichever is lower ({fmt_money(allowed, c)}); "
                        f"reject above {tol['hard_limit_pct']:g}%")
        base = dict(expected=f"≤ {fmt_money(remaining, c)} (remaining)", actual=fmt_money(total, c), rule=amt["rule"])
        if abs(diff) <= tol["rounding_abs"]:
            amt["classification"] = "EXACT"
            ch.append(Check("po", "AMOUNT_VS_PO", "Invoice amount within PO", PASS,
                            f"Invoice {fmt_money(total, c)} equals the remaining PO balance" +
                            (f" (after {fmt_money(consumed, c)} already invoiced)" if consumed else ""),
                            difference=fmt_money(diff, c), **base))
        elif diff < 0:
            amt["classification"] = "PARTIAL"
            ch.append(Check("po", "AMOUNT_VS_PO", "Invoice amount within PO", PASS,
                            f"Partial billing: {fmt_money(total, c)} of {fmt_money(remaining, c)} remaining. "
                            f"Balance after this invoice: {fmt_money(remaining - total, c)}",
                            difference=fmt_money(diff, c), **base))
        elif diff <= allowed:
            amt["classification"] = "WITHIN_TOLERANCE"
            ch.append(Check("po", "AMOUNT_VS_PO", "Invoice amount within PO", WARN,
                            f"Invoice exceeds remaining PO balance by {fmt_money(diff, c)} ({pct:.2f}%) - within the "
                            f"permitted {fmt_money(allowed, c)}. Variance recorded, not hidden.",
                            difference=f"+{fmt_money(diff, c)} (+{pct:.2f}%)", reason_code="AMOUNT_WITHIN_TOLERANCE",
                            **base))
        else:
            amt["classification"] = "OUTSIDE_TOLERANCE"
            code = "AMOUNT_OVER_HARD_LIMIT" if pct > tol["hard_limit_pct"] else (
                "PO_BALANCE_EXCEEDED" if consumed else "AMOUNT_OVER_TOLERANCE")
            ch.append(Check("po", "AMOUNT_VS_PO", "Invoice amount within PO", FAIL,
                            f"Invoice exceeds {'remaining ' if consumed else ''}PO balance by {fmt_money(diff, c)} "
                            f"({pct:.2f}% of PO value). Permitted excess is {fmt_money(allowed, c)}.",
                            difference=f"+{fmt_money(diff, c)} (+{pct:.2f}%)", reason_code=code, **base))
    res.amount = amt

    # --- line items
    res.line_matches, line_checks = _match_lines(inv, po, rules)
    ch += line_checks
    res.checks = ch
    return res


def _match_lines(inv: EffectiveInvoice, po: dict, rules: dict) -> tuple[list[dict], list[Check]]:
    tol = rules["tolerance"]
    thr = rules["matching"]["line_description_similarity"]
    inclusive = inv.tax_mode == "inclusive"
    c = po["currency"]
    checks: list[Check] = []
    if not inv.line_items:
        checks.append(Check("po", "LINE_ITEMS", "Line items match PO", UNABLE,
                            "Line items could not be extracted - only header-level matching was possible",
                            reason_code="LOW_CONFIDENCE"))
        return [], checks
    used: set[int] = set()
    matches = []
    for li in inv.line_items:
        best, best_s = None, 0.0
        for pl in po["lines"]:
            if pl["id"] in used:
                continue
            s = fuzz.token_set_ratio(_norm_desc(li["description"]), _norm_desc(pl["description"]))
            if s > best_s:
                best, best_s = pl, s
        m = dict(line_no=li["line_no"], description=li["description"], quantity=li.get("quantity"),
                 unit_price=li.get("unit_price"), amount=li.get("amount"), score=round(best_s), status="UNMATCHED")
        if best and best_s >= thr:
            used.add(best["id"])
            rate = li.get("tax_rate") if li.get("tax_rate") is not None else best["tax_rate"]
            up = _ex_tax(li.get("unit_price"), rate, inclusive)
            rem_qty = round(best["quantity"] - best["consumed_qty"], 4)
            m.update(po_line_id=best["id"], po_line_no=best["line_no"], po_description=best["description"],
                     po_quantity=best["quantity"], po_remaining_qty=rem_qty, po_unit_price=best["unit_price"],
                     unit_price_ex_tax=up, status="MATCHED", issues=[])
            q = li.get("quantity")
            if q is not None and q > rem_qty + 1e-6:
                m["issues"].append("quantity")
                checks.append(Check("po", "LINE_QTY", f"Line {li['line_no']} quantity", FAIL,
                                    f"'{li['description'][:50]}': invoiced {q:g}, PO remaining {rem_qty:g} "
                                    f"(ordered {best['quantity']:g})", expected=f"≤ {rem_qty:g}", actual=f"{q:g}",
                                    reason_code="QUANTITY_EXCEEDS_PO"))
            if up is not None:
                d = up - best["unit_price"]
                pct = d / best["unit_price"] * 100 if best["unit_price"] else 0
                m["price_diff_pct"] = round(pct, 2)
                if abs(d) <= 0.01:
                    pass
                elif abs(pct) <= tol["unit_price_pct"]:
                    m["issues"].append("price-within-tolerance")
                    checks.append(Check("po", "LINE_PRICE", f"Line {li['line_no']} unit price", WARN,
                                        f"'{li['description'][:50]}': {fmt_money(up, c)} vs PO {fmt_money(best['unit_price'], c)} "
                                        f"({pct:+.2f}%) - within ±{tol['unit_price_pct']:g}% price tolerance",
                                        expected=fmt_money(best["unit_price"], c), actual=fmt_money(up, c),
                                        difference=f"{pct:+.2f}%", rule=f"±{tol['unit_price_pct']:g}%"))
                else:
                    m["issues"].append("price")
                    checks.append(Check("po", "LINE_PRICE", f"Line {li['line_no']} unit price", FAIL,
                                        f"'{li['description'][:50]}': {fmt_money(up, c)} vs PO {fmt_money(best['unit_price'], c)} "
                                        f"({pct:+.2f}%)", expected=fmt_money(best["unit_price"], c),
                                        actual=fmt_money(up, c), difference=f"{pct:+.2f}%",
                                        rule=f"±{tol['unit_price_pct']:g}%", reason_code="UNIT_PRICE_MISMATCH"))
            if q is not None and q < rem_qty - 1e-6:
                m["partial"] = f"{q:g} of {rem_qty:g} remaining"
        matches.append(m)

    # bundled-line reconciliation
    unmatched_inv = [m for m in matches if m["status"] == "UNMATCHED"]
    unmatched_po = [pl for pl in po["lines"] if pl["id"] not in used]
    if len(unmatched_inv) == 1 and len(unmatched_po) >= 2:
        m = unmatched_inv[0]
        rate = unmatched_po[0]["tax_rate"]
        amt_ex = _ex_tax(m.get("amount"), rate, inclusive)
        po_sum = round(sum((pl["quantity"] - pl["consumed_qty"]) * pl["unit_price"] for pl in unmatched_po), 2)
        if amt_ex is not None and abs(amt_ex - po_sum) <= max(tol["rounding_abs"], po_sum * 0.005):
            m.update(status="BUNDLED", bundled_po_lines=[pl["line_no"] for pl in unmatched_po],
                     po_description=" + ".join(pl["description"] for pl in unmatched_po))
            for pl in unmatched_po:
                used.add(pl["id"])
            checks.append(Check("po", "LINE_BUNDLE", f"Line {m['line_no']} bundled", PASS,
                                f"'{m['description'][:60]}' ({fmt_money(amt_ex, c)} ex-tax) reconciles to PO lines "
                                f"{', '.join(str(pl['line_no']) for pl in unmatched_po)} "
                                f"({' + '.join(fmt_money((pl['quantity'] - pl['consumed_qty']) * pl['unit_price'], c) for pl in unmatched_po)})",
                                expected=fmt_money(po_sum, c), actual=fmt_money(amt_ex, c),
                                rule="One invoice line may bundle several PO lines if the amounts reconcile"))
    for m in matches:
        if m["status"] == "UNMATCHED":
            checks.append(Check("po", "LINE_MATCH", f"Line {m['line_no']} on PO", FAIL,
                                f"'{m['description'][:60]}' ({fmt_money(m.get('amount'), c)}) is not on {po['po_number']}"
                                + (f" (closest PO line {m['score']}% similar)" if m["score"] else ""),
                                actual=fmt_money(m.get("amount"), c), reason_code="LINE_ITEM_UNMATCHED"))
    n_ok = sum(1 for m in matches if m["status"] in ("MATCHED", "BUNDLED"))
    partial = [m for m in matches if m.get("partial")]
    checks.insert(0, Check("po", "LINE_ITEMS", "Line items match PO",
                           PASS if n_ok == len(matches) else FAIL,
                           f"{n_ok} of {len(matches)} invoice line(s) matched to {po['po_number']}" +
                           (f"; partial quantities: " + "; ".join(m["partial"] for m in partial) if partial else ""),
                           evidence={"matches": matches}))
    return matches, checks
