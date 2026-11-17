"""Stage 6 - financial validation. Every check reports expected vs actual, the difference and the rule.

Outcome vocabulary shown in the UI:
  EXACT (PASS) · WITHIN TOLERANCE (WARN) · OUTSIDE TOLERANCE (FAIL) · UNABLE TO VALIDATE (UNABLE)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..seed_data import BUYER
from .checks import FAIL, INFO, PASS, UNABLE, WARN, Check, EffectiveInvoice, fmt_money
from .po_match import POResult
from .vendor import VendorResult


def _cmp(code, name, expected: Optional[float], actual: Optional[float], tol: float, cur: str, reason: str,
         rule: str, what: str) -> Check:
    if expected is None or actual is None:
        return Check("financial", code, name, UNABLE, f"Unable to validate {what}: required values missing",
                     rule=rule)
    d = round(actual - expected, 2)
    base = dict(expected=fmt_money(expected, cur), actual=fmt_money(actual, cur),
                difference=("+" if d > 0 else "") + fmt_money(d, cur), rule=rule)
    if abs(d) < 0.005:
        return Check("financial", code, name, PASS, f"{what.capitalize()}: exact match", **base)
    if abs(d) <= tol:
        return Check("financial", code, name, PASS,
                     f"{what.capitalize()}: within rounding tolerance ({fmt_money(d, cur)})", **base)
    return Check("financial", code, name, FAIL, f"{what.capitalize()} does not add up: off by {fmt_money(d, cur)}",
                 reason_code=reason, **base)


def validate_financials(inv: EffectiveInvoice, vendor: VendorResult, po: POResult, rules: dict,
                        today: Optional[date] = None) -> list[Check]:
    today = today or date.today()
    r = rules["tolerance"]["rounding_abs"]
    cur = inv.get("currency") or "INR"
    inclusive = inv.tax_mode == "inclusive"
    lines = inv.line_items
    sub, tax, tot = inv.num("subtotal"), inv.num("tax_amount"), inv.num("total_amount")
    out: list[Check] = []

    # 1. line arithmetic
    if lines:
        bad = []
        for li in lines:
            q, p, a = li.get("quantity"), li.get("unit_price"), li.get("amount")
            if None in (q, p, a):
                bad.append(f"line {li['line_no']}: incomplete")
            elif abs(q * p - a) > max(r, a * 0.001):
                bad.append(f"line {li['line_no']}: {q:g} × {p:,.2f} = {q * p:,.2f}, invoice says {a:,.2f}")
        out.append(Check("financial", "LINE_ARITHMETIC", "Quantity × unit price = line total",
                         PASS if not bad else FAIL,
                         f"All {len(lines)} line(s) compute correctly" if not bad else "; ".join(bad),
                         rule=f"|qty × price − amount| ≤ {fmt_money(r, cur)}",
                         reason_code=None if not bad else "ARITHMETIC_ERROR"))
        s_lines = round(sum(li.get("amount") or 0 for li in lines), 2)
        if inclusive:
            out.append(_cmp("LINES_TO_TOTAL", "Sum of lines = invoice total (tax-inclusive)", s_lines, tot, r, cur,
                            "ARITHMETIC_ERROR", "Prices include GST: Σ line amounts must equal the total",
                            "sum of GST-inclusive line amounts vs total"))
        else:
            out.append(_cmp("LINES_TO_SUBTOTAL", "Sum of lines = subtotal", s_lines, sub, r, cur, "ARITHMETIC_ERROR",
                            "Σ line amounts must equal the subtotal", "sum of line amounts vs subtotal"))
    else:
        out.append(Check("financial", "LINE_ARITHMETIC", "Quantity × unit price = line total", UNABLE,
                         "No line items extracted"))

    # 2. subtotal + tax = total
    out.append(_cmp("TOTAL_CALC", "Subtotal + tax = total", (sub + tax) if sub is not None and tax is not None else None,
                    tot, r, cur, "ARITHMETIC_ERROR", "taxable value + tax must equal invoice total",
                    "subtotal + tax vs total"))

    # 3. tax recomputation from rates
    rate_by_line = {}
    for m in po.line_matches:
        if m.get("po_line_id"):
            pl = next((x for x in (po.po or {}).get("lines", []) if x["id"] == m["po_line_id"]), None)
            if pl:
                rate_by_line[m["line_no"]] = pl["tax_rate"]
    comp_rates = {c["rate"] for c in inv.tax_components}
    single_rate = None
    if len(comp_rates) == 1:      # one printed rate, e.g. "IGST @ 18%" or "CGST @ 9%" (+ SGST @ 9%)
        single_rate = next(iter(comp_rates)) * (2 if inv.get("tax_type") == "CGST+SGST" else 1)
    expected_tax, src = None, None
    if inv.get("tax_type") == "NONE" or (inv.tax_mode == "none"):
        expected_tax, src = 0.0, "no tax charged"
    elif lines:
        tot_tax, ok = 0.0, True
        srcs = set()
        for li in lines:
            rate = li.get("tax_rate")
            if rate is not None:
                srcs.add("invoice line rates")
            else:
                rate = rate_by_line.get(li["line_no"])
                if rate is None and po.line_matches:
                    bundle = next((m for m in po.line_matches if m["line_no"] == li["line_no"]
                                   and m["status"] == "BUNDLED"), None)
                    if bundle and po.po:
                        rate = po.po["lines"][bundle["bundled_po_lines"][0] - 1]["tax_rate"]
                if rate is not None:
                    srcs.add("PO line rates")
                elif single_rate is not None:
                    rate = single_rate
                    srcs.add("rate printed on tax line")
            if rate is None or li.get("amount") is None:
                ok = False
                break
            a = li["amount"]
            tot_tax += (a - a / (1 + rate / 100)) if inclusive else a * rate / 100
        if ok:
            expected_tax, src = round(tot_tax, 2), " + ".join(sorted(srcs))
    tax_tol = max(r, r * max(1, len(lines)))
    out.append(_cmp("TAX_CALC", "Tax amount recomputed from rates", expected_tax, tax, tax_tol, cur, "TAX_MISMATCH",
                    f"Σ(line × GST rate){' back-calculated from inclusive prices' if inclusive else ''}; "
                    f"rates from {src or 'n/a'}", "tax amount"))

    # 4. GST type vs place of supply
    vtax = inv.get("vendor_tax_id") or ""
    ttype = inv.get("tax_type")
    if vtax[:2].isdigit() and len(vtax) == 15:
        intra = vtax[:2] == BUYER["state_code"]
        exp = "CGST+SGST" if intra else "IGST"
        why = (f"Vendor state {vtax[:2]} {'=' if intra else '≠'} buyer state {BUYER['state_code']} "
               f"→ {'intra' if intra else 'inter'}-state supply → {exp}")
        if ttype is None:
            out.append(Check("financial", "TAX_TYPE", "GST type matches place of supply", UNABLE,
                             "Tax type not identified on invoice", rule=why))
        elif ttype == exp:
            out.append(Check("financial", "TAX_TYPE", "GST type matches place of supply", PASS, why,
                             expected=exp, actual=ttype))
        else:
            out.append(Check("financial", "TAX_TYPE", "GST type matches place of supply", FAIL,
                             f"Invoice charges {ttype}; expected {exp}. {why}", expected=exp, actual=ttype,
                             reason_code="TAX_TYPE_MISMATCH"))
    elif inv.get("currency") and inv.get("currency") != "INR":
        out.append(Check("financial", "TAX_TYPE", "GST type matches place of supply",
                         PASS if (tax or 0) == 0 else WARN,
                         "Overseas vendor: no Indian GST on invoice; IGST under reverse charge is self-assessed by "
                         "the buyer" if (tax or 0) == 0 else "Overseas vendor charging tax - verify",
                         expected="No GST (reverse charge)", actual=ttype or "—"))

    # 5. currency consistency with vendor master
    vmaster = vendor.vendor or vendor.tentative
    if vmaster and inv.get("currency"):
        ok = inv.get("currency") == vmaster["currency"]
        out.append(Check("financial", "CURRENCY_VENDOR", "Currency matches vendor master", PASS if ok else WARN,
                         f"Invoice {inv.get('currency')}, vendor master {vmaster['currency']}",
                         expected=vmaster["currency"], actual=inv.get("currency")))

    # 6. dates
    idate, ddate = inv.date("invoice_date"), inv.date("due_date")
    issues = []
    if idate:
        if idate > today:
            issues.append(f"invoice date {idate} is in the future")
        max_age = int(rules["tolerance"].get("max_invoice_age_days", 365))
        if idate < today - timedelta(days=max_age):
            issues.append(f"invoice date {idate} is more than {max_age} days old")
        if po.po and po.accepted and idate.isoformat() < po.po["po_date"]:
            issues.append(f"invoice dated {idate} before PO date {po.po['po_date']}")
        if ddate and ddate < idate:
            issues.append(f"due date {ddate} is before invoice date {idate}")
    if idate is None:
        out.append(Check("financial", "DATES", "Invoice and due dates plausible", UNABLE, "Invoice date not available"))
    else:
        out.append(Check("financial", "DATES", "Invoice and due dates plausible", FAIL if issues else PASS,
                         "; ".join(issues) if issues else
                         f"Invoice {idate}" + (f", due {ddate}" if ddate else "") +
                         (f", after PO date {po.po['po_date']}" if po.po and po.accepted else ""),
                         reason_code="DATE_ANOMALY" if issues else None))

    # 7. payment terms vs vendor master (informational)
    terms = inv.get("payment_terms")
    if vmaster and terms and vmaster.get("payment_terms"):
        same = terms.replace(" ", "").lower() == vmaster["payment_terms"].replace(" ", "").lower()
        out.append(Check("financial", "PAYMENT_TERMS", "Payment terms match vendor master", PASS if same else INFO,
                         f"Invoice '{terms}', master '{vmaster['payment_terms']}'" +
                         ("" if same else " - payment will be scheduled per contracted (master) terms"),
                         expected=vmaster["payment_terms"], actual=terms))
    return out
