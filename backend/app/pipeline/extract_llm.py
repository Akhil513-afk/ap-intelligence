"""Optional LLM extraction (Anthropic Claude) with grounding and agreement checks.

Used only when ANTHROPIC_API_KEY is set and extraction_mode is 'auto' or 'llm'. The LLM only *reads*
the document; it never decides anything. Its output is then:
  * grounded  - every value must be traceable to the document text, otherwise confidence is capped at 0.5;
  * compared  - against the deterministic parser; agreement raises confidence, disagreement flags review.
Any API/parse failure falls back to the local extractor - the pipeline never depends on the API.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

import httpx
from rapidfuzz import fuzz

from ..config import ANTHROPIC_API_KEY, ANTHROPIC_API_URL, ANTHROPIC_MODEL, LLM_TIMEOUT_S
from ..utils import norm_company, norm_invoice_number, norm_po, parse_date
from .document import DocModel
from .extract_local import ExtractionResult, FieldResult, LineResult

SCALAR_KEYS = ["invoice_number", "invoice_date", "due_date", "po_reference", "payment_terms", "currency",
               "subtotal", "tax_type", "tax_amount", "total_amount", "vendor_name", "vendor_address",
               "vendor_tax_id", "vendor_email", "bank_name", "bank_account", "bank_ifsc"]

TOOL = {
    "name": "record_invoice",
    "description": "Record the fields read from the invoice. Use null for anything not clearly present. "
                   "Never compute or guess values that are not printed on the document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "properties": {k: {"type": "object", "properties": {
                    "value": {"type": ["string", "null"]},
                    "source_text": {"type": ["string", "null"], "description": "exact text copied from the document"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                    "required": ["value", "confidence"]} for k in SCALAR_KEYS},
            },
            "line_items": {"type": "array", "items": {"type": "object", "properties": {
                "description": {"type": "string"}, "quantity": {"type": ["number", "null"]},
                "unit_price": {"type": ["number", "null"]}, "tax_rate": {"type": ["number", "null"]},
                "amount": {"type": ["number", "null"]}}, "required": ["description"]}},
            "tax_mode": {"type": "string", "enum": ["exclusive", "inclusive", "none"]},
        },
        "required": ["fields", "line_items", "tax_mode"],
    },
}

PROMPT = """You are reading a vendor invoice for an accounts-payable team in India.
The BUYER is Northwind Retail India Pvt Ltd - never report the buyer as the vendor.
Rules:
- Copy values exactly as printed. Dates as YYYY-MM-DD. Amounts as plain numbers (no commas/currency).
- po_reference: the purchase order number, normalised like PO-1234. tax_type: IGST, CGST+SGST, NONE or null.
- If a value is smudged, cut off, or not present, return null with low confidence. Do NOT infer it.
- source_text must be copied verbatim from the document.
Document text (from {method}):
<document>
{text}
</document>"""


def available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _grounded(value: str, source: Optional[str], doc_text: str) -> bool:
    hay = re.sub(r"\s+", " ", doc_text).lower()
    for cand in (source, value):
        if not cand:
            continue
        c = re.sub(r"\s+", " ", str(cand)).lower().strip()
        if c and (c in hay or fuzz.partial_ratio(c, hay) >= 92):
            return True
    try:   # numeric values: check the digits appear with any thousands grouping
        v = float(value)
        digits = f"{v:.2f}".replace(".", "")
        return digits in re.sub(r"[^0-9]", "", hay)
    except (TypeError, ValueError):
        return False


def _same(key: str, a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    if key in ("subtotal", "tax_amount", "total_amount"):
        try:
            return abs(float(a) - float(b)) <= 0.5
        except ValueError:
            return False
    if key in ("invoice_date", "due_date"):
        return parse_date(a) == parse_date(b)
    if key == "invoice_number":
        return norm_invoice_number(a) == norm_invoice_number(b)
    if key == "po_reference":
        return norm_po(a) == norm_po(b)
    if key == "vendor_name":
        return fuzz.token_set_ratio(norm_company(a), norm_company(b)) >= 92
    return str(a).strip().lower() == str(b).strip().lower()


def call_llm(doc: DocModel, page_png: Optional[bytes]) -> dict:
    content = []
    if page_png and doc.method == "ocr":
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": base64.b64encode(page_png).decode()}})
    content.append({"type": "text", "text": PROMPT.format(method=doc.method, text=doc.full_text[:15000])})
    resp = httpx.post(ANTHROPIC_API_URL, timeout=LLM_TIMEOUT_S, headers={
        "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": 2500, "temperature": 0,
              "tools": [TOOL], "tool_choice": {"type": "tool", "name": "record_invoice"},
              "messages": [{"role": "user", "content": content}]})
    resp.raise_for_status()
    for block in resp.json().get("content", []):
        if block.get("type") == "tool_use":
            return block["input"]
    raise ValueError("LLM returned no structured output")


def merge(llm: dict, local: ExtractionResult, doc: DocModel) -> ExtractionResult:
    """Combine LLM output with the deterministic parse. Deterministic validators still run downstream."""
    text = doc.full_text
    fields: dict[str, FieldResult] = {}
    for key in SCALAR_KEYS:
        lf = local.fields.get(key) or FieldResult(key)
        item = (llm.get("fields") or {}).get(key) or {}
        val = item.get("value")
        if key == "po_reference" and val:
            val = norm_po(val) or val
        if key in ("invoice_date", "due_date") and val:
            d = parse_date(val)
            val = d.isoformat() if d else val
        conf = float(item.get("confidence") or 0)
        if val in (None, ""):
            fields[key] = lf
            if lf.value:
                lf.notes.append("LLM returned no value; kept parser value")
            continue
        notes = []
        grounded = _grounded(val, item.get("source_text"), text)
        if not grounded:
            conf = min(conf, 0.5)
            notes.append("LLM value not found in document text - possible hallucination; capped confidence")
        if lf.value is not None:
            if _same(key, val, lf.value):
                conf = max(conf, lf.confidence, 0.95 if grounded else 0.5)
                notes.append("LLM and deterministic parser agree")
            else:
                conf = min(conf, 0.6)
                notes.append(f"LLM ('{val}') and parser ('{lf.value}') disagree - needs verification")
        fields[key] = FieldResult(key, str(val), item.get("source_text") or lf.raw_text, round(conf, 3),
                                  "llm" + ("+parser" if lf.value is not None else ""), lf.source, notes)
    items = local.line_items
    llm_items = llm.get("line_items") or []
    if llm_items and len(llm_items) != len(items):
        items = [LineResult(i + 1, li.get("description", ""), li.get("quantity"), li.get("unit_price"),
                            li.get("tax_rate"), li.get("amount"), 0.85, {"page": 1, "snippet": "LLM"})
                 for i, li in enumerate(llm_items)]
    return ExtractionResult(fields=fields, line_items=items, tax_mode=llm.get("tax_mode") or local.tax_mode,
                            tax_components=local.tax_components, method=f"llm({ANTHROPIC_MODEL})+{local.method}",
                            ocr_mean_conf=local.ocr_mean_conf, notes=local.notes)
