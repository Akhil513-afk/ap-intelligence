"""Generate realistic vendor invoice PDFs for the AP Intelligence demo.

Five layouts (classic GST, modern band, logistics, retail tax-inclusive, US SaaS), digital and
scanned variants. Scanned invoices are rasterised, rotated, noised and (for the OCR edge case)
smudged over the invoice number and PO reference.

Usage:  python sample_data/generate_invoices.py
"""
from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "backend"))
from app.seed_data import BUYER, VENDORS  # noqa: E402
from app.utils import make_gstin  # noqa: E402

W, H = A4
VMAP = {v["id"]: v for v in VENDORS}


# ----------------------------------------------------------------------------- helpers
def fmt_in(x: float) -> str:
    """Indian digit grouping: 1,08,000.00"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + "." + frac


def fmt_us(x: float) -> str:
    return f"{x:,.2f}"


ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
       "seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _words(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _words(n % 100))
    for div, name in ((10**7, "crore"), (10**5, "lakh"), (1000, "thousand")):
        if n >= div:
            return _words(n // div) + f" {name}" + ("" if n % div == 0 else " " + _words(n % div))
    return str(n)


def inr_words(x: float) -> str:
    rupees = int(round(x * 100)) // 100
    paise = int(round(x * 100)) % 100
    s = "Rupees " + _words(rupees).title()
    if paise:
        s += " and " + _words(paise).title() + " Paise"
    return s + " Only"


def d(s: str, style: str = "dmy") -> str:
    y, m, dd = map(int, s.split("-"))
    dt = date(y, m, dd)
    if style == "dmy":
        return dt.strftime("%d-%b-%Y")
    if style == "slash":
        return dt.strftime("%d/%m/%Y")
    if style == "long":
        return dt.strftime("%B %-d, %Y")
    return s


def compute(inv: dict) -> dict:
    """Compute line amounts, tax split and totals the way the vendor's billing system would."""
    mode = inv.get("tax_mode", "exclusive")
    vendor = inv["vendor"]
    intra = vendor.get("state_code") == BUYER["state_code"]
    lines = []
    for ln in inv["lines"]:
        amt = round(ln["qty"] * ln["price"], 2)
        lines.append({**ln, "amount": amt})
    if mode == "none":
        sub = round(sum(l["amount"] for l in lines), 2)
        return dict(lines=lines, subtotal=sub, tax_lines=[], tax_total=0.0, total=sub, intra=intra)
    by_rate: dict[float, float] = {}
    for l in lines:
        by_rate[l["rate"]] = by_rate.get(l["rate"], 0) + l["amount"]
    tax_lines = []
    if mode == "exclusive":
        sub = round(sum(l["amount"] for l in lines), 2)
        for rate, base in sorted(by_rate.items()):
            t = round(base * rate / 100, 2)
            if intra:
                half = round(t / 2, 2)
                tax_lines += [(f"CGST @ {rate/2:g}%", half), (f"SGST @ {rate/2:g}%", round(t - half, 2))]
            else:
                tax_lines.append((f"IGST @ {rate:g}%", t))
        tax_total = round(sum(t for _, t in tax_lines), 2)
        total = round(sub + tax_total, 2)
    else:  # inclusive: line amounts already contain GST
        total = round(sum(l["amount"] for l in lines), 2)
        taxable = 0.0
        for rate, gross in sorted(by_rate.items()):
            base = round(gross / (1 + rate / 100), 2)
            taxable += base
            t = round(gross - base, 2)
            if intra:
                half = round(t / 2, 2)
                tax_lines += [(f"CGST @ {rate/2:g}%", half), (f"SGST @ {rate/2:g}%", round(t - half, 2))]
            else:
                tax_lines.append((f"IGST @ {rate:g}%", t))
        sub = round(taxable, 2)
        tax_total = round(sum(t for _, t in tax_lines), 2)
    return dict(lines=lines, subtotal=sub, tax_lines=tax_lines, tax_total=tax_total, total=total, intra=intra)


