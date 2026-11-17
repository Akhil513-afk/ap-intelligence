"""Stage 9 - professional processing report (PDF) generated from persisted run data."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..config import REPORT_DIR
from ..models import ProcessingRun

_FONT, _BOLD = "Helvetica", "Helvetica-Bold"
for reg, bold in (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),):
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", reg))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
        _FONT, _BOLD = "DejaVu", "DejaVu-Bold"
    except Exception:
        pass

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#e5e7eb")
STATUS_COLORS = {"APPROVED": "#047857", "NEEDS_REVIEW": "#b45309", "REJECTED": "#b91c1c", "INFO_REQUESTED": "#1d4ed8"}
OUTCOME_COLORS = {"PASS": "#047857", "WARN": "#b45309", "FAIL": "#b91c1c", "INFO": "#1d4ed8", "UNABLE": "#6b7280"}

ss = {
    "h1": ParagraphStyle("h1", fontName=_BOLD, fontSize=15, leading=19, textColor=INK),
    "h2": ParagraphStyle("h2", fontName=_BOLD, fontSize=10.5, leading=14, textColor=INK, spaceBefore=10, spaceAfter=4),
    "p": ParagraphStyle("p", fontName=_FONT, fontSize=8.5, leading=11.5, textColor=INK),
    "small": ParagraphStyle("s", fontName=_FONT, fontSize=7.5, leading=10, textColor=MUTED),
    "cell": ParagraphStyle("c", fontName=_FONT, fontSize=7.5, leading=9.5, textColor=INK),
    "cellb": ParagraphStyle("cb", fontName=_BOLD, fontSize=7.5, leading=9.5, textColor=INK),
}


def P(t, st="cell"):
    return Paragraph(escape(str(t if t is not None else "—")), ss[st])


def C(t, color, bold=True):
    return Paragraph(f'<font color="{color}">{"<b>" if bold else ""}{escape(str(t))}{"</b>" if bold else ""}</font>',
                     ss["cell"])


def table(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6"))]
    t.setStyle(TableStyle(style))
    return t


def money(v, cur):
    if v is None:
        return "—"
    sym = {"INR": "₹", "USD": "$"}.get(cur or "", "")
    return f"{sym}{v:,.2f}"


def build_report(db, run: ProcessingRun) -> Path:
    path = REPORT_DIR / f"{run.id}_report.pdf"
    dec = run.decisions[-1] if run.decisions else None
    ctx = (dec.context if dec else {}) or {}
    cur = run.currency or "INR"
    W = A4[0] - 30 * mm
    el = []
    status = run.final_status or (dec.status if dec else run.status)
    el.append(Paragraph("AP Intelligence · Invoice Processing Report", ss["h1"]))
    el.append(Paragraph(f"Run {run.id} · generated {datetime.now():%d %b %Y %H:%M}", ss["small"]))
    el.append(Spacer(1, 6))
    head = [[P("Run ID", "cellb"), P(run.id), P("Final status", "cellb"),
             C(status.replace("_", " ") if status else "—", STATUS_COLORS.get(status, "#111827"))],
            [P("File", "cellb"), P(run.filename), P("Engine decision", "cellb"),
             P(f"{dec.status} (v{dec.version})" if dec else "—")],
            [P("Received", "cellb"), P(f"{run.created_at:%d %b %Y %H:%M:%S}"), P("Decided by", "cellb"),
             P(run.final_status_by)],
            [P("Extraction", "cellb"), P(f"{run.extraction_method} · {run.page_count} page(s)"),
             P("Processing time", "cellb"), P(f"{run.processing_ms or 0:,} ms compute")]]
    el.append(table(head, [W * .15, W * .38, W * .17, W * .30], header=False))

    if dec:
        el.append(Paragraph("Decision", ss["h2"]))
        el.append(Paragraph(f"<b>{escape(dec.headline)}</b>", ss["p"]))
        el.append(Spacer(1, 3))
        for e in dec.explanation:
            el.append(Paragraph("• " + escape(e), ss["p"]))
        el.append(Spacer(1, 3))
        el.append(Paragraph(f"<b>Recommended next action:</b> {escape(dec.recommended_action)}", ss["p"]))
        if dec.reason_codes:
            rows = [[P("Reason code", "cellb"), P("Effect", "cellb"), P("Detail", "cellb")]]
            for r in dec.reason_codes:
                rows.append([P(r["code"]), C(r["effect"], {"REJECT": "#b91c1c", "REVIEW": "#b45309"}.get(r["effect"], "#6b7280")),
                             P(r["message"])])
            el.append(Spacer(1, 4))
            el.append(table(rows, [W * .25, W * .1, W * .65]))
        s = dec.checks_summary
        el.append(Paragraph(f"Checks: {s['passed']} passed · {s['warnings']} warnings · {s['failed']} failed · "
                            f"{s['unable']} unable to validate · {s['info']} informational", ss["small"]))

    f = {x.key: x for x in run.fields}

    def fv(k):
        x = f.get(k)
        if not x:
            return "—"
        return x.corrected_value if x.corrected_value is not None else (x.value or "—")
    v = (ctx.get("vendor") or {}).get("vendor") or {}
    el.append(Paragraph("Invoice and vendor", ss["h2"]))
    rows = [[P("Invoice number", "cellb"), P(fv("invoice_number")), P("Vendor (master)", "cellb"),
             P(f"{v.get('id', '')} · {v.get('name')}" if v else "Not verified")],
            [P("Invoice date", "cellb"), P(fv("invoice_date")), P("Vendor on invoice", "cellb"), P(fv("vendor_name"))],
            [P("Due date", "cellb"), P(fv("due_date")), P("Tax ID", "cellb"), P(fv("vendor_tax_id"))],
            [P("PO reference", "cellb"), P(fv("po_reference")), P("Vendor status", "cellb"),
             P(f"{v.get('status')} / {v.get('approval_status')}" if v else "—")],
            [P("Subtotal / tax / total", "cellb"), P(f"{fv('subtotal')} / {fv('tax_amount')} / {fv('total_amount')} {cur}"),
             P("Payment terms", "cellb"), P(fv("payment_terms"))]]
    el.append(table(rows, [W * .18, W * .32, W * .17, W * .33], header=False))

    po = ctx.get("po") or {}
    if po.get("po"):
        p = po["po"]
        a = po.get("amount") or {}
        el.append(Paragraph(f"Purchase order {p['po_number']} ({'accepted' if po.get('accepted') else 'NOT accepted'})",
                            ss["h2"]))
        rows = [[P("PO vendor", "cellb"), P(f"{p['vendor_id']} · {p['vendor_name']}"), P("PO status", "cellb"), P(p["status"])],
                [P("PO total", "cellb"), P(money(p["total_amount"], p["currency"])), P("Already invoiced", "cellb"),
                 P(money(p["consumed"], p["currency"]))],
                [P("This invoice", "cellb"), P(money(a.get("invoice_total"), p["currency"])), P("Classification", "cellb"),
                 P((a.get("classification") or "—").replace("_", " "))],
                [P("Difference", "cellb"), P(f"{money(a.get('difference'), p['currency'])} ({a.get('difference_pct', 0):+.2f}%)"
                                            if a.get("difference") is not None else "—"),
                 P("Rule", "cellb"), P(a.get("rule"))]]
        el.append(table(rows, [W * .16, W * .30, W * .16, W * .38], header=False))
    if po.get("candidates"):
        el.append(Paragraph("Candidate POs suggested for the reviewer (not auto-selected)", ss["h2"]))
        rows = [[P("PO", "cellb"), P("Vendor", "cellb"), P("Remaining", "cellb"), P("Score", "cellb"), P("Why", "cellb")]]
        for c in po["candidates"]:
            rows.append([P(c["po_number"]), P(c["vendor_name"]), P(money(c["remaining"], c["currency"])),
                         P(f"{c['score']:.0%}"), P("; ".join(c["reasons"]))])
        el.append(table(rows, [W * .12, W * .25, W * .15, W * .08, W * .40]))

    el.append(Paragraph("Extraction results", ss["h2"]))
    rows = [[P("Field", "cellb"), P("Extracted (original)", "cellb"), P("Corrected", "cellb"), P("Conf.", "cellb"),
             P("Status", "cellb")]]
    from ..utils import mask_account
    for x in run.fields:
        shown = mask_account(x.value) if x.key == "bank_account" else x.value
        rows.append([P(x.label), P(shown), P(x.corrected_value if x.corrected_value is not None else ""),
                     P(f"{x.confidence:.0%}"),
                     C(x.validation_status, {"VALID": "#047857", "UNCERTAIN": "#b45309"}.get(x.validation_status, "#b91c1c"))])
    el.append(table(rows, [W * .22, W * .32, W * .22, W * .08, W * .16]))

    if run.line_items:
        el.append(Paragraph("Line items", ss["h2"]))
        rows = [[P("#", "cellb"), P("Description", "cellb"), P("Qty", "cellb"), P("Unit price", "cellb"),
                 P("Amount", "cellb"), P("PO match", "cellb")]]
        for li in run.line_items:
            m = li.match or {}
            rows.append([P(li.line_no), P(li.description), P(f"{li.quantity:g}" if li.quantity is not None else "—"),
                         P(f"{li.unit_price:,.2f}" if li.unit_price is not None else "—"),
                         P(f"{li.amount:,.2f}" if li.amount is not None else "—"),
                         P(f"{m.get('status', '—')}" + (f" → PO line {m['po_line_no']}" if m.get("po_line_no") else "")
                           + (f" → PO lines {m['bundled_po_lines']}" if m.get("bundled_po_lines") else ""))])
        el.append(table(rows, [W * .05, W * .42, W * .08, W * .13, W * .13, W * .19]))

    if dec:
        checks = [c for c in run.checks if c.decision_version == dec.version]
        el.append(Paragraph("Validation checks", ss["h2"]))
        rows = [[P("Stage", "cellb"), P("Check", "cellb"), P("Result", "cellb"), P("Detail", "cellb")]]
        for c in checks:
            rows.append([P(c.stage), P(c.name), C(c.outcome, OUTCOME_COLORS.get(c.outcome, "#111827")),
                         P(c.message + (f"  [expected {c.expected}; actual {c.actual}]" if c.expected and c.actual else ""))])
        el.append(table(rows, [W * .1, W * .22, W * .08, W * .60]))

    el.append(Paragraph("Processing stages", ss["h2"]))
    rows = [[P("#", "cellb"), P("Stage", "cellb"), P("Status", "cellb"), P("Time", "cellb"), P("Summary", "cellb")]]
    for s in run.stages:
        rows.append([P(s.seq), P(s.name), P(s.status), P(f"{s.completed_at:%H:%M:%S}" if s.completed_at else "—"),
                     P(s.summary)])
    el.append(table(rows, [W * .04, W * .24, W * .1, W * .09, W * .53]))

    if run.reviews:
        el.append(Paragraph("Human review history", ss["h2"]))
        rows = [[P("When", "cellb"), P("Reviewer", "cellb"), P("Action", "cellb"), P("Comment", "cellb")]]
        for r in run.reviews:
            rows.append([P(f"{r.created_at:%d %b %H:%M}"), P(r.reviewer), P(r.action), P(r.comment)])
        el.append(table(rows, [W * .14, W * .22, W * .16, W * .48]))
    if len(run.decisions) > 1:
        el.append(Paragraph("Decision history", ss["h2"]))
        rows = [[P("Version", "cellb"), P("Status", "cellb"), P("Trigger", "cellb"), P("By", "cellb"), P("When", "cellb")]]
        for d in run.decisions:
            rows.append([P(d.version), P(d.status), P(d.trigger), P(d.created_by), P(f"{d.created_at:%d %b %H:%M:%S}")])
        el.append(table(rows, [W * .1, W * .2, W * .2, W * .3, W * .2]))
    el.append(Spacer(1, 10))
    el.append(KeepTogether([Paragraph(
        "Controls: the decision is produced by deterministic, configurable business rules; AI is used only to read the "
        "document. No payment has been initiated and no vendor master data (including bank details) was changed by "
        "this process.", ss["small"])]))

    def footer(c, d):
        c.saveState()
        c.setFont(_FONT, 7)
        c.setFillColor(MUTED)
        c.drawString(15 * mm, 10 * mm, f"AP Intelligence · {run.id}")
        c.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {d.page}")
        c.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=16 * mm, title=f"Invoice processing report {run.id}").build(
        el, onFirstPage=footer, onLaterPages=footer)
    return path
