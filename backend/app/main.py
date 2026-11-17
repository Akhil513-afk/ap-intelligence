"""AP Intelligence API (FastAPI). Serves the built React app from / and the API under /api."""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import demo
from .config import FRONTEND_DIST, MAX_UPLOAD_MB, REVIEWERS, SAMPLE_DIR
from .db import get_db, session_scope
from .models import (AuditEvent, CheckResult, DecisionRecord, ExtractedField, InvoiceLineItem, POConsumption,
                     ProcessingRun, PurchaseOrder, ReviewAction, RunStage, Vendor)
from .pipeline import extract_llm
from .pipeline.checks import format_ok
from .pipeline.document import render_page_png
from .pipeline.report import build_report
from .pipeline.runner import STAGES, audit, consume_po, create_run, pos_list, reevaluate, submit
from .rules import REASON_CODES, get_rules, save_rules
from .utils import mask_account, norm_po, parse_amount, parse_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="AP Intelligence", version="1.0.0",
              description="AI invoice processing & decision engine (Zamp ASA case study, PS-1)")
RUN_ID_RE = re.compile(r"^RUN-\d{8}-\d{4}$")


@app.on_event("startup")
def _startup():
    demo.ensure_seeded()
    # Runs interrupted by a restart are marked failed (recoverable via retry) instead of hanging forever.
    with session_scope() as db:
        for r in db.scalars(select(ProcessingRun).where(ProcessingRun.status.in_(["QUEUED", "PROCESSING"]))):
            r.status, r.error = "FAILED", "Interrupted by a server restart - use Retry to process again"
            for st in r.stages:
                if st.status in ("PENDING", "RUNNING"):
                    st.status = "SKIPPED"


# ----------------------------------------------------------------------------- serialisers
def run_summary(r: ProcessingRun) -> dict:
    return dict(id=r.id, filename=r.filename, source=r.source, sample_id=r.sample_id, status=r.status,
                created_at=r.created_at.isoformat(timespec="seconds"),
                completed_at=r.completed_at.isoformat(timespec="seconds") if r.completed_at else None,
                processing_ms=r.processing_ms, is_scanned=r.is_scanned, extraction_method=r.extraction_method,
                vendor_id=r.vendor_id, vendor_name=r.vendor_name, invoice_number=r.invoice_number,
                invoice_date=r.invoice_date, po_number=r.po_number, currency=r.currency, total_amount=r.total_amount,
                system_decision=r.system_decision, final_status=r.final_status, final_status_by=r.final_status_by,
                is_duplicate=r.is_duplicate, duplicate_of=r.duplicate_of, error=r.error)


def stage_dict(s: RunStage) -> dict:
    return dict(seq=s.seq, key=s.key, name=s.name, status=s.status, summary=s.summary, details=s.details,
                started_at=s.started_at.isoformat(timespec="milliseconds") if s.started_at else None,
                completed_at=s.completed_at.isoformat(timespec="milliseconds") if s.completed_at else None,
                duration_ms=s.duration_ms)


def field_dict(f: ExtractedField, rules: dict) -> dict:
    from .pipeline.checks import assess
    corrected = f.corrected_value is not None
    eff = f.corrected_value if corrected else f.value
    conf = 1.0 if corrected else f.confidence
    status = assess(f.key, eff, conf, corrected, rules["extraction"]["confidence_threshold"])
    shown_orig = mask_account(f.value) if f.key == "bank_account" else f.value
    shown_eff = mask_account(eff) if f.key == "bank_account" else eff
    return dict(key=f.key, label=f.label, group=f.group, critical=f.critical, value=shown_eff, original=shown_orig,
                raw_text=None if f.key == "bank_account" else f.raw_text, confidence=f.confidence,
                effective_confidence=conf, method=f.method, source=f.source, status=status,
                original_status=f.validation_status, needs_review=f.critical and status != "VALID", notes=f.notes or [],
                corrected=corrected, corrected_by=f.corrected_by,
                corrected_at=f.corrected_at.isoformat(timespec="seconds") if f.corrected_at else None,
                correction_reason=f.correction_reason)


