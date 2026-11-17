"""Stage 7 - duplicate detection.

Three independent signals, compared only against EARLIER runs that are not themselves duplicates:
  1. identical file (SHA-256)                       -> confirmed duplicate
  2. same vendor + same normalised invoice number    -> confirmed if the total also matches,
                                                        otherwise "possible" (revised invoice?)
  3. same vendor + date + total + line amounts       -> "possible" (re-numbered copy?)
Same vendor and same amount alone is NOT a duplicate (recurring monthly bills are normal); it is
reported for transparency so a reviewer can see why it was not flagged.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from ..utils import norm_company, norm_invoice_number
from .checks import FAIL, INFO, PASS, UNABLE, WARN, Check, EffectiveInvoice, fmt_money
from .vendor import VendorResult


@dataclass
class DuplicateResult:
    status: str                       # CLEAR / CONFIRMED / POSSIBLE
    original_run_id: Optional[str] = None
    matches: list[dict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


def vendor_key(inv: EffectiveInvoice, vendor: VendorResult) -> Optional[str]:
    if vendor.vendor_id:
        return vendor.vendor_id
    if inv.get("vendor_tax_id"):
        return "TAX:" + inv.get("vendor_tax_id").upper()
    n = norm_company(inv.get("vendor_name"))
    return ("NAME:" + n) if n else None


def fingerprint(vkey: Optional[str], inv: EffectiveInvoice) -> Optional[str]:
    total = inv.num("total_amount")
    d = inv.get("invoice_date")
    if not vkey or total is None or not d:
        return None
    amounts = sorted(round(li.get("amount") or 0, 2) for li in inv.line_items)
    raw = f"{vkey}|{d}|{total:.2f}|{','.join(f'{a:.2f}' for a in amounts)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def detect_duplicates(inv: EffectiveInvoice, vendor: VendorResult, file_sha: str, prior: list[dict],
                      rules: dict) -> tuple[DuplicateResult, Optional[str]]:
    cfg = rules["duplicates"]
    vkey = vendor_key(inv, vendor)
    fp = fingerprint(vkey, inv)
    inv_no = norm_invoice_number(inv.get("invoice_number"))
    total = inv.num("total_amount")
    cur = inv.get("currency")
    res = DuplicateResult(status="CLEAR")
    ch: list[Check] = []

    def ref(p):
        return dict(run_id=p["id"], filename=p["filename"], invoice_number=p["invoice_number"],
                    total=p["total_amount"], status=p["final_status"], processed=p["created_at"])

    # 1. identical file
    if cfg["check_file_hash"]:
        hit = next((p for p in prior if p["file_sha256"] == file_sha), None)
        if hit:
            res.status, res.original_run_id = "CONFIRMED", hit["id"]
            res.matches.append(dict(ref(hit), signal="identical file (SHA-256)"))
            ch.append(Check("duplicate", "DUP_FILE", "Same file processed before", FAIL,
                            f"Byte-identical file was processed in {hit['id']} ({hit['filename']})",
                            reason_code="DUPLICATE_CONFIRMED", evidence={"original_run_id": hit["id"]}))
        else:
            ch.append(Check("duplicate", "DUP_FILE", "Same file processed before", PASS,
                            f"File hash {file_sha[:12]}… not seen before"))

    # 2. vendor + invoice number
    if cfg["check_vendor_invoice_number"]:
        if not inv_no or not vkey:
            ch.append(Check("duplicate", "DUP_INVOICE_NO", "Vendor + invoice number seen before", UNABLE,
                            "Invoice number or vendor identity unavailable - this check could not run"))
        else:
            hits = [p for p in prior if p["vendor_key"] == vkey and norm_invoice_number(p["invoice_number"]) == inv_no]
            same_amt = [p for p in hits if total is not None and p["total_amount"] is not None
                        and abs(p["total_amount"] - total) <= 1]
            if same_amt:
                hit = same_amt[0]
                if res.status != "CONFIRMED":
                    res.status, res.original_run_id = "CONFIRMED", hit["id"]
                res.matches.append(dict(ref(hit), signal="same vendor, invoice number and amount"))
                ch.append(Check("duplicate", "DUP_INVOICE_NO", "Vendor + invoice number seen before", FAIL,
                                f"Invoice {inv.get('invoice_number')} from this vendor for {fmt_money(total, cur)} was "
                                f"already processed in {hit['id']} (file '{hit['filename']}', status "
                                f"{hit['final_status']}). Different file name, same invoice.",
                                expected="unique (vendor, invoice number)", actual=f"also in {hit['id']}",
                                reason_code="DUPLICATE_CONFIRMED", evidence={"original_run_id": hit["id"]}))
            elif hits:
                hit = hits[0]
                if res.status == "CLEAR":
                    res.status, res.original_run_id = "POSSIBLE", hit["id"]
                res.matches.append(dict(ref(hit), signal="same vendor and invoice number, different amount"))
                ch.append(Check("duplicate", "DUP_INVOICE_NO", "Vendor + invoice number seen before", WARN,
                                f"Invoice number {inv.get('invoice_number')} already used by this vendor in "
                                f"{hit['id']} for {fmt_money(hit['total_amount'], cur)} (now "
                                f"{fmt_money(total, cur)}). Possibly a revised invoice.",
                                reason_code="POSSIBLE_DUPLICATE", evidence={"original_run_id": hit["id"]}))
            else:
                ch.append(Check("duplicate", "DUP_INVOICE_NO", "Vendor + invoice number seen before", PASS,
                                f"No earlier invoice {inv.get('invoice_number')} from this vendor"))

    # 3. content fingerprint
    if cfg["check_content_fingerprint"]:
        if not fp:
            ch.append(Check("duplicate", "DUP_CONTENT", "Same content under another number", UNABLE,
                            "Not enough data (vendor, date, total) to fingerprint the content"))
        else:
            hits = [p for p in prior if p["content_fingerprint"] == fp
                    and norm_invoice_number(p["invoice_number"]) != inv_no]
            if hits:
                hit = hits[0]
                if res.status == "CLEAR":
                    res.status, res.original_run_id = "POSSIBLE", hit["id"]
                res.matches.append(dict(ref(hit), signal="same vendor, date, total and line amounts"))
                ch.append(Check("duplicate", "DUP_CONTENT", "Same content under another number", WARN,
                                f"{hit['id']} has the same vendor, date, total and line amounts under invoice "
                                f"number '{hit['invoice_number']}'", reason_code="POSSIBLE_DUPLICATE",
                                evidence={"original_run_id": hit["id"]}))
            else:
                ch.append(Check("duplicate", "DUP_CONTENT", "Same content under another number", PASS,
                                "No earlier invoice with identical content"))

    # transparency: similar amounts that were deliberately NOT treated as duplicates
    if total is not None and vkey:
        flagged = {m["run_id"] for m in res.matches}
        similar = [p for p in prior if p["vendor_key"] == vkey and p["id"] not in flagged
                   and p["total_amount"] is not None and abs(p["total_amount"] - total) <= 1]
        if similar:
            ch.append(Check("duplicate", "DUP_SIMILAR_AMOUNT", "Same amount, different invoice", INFO,
                            f"{len(similar)} earlier invoice(s) from this vendor have the same amount "
                            f"({', '.join(p['id'] + ' / ' + (p['invoice_number'] or '?') for p in similar[:3])}) but "
                            f"different invoice numbers/content - not treated as duplicates (e.g. recurring monthly "
                            f"billing)."))
    res.checks = ch
    return res, fp
