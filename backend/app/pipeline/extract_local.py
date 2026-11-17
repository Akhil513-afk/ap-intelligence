"""Deterministic, layout-aware invoice extractor (the always-available fallback).

How it works
  1. Find a label (e.g. "Invoice No", "Bill No.", "Inv. No.") in a text segment.
  2. Read the value inline, in the next cell to the right, or in the cell directly below.
  3. Validate/normalise the value with a field-specific parser (dates, amounts, GSTIN checksum...).
  4. Confidence = placement base score x OCR word confidence, then raised/lowered by cross-checks
     (GSTIN checksum, subtotal + tax = total, sum of lines = subtotal, header vs signature vendor name).

It never invents a value: if a label is found but the value cannot be read, the field is returned
as unreadable with the raw OCR text attached, and confidence 0.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

from rapidfuzz import fuzz

from ..seed_data import BUYER
from ..utils import EIN_RE, gstin_check_char, gstin_valid, norm_company, norm_po, parse_date
from .document import DocModel, Segment


# --------------------------------------------------------------------------- result types
@dataclass
class FieldResult:
    key: str
    value: Optional[str] = None
    raw_text: Optional[str] = None
    confidence: float = 0.0
    method: str = "not-found"
    source: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class LineResult:
    line_no: int
    description: str
    quantity: Optional[float]
    unit_price: Optional[float]
    tax_rate: Optional[float]
    amount: Optional[float]
    confidence: float
    source: dict


@dataclass
class ExtractionResult:
    fields: dict[str, FieldResult]
    line_items: list[LineResult]
    tax_mode: str                       # exclusive / inclusive / none
    tax_components: list[dict]
    method: str
    ocr_mean_conf: Optional[float] = None
    notes: list[str] = field(default_factory=list)


FIELD_DEFS = [
    # key, label, group
    ("invoice_number", "Invoice number", "invoice"),
    ("invoice_date", "Invoice date", "invoice"),
    ("due_date", "Due date", "invoice"),
    ("po_reference", "PO reference", "invoice"),
    ("payment_terms", "Payment terms", "invoice"),
    ("currency", "Currency", "invoice"),
    ("subtotal", "Subtotal (taxable value)", "amounts"),
    ("tax_type", "Tax type", "amounts"),
    ("tax_amount", "Tax amount", "amounts"),
    ("total_amount", "Total amount", "amounts"),
    ("vendor_name", "Vendor name", "vendor"),
    ("vendor_address", "Vendor address", "vendor"),
    ("vendor_tax_id", "Vendor tax ID (GSTIN/EIN)", "vendor"),
    ("vendor_email", "Vendor email", "vendor"),
    ("bank_name", "Bank name", "payment"),
    ("bank_account", "Bank account number", "payment"),
    ("bank_ifsc", "IFSC code", "payment"),
]

I = re.IGNORECASE
LABELS: dict[str, list[re.Pattern]] = {
    "invoice_number": [re.compile(p, I) for p in (
        r"\binvoice\s*(?:no|number|num|#)\.?", r"\binv\.?\s*(?:no|#)\.?", r"\bbill\s*(?:no|number)\.?",
        r"\binvoice\s*#", r"^invoice\s*id\b")],
    "invoice_date": [re.compile(p, I) for p in (
        r"\binvoice\s*date\b", r"\bissue\s*date\b", r"\bbill\s*date\b", r"\bdate\s*of\s*issue\b", r"^dated\b")],
    "due_date": [re.compile(p, I) for p in (r"\bdue\s*date\b", r"\bpayment\s*due\b", r"\bdue\s*by\b")],
    "po_reference": [re.compile(p, I) for p in (
        r"\bp\.?\s?o\.?\s*(?:no|number|num|#|ref(?:erence)?)\.?", r"\bpurchase\s*order(?:\s*(?:no|number|#))?",
        r"\bbuyer'?s?\s*order\s*no\.?", r"\byour\s*p\.?o\.?\s*r\w*\.?", r"\border\s*ref(?:erence)?\.?")],
    "payment_terms": [re.compile(p, I) for p in (
        r"\bpayment\s*terms\b", r"\bterms\s*of\s*payment\b", r"^terms\b")],
    "subtotal": [re.compile(p, I) for p in (
        r"\bsub[\s-]?total\b", r"\btaxable\s*value\b", r"\btaxable\s*amount\b")],
    "total_amount": [re.compile(p, I) for p in (
        r"\bgrand\s*total\b", r"\btotal\s*due\b", r"\btotal\s*payable\b", r"\bamount\s*due\b",
        r"\bbalance\s*due\b", r"\binvoice\s*total\b", r"\btotal\s*amount\b", r"\btotal\s*\(incl\.?\s*gst\)",
        r"^total\b(?!\s*tax)")],
    "bank_account": [re.compile(p, I) for p in (
        r"\ba[/l1]?c\.?\s*no\.?", r"\baccount\s*(?:no|number)\.?")],
    "bank_ifsc": [re.compile(r"\bifsc(?:\s*code)?\b", I)],
    "bank_name": [re.compile(r"^bank\s*:", I), re.compile(r"^bank\s*name\b", I)],
}

LABEL_WORDS = {"date", "no", "number", "invoice", "due", "terms", "po", "ref", "order", "bill", "total"}
TAX_COMPONENT_RE = re.compile(r"\b(IGST|CGST|SGST|UTGST)\b[^0-9%]{0,8}(\d{1,2}(?:\.\d+)?)\s*%", I)
SALES_TAX_RE = re.compile(r"\b(?:sales\s*tax|vat)\b[^0-9]{0,4}(?:\((\d{1,2}(?:\.\d+)?)\s*%\))?", I)
TOTAL_TAX_RE = re.compile(r"\btotal\s*tax\b(?!able)", I)
INCLUSIVE_RE = re.compile(r"inclusive\s+of\s+gst|incl\.?\s*gst|gst\s+inclusive|prices?\s+include", I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
AMOUNT_TOKEN_RE = re.compile(r"\$?\d[\d,.]*\d")


# --------------------------------------------------------------------------- value parsers
def fix_amount_token(tok: str) -> Optional[float]:
    t = tok.replace("$", "").strip(".,")
    if t.count(".") > 1:                       # OCR: 84.000.00 -> 84000.00
        head, _, tail = t.rpartition(".")
        t = head.replace(".", "") + "." + tail
    t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def last_amount(text: str, require_decimals=True) -> Optional[tuple[float, str]]:
    toks = AMOUNT_TOKEN_RE.findall(text)
    for tok in reversed(toks):
        if require_decimals and not re.search(r"[.,]\d{2}$", tok):
            continue
        v = fix_amount_token(tok)
        if v is not None:
            return v, tok
    return None


def v_invoice_number(text: str):
    for tok in text.replace("|", " ").split():
        tok = tok.strip(":;,")
        up = tok.upper()
        if up.lower() in LABEL_WORDS:
            continue
        if re.fullmatch(r"[A-Z0-9][A-Z0-9/\-_.]{2,29}", up) and re.search(r"\d", up) and not parse_date(up):
            return tok, tok
        return None   # first real token decides; never skip ahead to an unrelated token
    return None


def v_date(text: str):
    d = parse_date(text)
    return (d.isoformat(), text.strip()) if d else None


def v_po(text: str):
    m = re.search(r"(P\.?\s?O\.?\s*[-#:]?\s*[0-9OIlSB]{3,6})\b|\b(\d{4,6})\b", text, I)
    if not m:
        return None
    raw = m.group(0)
    po = norm_po(raw)
    return (po, raw) if po else None


def v_terms(text: str):
    m = re.search(r"\bne[tf]\s*(\d{1,3})\b", text, I)
    if m:
        return f"Net {m.group(1)}", m.group(0)
    m = re.search(r"due\s+on\s+receipt|immediate", text, I)
    return (m.group(0).title(), m.group(0)) if m else None


def v_amount(text: str):
    r = last_amount(text)
    return (f"{r[0]:.2f}", r[1]) if r else None


def v_account(text: str):
    m = re.search(r"\b\d[\d ]{7,22}\d\b", text)
    return (re.sub(r"\s", "", m.group(0)), m.group(0)) if m else None


def v_ifsc(text: str):
    m = re.search(r"\b([A-Z]{4})([0O])([A-Z0-9]{6})\b", text.upper())
    return (m.group(1) + "0" + m.group(3), m.group(0)) if m else None


def v_text(text: str):
    t = text.strip(" :|")
    return (t, t) if len(t) >= 2 else None


VALUE_FN: dict[str, Callable] = {
    "invoice_number": v_invoice_number, "invoice_date": v_date, "due_date": v_date, "po_reference": v_po,
    "payment_terms": v_terms, "subtotal": v_amount, "total_amount": v_amount, "bank_account": v_account,
    "bank_ifsc": v_ifsc, "bank_name": v_text,
}

# OCR positional correction for GSTIN: [digit x2][letter x5][digit x4][letter][alnum][Z][alnum]
TO_DIGIT = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})
TO_ALPHA = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"})


def correct_gstin(tok: str) -> tuple[str, list[str]]:
    t = re.sub(r"[^A-Za-z0-9]", "", tok).upper()
    if len(t) != 15:
        return t, []
    kinds = "DDAAAAADDDDAXZX"
    out, changes = [], []
    for i, (ch, k) in enumerate(zip(t, kinds)):
        new = ch
        if k == "D":
            new = ch.translate(TO_DIGIT)
        elif k == "A":
            new = ch.translate(TO_ALPHA)
        elif k == "Z" and ch == "2":
            new = "Z"
        if new != ch:
            changes.append(f"'{ch}'→'{new}' at position {i + 1}")
        out.append(new)
    return "".join(out), changes


# --------------------------------------------------------------------------- extractor
class LocalExtractor:
    def __init__(self, doc: DocModel):
        self.doc = doc
        self.segs = doc.segments
        self.by_line: dict[int, list[Segment]] = {}
        for s in self.segs:
            self.by_line.setdefault(s.line_idx, []).append(s)
        self.ocr = doc.method == "ocr"

    # -- helpers
    def _src(self, seg: Segment, snippet: Optional[str] = None) -> dict:
        return dict(page=seg.page, bbox=seg.bbox(), line=seg.line_idx, snippet=(snippet or seg.text)[:160])

    @staticmethod
    def _value_conf(words, raw: str) -> float:
        toks = {t.strip(":;,.|") for t in raw.split()}
        sel = [w for w in words if w.text.strip(":;,.|") in toks] or list(words)
        return statistics.fmean(w.conf for w in sel) if sel else 0.0

    def _right_of(self, seg: Segment) -> Optional[Segment]:
        """Nearest cell to the right on the same line, skipping OCR junk cells like ':' or '|'."""
        cands = sorted((s for s in self.by_line.get(seg.line_idx, []) if s.x0 > seg.x1 - 1), key=lambda s: s.x0)
        for s in cands[:3]:
            if re.search(r"[A-Za-z0-9]", s.text) and not (self.ocr and len(s.text.strip()) <= 2 and s.conf < 0.4):
                return s
        return None

    def _below(self, seg: Segment) -> Optional[Segment]:
        for li in (seg.line_idx + 1, seg.line_idx + 2):
            for s in self.by_line.get(li, []):
                if s.page != seg.page or s.top - seg.bottom > 22:
                    continue
                if s.x0 < seg.x1 + 30 and s.x1 > seg.x0 - 10:
                    return s
        return None

    def labeled(self, key: str) -> FieldResult:
        fn = VALUE_FN[key]
        unreadable: Optional[FieldResult] = None
        for seg in self.segs:
            for pat in LABELS[key]:
                m = pat.search(seg.text)
                if not m:
                    continue
                cands = []
                rest = seg.text[m.end():].strip(" :#.-|\t")
                if rest:
                    # words after the label inside this segment
                    pos, words_after = 0, []
                    for w in seg.words:
                        if pos >= m.end():
                            words_after.append(w)
                        pos += len(w.text) + 1
                    cands.append((rest, words_after or seg.words, seg, "label-inline", 0.97))
                r = self._right_of(seg)
                if r:
                    cands.append((r.text, r.words, r, "label-right", 0.95))
                b = self._below(seg)
                if b:
                    cands.append((b.text, b.words, b, "label-below", 0.92))
                for text, words, vseg, method, base in cands:
                    parsed = fn(text)
                    if parsed:
                        value, raw = parsed
                        q = self._value_conf(words, raw) if self.ocr else 1.0
                        fr = FieldResult(key, value, raw, round(base * q, 3), method, self._src(vseg, raw))
                        if self.ocr:
                            fr.notes.append(f"OCR word confidence {q:.0%}")
                        if q < 0.6:
                            fr.notes.append("OCR could not read this value reliably")
                        return fr
                if unreadable is None:
                    near = [c for c in cands if c[3] != "label-below"]
                    raw = (near[0][0] if near else "").strip()
                    unreadable = FieldResult(key, None, raw or None, 0.0, "label-found-value-unreadable",
                                             self._src(seg), [f"Label '{m.group(0).strip()}' found but the value "
                                                              f"could not be read" + (f" (OCR read: '{raw}')" if raw
                                                                                      else "")])
        return unreadable or FieldResult(key, notes=["No matching label found in the document"])

    # -- specific fields
    def vendor_name(self) -> FieldResult:
        page_h = self.doc.page_sizes[0][1] if self.doc.page_sizes else 842
        buyer = norm_company(BUYER["name"])
        best = None
        for s in self.segs:
            if s.page != 1 or s.top > page_h * 0.22:
                continue
            if self.ocr and s.conf < 0.5:
                continue
            t = re.sub(r"\s*\|?\s*((tax|gst)\s+)?invoice\b.*$", "", s.text, flags=I).strip(" |:-")
            if len(t) < 4 or not re.search(r"[A-Za-z]{3}", t) or re.match(r"^(tax\s+|gst\s+)?invoice$", t, I):
                continue
            if sum(c.isdigit() for c in t) > len(t) * 0.3 or "@" in t:
                continue
            if fuzz.token_set_ratio(norm_company(t), buyer) > 90:
                continue
            size = statistics.median(w.size for w in s.words) if self.ocr else s.size
            if best is None or size > best[2] + 0.5:
                best = (s, t, size)
        if not best:
            return FieldResult("vendor_name", notes=["No prominent company name found in the header"])
        seg, name, _ = best
        name = re.sub(r"(\s+[^A-Za-z0-9&.]{1,3}|\s+[a-z]{1,2})+$", "", name).strip()   # trailing OCR debris
        q = seg.conf if self.ocr else 1.0
        conf = 0.86
        notes = ["Largest text in the page header"]
        if re.search(r"\b(pvt|private|ltd|limited|llp|inc|co|industries|services|corp)\b\.?", name, I):
            conf += 0.04
        for s in self.segs:   # signature block "For <Vendor>" corroborates the header
            m = re.match(r"^for\s+(.+)$", s.text, I)
            if m and fuzz.token_set_ratio(norm_company(m.group(1)), norm_company(name)) >= 90:
                conf += 0.06
                notes.append("Matches signature block")
                break
        fr = FieldResult("vendor_name", name, seg.text, round(min(conf, 0.98) * q, 3), "header-heuristic",
                         self._src(seg), notes)
        self._vendor_seg = seg
        return fr

    def vendor_address(self) -> FieldResult:
        seg = getattr(self, "_vendor_seg", None)
        if not seg:
            return FieldResult("vendor_address", notes=["Vendor header not found"])
        parts, words = [], []
        for li in (seg.line_idx + 1, seg.line_idx + 2):
            for s in self.by_line.get(li, []):
                if s.x0 < seg.x1 and s.x1 > seg.x0 - 5 and abs(s.x0 - seg.x0) < 120:
                    if re.search(r"gstin|@|phone|\bph\b|email|invoice|tax id", s.text, I):
                        break
                    parts.append(s.text.strip(" ,"))
                    words += s.words
                    break
            else:
                continue
            if len(parts) and re.search(r"\b\d{6}\b|\bUSA\b|\b\d{5}\b", parts[-1]):
                break
        if not parts:
            return FieldResult("vendor_address", notes=["No address lines under the vendor name"])
        q = statistics.fmean(w.conf for w in words) if self.ocr else 1.0
        return FieldResult("vendor_address", ", ".join(parts), " / ".join(parts), round(0.85 * q, 3),
                           "below-vendor-name", self._src(seg, ", ".join(parts)))

    def tax_ids(self) -> FieldResult:
        buyer12 = BUYER["gstin"][:12]
        cands = []
        for s in self.segs:
            for w in s.words:
                tok = re.sub(r"[^A-Za-z0-9]", "", w.text)
                if len(tok) != 15:
                    continue
                fixed, changes = correct_gstin(tok)
                if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]", fixed):
                    continue
                if fixed[:12] == buyer12:
                    continue
                cands.append((s, w, tok, fixed, changes))
        if cands:
            s, w, tok, fixed, changes = cands[0]
            valid = gstin_valid(fixed)
            notes = []
            if changes:
                notes.append("OCR positional correction: " + ", ".join(changes))
            if valid:
                conf = 0.99 if not self.ocr else 0.96
                notes.append("GSTIN checksum valid")
            else:
                conf = 0.45
                notes.append(f"GSTIN checksum FAILED (expected check character "
                             f"'{gstin_check_char(fixed[:14])}')")
            if len(cands) > 1 and any(c[3] != fixed for c in cands[1:]):
                notes.append(f"{len(cands)} non-buyer GSTINs on the document; used the first (header)")
            return FieldResult("vendor_tax_id", fixed, tok, conf, "gstin-pattern", self._src(s, w.text), notes)
        for s in self.segs:
            if re.search(r"\b(ein|tax\s*id|tin)\b", s.text, I):
                m = EIN_RE.search(s.text)
                if m:
                    q = s.conf if self.ocr else 1.0
                    return FieldResult("vendor_tax_id", m.group(1), m.group(0), round(0.95 * q, 3), "ein-pattern",
                                       self._src(s, m.group(0)), ["US Employer Identification Number"])
        return FieldResult("vendor_tax_id", notes=["No GSTIN or other tax ID found"])

    def email(self) -> FieldResult:
        for s in self.segs:
            for m in EMAIL_RE.finditer(s.text):
                e = m.group(0).strip(".").lower()
                if BUYER["email_domain"] in e:
                    continue
                q = self._value_conf(s.words, m.group(0)) if self.ocr else 1.0
                return FieldResult("vendor_email", e, m.group(0), round(0.95 * q, 3), "email-pattern",
                                   self._src(s, m.group(0)))
        return FieldResult("vendor_email", notes=["No vendor email on the document"])

    def currency(self) -> FieldResult:
        text = self.doc.full_text
        if re.search(r"\bUSD\b|US\$|\$\s?\d", text):
            return FieldResult("currency", "USD", "USD / $", 0.95, "symbol-scan", None, ["'USD' or '$' on document"])
        if re.search(r"\bINR\b|\bRs\.?\s|₹|\bRupees\b", text, I):
            return FieldResult("currency", "INR", "INR / Rs / Rupees", 0.95, "symbol-scan", None,
                               ["'INR', 'Rs' or 'Rupees' on document"])
        if re.search(r"GSTIN", text, I):
            return FieldResult("currency", "INR", None, 0.75, "inferred", None,
                               ["No currency stated; inferred INR from Indian GST registration"])
        return FieldResult("currency", notes=["No currency indicator found"])

    def taxes(self) -> tuple[list[dict], Optional[FieldResult], FieldResult]:
        comps, seen = [], set()
        for s in self.segs:
            m = TAX_COMPONENT_RE.search(s.text)
            sales = None if m else SALES_TAX_RE.search(s.text)
            if not (m or sales):
                continue
            val = last_amount(s.text[(m or sales).end():])
            src = s
            if not val:
                r = self._right_of(s)
                if r:
                    val = last_amount(r.text)
                    src = r
            if not val:
                continue
            kind = m.group(1).upper() if m else "SALES TAX"
            rate = float(m.group(2)) if m else float(sales.group(1) or 0)
            key = (kind, rate, val[0])
            if key in seen:
                continue
            seen.add(key)
            q = self._value_conf(src.words, val[1]) if self.ocr else 1.0
            comps.append(dict(type=kind, rate=rate, amount=val[0], confidence=round(0.95 * q, 3),
                              source=self._src(src, s.text)))
        explicit = None
        for s in self.segs:
            if TOTAL_TAX_RE.search(s.text):
                val = last_amount(s.text) or (last_amount(self._right_of(s).text) if self._right_of(s) else None)
                if val:
                    q = self._value_conf(s.words, val[1]) if self.ocr else 1.0
                    explicit = FieldResult("tax_amount", f"{val[0]:.2f}", val[1], round(0.96 * q, 3),
                                           "label-total-tax", self._src(s, s.text))
                    break
        types = {c["type"] for c in comps}
        if "IGST" in types:
            tt = "IGST"
        elif {"CGST", "SGST"} & types:
            tt = "CGST+SGST"
        elif "SALES TAX" in types:
            tt = "NONE" if all(c["amount"] == 0 for c in comps) else "SALES TAX"
        else:
            tt = None
        tax_type = FieldResult("tax_type", tt, ", ".join(sorted(types)) or None, 0.95 if tt else 0.0,
                               "tax-lines" if tt else "not-found")
        if explicit is None and comps:
            total = round(sum(c["amount"] for c in comps), 2)
            conf = min(c["confidence"] for c in comps)
            explicit = FieldResult("tax_amount", f"{total:.2f}", " + ".join(f"{c['type']} {c['amount']:.2f}"
                                                                           for c in comps),
                                   round(conf, 3), "sum-of-tax-lines", comps[0]["source"],
                                   [f"Sum of {len(comps)} tax line(s)"])
        return comps, explicit, tax_type

    def line_items(self) -> list[LineResult]:
        lines = self.doc.lines
        start, end = 0, len(lines)
        for i, ln in enumerate(lines):
            if re.search(r"\b(description|item|particulars)\b", ln.text, I) and \
                    re.search(r"\b(qty|quantity|trips|amount|amt)\b", ln.text, I):
                start = i + 1
                break
        for j in range(start, len(lines)):
            if re.search(r"sub[\s-]?total|taxable\s*value|amount\s+in\s+words|^total\b|rupees:", lines[j].text, I):
                end = j
                break
        rx = re.compile(
            r"^(?:(?P<idx>\d{1,3})[.)]?\s+)?(?P<desc>.*?[A-Za-z].*?)\s+(?:(?P<hsn>\d{4,8})\s+)?"
            r"(?P<qty>\d+(?:\.\d+)?)\s+(?:(?P<unit>[A-Za-z]{2,8})\.?\s+)?(?P<price>\$?\d[\d,.]*?(?:[.,]\d{2})?)\s+"
            r"(?:(?P<rate>\d{1,2}(?:\.\d+)?)\s*%?\s+)?(?P<amount>\$?\d[\d,.]*[.,]\d{2})$")
        out: list[LineResult] = []
        for ln in lines[start:end]:
            text = re.sub(r"[\s.|:,]+$", "", ln.text.strip()).strip("| ")
            m = rx.match(text)
            if not m:
                if out and not re.search(r"\d[.,]\d{2}", text) and len(text) > 3 and ln.page == out[-1].source["page"]:
                    out[-1].description += " " + text     # wrapped description
                continue
            qty = float(m.group("qty"))
            price = fix_amount_token(m.group("price"))
            amount = fix_amount_token(m.group("amount"))
            rate = float(m.group("rate")) if m.group("rate") else None
            q = ln.conf if self.ocr else 1.0
            conf = 0.95 * q
            if price is not None and amount is not None and abs(qty * price - amount) <= max(1.0, amount * 0.005):
                conf = max(conf, 0.97 if not self.ocr else 0.9)    # qty x price = amount corroborates the row
            else:
                conf *= 0.6
            out.append(LineResult(len(out) + 1, m.group("desc").strip(" -"), qty, price, rate, amount,
                                  round(conf, 3), dict(page=ln.page, line=ln.idx, snippet=ln.text[:160])))
        return out

    # -- orchestrate
    def run(self) -> ExtractionResult:
        f: dict[str, FieldResult] = {}
        for key in ("invoice_number", "invoice_date", "due_date", "po_reference", "payment_terms", "subtotal",
                    "total_amount", "bank_account", "bank_ifsc", "bank_name"):
            f[key] = self.labeled(key)
        f["vendor_name"] = self.vendor_name()
        f["vendor_address"] = self.vendor_address()
        f["vendor_tax_id"] = self.tax_ids()
        f["vendor_email"] = self.email()
        f["currency"] = self.currency()
        comps, tax_amt, tax_type = self.taxes()
        f["tax_amount"] = tax_amt or FieldResult("tax_amount", notes=["No tax lines found"])
        f["tax_type"] = tax_type
        items = self.line_items()
        notes = []

        # PO reference fallback: an unlabelled "PO-1234" anywhere is weaker evidence than a labelled one.
        if not f["po_reference"].value:
            for s in self.segs:
                m = re.search(r"\bPO[-\s]?\d{4,6}\b", s.text)
                if m:
                    prev = f["po_reference"]
                    f["po_reference"] = FieldResult("po_reference", norm_po(m.group(0)), m.group(0),
                                                    round(0.7 * (s.conf if self.ocr else 1.0), 3), "unlabelled-pattern",
                                                    self._src(s, m.group(0)),
                                                    ["Found without a PO label; implied reference"] + prev.notes)
                    break
        po = f["po_reference"]
        if po.value and po.raw_text and re.search(r"[OIlSB]", re.sub(r"^\s*P\.?\s?O", "", po.raw_text, flags=I)):
            po.confidence = round(po.confidence * 0.85, 3)
            po.notes.append(f"OCR character substitution applied ('{po.raw_text}' → {po.value})")

        tax_mode = "inclusive" if INCLUSIVE_RE.search(self.doc.full_text) else (
            "none" if f["tax_type"].value == "NONE" else "exclusive")

        # Cross-checks that raise confidence when independent numbers agree.
        def fv(k):
            v = f[k].value
            try:
                return float(v) if v is not None else None
            except ValueError:
                return None
        sub, tax, tot = fv("subtotal"), fv("tax_amount"), fv("total_amount")
        cap = 0.97 if not self.ocr else 0.93
        if sub is not None and tax is not None and tot is not None and abs(sub + tax - tot) <= 1.0:
            for k in ("subtotal", "tax_amount", "total_amount"):
                if f[k].confidence >= 0.5:
                    f[k].confidence = max(f[k].confidence, cap)
                    f[k].notes.append("Cross-check: subtotal + tax = total")
        if items and sub is not None:
            s_lines = round(sum(i.amount or 0 for i in items), 2)
            target = tot if tax_mode == "inclusive" else sub
            if target is not None and abs(s_lines - target) <= 1.0:
                for k in ("subtotal", "total_amount") if tax_mode == "inclusive" else ("subtotal",):
                    if f[k].confidence >= 0.5:
                        f[k].confidence = max(f[k].confidence, cap)
                        f[k].notes.append("Cross-check: sum of line items matches")
        if tot is None and sub is not None and tax is not None:
            f["total_amount"].notes.append(
                f"Not stated on the document. Subtotal + tax would be {sub + tax:,.2f} - NOT filled in automatically.")
        if self.ocr and self.doc.ocr_mean_conf is not None:
            notes.append(f"OCR mean word confidence {self.doc.ocr_mean_conf:.0%}")
        return ExtractionResult(fields=f, line_items=items, tax_mode=tax_mode, tax_components=comps,
                                method=self.doc.method, ocr_mean_conf=self.doc.ocr_mean_conf, notes=notes)
