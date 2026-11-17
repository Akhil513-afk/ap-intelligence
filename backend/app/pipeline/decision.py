"""Stage 8 - deterministic decision engine.

The LLM (if used) only reads documents. The decision is a pure function of the checks and the
configured policy table:  any REJECT-effect reason -> REJECTED; else any REVIEW-effect reason ->
NEEDS_REVIEW; else APPROVED. Every decision carries its reason codes, evidence and the rules snapshot,
so it can be reproduced and audited.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..rules import REASON_CODES
from .checks import FAIL, INFO, PASS, UNABLE, WARN, Check, EffectiveInvoice, fmt_money
from .duplicates import DuplicateResult
from .po_match import POResult
from .vendor import VendorResult

# Reason codes a reviewer may NOT override by simply approving: they must be fixed (correct a field,
# select the right PO, onboard/reinstate the vendor) and the invoice re-evaluated first.
HARD_BLOCKERS = {"VENDOR_PO_MISMATCH", "PO_MISSING", "PO_NOT_FOUND", "VENDOR_LOOKALIKE", "VENDOR_NOT_FOUND",
                 "VENDOR_NOT_APPROVED", "VENDOR_BLOCKED", "BANK_DETAILS_MISMATCH", "MISSING_FIELD",
                 "DUPLICATE_CONFIRMED", "PO_CLOSED", "VENDOR_TAX_ID_MISSING", "CURRENCY_MISMATCH"}

SEVERITY_ORDER = ["DUPLICATE_CONFIRMED", "VENDOR_BLOCKED", "VENDOR_LOOKALIKE", "BANK_DETAILS_MISMATCH",
                  "VENDOR_PO_MISMATCH", "PO_CLOSED", "AMOUNT_OVER_HARD_LIMIT", "VENDOR_NOT_FOUND",
                  "VENDOR_NOT_APPROVED", "AMOUNT_OVER_TOLERANCE", "PO_BALANCE_EXCEEDED", "MISSING_FIELD",
                  "PO_MISSING", "PO_NOT_FOUND", "PO_UNCERTAIN", "LOW_CONFIDENCE", "POSSIBLE_DUPLICATE",
                  "CURRENCY_MISMATCH", "LINE_ITEM_UNMATCHED", "QUANTITY_EXCEEDS_PO", "UNIT_PRICE_MISMATCH",
                  "ARITHMETIC_ERROR", "TAX_MISMATCH", "TAX_TYPE_MISMATCH", "VENDOR_TAX_ID_MISSING",
                  "INVALID_TAX_ID", "VENDOR_NAME_MISMATCH", "DATE_ANOMALY", "AMOUNT_WITHIN_TOLERANCE"]


@dataclass
class Decision:
    status: str
    headline: str
    reason_codes: list[dict]
    explanation: list[str]
    recommended_action: str
    checks_summary: dict
    blocking_for_approval: list[str]


def decide(checks: list[Check], rules: dict, inv: EffectiveInvoice, vendor: VendorResult, po: POResult,
           dup: DuplicateResult) -> Decision:
    policies = rules["policies"]
    reasons: dict[str, dict] = {}
    for c in checks:
        if not c.reason_code:
            continue
        eff = policies.get(c.reason_code, REASON_CODES.get(c.reason_code, {}).get("default", "REVIEW"))
        r = reasons.setdefault(c.reason_code, dict(code=c.reason_code, effect=eff,
                                                   label=REASON_CODES.get(c.reason_code, {}).get("label", c.reason_code),
                                                   messages=[], stage=c.stage))
        r["messages"].append(c.message)
    order = {code: i for i, code in enumerate(SEVERITY_ORDER)}
    rank = {"REJECT": 0, "REVIEW": 1, "NONE": 2}
    rc = sorted(reasons.values(), key=lambda r: (rank[r["effect"]], order.get(r["code"], 99)))
    rejects = [r for r in rc if r["effect"] == "REJECT"]
    reviews = [r for r in rc if r["effect"] == "REVIEW"]
    status = "REJECTED" if rejects else "NEEDS_REVIEW" if reviews else "APPROVED"

    summary = dict(passed=sum(c.outcome == PASS for c in checks), warnings=sum(c.outcome == WARN for c in checks),
                   failed=sum(c.outcome == FAIL for c in checks), unable=sum(c.outcome == UNABLE for c in checks),
                   info=sum(c.outcome == INFO for c in checks), total=len(checks))
    cur = inv.get("currency")
    total = inv.num("total_amount")
    vname = (vendor.vendor or {}).get("name") or inv.get("vendor_name") or "Unknown vendor"
    po_no = po.po["po_number"] if po.po and po.accepted else None

    expl: list[str] = []
    if status == "APPROVED":
        headline = (f"{vname} invoice {inv.get('invoice_number')} for {fmt_money(total, cur)} passed all mandatory "
                    f"checks against {po_no}.")
        expl.append(f"Vendor verified by tax ID ({inv.get('vendor_tax_id')}), active and approved.")
        if po.amount:
            cls = po.amount.get("classification")
            expl.append({"EXACT": f"Amount equals the remaining balance of {po_no}.",
                         "PARTIAL": f"Partial billing within the remaining balance of {po_no} "
                                    f"({fmt_money(po.amount.get('remaining_before'), cur)} before this invoice).",
                         "WITHIN_TOLERANCE": f"Amount is {fmt_money(po.amount.get('difference'), cur)} "
                                             f"({po.amount.get('difference_pct')}%) over {po_no} - within the "
                                             f"permitted tolerance; variance recorded."}.get(cls, ""))
        expl.append(f"{summary['passed']} checks passed, {summary['warnings']} warning(s), 0 blocking issues.")
        due = inv.get("due_date")
        action = (f"Eligible for AP approval. Schedule in the payment run per vendor terms"
                  f"{' (invoice due ' + due + ')' if due else ''}. No payment has been initiated - payment release "
                  f"remains a human-controlled step.")
    else:
        top = (rejects or reviews)[0]
        blocking_n = len([r for r in rc if r["effect"] != "NONE"])
        more = f" (+{blocking_n - 1} more issue{'s' if blocking_n > 2 else ''})" if blocking_n > 1 else ""
        headline = f"{'Rejected' if status == 'REJECTED' else 'Needs review'}: {top['label']}{more}."
        for r in rejects + reviews:
            expl.append(f"[{r['effect']}] {r['label']}: {r['messages'][0]}")
        action = REASON_CODES.get(top["code"], {}).get("action", "Review the invoice.")
        if dup.original_run_id and top["code"] == "DUPLICATE_CONFIRMED":
            action += f" Original: {dup.original_run_id}."
    for r in rc:
        if r["effect"] == "NONE":
            expl.append(f"[INFO] {r['label']}: {r['messages'][0]}")
    blocking = [r["code"] for r in reviews if r["code"] in HARD_BLOCKERS]
    return Decision(status, headline, [dict(code=r["code"], effect=r["effect"], label=r["label"], stage=r["stage"],
                                            message=r["messages"][0]) for r in rc],
                    [e for e in expl if e], action, summary, blocking)