class Doc:
    def __init__(self):
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.pos: dict[str, tuple[float, float, float]] = {}

    def text(self, x, y, s, font="Helvetica", size=9, color=colors.black, align="left", key=None):
        c = self.c
        c.setFont(font, size)
        c.setFillColor(color)
        w = c.stringWidth(s, font, size)
        if align == "right":
            c.drawRightString(x, y, s)
            x0 = x - w
        elif align == "center":
            c.drawCentredString(x, y, s)
            x0 = x - w / 2
        else:
            c.drawString(x, y, s)
            x0 = x
        if key:
            self.pos[key] = (x0, y, w)
        return w

    def kv(self, x, y, label, value, key=None, lw=78, size=9, font="Helvetica"):
        bold = {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold", "Courier": "Courier-Bold"}.get(font, font)
        self.text(x, y, label, bold, size)
        self.text(x + lw, y, value, font, size, key=key)

    def hline(self, x1, x2, y, width=0.6, color=colors.black):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(x1, y, x2, y)

    def finish(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buf.getvalue()


def bank_block(doc: Doc, x, y, v, size=8.5):
    doc.text(x, y, "Bank Details for Payment", "Helvetica-Bold", size)
    doc.text(x, y - 12, f"Bank: {v['bank_name']}", size=size)
    doc.text(x, y - 23, f"A/c No: {v['bank_account']}", size=size)
    if v.get("ifsc"):
        doc.text(x, y - 34, f"IFSC: {v['ifsc']}", size=size)


def wrap(s: str, n: int) -> list[str]:
    words, out, cur = s.split(), [], ""
    for w_ in words:
        if len(cur) + len(w_) + 1 > n and cur:
            out.append(cur)
            cur = w_
        else:
            cur = (cur + " " + w_).strip()
    if cur:
        out.append(cur)
    return out


# ----------------------------------------------------------------------------- templates
def t_classic(inv, calc) -> Doc:
    """T1: classic Indian GST invoice with ruled table."""
    v, doc = inv["vendor"], Doc()
    top = H - 50
    doc.text(40, top, v["name"], "Helvetica-Bold", 16)
    y = top - 16
    for ln in wrap(v["address"], 60):
        doc.text(40, y, ln, size=8.5)
        y -= 11
    doc.text(40, y, f"GSTIN: {v['tax_id']}", "Helvetica-Bold", 8.5)
    doc.text(40, y - 11, f"Email: {v['email']}  |  Phone: {v['phone']}", size=8.5)
    doc.text(W - 40, top, "TAX INVOICE", "Helvetica-Bold", 18, colors.HexColor("#1f2937"), align="right")
    # meta box
    bx, by = W - 250, top - 92
    doc.c.setStrokeColor(colors.HexColor("#9ca3af"))
    doc.c.rect(bx, by, 210, 78)
    rows = [("Invoice No:", inv["invoice_no"], "invoice_number"),
            ("Invoice Date:", d(inv["date"]), "invoice_date"),
            ("Due Date:", d(inv["due"]), "due_date"),
            ("PO Number:", inv.get("po_ref") or "-", "po_reference"),
            ("Payment Terms:", inv["terms"], "payment_terms")]
    for i, (lab, val, key) in enumerate(rows):
        doc.kv(bx + 8, by + 64 - i * 14, lab, val, key=key, lw=80, size=8.5)
    # bill to
    y = top - 118
    doc.text(40, y, "Bill To:", "Helvetica-Bold", 9)
    doc.text(40, y - 12, BUYER["name"], "Helvetica-Bold", 9)
    for i, ln in enumerate(wrap(BUYER["address"], 60)):
        doc.text(40, y - 24 - i * 11, ln, size=8.5)
    doc.text(40, y - 24 - 2 * 11 - 2, f"GSTIN: {BUYER['gstin']}", size=8.5)
    doc.text(W - 250, y, "Place of Supply:", "Helvetica-Bold", 9)
    doc.text(W - 170, y, "Haryana (06)", size=9)
    # table
    ty = y - 80
    cols = [40, 60, 290, 345, 385, 455, 490]
    heads = ["#", "Description", "HSN/SAC", "Qty", "Rate", "GST%", "Amount"]
    doc.c.setFillColor(colors.HexColor("#f3f4f6"))
    doc.c.rect(40, ty - 4, W - 80, 16, fill=1, stroke=0)
    num_x = {"Qty": cols[4] - 4, "Rate": cols[5] - 6, "GST%": cols[6] - 6, "Amount": W - 44}
    for x, hd in zip(cols, heads):
        if hd in num_x:
            doc.text(num_x[hd], ty + 1, hd, "Helvetica-Bold", 8.5, align="right")
        else:
            doc.text(x + 2, ty + 1, hd, "Helvetica-Bold", 8.5)
    doc.hline(40, W - 40, ty - 4)
    yy = ty - 18
    for i, l in enumerate(calc["lines"], 1):
        doc.text(cols[0] + 2, yy, str(i), size=8.5)
        doc.text(cols[1] + 2, yy, l["desc"], size=8.5)
        doc.text(cols[2] + 2, yy, l.get("hsn", "9983"), size=8.5)
        doc.text(cols[4] - 4, yy, f"{l['qty']:g}", size=8.5, align="right")
        doc.text(cols[5] - 6, yy, fmt_in(l["price"]), size=8.5, align="right")
        doc.text(cols[6] - 6, yy, f"{l['rate']:g}%", size=8.5, align="right")
        doc.text(W - 44, yy, fmt_in(l["amount"]), size=8.5, align="right")
        yy -= 16
    doc.hline(40, W - 40, yy + 8)
    # totals
    ty2 = yy - 10
    tx = W - 200
    doc.text(tx, ty2, "Sub Total", "Helvetica-Bold", 9)
    doc.text(W - 44, ty2, fmt_in(calc["subtotal"]), size=9, align="right", key="subtotal")
    k = 1
    for lab, amt in calc["tax_lines"]:
        doc.text(tx, ty2 - 14 * k, lab, size=9)
        doc.text(W - 44, ty2 - 14 * k, fmt_in(amt), size=9, align="right")
        k += 1
    doc.hline(tx, W - 40, ty2 - 14 * k + 10)
    doc.text(tx, ty2 - 14 * k - 2, "Grand Total (INR)", "Helvetica-Bold", 10)
    doc.text(W - 44, ty2 - 14 * k - 2, fmt_in(calc["total"]), "Helvetica-Bold", 10, align="right", key="total")
    doc.text(40, ty2, "Amount in words:", "Helvetica-Bold", 8.5)
    for i, ln in enumerate(wrap(inr_words(calc["total"]), 52)):
        doc.text(40, ty2 - 12 - i * 11, ln, "Helvetica-Oblique", 8.5)
    bank_block(doc, 40, ty2 - 90, v)
    doc.text(W - 40, ty2 - 90, f"For {v['name']}", "Helvetica-Bold", 8.5, align="right")
    doc.text(W - 40, ty2 - 130, "Authorised Signatory", size=8.5, align="right")
    doc.text(W / 2, 40, "This is a computer generated invoice. Subject to Delhi jurisdiction.", size=7.5,
             color=colors.HexColor("#6b7280"), align="center")
    return doc


def t_modern(inv, calc) -> Doc:
    """T2: modern SaaS-style invoice, coloured band, labels above values."""
    v, doc = inv["vendor"], Doc()
    c = doc.c
    band = colors.HexColor(inv.get("accent", "#0f3d4c"))
    c.setFillColor(band)
    c.rect(0, H - 110, W, 110, fill=1, stroke=0)
    doc.text(40, H - 50, v["name"], "Helvetica-Bold", 17, colors.white)
    doc.text(40, H - 66, v["address"], size=8, color=colors.HexColor("#d1e3e8"))
    doc.text(40, H - 78, f"GSTIN {v['tax_id']}   ·   {v['email']}   ·   {v['phone']}", size=8,
             color=colors.HexColor("#d1e3e8"))
    doc.text(W - 40, H - 52, "INVOICE", "Helvetica-Bold", 26, colors.white, align="right")
    # label-over-value grid
    y = H - 145
    grid = [("Invoice Number", inv["invoice_no"], "invoice_number"),
            ("Issue Date", d(inv["date"], "dmy"), "invoice_date"),
            ("Payment Due", d(inv["due"], "dmy"), "due_date"),
            ("Purchase Order", inv.get("po_ref") or "Not provided", "po_reference"),
            ("Terms", inv["terms"], "payment_terms")]
    xs = [40, 150, 250, 350, 465]
    for x, (lab, val, key) in zip(xs, grid):
        doc.text(x, y, lab.upper(), "Helvetica-Bold", 7, colors.HexColor("#6b7280"))
        doc.text(x, y - 14, val, "Helvetica", 9.5, key=key)
    y -= 50
    doc.text(40, y, "BILLED TO", "Helvetica-Bold", 7, colors.HexColor("#6b7280"))
    doc.text(40, y - 14, BUYER["name"], "Helvetica-Bold", 10)
    doc.text(40, y - 27, BUYER["address"], size=8.5)
    doc.text(40, y - 39, f"GSTIN {BUYER['gstin']}", size=8.5)
    # table
    ty = y - 80
    doc.text(40, ty, "Item", "Helvetica-Bold", 8.5, colors.HexColor("#374151"))
    doc.text(360, ty, "Qty", "Helvetica-Bold", 8.5, colors.HexColor("#374151"), align="right")
    doc.text(440, ty, "Unit Price", "Helvetica-Bold", 8.5, colors.HexColor("#374151"), align="right")
    doc.text(485, ty, "GST %", "Helvetica-Bold", 8.5, colors.HexColor("#374151"), align="right")
    doc.text(W - 40, ty, "Amount", "Helvetica-Bold", 8.5, colors.HexColor("#374151"), align="right")
    doc.hline(40, W - 40, ty - 6, 1, band)
    yy = ty - 22
    for l in calc["lines"]:
        doc.text(40, yy, l["desc"], size=9)
        doc.text(360, yy, f"{l['qty']:g}", size=9, align="right")
        doc.text(440, yy, fmt_in(l["price"]), size=9, align="right")
        doc.text(485, yy, f"{l['rate']:g}", size=9, align="right")
        doc.text(W - 40, yy, fmt_in(l["amount"]), size=9, align="right")
        doc.hline(40, W - 40, yy - 7, 0.3, colors.HexColor("#e5e7eb"))
        yy -= 22
    sy = yy - 10
    sx = W - 230
    doc.text(sx, sy, "Subtotal", size=9.5)
    doc.text(W - 40, sy, fmt_in(calc["subtotal"]), size=9.5, align="right", key="subtotal")
    k = 1
    for lab, amt in calc["tax_lines"]:
        doc.text(sx, sy - 16 * k, lab.replace(" @ ", " (").replace("%", "%)"), size=9.5)
        doc.text(W - 40, sy - 16 * k, fmt_in(amt), size=9.5, align="right")
        k += 1
    c.setFillColor(colors.HexColor("#eef5f7"))
    c.rect(sx - 8, sy - 16 * k - 8, W - 40 - sx + 16, 22, fill=1, stroke=0)
    doc.text(sx, sy - 16 * k, "Total Due (INR)", "Helvetica-Bold", 11, band)
    doc.text(W - 40, sy - 16 * k, fmt_in(calc["total"]), "Helvetica-Bold", 11, band, align="right", key="total")
    by_ = sy - 16 * k - 60
    doc.text(40, by_, "PAYMENT DETAILS", "Helvetica-Bold", 7, colors.HexColor("#6b7280"))
    doc.text(40, by_ - 13, f"Account Name: {v['name']}", size=8.5)
    doc.text(40, by_ - 25, f"Bank: {v['bank_name']}", size=8.5)
    doc.text(40, by_ - 37, f"Account Number: {v['bank_account']}", size=8.5)
    doc.text(40, by_ - 49, f"IFSC: {v['ifsc']}", size=8.5)
    doc.text(40, 50, inv.get("note", "Thank you for your business. Please quote the invoice number with your payment."),
             size=8, color=colors.HexColor("#6b7280"))
    return doc


def t_logistics(inv, calc) -> Doc:
    """T3: logistics-style invoice with 'Bill No.' and 'Your PO Ref.' labels."""
    v, doc = inv["vendor"], Doc()
    top = H - 48
    doc.text(W / 2, top, v["name"].upper(), "Helvetica-Bold", 15, align="center")
    doc.text(W / 2, top - 14, v["address"], size=8.5, align="center")
    doc.text(W / 2, top - 26, f"GSTIN: {v['tax_id']}   Ph: {v['phone']}   {v['email']}", size=8.5, align="center")
    doc.hline(40, W - 40, top - 36, 1.2)
    doc.text(W / 2, top - 54, "GST INVOICE - TRANSPORT SERVICES", "Helvetica-Bold", 11, align="center")
    y = top - 80
    doc.kv(40, y, "Bill No.:", inv["invoice_no"], key="invoice_number", lw=62)
    doc.kv(40, y - 14, "Bill Date:", d(inv["date"], "slash"), key="invoice_date", lw=62)
    doc.kv(40, y - 28, "Due By:", d(inv["due"], "slash"), key="due_date", lw=62)
    doc.kv(330, y, "Your PO Ref.:", inv.get("po_ref") or "", key="po_reference", lw=78)
    doc.kv(330, y - 14, "Terms:", inv["terms"], key="payment_terms", lw=78)
    doc.kv(330, y - 28, "SAC:", "996511 (Road transport of goods)", lw=78)
    y -= 56
    doc.text(40, y, "Consignee / Billed To:", "Helvetica-Bold", 9)
    doc.text(40, y - 12, f"{BUYER['name']}, {BUYER['address']}", size=8.5)
    doc.text(40, y - 24, f"GSTIN: {BUYER['gstin']}", size=8.5)
    ty = y - 52
    doc.c.setStrokeColor(colors.black)
    heads = [(40, "S.No"), (75, "Particulars"), (330, "Trips"), (400, "Rate"), (W - 44, "Taxable Amt")]
    doc.hline(40, W - 40, ty + 12)
    for x, hd in heads:
        doc.text(x, ty, hd, "Helvetica-Bold", 8.5, align="right" if hd in ("Taxable Amt",) else "left")
    doc.hline(40, W - 40, ty - 5)
    yy = ty - 19
    for i, l in enumerate(calc["lines"], 1):
        doc.text(40, yy, f"{i}.", size=8.5)
        doc.text(75, yy, l["desc"], size=8.5)
        doc.text(350, yy, f"{l['qty']:g}", size=8.5, align="right")
        doc.text(460, yy, fmt_in(l["price"]), size=8.5, align="right")
        doc.text(W - 44, yy, fmt_in(l["amount"]), size=8.5, align="right")
        yy -= 15
    doc.hline(40, W - 40, yy + 6)
    sy = yy - 12
    doc.text(360, sy, "Taxable Value", size=9)
    doc.text(W - 44, sy, fmt_in(calc["subtotal"]), size=9, align="right", key="subtotal")
    k = 1
    for lab, amt in calc["tax_lines"]:
        doc.text(360, sy - 14 * k, lab, size=9)
        doc.text(W - 44, sy - 14 * k, fmt_in(amt), size=9, align="right")
        k += 1
    doc.text(360, sy - 14 * k - 4, "Total Payable", "Helvetica-Bold", 10)
    doc.text(W - 44, sy - 14 * k - 4, fmt_in(calc["total"]), "Helvetica-Bold", 10, align="right", key="total")
    doc.text(40, sy, "Rupees:", "Helvetica-Bold", 8.5)
    for i, ln in enumerate(wrap(inr_words(calc["total"]), 50)):
        doc.text(40, sy - 12 - i * 11, ln, size=8.5)
    bank_block(doc, 40, sy - 80, v)
    doc.text(40, 60, "GST payable under forward charge. E. & O.E.", size=7.5)
    return doc


def t_retail(inv, calc) -> Doc:
    """T4: manufacturer invoice with GST-inclusive prices and a separate tax summary."""
    v, doc = inv["vendor"], Doc()
    top = H - 46
    c = doc.c
    c.setStrokeColor(colors.HexColor("#2f6b3a"))
    c.setLineWidth(1.5)
    c.rect(30, 30, W - 60, H - 60)
    doc.text(45, top, v["name"], "Times-Bold", 18, colors.HexColor("#2f6b3a"))
    doc.text(45, top - 15, v["address"], "Times-Roman", 9)
    doc.text(45, top - 27, f"GSTIN/UIN: {v['tax_id']}   E-Mail: {v['email']}", "Times-Roman", 9)
    doc.text(W / 2, top - 55, "TAX INVOICE", "Times-Bold", 14, align="center")
    doc.text(W / 2, top - 68, "(All prices are inclusive of GST)", "Times-Italic", 9, align="center")
    y = top - 95
    doc.kv(45, y, "Inv. No.:", inv["invoice_no"], key="invoice_number", lw=70, font="Times-Roman", size=10)
    doc.kv(45, y - 14, "Dated:", d(inv["date"]), key="invoice_date", lw=70, font="Times-Roman", size=10)
    doc.kv(45, y - 28, "Due Date:", d(inv["due"]), key="due_date", lw=70, font="Times-Roman", size=10)
    doc.kv(320, y, "Buyer's Order No.:", inv.get("po_ref") or "", key="po_reference", lw=100,
           font="Times-Roman", size=10)
    doc.kv(320, y - 14, "Terms of Payment:", inv["terms"], key="payment_terms", lw=100, font="Times-Roman", size=10)
    y -= 50
    doc.text(45, y, "Buyer (Bill to):", "Times-Bold", 10)
    doc.text(45, y - 13, BUYER["name"], "Times-Roman", 10)
    doc.text(45, y - 26, BUYER["address"], "Times-Roman", 9)
    doc.text(45, y - 38, f"GSTIN/UIN: {BUYER['gstin']}   State: Haryana, Code: 06", "Times-Roman", 9)
    ty = y - 66
    doc.hline(45, W - 45, ty + 13)
    doc.text(48, ty, "Description of Goods", "Times-Bold", 9.5)
    doc.text(300, ty, "HSN", "Times-Bold", 9.5)
    doc.text(375, ty, "Qty", "Times-Bold", 9.5, align="right")
    doc.text(465, ty, "Rate (incl. GST)", "Times-Bold", 9.5, align="right")
    doc.text(W - 48, ty, "Amount", "Times-Bold", 9.5, align="right")
    doc.hline(45, W - 45, ty - 5)
    yy = ty - 19
    for l in calc["lines"]:
        doc.text(48, yy, l["desc"], "Times-Roman", 9.5)
        doc.text(300, yy, l.get("hsn", "4819"), "Times-Roman", 9.5)
        doc.text(375, yy, f"{l['qty']:g}", "Times-Roman", 9.5, align="right")
        doc.text(465, yy, fmt_in(l["price"]), "Times-Roman", 9.5, align="right")
        doc.text(W - 48, yy, fmt_in(l["amount"]), "Times-Roman", 9.5, align="right")
        yy -= 15
    doc.hline(45, W - 45, yy + 6)
    doc.text(300, yy - 8, "Total (incl. GST)", "Times-Bold", 11)
    doc.text(W - 48, yy - 8, fmt_in(calc["total"]), "Times-Bold", 11, align="right", key="total")
    doc.text(48, yy - 30, "Amount Chargeable (in words): " + inr_words(calc["total"]), "Times-Italic", 9)
    sy = yy - 62
    doc.text(48, sy, "Tax Summary", "Times-Bold", 10)
    doc.text(48, sy - 15, "Taxable Value", "Times-Roman", 9.5)
    doc.text(250, sy - 15, fmt_in(calc["subtotal"]), "Times-Roman", 9.5, align="right", key="subtotal")
    k = 2
    for lab, amt in calc["tax_lines"]:
        doc.text(48, sy - 15 * k, lab, "Times-Roman", 9.5)
        doc.text(250, sy - 15 * k, fmt_in(amt), "Times-Roman", 9.5, align="right")
        k += 1
    doc.text(48, sy - 15 * k, "Total Tax", "Times-Bold", 9.5)
    doc.text(250, sy - 15 * k, fmt_in(calc["tax_total"]), "Times-Bold", 9.5, align="right")
    bank_block(doc, 330, sy, v, 9)
    doc.text(W - 48, 70, f"for {v['name']}", "Times-Bold", 9.5, align="right")
    doc.text(W - 48, 45, "Authorised Signatory", "Times-Roman", 9, align="right")
    return doc


def t_us(inv, calc) -> Doc:
    """T5: US SaaS vendor invoice in USD (no GST; reverse charge applies in India)."""
    v, doc = inv["vendor"], Doc()
    top = H - 60
    doc.text(40, top, v["name"], "Helvetica-Bold", 18, colors.HexColor("#14532d"))
    doc.text(40, top - 16, v["address"], size=8.5, color=colors.HexColor("#4b5563"))
    doc.text(40, top - 28, f"Tax ID (EIN): {v['tax_id']}   ·   {v['email']}", size=8.5,
             color=colors.HexColor("#4b5563"))
    doc.text(W - 40, top, "Invoice", "Helvetica", 28, colors.HexColor("#111827"), align="right")
    y = top - 70
    doc.kv(W - 230, y, "Invoice #", inv["invoice_no"], key="invoice_number", lw=80)
    doc.kv(W - 230, y - 14, "Invoice Date", d(inv["date"], "long"), key="invoice_date", lw=80)
    doc.kv(W - 230, y - 28, "Due Date", d(inv["due"], "long"), key="due_date", lw=80)
    doc.kv(W - 230, y - 42, "PO Number", inv.get("po_ref") or "", key="po_reference", lw=80)
    doc.kv(W - 230, y - 56, "Terms", inv["terms"], key="payment_terms", lw=80)
    doc.text(40, y, "Bill To", "Helvetica-Bold", 9)
    doc.text(40, y - 13, BUYER["name"], size=9)
    for i, ln in enumerate(wrap(BUYER["address"], 55)):
        doc.text(40, y - 25 - i * 11, ln, size=8.5)
    ty = y - 100
    doc.c.setFillColor(colors.HexColor("#14532d"))
    doc.c.rect(40, ty - 5, W - 80, 18, fill=1, stroke=0)
    doc.text(46, ty, "Description", "Helvetica-Bold", 9, colors.white)
    doc.text(380, ty, "Qty", "Helvetica-Bold", 9, colors.white, align="right")
    doc.text(470, ty, "Unit Price", "Helvetica-Bold", 9, colors.white, align="right")
    doc.text(W - 46, ty, "Amount", "Helvetica-Bold", 9, colors.white, align="right")
    yy = ty - 22
    for l in calc["lines"]:
        doc.text(46, yy, l["desc"], size=9)
        doc.text(380, yy, f"{l['qty']:g}", size=9, align="right")
        doc.text(470, yy, fmt_us(l["price"]), size=9, align="right")
        doc.text(W - 46, yy, fmt_us(l["amount"]), size=9, align="right")
        yy -= 18
    doc.hline(40, W - 40, yy + 8, 0.5, colors.HexColor("#d1d5db"))
    sy = yy - 10
    doc.text(370, sy, "Subtotal", size=9.5)
    doc.text(W - 46, sy, "$" + fmt_us(calc["subtotal"]), size=9.5, align="right", key="subtotal")
    doc.text(370, sy - 16, "Sales Tax (0%)", size=9.5)
    doc.text(W - 46, sy - 16, "$0.00", size=9.5, align="right")
    doc.text(370, sy - 36, "Total Due (USD)", "Helvetica-Bold", 11)
    doc.text(W - 46, sy - 36, "$" + fmt_us(calc["total"]), "Helvetica-Bold", 11, align="right", key="total")
    doc.text(40, sy - 80, "Wire Instructions", "Helvetica-Bold", 9)
    doc.text(40, sy - 93, f"Bank: {v['bank_name']}   Account No: {v['bank_account']}   SWIFT: SVCBUS6S", size=8.5)
    doc.text(40, sy - 115, "Export of services. Reverse charge: IGST, if applicable, is payable by the recipient.",
             size=8, color=colors.HexColor("#6b7280"))
    return doc


TEMPLATES = {"classic": t_classic, "modern": t_modern, "logistics": t_logistics, "retail": t_retail, "us": t_us}


# ----------------------------------------------------------------------------- scanning
def add_smudges(doc: Doc, keys: list[str]):
    """Ink smear / coffee stain over chosen values so OCR cannot read them reliably."""
    c = doc.c
    rnd = random.Random(7)
    for k in keys:
        if k not in doc.pos:
            continue
        x, y, w = doc.pos[k]
        c.saveState()
        c.setFillColor(colors.Color(0.35, 0.28, 0.2, alpha=0.62))
        for _ in range(26):
            cx = x + rnd.uniform(0.15, 0.95) * w
            cy = y + rnd.uniform(-1, 7)
            c.ellipse(cx - rnd.uniform(3, 9), cy - rnd.uniform(2, 5), cx + rnd.uniform(3, 9), cy + rnd.uniform(2, 6),
                      fill=1, stroke=0)
        c.restoreState()


def rasterise(pdf_bytes: bytes, dpi=150, angle=0.7, noise=0.05, blur=0.6, seed=1) -> bytes:
    rnd = random.Random(seed)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(pdf_bytes)
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", str(src), str(Path(td) / "pg")], check=True)
        pages = sorted(Path(td).glob("pg*.png"))
        imgs = []
        for p in pages:
            im = Image.open(p).convert("L")
            im = im.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
            im = im.filter(ImageFilter.GaussianBlur(blur))
            px = im.load()
            wid, hei = im.size
            for _ in range(int(wid * hei * noise)):
                xx, yy = rnd.randrange(wid), rnd.randrange(hei)
                px[xx, yy] = rnd.choice((0, 60, 200, 255)) if rnd.random() < 0.5 else px[xx, yy]
            # paper tint
            im = Image.eval(im, lambda v: int(v * 0.93 + 10))
            imgs.append(im.convert("RGB"))
        out = io.BytesIO()
        imgs[0].save(out, "PDF", resolution=dpi, save_all=True, append_images=imgs[1:])
        return out.getvalue()


def build(inv: dict) -> bytes:
    calc = compute(inv)
    doc = TEMPLATES[inv["template"]](inv, calc)
    if inv.get("stamp"):
        c = doc.c
        c.saveState()
        c.setFillColor(colors.Color(0.75, 0.1, 0.1, alpha=0.75))
        c.setFont("Helvetica-Bold", 13)
        c.translate(W - 175, 95)
        c.rotate(12)
        c.drawCentredString(0, 0, inv["stamp"])
        c.restoreState()
    if inv.get("smudge"):
        add_smudges(doc, inv["smudge"])
    pdf = doc.finish()
    if inv.get("scan"):
        pdf = rasterise(pdf, **inv["scan"])
    return pdf


# ----------------------------------------------------------------------------- invoice specs
def ln(desc, qty, price, rate, hsn=None):
    out = dict(desc=desc, qty=qty, price=price, rate=rate)
    if hsn:
        out["hsn"] = hsn
    return out


LOOKALIKE = dict(VMAP["V001"], name="Apex Office Supply Co.",
                 address="22 Naraina Vihar Market, New Delhi, Delhi 110028",
                 tax_id=make_gstin("07", "AAKFA9917B"), email="billing.apexoffice@gmail.com",
                 phone="+91 98110 44210", bank_name="IndusInd Bank", bank_account="259876543210", ifsc="INDB0000598")

APEX_1001_LINES = [ln("A4 copier paper 75 GSM (ream of 500)", 50, 285.00, 18, "4802"),
                   ln("Ballpoint pens, blue (box of 50)", 40, 120.00, 18, "9608"),
                   ln("Heavy-duty stapler", 10, 650.00, 18, "8472"),
                   ln("Whiteboard markers (set of 4)", 25, 340.00, 18, "9608")]

DEMO = [
    dict(id="01", file="Apex_Office_Supplies_INV_APX-0412.pdf", category="Happy path", order=1,
         title="Clean digital invoice - exact PO match",
         description="Apex Office Supplies bills PO-1001 in full. Machine-readable PDF, 4 itemised lines, IGST 18%.",
         expected="APPROVED",
         vendor="V001", template="classic", invoice_no="APX/26-27/0412", date="2026-11-02", due="2026-12-02",
         po_ref="PO-1001", terms="Net 30", lines=APEX_1001_LINES),
    dict(id="02", file="Brightline_IT_Invoice_BIT-2026-11873.pdf", category="Happy path", order=2,
         title="Different layout - IT hardware, IGST",
         description="Brightline IT bills the laptop refresh (PO-1002). Modern layout with labels above values.",
         expected="APPROVED",
         vendor="V002", template="modern", invoice_no="BIT-2026-11873", date="2026-11-05", due="2026-12-20",
         po_ref="PO-1002", terms="Net 45",
         lines=[ln("Business laptop 14-inch, i5 / 16GB / 512GB SSD", 5, 68500.00, 18),
                ln("24-inch IPS monitor", 5, 12800.00, 18),
                ln("USB-C docking station", 5, 7900.00, 18),
                ln("On-site setup and imaging", 5, 1500.00, 18)]),
    dict(id="03", file="Coastal_Logistics_Bill_CL-2026-1187.pdf", category="Happy path", order=3,
         title="Small variance within tolerance",
         description="Freight rate billed at 48,700 vs 48,000 on PO-1003 (diesel revision). Total is 1.36% over PO - "
                     "inside the 2% / Rs 5,000 tolerance, so approved but the variance stays visible.",
         expected="APPROVED",
         vendor="V003", template="logistics", invoice_no="CL/2026/1187", date="2026-11-08", due="2026-11-23",
         po_ref="PO-1003", terms="Net 15",
         lines=[ln("FTL freight Bhiwandi to Gurugram, 32ft MXL", 2, 48700.00, 12),
                ln("Loading and unloading charges", 2, 3500.00, 12)]),
    dict(id="04", file="Evergreen_Packaging_EPI-1124-26.pdf", category="Happy path", order=4,
         title="Tax-inclusive prices + bundled line",
         description="Evergreen quotes GST-inclusive prices and bundles bubble wrap + tape into one line. The engine "
                     "back-calculates tax and reconciles the bundle against two PO lines.",
         expected="APPROVED",
         vendor="V005", template="retail", tax_mode="inclusive", invoice_no="EPI/1124/26", date="2026-11-10",
         due="2026-12-10", po_ref="PO-1005", terms="Net 30",
         lines=[ln("Corrugated boxes 3-ply 12x10x8 in", 2000, 20.72, 12, "4819"),
                ln("Protective packing kit (bubble wrap 40 rolls + tape 200 rolls)", 1, 60928.00, 12, "3923")]),
    dict(id="05", file="Deccan_Facility_Oct2026_Invoice.pdf", category="Happy path", order=5,
         title="Partial invoice against a 3-month PO",
         description="Deccan bills month 1 of 3 on PO-1004 (intra-state, CGST + SGST). Approved and the PO's "
                     "remaining balance drops by one month.",
         expected="APPROVED", accent="#3b2f63",
         vendor="V004", template="modern", invoice_no="DFS/OCT/2026/118", date="2026-11-01", due="2026-12-01",
         po_ref="PO-1004", terms="Net 30",
         lines=[ln("Housekeeping services - monthly (6 staff) - October 2026", 1, 84000.00, 18)]),
    dict(id="06", file="Falcon_Print_Media_FPM-26-0981.pdf", category="Edge case", order=6,
         title="EDGE 2 - Invoice exceeds PO tolerance (Rs 1,08,000 vs Rs 1,00,000)",
         description="Falcon adds an unapproved 'design revisions' line. Invoice is Rs 8,000 (8%) above PO-1006, "
                     "beyond the 2% / Rs 5,000 tolerance - routed to review with the exact variance.",
         expected="NEEDS_REVIEW",
         vendor="V006", template="classic", invoice_no="FPM-26-0981", date="2026-11-06", due="2026-12-06",
         po_ref="PO-1006", terms="Net 30",
         lines=[ln("Product catalogue A4, 32 pages, 4-colour", 1000, 62.00, 18, "4911"),
                ln("Tri-fold brochure A4, 4-colour", 2000, 9.50, 18, "4911"),
                ln("Design and pre-press", 1, 3745.76, 18, "998391"),
                ln("Additional design revisions (3 rounds)", 1, 6779.66, 18, "998391")]),
    dict(id="07", file="scan_resend_0412_reminder.pdf", category="Edge case", order=7,
         title="EDGE 1 - Duplicate invoice (re-sent with a new filename)",
         description="Apex re-sends invoice APX/26-27/0412 as a payment reminder. Different file, same invoice. "
                     "Run after sample 01.",
         expected="REJECTED", run_after="01",
         vendor="V001", template="classic", invoice_no="APX/26-27/0412", date="2026-11-02", due="2026-12-02",
         po_ref="PO-1001", terms="Net 30", stamp="PAYMENT REMINDER - COPY", lines=APEX_1001_LINES),
    dict(id="08", file="Deccan_Nov2026_scanned.pdf", category="Edge case", order=8,
         title="EDGE 3 - Scanned invoice, smudged invoice no. and PO ref",
         description="A scanned paper invoice (no text layer). An ink smear covers the invoice number and PO number. "
                     "OCR must not guess - uncertain fields go to a human, with PO candidates suggested.",
         expected="NEEDS_REVIEW",
         vendor="V004", template="classic", invoice_no="DFS/NOV/2026/131", date="2026-11-14", due="2026-12-14",
         po_ref="PO-1004", terms="Net 30", smudge=["invoice_number", "po_reference"],
         scan=dict(dpi=200, angle=0.8, noise=0.008, blur=0.45, seed=3),
         lines=[ln("Housekeeping services - monthly (6 staff) - November 2026", 1, 84000.00, 18)]),
    dict(id="09", file="Coastal_Logistics_Bill_CL-2026-1203.pdf", category="Edge case", order=9,
         title="EDGE 4 - Valid PO, wrong vendor",
         description="Coastal Logistics quotes PO-1007 - a real, open PO, but issued to Brightline IT. "
                     "The PO match is refused, both identities shown, and Coastal's own open PO suggested.",
         expected="NEEDS_REVIEW",
         vendor="V003", template="logistics", invoice_no="CL/2026/1203", date="2026-11-12", due="2026-11-27",
         po_ref="PO-1007", terms="Net 15",
         lines=[ln("Express LTL freight Gurugram to Chennai", 3, 18500.00, 12)]),
    dict(id="10", file="Horizon_Security_HSS-2026-077.pdf", category="Additional", order=10,
         title="Blocked vendor",
         description="Horizon Security is on compliance hold (GST returns not filed). Invoice is otherwise clean.",
         expected="REJECTED",
         vendor="V008", template="classic", invoice_no="HSS/2026/077", date="2026-11-04", due="2026-12-04",
         po_ref="PO-1008", terms="Net 30",
         lines=[ln("CCTV supply and installation (32 cameras, NVR)", 1, 220000.00, 18, "8525")]),
    dict(id="11", file="Apex_Office_Supply_Co_INV-7781.pdf", category="Additional", order=11,
         title="Look-alike vendor (possible impersonation)",
         description="'Apex Office Supply Co.' - name 90%+ similar to a registered vendor, but a different GSTIN "
                     "and bank account. Must not be treated as Apex.",
         expected="NEEDS_REVIEW", vendor_override=LOOKALIKE,
         vendor="V001", template="classic", invoice_no="AOS-7781", date="2026-11-11", due="2026-11-18",
         po_ref="PO-1009", terms="Net 7",
         lines=[ln("Laser toner cartridge, black (high yield)", 20, 4200.00, 18, "8443"),
                ln("A3 copier paper 80 GSM (ream of 500)", 30, 610.00, 18, "4802")]),
    dict(id="12", file="Jaipur_Textiles_JTU-2026-2231.pdf", category="Additional", order=12,
         title="Invoice against a closed PO",
         description="Jaipur Textiles bills PO-1013, which was fully received and closed in August.",
         expected="REJECTED",
         vendor="V010", template="classic", invoice_no="JTU/2026/2231", date="2026-11-03", due="2026-12-03",
         po_ref="PO-1013", terms="Net 30",
         lines=[ln("Polo T-shirt with embroidered logo", 100, 385.00, 5, "6105"),
                ln("Cotton trousers, navy", 100, 520.00, 5, "6203")]),
    dict(id="13", file="GreenfieldCloud_Invoice_GC-INV-20931.pdf", category="Additional", order=13,
         title="Overseas vendor in USD (no GST)",
         description="US SaaS vendor, USD currency, EIN instead of GSTIN, reverse-charge note. Exact match to PO-1011.",
         expected="APPROVED",
         vendor="V007", template="us", tax_mode="none", invoice_no="GC-INV-20931", date="2026-11-01",
         due="2026-12-01", po_ref="PO-1011", terms="Net 30",
         lines=[ln("Cloud analytics platform - annual subscription (per seat)", 50, 240.00, 0),
                ln("Premium support - annual", 1, 1800.00, 0)]),
    dict(id="14", file="Coastal_Logistics_scan_CL-2026-1190.pdf", category="Additional", order=14,
         title="Scanned invoice, readable - OCR happy path",
         description="A clean scan with no text layer. OCR reads every field with high confidence; 1 of 3 "
                     "shipments on PO-1010.",
         expected="APPROVED",
         vendor="V003", template="logistics", invoice_no="CL/2026/1190", date="2026-11-09", due="2026-11-24",
         po_ref="PO-1010", terms="Net 15", scan=dict(dpi=200, angle=0.4, noise=0.01, blur=0.3, seed=5),
         lines=[ln("Express LTL freight Gurugram to Chennai", 1, 18500.00, 12)]),
]

HISTORY = [
    dict(id="H1", file="hist_Apex_APX-26-27-0377.pdf", vendor="V001", template="classic",
         invoice_no="APX/26-27/0377", date="2026-09-18", due="2026-10-18", po_ref="PO-1015", terms="Net 30",
         lines=[ln("Tissue paper boxes (carton of 48)", 20, 960.00, 18, "4818"),
                ln("Hand sanitiser 500ml", 60, 145.00, 18, "3808")]),
    dict(id="H2", file="hist_Jaipur_JTU-2026-2104.pdf", vendor="V010", template="classic",
         invoice_no="JTU/2026/2104", date="2026-10-15", due="2026-11-14", po_ref="PO-1014", terms="Net 30",
         lines=[ln("Fleece jacket with embroidered logo", 150, 1150.00, 12, "6201"),
                ln("Woollen beanie", 150, 180.00, 5, "6505")]),
    dict(id="H3", file="hist_Brightline_BIT-2026-11502.pdf", vendor="V002", template="modern",
         invoice_no="BIT-2026-11502", date="2026-10-02", due="2026-11-16", po_ref="PO-1016", terms="Net 45",
         lines=[ln("Wireless keyboard and mouse combo", 40, 1650.00, 18)]),
    dict(id="H4", file="hist_Coastal_CL-2026-1098.pdf", vendor="V003", template="logistics",
         invoice_no="CL/2026/1098", date="2026-10-05", due="2026-10-20", po_ref="PO-1017", terms="Net 15",
         lines=[ln("FTL freight Chennai to Gurugram, 32ft MXL", 1, 61000.00, 12)]),
    dict(id="H5", file="hist_Apex_APX-0377_resent.pdf", vendor="V001", template="classic",
         invoice_no="APX/26-27/0377", date="2026-09-18", due="2026-10-18", po_ref="PO-1015", terms="Net 30",
         stamp="DUPLICATE COPY",
         lines=[ln("Tissue paper boxes (carton of 48)", 20, 960.00, 18, "4818"),
                ln("Hand sanitiser 500ml", 60, 145.00, 18, "3808")]),
    dict(id="H6", file="hist_Indus_Catering_ICC-1107.pdf", vendor="V009", template="modern", accent="#7c2d12",
         invoice_no="ICC/1107", date="2026-11-03", due="2026-11-18", po_ref="PO-1012", terms="Net 15",
         lines=[ln("Cafeteria services - November 2026", 1, 96000.00, 5)]),
]


def main():
    out_demo, out_hist = ROOT / "invoices", ROOT / "history"
    out_demo.mkdir(exist_ok=True)
    out_hist.mkdir(exist_ok=True)
    manifest, hist_manifest = [], []
    for inv in DEMO + HISTORY:
        inv = dict(inv)
        inv["vendor"] = inv.pop("vendor_override", None) or VMAP[inv["vendor"]]
        pdf = build(inv)
        target = (out_hist if inv["id"].startswith("H") else out_demo) / inv["file"]
        target.write_bytes(pdf)
        calc = compute(inv)
        meta = dict(id=inv["id"], file=inv["file"], scanned=bool(inv.get("scan")),
                    expected_total=calc["total"])
        if inv["id"].startswith("H"):
            hist_manifest.append(meta)
        else:
            meta.update({k: inv.get(k) for k in ("category", "order", "title", "description", "expected", "run_after")})
            manifest.append(meta)
        print(f"{inv['id']:>3}  {target.name:<45} total={calc['total']:>12,.2f}  {'(scanned)' if inv.get('scan') else ''}")
    (out_demo / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out_hist / "manifest.json").write_text(json.dumps(hist_manifest, indent=2))


if __name__ == "__main__":
    main()