def check_dict(c: CheckResult) -> dict:
    return {k: getattr(c, k) for k in ("id", "stage", "code", "name", "outcome", "message", "expected", "actual",
                                       "difference", "rule", "reason_code", "evidence", "decision_version")}


def decision_dict(d: DecisionRecord) -> dict:
    return dict(version=d.version, status=d.status, headline=d.headline, reason_codes=d.reason_codes,
                explanation=d.explanation, recommended_action=d.recommended_action, checks_summary=d.checks_summary,
                trigger=d.trigger, created_by=d.created_by, created_at=d.created_at.isoformat(timespec="seconds"),
                blocking=d.blocking or [], context=d.context or {})


def audit_dict(a: AuditEvent) -> dict:
    return dict(id=a.id, ts=a.ts.isoformat(timespec="milliseconds"), run_id=a.run_id, actor=a.actor,
                event_type=a.event_type, message=a.message, data=a.data)


def get_run_or_404(db: Session, run_id: str) -> ProcessingRun:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(400, "Invalid run id")
    r = db.get(ProcessingRun, run_id)
    if not r:
        raise HTTPException(404, f"Run {run_id} not found")
    return r


def allowed_actions(r: ProcessingRun) -> dict:
    latest = r.decisions[-1] if r.decisions else None
    open_ = r.status == "COMPLETED" and r.final_status in ("NEEDS_REVIEW", "INFO_REQUESTED")
    blocking = (latest.blocking or []) if latest else []
    return dict(can_review=open_, can_approve=open_ and not blocking, approve_blocked_by=blocking if open_ else [],
                approve_needs_justification=open_ and latest is not None and latest.status != "APPROVED",
                can_retry=r.status == "FAILED")


def run_detail(db: Session, r: ProcessingRun) -> dict:
    rules = get_rules(db)
    latest = r.decisions[-1] if r.decisions else None
    checks = [check_dict(c) for c in r.checks if latest and c.decision_version == latest.version]
    events = db.scalars(select(AuditEvent).where(AuditEvent.run_id == r.id).order_by(AuditEvent.id)).all()
    dup_ctx = (latest.context or {}).get("duplicate", {}) if latest else {}
    original = db.get(ProcessingRun, dup_ctx["original_run_id"]) if dup_ctx.get("original_run_id") else None
    return dict(run=run_summary(r), stages=[stage_dict(s) for s in r.stages],
                fields=[field_dict(f, rules) for f in r.fields],
                line_items=[dict(line_no=li.line_no, description=li.description, quantity=li.quantity,
                                 unit_price=li.unit_price, tax_rate=li.tax_rate, amount=li.amount,
                                 confidence=li.confidence, match=li.match, source=li.source) for li in r.line_items],
                decision=decision_dict(latest) if latest else None,
                decisions=[decision_dict(d) for d in r.decisions], checks=checks,
                reviews=[dict(id=x.id, action=x.action, reviewer=x.reviewer, comment=x.comment, payload=x.payload,
                              created_at=x.created_at.isoformat(timespec="seconds")) for x in r.reviews],
                audit=[audit_dict(a) for a in events], original_run=run_summary(original) if original else None,
                actions=allowed_actions(r), reviewers=REVIEWERS,
                confidence_threshold=rules["extraction"]["confidence_threshold"])


# ----------------------------------------------------------------------------- runs
@app.get("/api/health")
def health():
    return dict(status="ok", llm_available=extract_llm.available(), time=datetime.now().isoformat(timespec="seconds"))


@app.get("/api/runs")
def list_runs(status: Optional[str] = None, q: Optional[str] = None, limit: int = Query(200, le=1000),
              db: Session = Depends(get_db)):
    stmt = select(ProcessingRun).order_by(ProcessingRun.created_at.desc()).limit(limit)
    runs = db.scalars(stmt).all()
    if status:
        wanted = set(status.split(","))
        runs = [r for r in runs if (r.final_status or r.status) in wanted]
    if q:
        ql = q.lower()
        runs = [r for r in runs if any(ql in (x or "").lower() for x in
                                       (r.id, r.filename, r.vendor_name, r.invoice_number, r.po_number))]
    return [run_summary(r) for r in runs]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    return run_detail(db, get_run_or_404(db, run_id))


