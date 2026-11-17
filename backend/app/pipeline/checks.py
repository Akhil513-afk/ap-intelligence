"""Shared check model + field assessment. All business validation produces `Check` objects."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

from ..utils import EIN_RE, gstin_valid, parse_date

PASS, WARN, FAIL, INFO, UNABLE = "PASS", "WARN", "FAIL", "INFO", "UNABLE"


@dataclass
class Check:
    stage: str
    code: str
    name: str
    outcome: str
    message: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    difference: Optional[str] = None
    rule: Optional[str] = None
    reason_code: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def fmt_money(v: Optional[float], cur: Optional[str] = "INR") -> str:
    if v is None:
        return "—"
    sym = {"INR": "₹", "USD": "$"}.get(cur or "", (cur or "") + " ")
    return f"{sym}{v:,.2f}"


@dataclass
class FieldView:
    key: str
    value: Optional[str]          # effective value (corrected if a reviewer corrected it)
    original: Optional[str]
    confidence: float
    status: str                   # VALID / UNCERTAIN / INVALID / MISSING
    corrected: bool
    critical: bool


@dataclass
class EffectiveInvoice:
    fields: dict[str, FieldView]
    line_items: list[dict]
    tax_mode: str
    tax_components: list[dict]

    def get(self, key: str) -> Optional[str]:
        f = self.fields.get(key)
        return f.value if f else None

    def num(self, key: str) -> Optional[float]:
        v = self.get(key)
        try:
            return float(str(v).replace(",", "")) if v not in (None, "") else None
        except ValueError:
            return None

    def date(self, key: str) -> Optional[date]:
        return parse_date(self.get(key))

    def status(self, key: str) -> str:
        f = self.fields.get(key)
        return f.status if f else "MISSING"


def format_ok(key: str, value: Optional[str]) -> tuple[bool, Optional[str]]:
    """Field-level format validation. Returns (ok, problem)."""
    if value in (None, ""):
        return False, "missing"
    if key in ("invoice_date", "due_date"):
        return (parse_date(value) is not None, "not a valid date")
    if key in ("subtotal", "tax_amount", "total_amount"):
        try:
            v = float(value)
            return (v >= 0, "negative amount")
        except ValueError:
            return False, "not a number"
    if key == "vendor_tax_id":
        if EIN_RE.fullmatch(value):
            return True, None
        return (gstin_valid(value), "GSTIN format/checksum invalid")
    if key == "po_reference":
        return (bool(re.fullmatch(r"PO-\d{3,6}", value)), "not a PO number")
    if key == "vendor_email":
        return (bool(re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", value)), "not an email address")
    if key == "currency":
        return (value in ("INR", "USD", "EUR", "GBP"), "unsupported currency")
    if key == "bank_ifsc":
        return (bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", value)), "IFSC format invalid")
    return True, None


def assess(key: str, value: Optional[str], confidence: float, corrected: bool, threshold: float) -> str:
    if corrected:
        ok, _ = format_ok(key, value)
        return "VALID" if ok else "INVALID"
    if value in (None, ""):
        return "MISSING"
    ok, _ = format_ok(key, value)
    if not ok:
        return "INVALID"
    return "VALID" if confidence >= threshold else "UNCERTAIN"


def to_jsonable(x: Any):
    if isinstance(x, date):
        return x.isoformat()
    return x
