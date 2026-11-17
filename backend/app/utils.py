"""Small, dependency-free helpers shared across the pipeline."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
EIN_RE = re.compile(r"\b(\d{2}-\d{7})\b")

# Indian GST state codes used by the demo data (subset).
GST_STATES = {
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu", "36": "Telangana",
}


def gstin_check_char(first14: str) -> str:
    """GSTIN check digit (Luhn mod-36 variant used by GSTN)."""
    total = 0
    for i, ch in enumerate(first14):
        val = GSTIN_CHARS.index(ch)
        prod = val * (2 if i % 2 else 1)
        total += prod // 36 + prod % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + gstin_check_char(base)


def gstin_valid(gstin: Optional[str]) -> bool:
    if not gstin or not GSTIN_RE.fullmatch(gstin):
        return False
    return gstin_check_char(gstin[:14]) == gstin[14]


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_float(x) -> Optional[float]:
    return None if x is None else float(money(x))


AMOUNT_RE = re.compile(r"(?<![\w.])-?\(?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?\)?(?![\w])")


def parse_amount(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    t = str(text)
    t = re.sub(r"(?i)\b(rs\.?|inr|usd|us\$)\s*", "", t).replace("$", "").replace("₹", "")
    m = re.search(r"-?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?", t)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),                       # 2026-11-03
    re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b"),                # 03/11/2026 (DD/MM)
    re.compile(r"\b(\d{1,2})[\s-]([A-Za-z]{3,9})[,\s-]+(\d{4})\b"),       # 03-Nov-2026, 3 November 2026
    re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"),         # November 3, 2026
]


def parse_date(text: Optional[str]) -> Optional[date]:
    """Parse common invoice date formats. Numeric dates are read as DD/MM/YYYY (Indian convention)."""
    if not text:
        return None
    for i, pat in enumerate(DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        try:
            if i == 0:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif i == 1:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif i == 2:
                d, y = int(m.group(1)), int(m.group(3))
                mo = MONTHS.get(m.group(2)[:3].lower())
            else:
                mo = MONTHS.get(m.group(1)[:3].lower())
                d, y = int(m.group(2)), int(m.group(3))
            if not mo:
                continue
            return date(y, mo, d)
        except ValueError:
            continue
    return None


def norm_invoice_number(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return re.sub(r"[^A-Z0-9]", "", s.upper()) or None


def norm_po(s: Optional[str]) -> Optional[str]:
    """Normalise a PO reference to PO-NNNN. Applies OCR-confusion fixes to the numeric part."""
    if not s:
        return None
    t = s.upper().replace(" ", "")
    t = re.sub(r"^(PURCHASEORDER|P\.?O\.?)?(NO\.?|NUMBER|NUM|#|REF\.?)?[-#:./]*", "", t)
    m = re.search(r"(?:P\.?O\.?|PURCHASEORDER)?[-#:/]*([0-9OILSB]{3,6})$", t)
    if not m:
        return None
    digits = m.group(1).translate(str.maketrans("OILSB", "01158"))
    if not digits.isdigit():
        return None
    return f"PO-{digits}"


COMPANY_SUFFIXES = r"\b(private|pvt|limited|ltd|llp|inc|co|company|corp|corporation|the)\b\.?"


def norm_company(name: Optional[str]) -> str:
    if not name:
        return ""
    t = name.lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(COMPANY_SUFFIXES, " ", t)
    return re.sub(r"\s+", " ", t).strip()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def now() -> datetime:
    return datetime.now()


def mask_account(acct: Optional[str]) -> Optional[str]:
    if not acct:
        return acct
    d = re.sub(r"\D", "", acct)
    return ("•" * max(0, len(d) - 4)) + d[-4:] if len(d) > 4 else acct
