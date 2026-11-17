"""Stage 4 - vendor verification against the vendor master.

Identity is established by tax ID (GSTIN/EIN), never by name alone. A similar name with a different
tax ID is treated as a look-alike (possible impersonation), not as the registered vendor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from ..utils import mask_account, norm_company
from .checks import FAIL, INFO, PASS, UNABLE, WARN, Check, EffectiveInvoice


@dataclass
class VendorResult:
    status: str                         # VERIFIED / UNVERIFIED / NOT_FOUND
    vendor: Optional[dict] = None       # verified vendor master record
    tentative: Optional[dict] = None    # matched by name only
    method: Optional[str] = None
    candidates: list[dict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def vendor_id(self):
        return self.vendor["id"] if self.vendor else None


def verify_vendor(inv: EffectiveInvoice, vendors: list[dict], rules: dict) -> VendorResult:
    name = inv.get("vendor_name")
    tax_id = (inv.get("vendor_tax_id") or "").upper() or None
    sim_threshold = rules["matching"]["vendor_name_similarity"]
    ch: list[Check] = []

    def sim(v):
        return fuzz.token_set_ratio(norm_company(name), norm_company(v["name"])) if name else 0

    scored = sorted(({"vendor_id": v["id"], "name": v["name"], "tax_id": v["tax_id"], "score": round(sim(v))}
                     for v in vendors), key=lambda c: -c["score"])
    by_tax = {(v["tax_id"] or "").upper(): v for v in vendors}
    res = VendorResult(status="NOT_FOUND", candidates=[c for c in scored[:3] if c["score"] >= 60])

    if tax_id and tax_id in by_tax:
        v = by_tax[tax_id]
        res.status, res.vendor, res.method = "VERIFIED", v, "tax-id"
        ch.append(Check("vendor", "VENDOR_IDENTITY", "Vendor identified by tax ID", PASS,
                        f"{v['tax_id_type']} {tax_id} is registered to {v['id']} · {v['name']}",
                        expected="Tax ID registered in vendor master", actual=tax_id,
                        evidence={"vendor_id": v["id"]}))
        s = sim(v)
        if name is None:
            ch.append(Check("vendor", "VENDOR_NAME", "Vendor name consistent", UNABLE,
                            "Vendor name not extracted; identity rests on tax ID"))
        elif s >= sim_threshold:
            ch.append(Check("vendor", "VENDOR_NAME", "Vendor name consistent", PASS,
                            f"Invoice name '{name}' matches master '{v['name']}' (similarity {s:.0f}%)",
                            expected=v["name"], actual=name))
        else:
            ch.append(Check("vendor", "VENDOR_NAME", "Vendor name consistent", WARN,
                            f"Invoice name '{name}' differs from master '{v['name']}' (similarity {s:.0f}%)",
                            expected=v["name"], actual=name, rule=f"similarity >= {sim_threshold}%",
                            reason_code="VENDOR_NAME_MISMATCH"))
        if v["status"] != "ACTIVE":
            ch.append(Check("vendor", "VENDOR_STATUS", "Vendor active", FAIL,
                            f"Vendor status is {v['status']}." + (f" {v['notes']}" if v.get("notes") else ""),
                            expected="ACTIVE", actual=v["status"], reason_code="VENDOR_BLOCKED"))
        else:
            ch.append(Check("vendor", "VENDOR_STATUS", "Vendor active", PASS, "Vendor is ACTIVE",
                            expected="ACTIVE", actual="ACTIVE"))
        if v["approval_status"] != "APPROVED":
            ch.append(Check("vendor", "VENDOR_APPROVAL", "Vendor onboarding approved", FAIL,
                            f"Onboarding status is {v['approval_status']}." +
                            (f" {v['notes']}" if v.get("notes") else ""),
                            expected="APPROVED", actual=v["approval_status"], reason_code="VENDOR_NOT_APPROVED"))
        else:
            ch.append(Check("vendor", "VENDOR_APPROVAL", "Vendor onboarding approved", PASS,
                            f"Approved vendor since {v.get('onboarded') or 'n/a'}", expected="APPROVED",
                            actual="APPROVED"))
        ch.append(_bank_check(inv, v))
    else:
        top = scored[0] if scored else None
        if tax_id:
            if top and top["score"] >= sim_threshold:
                res.status = "UNVERIFIED"
                ch.append(Check("vendor", "VENDOR_IDENTITY", "Vendor identified by tax ID", FAIL,
                                f"Name '{name}' resembles registered vendor {top['vendor_id']} · {top['name']} "
                                f"(similarity {top['score']}%), but tax ID {tax_id} is NOT registered to it "
                                f"(registered: {top['tax_id']}). Treated as a different entity.",
                                expected=top["tax_id"], actual=tax_id, rule="identity = tax ID, never name alone",
                                reason_code="VENDOR_LOOKALIKE", evidence={"lookalike_of": top["vendor_id"]}))
                ch.append(_bank_check(inv, next(v for v in vendors if v["id"] == top["vendor_id"]), lookalike=True))
            else:
                ch.append(Check("vendor", "VENDOR_IDENTITY", "Vendor identified by tax ID", FAIL,
                                f"Tax ID {tax_id} ('{name or 'unknown name'}') is not in the vendor master",
                                actual=tax_id, reason_code="VENDOR_NOT_FOUND"))
        else:
            if top and top["score"] >= 95:
                v = next(x for x in vendors if x["id"] == top["vendor_id"])
                res.status, res.tentative, res.method = "UNVERIFIED", v, "name-only"
                ch.append(Check("vendor", "VENDOR_IDENTITY", "Vendor identified by tax ID", FAIL,
                                f"No tax ID readable on the invoice. Name matches {v['id']} · {v['name']} "
                                f"({top['score']}%), but name alone is not proof of identity.",
                                expected=v["tax_id"], actual=None, reason_code="VENDOR_TAX_ID_MISSING"))
            elif top and top["score"] >= sim_threshold:
                res.status = "UNVERIFIED"
                ch.append(Check("vendor", "VENDOR_IDENTITY", "Vendor identified by tax ID", FAIL,
                                f"No tax ID, and name '{name}' only resembles {top['name']} ({top['score']}%)",
                                reason_code="VENDOR_LOOKALIKE", evidence={"lookalike_of": top["vendor_id"]}))
            else:
                ch.append(Check("vendor", "VENDOR_IDENTITY", "Vendor identified by tax ID", FAIL,
                                f"Vendor '{name or 'unknown'}' has no tax ID on the invoice and no match in the "
                                f"vendor master", reason_code="VENDOR_NOT_FOUND"))
    res.checks = ch
    return res


def _bank_check(inv: EffectiveInvoice, v: dict, lookalike: bool = False) -> Check:
    acct = re.sub(r"\D", "", inv.get("bank_account") or "")
    ifsc = (inv.get("bank_ifsc") or "").upper()
    master_acct = re.sub(r"\D", "", v.get("bank_account") or "")
    if not acct:
        return Check("vendor", "BANK_DETAILS", "Bank details match vendor master", INFO,
                     "No bank account on the invoice; payment will use vendor-master bank details only")
    mismatch = acct != master_acct or (ifsc and v.get("ifsc") and ifsc != v["ifsc"])
    if mismatch:
        return Check("vendor", "BANK_DETAILS", "Bank details match vendor master", FAIL,
                     ("Bank account on the invoice differs from the registered account of the vendor it resembles. "
                      if lookalike else "Bank account on the invoice differs from vendor master. ") +
                     "Bank details are never updated from an invoice.",
                     expected=f"{mask_account(master_acct)} / {v.get('ifsc') or '—'}",
                     actual=f"{mask_account(acct)} / {ifsc or '—'}", reason_code="BANK_DETAILS_MISMATCH")
    return Check("vendor", "BANK_DETAILS", "Bank details match vendor master", PASS,
                 f"Account {mask_account(acct)} and IFSC {ifsc or '—'} match vendor master",
                 expected=mask_account(master_acct), actual=mask_account(acct))
