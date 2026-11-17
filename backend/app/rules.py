"""Configurable business rules. Stored in the DB (settings table) and snapshotted into every decision."""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from .models import Setting

# Every reason code the engine can raise, what it means and the effect allowed for it.
# effect: REJECT -> blocks the invoice; REVIEW -> routes to a human; NONE -> informational only.
REASON_CODES: dict[str, dict] = {
    # duplicates
    "DUPLICATE_CONFIRMED": dict(label="Confirmed duplicate invoice", stage="duplicate", default="REJECT",
                                allowed=["REJECT", "REVIEW"],
                                action="Reject and notify the vendor that this invoice was already received."),
    "POSSIBLE_DUPLICATE": dict(label="Possible duplicate", stage="duplicate", default="REVIEW", allowed=["REVIEW"],
                               action="Compare with the earlier invoice before paying."),
    # vendor
    "VENDOR_BLOCKED": dict(label="Vendor blocked / inactive", stage="vendor", default="REJECT",
                           allowed=["REJECT", "REVIEW"],
                           action="Reject. Vendor management must reinstate the vendor before any payment."),
    "VENDOR_NOT_APPROVED": dict(label="Vendor onboarding not complete", stage="vendor", default="REVIEW",
                                allowed=["REVIEW", "REJECT"],
                                action="Hold until vendor onboarding is approved."),
    "VENDOR_NOT_FOUND": dict(label="Vendor not in vendor master", stage="vendor", default="REVIEW",
                             allowed=["REVIEW", "REJECT"],
                             action="Confirm the supplier and start onboarding if legitimate."),
    "VENDOR_LOOKALIKE": dict(label="Look-alike of a registered vendor", stage="vendor", default="REVIEW",
                             allowed=["REVIEW", "REJECT"],
                             action="Escalate to vendor management: possible impersonation. Do not pay."),
    "VENDOR_TAX_ID_MISSING": dict(label="Vendor tax ID not found on invoice", stage="vendor", default="REVIEW",
                                  allowed=["REVIEW"],
                                  action="Verify vendor identity manually; request a compliant tax invoice."),
    "VENDOR_NAME_MISMATCH": dict(label="Vendor name differs from master", stage="vendor", default="REVIEW",
                                 allowed=["REVIEW", "NONE"], action="Confirm the trading name with the vendor."),
    "BANK_DETAILS_MISMATCH": dict(label="Bank details differ from vendor master", stage="vendor", default="REVIEW",
                                  allowed=["REVIEW", "REJECT"],
                                  action="Do NOT update bank details from an invoice. Verify by phone with a "
                                         "known contact (call-back control)."),
    # PO
    "PO_MISSING": dict(label="No PO reference", stage="po", default="REVIEW", allowed=["REVIEW", "REJECT"],
                       action="Select the correct PO from the suggested candidates or ask the vendor."),
    "PO_NOT_FOUND": dict(label="PO reference not found", stage="po", default="REVIEW", allowed=["REVIEW", "REJECT"],
                         action="Check the PO number with the requester or the vendor."),
    "PO_UNCERTAIN": dict(label="PO reference unclear", stage="po", default="REVIEW", allowed=["REVIEW"],
                         action="Confirm the PO number against the source document."),
    "VENDOR_PO_MISMATCH": dict(label="PO belongs to a different vendor", stage="po", default="REVIEW",
                               allowed=["REVIEW", "REJECT"],
                               action="Escalate: confirm the correct PO with the vendor. Never pay against "
                                      "another vendor's PO."),
    "PO_CLOSED": dict(label="PO closed or cancelled", stage="po", default="REJECT", allowed=["REJECT", "REVIEW"],
                      action="Reject. If goods/services were genuinely extra, the requester must raise a new PO."),
    "CURRENCY_MISMATCH": dict(label="Currency differs from PO", stage="po", default="REVIEW", allowed=["REVIEW"],
                              action="Confirm the billing currency with the vendor."),
    "AMOUNT_OVER_TOLERANCE": dict(label="Invoice exceeds PO beyond tolerance", stage="po", default="REVIEW",
                                  allowed=["REVIEW", "REJECT"],
                                  action="Get budget-owner approval for the excess or a credit note from the vendor."),
    "AMOUNT_OVER_HARD_LIMIT": dict(label="Invoice exceeds PO beyond hard limit", stage="po", default="REJECT",
                                   allowed=["REJECT", "REVIEW"],
                                   action="Reject. Ask the requester to raise a PO amendment."),
    "AMOUNT_WITHIN_TOLERANCE": dict(label="Variance within tolerance", stage="po", default="NONE",
                                    allowed=["NONE", "REVIEW"], action="No action; variance recorded."),
    "PO_BALANCE_EXCEEDED": dict(label="Exceeds remaining PO balance", stage="po", default="REVIEW",
                                allowed=["REVIEW", "REJECT"],
                                action="Check earlier invoices against this PO; possible over-billing."),
    "LINE_ITEM_UNMATCHED": dict(label="Invoice line not on PO", stage="po", default="REVIEW", allowed=["REVIEW"],
                                action="Confirm with the requester whether the extra item was ordered."),
    "QUANTITY_EXCEEDS_PO": dict(label="Quantity exceeds PO", stage="po", default="REVIEW", allowed=["REVIEW"],
                                action="Confirm goods receipt for the extra quantity."),
    "UNIT_PRICE_MISMATCH": dict(label="Unit price differs from PO", stage="po", default="REVIEW",
                                allowed=["REVIEW"], action="Confirm the agreed rate with procurement."),
    # financial
    "ARITHMETIC_ERROR": dict(label="Arithmetic does not add up", stage="financial", default="REVIEW",
                             allowed=["REVIEW", "REJECT"], action="Ask the vendor for a corrected invoice."),
    "TAX_MISMATCH": dict(label="Tax calculation mismatch", stage="financial", default="REVIEW", allowed=["REVIEW"],
                         action="Check the GST computation with the tax team."),
    "TAX_TYPE_MISMATCH": dict(label="Wrong GST type for place of supply", stage="financial", default="REVIEW",
                              allowed=["REVIEW", "NONE"],
                              action="Inter-state supply needs IGST; intra-state needs CGST + SGST. Request a "
                                     "corrected invoice."),
    "DATE_ANOMALY": dict(label="Date anomaly", stage="financial", default="REVIEW", allowed=["REVIEW", "NONE"],
                         action="Verify invoice and due dates."),
    # extraction
    "MISSING_FIELD": dict(label="Mandatory field missing", stage="extraction", default="REVIEW", allowed=["REVIEW"],
                          action="Verify the field on the document and enter it."),
    "LOW_CONFIDENCE": dict(label="Low-confidence extraction", stage="extraction", default="REVIEW",
                           allowed=["REVIEW"], action="Verify the highlighted fields against the document."),
    "INVALID_TAX_ID": dict(label="Tax ID fails format/checksum", stage="extraction", default="REVIEW",
                           allowed=["REVIEW"], action="Verify the GSTIN on the GST portal."),
}