@app.get("/api/runs/{run_id}/live")
def get_run_live(run_id: str, db: Session = Depends(get_db)):
    """Lightweight polling endpoint for the live processing view."""
    r = get_run_or_404(db, run_id)
    latest = r.decisions[-1] if r.decisions else None
    return dict(run=run_summary(r), stages=[stage_dict(s) for s in r.stages],
                decision=decision_dict(latest) if latest else None)


ALLOWED_TYPES = {"application/pdf", "application/x-pdf", "application/octet-stream", ""}


@app.post("/api/runs/upload")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "invoice.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF invoices are accepted (.pdf)")
    if (file.content_type or "") not in ALLOWED_TYPES:
        raise HTTPException(400, f"Unexpected content type {file.content_type}")
    data = await file.read(int(MAX_UPLOAD_MB * 1024 * 1024) + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_MB:g} MB limit")
    if len(data) == 0:
        raise HTTPException(400, "File is empty")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "File content is not a PDF (missing %PDF header)")
    rid = create_run(name, data, source="upload", actor="Uploaded via web")
    submit(rid)
    return dict(run_id=rid)


@app.get("/api/samples")
def list_samples(db: Session = Depends(get_db)):
    out = []
    runs = db.scalars(select(ProcessingRun).where(ProcessingRun.source == "sample")
                      .order_by(ProcessingRun.created_at.desc())).all()
    for s in demo.samples():
        last = next((r for r in runs if r.sample_id == s["id"]), None)
        out.append(dict(s, last_run_id=last.id if last else None,
                        last_status=(last.final_status or last.status) if last else None))
    return out


@app.get("/api/samples/{sample_id}/file")
def sample_file(sample_id: str):
    s = next((x for x in demo.samples() if x["id"] == sample_id), None)
    if not s:
        raise HTTPException(404, "Unknown sample")
    return FileResponse(SAMPLE_DIR / s["file"], media_type="application/pdf", filename=s["file"],
                        content_disposition_type="inline")


@app.post("/api/samples/{sample_id}/run")
def run_sample(sample_id: str):
    s = next((x for x in demo.samples() if x["id"] == sample_id), None)
    if not s:
        raise HTTPException(404, "Unknown sample")
    rid = create_run(s["file"], (SAMPLE_DIR / s["file"]).read_bytes(), source="sample", sample_id=sample_id,
                     actor="Sample launcher")
    submit(rid)
    return dict(run_id=rid)


@app.post("/api/runs/{run_id}/retry")
def retry(run_id: str, db: Session = Depends(get_db)):
    r = get_run_or_404(db, run_id)
    if r.status != "FAILED":
        raise HTTPException(409, "Only failed runs can be retried")
    for model in (ExtractedField, InvoiceLineItem, CheckResult, DecisionRecord):
        for x in db.scalars(select(model).where(model.run_id == r.id)):
            db.delete(x)
    for st in r.stages:
        st.status, st.summary, st.details, st.started_at, st.completed_at, st.duration_ms = \
            "PENDING", None, None, None, None, None
    r.status, r.error, r.final_status, r.system_decision = "QUEUED", None, None, None
    audit(db, r.id, "AP user", "RUN_RETRIED", "Processing retried after failure")
    db.commit()
    submit(r.id)
    return dict(run_id=r.id)


@app.get("/api/runs/{run_id}/document")
def document(run_id: str, db: Session = Depends(get_db)):
    r = get_run_or_404(db, run_id)
    return FileResponse(r.file_path, media_type="application/pdf", filename=r.filename,
                        content_disposition_type="inline")


@app.get("/api/runs/{run_id}/page/{page}.png")
def page_png(run_id: str, page: int, db: Session = Depends(get_db)):
    r = get_run_or_404(db, run_id)
    if page < 1 or page > (r.page_count or 1):
        raise HTTPException(404, "No such page")
    return Response(render_page_png(Path(r.file_path), page, 110), media_type="image/png",
                    headers={"Cache-Control": "max-age=3600"})


@app.get("/api/runs/{run_id}/report.pdf")
def report_pdf(run_id: str, db: Session = Depends(get_db)):
    r = get_run_or_404(db, run_id)
    if r.status != "COMPLETED":
        raise HTTPException(409, "Report available once processing completes")
    path = Path(r.report_path) if r.report_path else None
    if not path or not path.exists():
        path = build_report(db, r)
        r.report_path = str(path)
        db.commit()
    return FileResponse(path, media_type="application/pdf", filename=f"{r.id}_processing_report.pdf")


@app.get("/api/runs/{run_id}/report.json")
def report_json(run_id: str, db: Session = Depends(get_db)):
    r = get_run_or_404(db, run_id)
    return JSONResponse(run_detail(db, r), headers={"Content-Disposition": f'attachment; filename="{r.id}.json"'})


# ----------------------------------------------------------------------------- human review
class ReviewIn(BaseModel):
    action: str = Field(pattern="^(APPROVE|REJECT|REQUEST_INFO|NOTE)$")
    reviewer: str
    comment: Optional[str] = Field(default=None, max_length=2000)


class CorrectionIn(BaseModel):
    value: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=3, max_length=500)
    reviewer: str


def _check_reviewer(name: str):
    if name not in REVIEWERS:
        raise HTTPException(403, "Not an authorised AP reviewer")


def _normalise(key: str, value: str) -> str:
    v = value.strip()
    if key == "po_reference":
        return norm_po(v) or v
    if key in ("invoice_date", "due_date"):
        d = parse_date(v)
        return d.isoformat() if d else v
    if key in ("subtotal", "tax_amount", "total_amount"):
        a = parse_amount(v)
        return f"{a:.2f}" if a is not None else v
    if key in ("vendor_tax_id", "bank_ifsc", "currency"):
        return v.upper().replace(" ", "")
    return v


@app.post("/api/runs/{run_id}/fields/{key}/correct")
def correct_field(run_id: str, key: str, body: CorrectionIn, db: Session = Depends(get_db)):
    _check_reviewer(body.reviewer)
    r = get_run_or_404(db, run_id)
    if not allowed_actions(r)["can_review"]:
        raise HTTPException(409, f"Run is {r.final_status}; corrections are only allowed while under review")
    f = db.scalar(select(ExtractedField).where(ExtractedField.run_id == r.id, ExtractedField.key == key))
    if not f:
        raise HTTPException(404, f"Unknown field {key}")
    if key == "bank_account":
        raise HTTPException(403, "Bank details cannot be changed from an invoice review (vendor master control)")
    val = _normalise(key, body.value)
    ok, problem = format_ok(key, val)
    if not ok:
        raise HTTPException(422, f"{f.label}: {problem}")
    prev = f.corrected_value
    f.corrected_value, f.corrected_by, f.corrected_at, f.correction_reason = val, body.reviewer, datetime.now(), body.reason
    db.add(ReviewAction(run_id=r.id, action="CORRECT_FIELD", reviewer=body.reviewer, comment=body.reason,
                        payload=dict(field=key, original=f.value, previous_correction=prev, corrected=val)))
    audit(db, r.id, body.reviewer, "FIELD_CORRECTED",
          f"{f.label}: {repr(f.value) if f.value is not None else '(unreadable)'} → '{val}' "
          f"(original extraction preserved). Reason: {body.reason}",
          {"field": key, "original": f.value, "corrected": val})
    db.commit()
    return run_detail(db, r)