DEFAULT_RULES = {
    "tolerance": {
        "amount_pct": 2.0,          # invoice may exceed PO by up to 2% ...
        "amount_abs": 5000.0,       # ... and by no more than this absolute amount (whichever is lower)
        "hard_limit_pct": 15.0,     # beyond this the invoice is rejected outright (policy)
        "unit_price_pct": 2.0,      # per-line unit price tolerance
        "rounding_abs": 1.0,        # arithmetic rounding tolerance (currency units)
        "max_invoice_age_days": 365,
    },
    "extraction": {
        "confidence_threshold": 0.80,
        "critical_fields": ["invoice_number", "invoice_date", "vendor_name", "vendor_tax_id", "total_amount",
                            "po_reference", "currency"],
    },
    "matching": {
        "vendor_name_similarity": 85,    # rapidfuzz score to call two vendor names "similar"
        "line_description_similarity": 70,
        "allow_po_candidate_suggestions": True,
    },
    "duplicates": {
        "check_file_hash": True,
        "check_vendor_invoice_number": True,
        "check_content_fingerprint": True,
    },
    "policies": {code: meta["default"] for code, meta in REASON_CODES.items()},
    "demo": {
        "stage_pacing_ms": 550,     # visible pause between stages for live demos; excluded from timings
    },
    "extraction_mode": "auto",       # auto (LLM if key present, else local) / local / llm
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_rules(db: Session) -> dict:
    row = db.get(Setting, "rules")
    return _merge(DEFAULT_RULES, row.value if row else {})


def validate_rules(new: dict) -> dict:
    """Merge onto defaults and validate types/ranges. Raises ValueError with a readable message."""
    merged = _merge(DEFAULT_RULES, new)
    t = merged["tolerance"]
    for k in ("amount_pct", "amount_abs", "hard_limit_pct", "unit_price_pct", "rounding_abs", "max_invoice_age_days"):
        t[k] = float(t[k])
        if t[k] < 0:
            raise ValueError(f"tolerance.{k} must be >= 0")
    if t["hard_limit_pct"] < t["amount_pct"]:
        raise ValueError("Hard-reject limit must be greater than or equal to the review tolerance")
    ct = float(merged["extraction"]["confidence_threshold"])
    if not 0.3 <= ct <= 0.99:
        raise ValueError("Confidence threshold must be between 0.30 and 0.99")
    merged["extraction"]["confidence_threshold"] = ct
    for code, eff in merged["policies"].items():
        if code not in REASON_CODES:
            raise ValueError(f"Unknown reason code {code}")
        if eff not in REASON_CODES[code]["allowed"]:
            raise ValueError(f"{code}: effect {eff} not allowed (allowed: {REASON_CODES[code]['allowed']})")
    merged["demo"]["stage_pacing_ms"] = max(0, min(3000, int(merged["demo"]["stage_pacing_ms"])))
    if merged["extraction_mode"] not in ("auto", "local", "llm"):
        raise ValueError("extraction_mode must be auto, local or llm")
    return merged


def save_rules(db: Session, rules: dict, user: str) -> dict:
    merged = validate_rules(rules)
    row = db.get(Setting, "rules")
    if row:
        row.value = merged
        row.updated_by = user
    else:
        db.add(Setting(key="rules", value=merged, updated_by=user))
    return merged