@app.post("/api/runs/{run_id}/reevaluate")
def reevaluate_run(run_id: str, body: dict, db: Session = Depends(get_db)):
    reviewer = body.get("reviewer", "")
    _check_reviewer(reviewer)
    r = get_run_or_404(db, run_id)
    if not allowed_actions(r)["can_review"]:
        raise HTTPException(409, "Only runs under review can be re-evaluated")
    db.add(ReviewAction(run_id=r.id, action="REEVALUATE", reviewer=reviewer,
                        comment="Re-ran validation with corrected values"))
    reevaluate(db, r, reviewer)
    db.commit()
    db.refresh(r)
    return run_detail(db, r)


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: ReviewIn, db: Session = Depends(get_db)):
    _check_reviewer(body.reviewer)
    r = get_run_or_404(db, run_id)
    acts = allowed_actions(r)
    comment = (body.comment or "").strip()
    if body.action == "NOTE":
        if not comment:
            raise HTTPException(422, "A note cannot be empty")
        db.add(ReviewAction(run_id=r.id, action="NOTE", reviewer=body.reviewer, comment=comment))
        audit(db, r.id, body.reviewer, "NOTE_ADDED", comment)
        db.commit()
        return run_detail(db, r)
    if not acts["can_review"]:
        raise HTTPException(409, f"Run is {r.final_status}; no review action possible")
    latest = r.decisions[-1]
    if body.action == "APPROVE":
        if not acts["can_approve"]:
            raise HTTPException(409, "Approval blocked until these are resolved (correct the data and re-evaluate): "
                                     + ", ".join(acts["approve_blocked_by"]))
        if acts["approve_needs_justification"] and len(comment) < 10:
            raise HTTPException(422, "Approving over open review items requires a justification (min 10 characters)")
        r.final_status, r.final_status_by = "APPROVED", body.reviewer
        db.add(ReviewAction(run_id=r.id, action="APPROVE", reviewer=body.reviewer, comment=comment or None,
                            payload=dict(overridden=[x["code"] for x in latest.reason_codes if x["effect"] == "REVIEW"],
                                         decision_version=latest.version)))
        audit(db, r.id, body.reviewer, "APPROVED_BY_REVIEWER",
              "Invoice approved for payment scheduling (no payment initiated)" + (f". Justification: {comment}"
                                                                                  if comment else ""))
        db.flush()
        consume_po(db, r, latest, body.reviewer)
    elif body.action == "REJECT":
        if len(comment) < 5:
            raise HTTPException(422, "A rejection reason is required")
        r.final_status, r.final_status_by = "REJECTED", body.reviewer
        db.add(ReviewAction(run_id=r.id, action="REJECT", reviewer=body.reviewer, comment=comment))
        audit(db, r.id, body.reviewer, "REJECTED_BY_REVIEWER", f"Invoice rejected. Reason: {comment}")
    elif body.action == "REQUEST_INFO":
        if len(comment) < 5:
            raise HTTPException(422, "Describe what information is needed")
        r.final_status, r.final_status_by = "INFO_REQUESTED", body.reviewer
        db.add(ReviewAction(run_id=r.id, action="REQUEST_INFO", reviewer=body.reviewer, comment=comment))
        audit(db, r.id, body.reviewer, "INFO_REQUESTED", f"Additional information requested: {comment}")
    db.flush()
    db.refresh(r)
    r.report_path = str(build_report(db, r))
    db.commit()
    return run_detail(db, r)


@app.get("/api/review-queue")
def review_queue(db: Session = Depends(get_db)):
    runs = db.scalars(select(ProcessingRun).where(ProcessingRun.final_status.in_(["NEEDS_REVIEW", "INFO_REQUESTED"]))
                      .order_by(ProcessingRun.created_at)).all()
    out = []
    for r in runs:
        d = r.decisions[-1] if r.decisions else None
        out.append(dict(run_summary(r), headline=d.headline if d else None,
                        reason_codes=d.reason_codes if d else [], recommended_action=d.recommended_action if d else None,
                        age_hours=round((datetime.now() - r.created_at).total_seconds() / 3600, 1),
                        blocking=d.blocking if d else []))
    return out


# ----------------------------------------------------------------------------- master data
@app.get("/api/vendors")
def vendors(db: Session = Depends(get_db)):
    runs = db.scalars(select(ProcessingRun).where(ProcessingRun.status == "COMPLETED")).all()
    out = []
    for v in db.scalars(select(Vendor).order_by(Vendor.id)):
        mine = [r for r in runs if r.vendor_id == v.id]
        approved = [r for r in mine if r.final_status == "APPROVED"]
        out.append(dict(id=v.id, name=v.name, category=v.category, address=v.address, country=v.country,
                        tax_id=v.tax_id, tax_id_type=v.tax_id_type, email=v.email, phone=v.phone,
                        bank_name=v.bank_name, bank_account=mask_account(v.bank_account), ifsc=v.ifsc,
                        currency=v.currency, status=v.status, approval_status=v.approval_status,
                        payment_terms=v.payment_terms, onboarded=v.onboarded, notes=v.notes,
                        invoices=len(mine), approved_value=round(sum(r.total_amount or 0 for r in approved), 2),
                        open_pos=sum(1 for p in db.scalars(select(PurchaseOrder).where(
                            PurchaseOrder.vendor_id == v.id, PurchaseOrder.status == "OPEN")))))
    return out


@app.get("/api/purchase-orders")
def purchase_orders(db: Session = Depends(get_db)):
    pos = pos_list(db)
    cons = db.scalars(select(POConsumption).where(POConsumption.reversed.is_(False))).all()
    runs_by_po = defaultdict(list)
    for r in db.scalars(select(ProcessingRun).where(ProcessingRun.po_number.is_not(None))):
        runs_by_po[r.po_number].append(dict(id=r.id, invoice_number=r.invoice_number, total=r.total_amount,
                                            status=r.final_status or r.status, vendor_name=r.vendor_name))
    for p in pos:
        p["invoices"] = runs_by_po.get(p["po_number"], [])
        p["consumptions"] = [dict(run_id=c.run_id, amount=c.amount, at=c.created_at.isoformat(timespec="seconds"))
                             for c in cons if c.po_number == p["po_number"]]
        p["utilisation_pct"] = round(p["consumed"] / p["total_amount"] * 100, 1) if p["total_amount"] else 0
    return pos


@app.get("/api/audit")
def audit_log(run_id: Optional[str] = None, event_type: Optional[str] = None, actor: Optional[str] = None,
              limit: int = Query(300, le=2000), db: Session = Depends(get_db)):
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc())
    if run_id:
        stmt = stmt.where(AuditEvent.run_id == run_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if actor:
        stmt = stmt.where(AuditEvent.actor == actor)
    return [audit_dict(a) for a in db.scalars(stmt.limit(limit))]


# ----------------------------------------------------------------------------- dashboard
@app.get("/api/metrics")
def metrics(db: Session = Depends(get_db)):
    runs = db.scalars(select(ProcessingRun)).all()
    done = [r for r in runs if r.status == "COMPLETED"]
    fs = Counter(r.final_status for r in done)
    auto = [r for r in done if r.final_status_by == "Decision engine" and r.final_status in ("APPROVED", "REJECTED")]
    human = [r for r in done if r.final_status_by in REVIEWERS]
    value_by_cur = defaultdict(float)
    approved_by_cur = defaultdict(float)
    for r in done:
        if r.total_amount is not None and not r.is_duplicate:
            value_by_cur[r.currency or "INR"] += r.total_amount
            if r.final_status == "APPROVED":
                approved_by_cur[r.currency or "INR"] += r.total_amount
    reasons = Counter()
    for r in done:
        if r.decisions:
            primary = next((rc for rc in r.decisions[-1].reason_codes if rc["effect"] != "NONE"), None)
            if primary:
                reasons[(primary["code"], primary["label"])] += 1
    vendor_val = defaultdict(float)
    for r in done:
        if r.total_amount and (r.currency or "INR") == "INR" and not r.is_duplicate:
            vendor_val[r.vendor_name or "Unverified"] += r.total_amount
    times = [r.processing_ms for r in done if r.processing_ms]
    scanned = [r.processing_ms for r in done if r.processing_ms and r.is_scanned]
    digital = [r.processing_ms for r in done if r.processing_ms and not r.is_scanned]
    stage_ms = defaultdict(list)
    for r in done:
        for s in r.stages:
            if s.duration_ms is not None:
                stage_ms[s.name].append(s.duration_ms)
    return dict(
        total_processed=len(done), in_progress=sum(r.status in ("QUEUED", "PROCESSING") for r in runs),
        failed=sum(r.status == "FAILED" for r in runs),
        approved=fs.get("APPROVED", 0), needs_review=fs.get("NEEDS_REVIEW", 0) + fs.get("INFO_REQUESTED", 0),
        rejected=fs.get("REJECTED", 0), info_requested=fs.get("INFO_REQUESTED", 0),
        duplicates=sum(r.is_duplicate for r in done),
        value_by_currency={k: round(v, 2) for k, v in value_by_cur.items()},
        approved_value_by_currency={k: round(v, 2) for k, v in approved_by_cur.items()},
        avg_processing_ms=round(sum(times) / len(times)) if times else None,
        avg_processing_ms_digital=round(sum(digital) / len(digital)) if digital else None,
        avg_processing_ms_scanned=round(sum(scanned) / len(scanned)) if scanned else None,
        straight_through=len(auto), human_decided=len(human),
        automation_rate=round(len(auto) / len(done) * 100, 1) if done else None,
        scanned_count=sum(1 for r in done if r.is_scanned),
        decision_breakdown=[dict(status=k, count=fs.get(k, 0)) for k in
                            ("APPROVED", "NEEDS_REVIEW", "INFO_REQUESTED", "REJECTED")],
        top_reasons=[dict(code=c, label=l, count=n) for (c, l), n in reasons.most_common(8)],
        value_by_vendor=[dict(vendor=k, value=round(v, 2)) for k, v in
                         sorted(vendor_val.items(), key=lambda x: -x[1])[:7]],
        stage_avg_ms=[dict(stage=n, ms=round(sum(v) / len(v))) for n, v in stage_ms.items()
                      if n in dict(STAGES).values()],
        recent=[run_summary(r) for r in sorted(runs, key=lambda r: r.created_at, reverse=True)[:8]],
    )


# ----------------------------------------------------------------------------- settings & demo
@app.get("/api/settings")
def settings(db: Session = Depends(get_db)):
    return dict(rules=get_rules(db), reason_codes=REASON_CODES, llm_available=extract_llm.available(),
                reviewers=REVIEWERS, max_upload_mb=MAX_UPLOAD_MB)


@app.put("/api/settings")
def update_settings(body: dict, db: Session = Depends(get_db)):
    user = body.get("user") or REVIEWERS[0]
    _check_reviewer(user)
    before = get_rules(db)
    try:
        after = save_rules(db, body.get("rules") or {}, user)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(422, str(e))
    changes = []
    for sec in ("tolerance", "extraction", "matching", "duplicates", "policies", "demo"):
        for k, v in (after.get(sec) or {}).items():
            if (before.get(sec) or {}).get(k) != v:
                changes.append(f"{sec}.{k}: {before.get(sec, {}).get(k)} → {v}")
    if before.get("extraction_mode") != after.get("extraction_mode"):
        changes.append(f"extraction_mode: {before.get('extraction_mode')} → {after.get('extraction_mode')}")
    audit(db, None, user, "RULES_UPDATED", "Business rules updated: " + ("; ".join(changes) or "no changes"),
          {"changes": changes})
    db.commit()
    return dict(rules=after, changes=changes)


@app.post("/api/demo/reset")
def demo_reset(body: dict):
    user = body.get("user") or "Demo admin"
    demo.reset_demo(user, keep_settings=not body.get("reset_rules", False))
    ids = demo.preload_history(user) if body.get("preload_history", True) else []
    return dict(ok=True, history_runs=ids)


# ----------------------------------------------------------------------------- SPA
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = FRONTEND_DIST / path
        if path and f.is_file() and FRONTEND_DIST in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(FRONTEND_DIST / "index.html")
