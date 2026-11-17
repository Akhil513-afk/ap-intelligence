"""Pipeline orchestration: nine persisted stages, re-evaluation after human corrections, PO consumption.

Each stage is written to the DB before and after it executes, so the live view shows real progress.
`stage_pacing_ms` adds a visible pause *between* stages for demos; stage durations exclude it.
Runs are processed one at a time (single worker) so duplicate detection is deterministic.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import UPLOAD_DIR
from ..db import session_scope
from ..models import (AuditEvent, CheckResult, DecisionRecord, ExtractedField, InvoiceLineItem, POConsumption,
                      ProcessingRun, PurchaseOrder, RunStage, Vendor)
from ..rules import get_rules
from ..utils import sha256_bytes
from . import extract_llm
from .checks import FAIL, PASS, UNABLE, WARN, Check, EffectiveInvoice, FieldView, assess
from .decision import decide
from .document import analyze_pdf, ocr_doc, render_page_png, text_layer_doc
from .duplicates import detect_duplicates, vendor_key
from .extract_local import FIELD_DEFS, LocalExtractor
from .financial import validate_financials
from .po_match import match_po
from .vendor import verify_vendor

log = logging.getLogger("ap.pipeline")

STAGES = [
    ("upload", "Invoice uploaded"),
    ("analyze", "PDF analysed"),
    ("extract", "OCR / extraction completed"),
    ("vendor", "Vendor verified"),
    ("po", "PO matched"),
    ("financial", "Financial validations completed"),
    ("duplicate", "Duplicate detection completed"),
    ("decision", "Decision generated"),
    ("report", "Report generated"),
]

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ap-pipeline")
_id_lock = threading.Lock()


def audit(db: Session, run_id: Optional[str], actor: str, event: str, message: str, data: Optional[dict] = None):
    db.add(AuditEvent(run_id=run_id, actor=actor, event_type=event, message=message, data=data or {}))


# ----------------------------------------------------------------------------- data access helpers
def vendors_list(db: Session) -> list[dict]:
    return [{c.name: getattr(v, c.name) for c in Vendor.__table__.columns} for v in db.scalars(select(Vendor))]


def pos_list(db: Session, exclude_run: Optional[str] = None) -> list[dict]:
    out = []
    cons = db.scalars(select(POConsumption).where(POConsumption.reversed.is_(False))).all()
    for po in db.scalars(select(PurchaseOrder)):
        mine = [c for c in cons if c.po_number == po.po_number and c.run_id != exclude_run]
        consumed = round(sum(c.amount for c in mine), 2)
        lines = []
        for ln in po.lines:
            q = sum(float(c.line_quantities.get(str(ln.id), 0)) for c in mine)
            lines.append(dict(id=ln.id, line_no=ln.line_no, description=ln.description, quantity=ln.quantity,
                              unit=ln.unit, unit_price=ln.unit_price, tax_rate=ln.tax_rate, amount=ln.amount,
                              consumed_qty=q))
        out.append(dict(po_number=po.po_number, vendor_id=po.vendor_id, vendor_name=po.vendor.name,
                        vendor_tax_id=po.vendor.tax_id, po_date=po.po_date, status=po.status, currency=po.currency,
                        tax_treatment=po.tax_treatment, description=po.description, requester=po.requester,
                        subtotal=po.subtotal, tax_amount=po.tax_amount, total_amount=po.total_amount,
                        consumed=consumed, remaining=round(po.total_amount - consumed, 2), lines=lines))
    return out


def effective_invoice(db: Session, run: ProcessingRun, rules: dict) -> EffectiveInvoice:
    thr = rules["extraction"]["confidence_threshold"]
    crit = set(rules["extraction"]["critical_fields"])
    fields = {}
    for f in run.fields:
        corrected = f.corrected_value is not None
        val = f.corrected_value if corrected else f.value
        conf = 1.0 if corrected else f.confidence
        fields[f.key] = FieldView(f.key, val, f.value, conf, assess(f.key, val, conf, corrected, thr), corrected,
                                  f.key in crit)
    items = [dict(line_no=li.line_no, description=li.description, quantity=li.quantity, unit_price=li.unit_price,
                  tax_rate=li.tax_rate, amount=li.amount, confidence=li.confidence) for li in run.line_items]
    meta = next((s.details for s in run.stages if s.key == "extract" and s.details), {}) or {}
    return EffectiveInvoice(fields, items, meta.get("tax_mode", "exclusive"), meta.get("tax_components", []))


def extraction_checks(inv: EffectiveInvoice, rules: dict) -> list[Check]:
    thr = rules["extraction"]["confidence_threshold"]
    out = []
    labels = {k: lbl for k, lbl, _ in FIELD_DEFS}
    for key in rules["extraction"]["critical_fields"]:
        f = inv.fields.get(key)
        st = f.status if f else "MISSING"
        conf = f.confidence if f else 0
        lbl = labels.get(key, key)
        tag = " (human-verified)" if f and f.corrected else ""
        if key == "po_reference":          # evaluated in PO matching
            continue
        if st == "VALID":
            out.append(Check("extraction", "FIELD_" + key.upper(), f"{lbl} extracted", PASS,
                             f"'{f.value}' at {conf:.0%} confidence{tag}", actual=f.value,
                             rule=f"confidence ≥ {thr:.0%}"))
        elif st == "MISSING":
            if key == "vendor_tax_id":
                out.append(Check("extraction", "FIELD_" + key.upper(), f"{lbl} extracted", FAIL,
                                 "Not found on the document (vendor identity checked in vendor stage)"))
                continue
            out.append(Check("extraction", "FIELD_" + key.upper(), f"{lbl} extracted", FAIL,
                             f"{lbl} could not be extracted - not guessed", reason_code="MISSING_FIELD",
                             rule="Mandatory field"))
        elif st == "INVALID":
            out.append(Check("extraction", "FIELD_" + key.upper(), f"{lbl} extracted", FAIL,
                             f"'{f.value}' fails format validation", actual=f.value,
                             reason_code="INVALID_TAX_ID" if key == "vendor_tax_id" else "LOW_CONFIDENCE"))
        else:
            out.append(Check("extraction", "FIELD_" + key.upper(), f"{lbl} extracted", WARN,
                             f"'{f.value}' read at only {conf:.0%} confidence - verify against the document",
                             actual=f.value, rule=f"confidence ≥ {thr:.0%}", reason_code="LOW_CONFIDENCE"))
    return out


# ----------------------------------------------------------------------------- evaluation (stages 4-8)
class Evaluation:
    """Business validation over the effective invoice. Used by the pipeline and by re-evaluation."""

    def __init__(self, db: Session, run: ProcessingRun, rules: dict, today=None):
        self.db, self.run, self.rules, self.today = db, run, rules, today
        self.inv = effective_invoice(db, run, rules)
        self.checks: list[Check] = extraction_checks(self.inv, rules)

    def vendor(self):
        self.v = verify_vendor(self.inv, vendors_list(self.db), self.rules)
        self.checks += self.v.checks
        return self.v

    def po(self):
        self.p = match_po(self.inv, self.v, pos_list(self.db, exclude_run=self.run.id), self.rules)
        self.checks += self.p.checks
        return self.p

    def financial(self):
        self.f = validate_financials(self.inv, self.v, self.p, self.rules, self.today)
        self.checks += self.f
        return self.f

    def duplicates(self):
        prior_runs = self.db.scalars(select(ProcessingRun).where(
            ProcessingRun.id != self.run.id, ProcessingRun.status == "COMPLETED",
            ProcessingRun.is_duplicate.is_(False), ProcessingRun.created_at < self.run.created_at)).all()
        prior = [dict(id=p.id, filename=p.filename, file_sha256=p.file_sha256, invoice_number=p.invoice_number,
                      total_amount=p.total_amount, final_status=p.final_status, vendor_key=p.vendor_key,
                      content_fingerprint=p.content_fingerprint, created_at=p.created_at.isoformat(timespec="seconds"))
                 for p in prior_runs]
        self.d, fp = detect_duplicates(self.inv, self.v, self.run.file_sha256, prior, self.rules)
        self.run.content_fingerprint = fp
        self.run.vendor_key = vendor_key(self.inv, self.v)
        self.checks += self.d.checks
        return self.d

    def decide(self, version: int, trigger: str, actor: str) -> DecisionRecord:
        dec = decide(self.checks, self.rules, self.inv, self.v, self.p, self.d)
        run = self.run
        for c in self.checks:
            self.db.add(CheckResult(run_id=run.id, decision_version=version, **c.to_dict()))
        line_cons = {}
        for m in self.p.line_matches:
            if m.get("po_line_id"):
                line_cons[str(m["po_line_id"])] = m.get("quantity") or 0
            for ln_no in m.get("bundled_po_lines", []) or []:
                pl = next((x for x in self.p.po["lines"] if x["line_no"] == ln_no), None)
                if pl:
                    line_cons[str(pl["id"])] = pl["quantity"] - pl["consumed_qty"]
        ctx = dict(
            vendor=dict(status=self.v.status, vendor=_vendor_public(self.v.vendor),
                        tentative=_vendor_public(self.v.tentative), method=self.v.method,
                        candidates=self.v.candidates),
            po=dict(status=self.p.status, accepted=self.p.accepted, po=_po_public(self.p.po),
                    candidates=self.p.candidates, line_matches=self.p.line_matches, amount=self.p.amount,
                    line_consumption=line_cons),
            duplicate=dict(status=self.d.status, original_run_id=self.d.original_run_id, matches=self.d.matches),
            tax_mode=self.inv.tax_mode,
        )
        rec = DecisionRecord(run_id=run.id, version=version, status=dec.status, headline=dec.headline,
                             reason_codes=dec.reason_codes, explanation=dec.explanation,
                             recommended_action=dec.recommended_action, checks_summary=dec.checks_summary,
                             rules_snapshot=self.rules, trigger=trigger, created_by=actor, context=ctx,
                             blocking=dec.blocking_for_approval)
        self.db.add(rec)
        # denormalised run summary
        run.system_decision = dec.status
        run.vendor_id = self.v.vendor_id
        run.vendor_name = (self.v.vendor or {}).get("name") or self.inv.get("vendor_name")
        run.invoice_number = self.inv.get("invoice_number")
        run.invoice_date = self.inv.get("invoice_date")
        run.po_number = self.p.po["po_number"] if self.p.po else self.inv.get("po_reference")
        run.currency = self.inv.get("currency")
        run.total_amount = self.inv.num("total_amount")
        run.is_duplicate = self.d.status == "CONFIRMED"
        run.duplicate_of = self.d.original_run_id if self.d.status == "CONFIRMED" else None
        return rec


def _vendor_public(v: Optional[dict]) -> Optional[dict]:
    if not v:
        return None
    from ..utils import mask_account
    return {k: (mask_account(v[k]) if k == "bank_account" else v[k]) for k in
            ("id", "name", "tax_id", "tax_id_type", "status", "approval_status", "address", "email", "bank_name",
             "bank_account", "ifsc", "currency", "payment_terms", "category", "notes")}


def _po_public(p: Optional[dict]) -> Optional[dict]:
    return None if not p else {k: p[k] for k in p}


def consume_po(db: Session, run: ProcessingRun, rec: DecisionRecord, actor: str):
    po = (rec.context or {}).get("po") or {}
    if not po.get("accepted") or not po.get("po") or run.total_amount is None:
        return
    exists = db.scalar(select(POConsumption).where(POConsumption.run_id == run.id, POConsumption.reversed.is_(False)))
    if exists:
        return
    db.add(POConsumption(po_number=po["po"]["po_number"], run_id=run.id, amount=run.total_amount,
                         line_quantities=po.get("line_consumption") or {}))
    remaining = po["po"]["remaining"] - run.total_amount
    audit(db, run.id, actor, "PO_BALANCE_UPDATED",
          f"{po['po']['po_number']}: {run.currency} {run.total_amount:,.2f} committed; remaining {remaining:,.2f}",
          {"po_number": po["po"]["po_number"], "amount": run.total_amount, "remaining": remaining})


# ----------------------------------------------------------------------------- run creation
def next_run_id(db: Session) -> str:
    day = datetime.now().strftime("%Y%m%d")
    n = db.scalar(select(func.count()).select_from(ProcessingRun).where(ProcessingRun.id.like(f"RUN-{day}-%"))) or 0
    while True:
        n += 1
        rid = f"RUN-{day}-{n:04d}"
        if not db.get(ProcessingRun, rid):
            return rid


def create_run(filename: str, data: bytes, source: str = "upload", sample_id: Optional[str] = None,
               actor: str = "AP Inbox") -> str:
    with _id_lock, session_scope() as db:
        rid = next_run_id(db)
        path = UPLOAD_DIR / f"{rid}.pdf"
        path.write_bytes(data)
        run = ProcessingRun(id=rid, filename=filename, file_path=str(path), file_sha256=sha256_bytes(data),
                            file_size=len(data), source=source, sample_id=sample_id, status="QUEUED")
        db.add(run)
        for i, (k, n) in enumerate(STAGES, start=1):
            db.add(RunStage(run_id=rid, seq=i, key=k, name=n, status="PENDING"))
        audit(db, rid, actor, "RUN_CREATED", f"Invoice '{filename}' received ({len(data):,} bytes) via {source}",
              {"sha256": run.file_sha256, "source": source, "sample_id": sample_id})
        return rid


def submit(run_id: str, pacing_ms: Optional[int] = None, wait: bool = False):
    fut = _executor.submit(process_run, run_id, pacing_ms)
    if wait:
        fut.result()
    return fut


# ----------------------------------------------------------------------------- the pipeline
def process_run(run_id: str, pacing_ms: Optional[int] = None, today=None):
    with session_scope() as db:
        rules = get_rules(db)
        run = db.get(ProcessingRun, run_id)
        run.status, run.started_at = "PROCESSING", datetime.now()
    pacing = rules["demo"]["stage_pacing_ms"] if pacing_ms is None else pacing_ms
    state: dict = {}
    compute_ms = 0

    def stage(key: str, fn: Callable[[Session, ProcessingRun], tuple[str, str, dict]]):
        nonlocal compute_ms
        with session_scope() as db:
            st = db.scalar(select(RunStage).where(RunStage.run_id == run_id, RunStage.key == key))
            st.status, st.started_at = "RUNNING", datetime.now()
        t0 = time.perf_counter()
        with session_scope() as db:
            run = db.get(ProcessingRun, run_id)
            status, summary, details = fn(db, run)
            ms = int((time.perf_counter() - t0) * 1000)
            compute_ms += ms
            st = db.scalar(select(RunStage).where(RunStage.run_id == run_id, RunStage.key == key))
            st.status, st.summary, st.details = status, summary, details
            st.completed_at, st.duration_ms = datetime.now(), ms
            audit(db, run_id, "Pipeline", "STAGE_" + status, f"{st.name}: {summary}", {"stage": key, "ms": ms})
        if pacing:
            time.sleep(pacing / 1000)

    try:
        stage("upload", lambda db, run: _s_upload(run))
        stage("analyze", lambda db, run: _s_analyze(run, state))
        stage("extract", lambda db, run: _s_extract(db, run, rules, state))
        stage("vendor", lambda db, run: _s_vendor(db, run, rules, state, today))
        stage("po", lambda db, run: _s_po(db, run, state))
        stage("financial", lambda db, run: _s_financial(db, run, state))
        stage("duplicate", lambda db, run: _s_duplicate(db, run, state))
        stage("decision", lambda db, run: _s_decision(db, run, state))
        stage("report", lambda db, run: _s_report(db, run))
        with session_scope() as db:
            run = db.get(ProcessingRun, run_id)
            run.status, run.completed_at, run.processing_ms = "COMPLETED", datetime.now(), compute_ms
    except Exception as e:  # visible & recoverable: the run is marked FAILED with the error and can be retried
        log.exception("run %s failed", run_id)
        with session_scope() as db:
            run = db.get(ProcessingRun, run_id)
            run.status, run.error, run.completed_at = "FAILED", f"{type(e).__name__}: {e}", datetime.now()
            run.processing_ms = compute_ms
            for st in run.stages:
                if st.status == "RUNNING":
                    st.status, st.summary, st.completed_at = "FAILED", f"{type(e).__name__}: {e}", datetime.now()
                    st.details = {"traceback": traceback.format_exc()[-2000:]}
                elif st.status == "PENDING":
                    st.status, st.summary = "SKIPPED", "Not executed - an earlier stage failed"
            audit(db, run_id, "Pipeline", "RUN_FAILED", f"Processing failed: {type(e).__name__}: {e}")


def _s_upload(run: ProcessingRun):
    p = Path(run.file_path)
    head = p.read_bytes()[:5]
    if head != b"%PDF-":
        raise ValueError("File is not a valid PDF")
    return "SUCCESS", f"{run.filename} stored as {run.id} ({run.file_size / 1024:.0f} KB)", dict(
        run_id=run.id, filename=run.filename, size_bytes=run.file_size, sha256=run.file_sha256, source=run.source)


def _s_analyze(run: ProcessingRun, state: dict):
    a = analyze_pdf(Path(run.file_path))
    state["analysis"] = a
    run.page_count, run.is_scanned = a["page_count"], a["is_scanned"]
    if a["needs_ocr"]:
        return "WARNING", (f"{a['page_count']} page(s), no usable text layer on page(s) {a['scanned_pages']} - "
                           f"scanned document, OCR required"), a
    return "SUCCESS", f"{a['page_count']} page(s), machine-readable text layer ({sum(a['text_chars_per_page'])} chars)", a


def _s_extract(db: Session, run: ProcessingRun, rules: dict, state: dict):
    path = Path(run.file_path)
    a = state["analysis"]
    doc = ocr_doc(path) if a["needs_ocr"] else text_layer_doc(path)
    result = LocalExtractor(doc).run()
    mode = rules["extraction_mode"]
    notes = list(result.notes)
    use_llm = mode in ("auto", "llm") and extract_llm.available()
    if mode == "llm" and not extract_llm.available():
        notes.append("LLM mode selected but ANTHROPIC_API_KEY is not set - used local extractor")
    if use_llm:
        try:
            png = render_page_png(path, 1, 150) if doc.method == "ocr" else None
            result = extract_llm.merge(extract_llm.call_llm(doc, png), result, doc)
        except Exception as e:  # never depend on the API
            notes.append(f"LLM extraction failed ({type(e).__name__}); fell back to local extractor")
    run.extraction_method = result.method if not use_llm or result.method.startswith("llm") else doc.method
    thr = rules["extraction"]["confidence_threshold"]
    crit = set(rules["extraction"]["critical_fields"])
    flagged = []
    for i, (key, label, group) in enumerate(FIELD_DEFS):
        fr = result.fields.get(key)
        st = assess(key, fr.value if fr else None, fr.confidence if fr else 0, False, thr)
        needs = key in crit and st != "VALID"
        if needs:
            flagged.append(label)
        db.add(ExtractedField(run_id=run.id, seq=i, key=key, label=label, group=group, critical=key in crit,
                              value=fr.value if fr else None, raw_text=fr.raw_text if fr else None,
                              confidence=fr.confidence if fr else 0.0, method=fr.method if fr else "not-found",
                              source=fr.source if fr else None, validation_status=st, needs_review=needs,
                              notes=fr.notes if fr else []))
    for li in result.line_items:
        db.add(InvoiceLineItem(run_id=run.id, line_no=li.line_no, description=li.description, quantity=li.quantity,
                               unit_price=li.unit_price, tax_rate=li.tax_rate, amount=li.amount,
                               confidence=li.confidence, source=li.source))
    f = result.fields
    details = dict(method=run.extraction_method, tax_mode=result.tax_mode, tax_components=result.tax_components,
                   ocr_mean_conf=result.ocr_mean_conf, deskew_degrees=doc.deskew_degrees, notes=notes,
                   fields_found=sum(1 for x in f.values() if x.value is not None), fields_total=len(FIELD_DEFS),
                   line_items=len(result.line_items), needs_verification=flagged,
                   highlights={k: dict(value=f[k].value, confidence=f[k].confidence) for k in
                               ("vendor_name", "invoice_number", "invoice_date", "po_reference", "total_amount")})
    summ = (f"{details['fields_found']}/{len(FIELD_DEFS)} fields, {len(result.line_items)} line item(s) via "
            f"{'OCR (Tesseract)' if doc.method == 'ocr' else 'PDF text layer'}"
            + (f", mean OCR confidence {result.ocr_mean_conf:.0%}" if result.ocr_mean_conf else "")
            + (f" · {len(flagged)} field(s) need human verification: {', '.join(flagged)}" if flagged else ""))
    return ("WARNING" if flagged else "SUCCESS"), summ, details


def _s_vendor(db, run, rules, state, today):
    db.flush()
    db.refresh(run)
    ev = Evaluation(db, run, rules, today)
    state["ev"] = ev
    v = ev.vendor()
    fails = [c for c in v.checks if c.outcome == FAIL]
    if v.status == "VERIFIED":
        msg = f"{v.vendor['id']} · {v.vendor['name']} verified by {v.vendor['tax_id_type']} {v.vendor['tax_id']}"
    elif v.status == "UNVERIFIED":
        msg = f"Vendor NOT verified: {fails[0].message if fails else 'identity unconfirmed'}"
    else:
        msg = f"Vendor not found in vendor master: {fails[0].message if fails else ''}"
    if fails and v.status == "VERIFIED":
        msg += " · " + "; ".join(c.name + ": " + c.message for c in fails)
    return ("FAILED" if v.status != "VERIFIED" else "WARNING" if fails else "SUCCESS"), msg, dict(
        status=v.status, vendor=_vendor_public(v.vendor), candidates=v.candidates,
        checks=[c.to_dict() for c in v.checks])


def _bind(state, db, run) -> Evaluation:
    """Each stage has its own session; rebind the evaluation to it so writes are committed."""
    ev: Evaluation = state["ev"]
    ev.db, ev.run = db, run
    return ev


def _s_po(db, run, state):
    ev = _bind(state, db, run)
    p = ev.po()
    for li in run.line_items:
        li.match = next((m for m in p.line_matches if m["line_no"] == li.line_no), None)
    a = p.amount or {}
    if p.status == "MATCHED":
        cls = a.get("classification", "UNABLE")
        msg = f"{p.po['po_number']} accepted · amount {cls.replace('_', ' ').lower()}"
        if a.get("difference") is not None:
            msg += f" ({'+' if a['difference'] > 0 else ''}{a['difference']:,.2f}, {a.get('difference_pct', 0):+.2f}%)"
        n_ok = sum(1 for m in p.line_matches if m["status"] in ("MATCHED", "BUNDLED"))
        msg += f" · {n_ok}/{len(p.line_matches)} lines matched"
        bad = [c for c in p.checks if c.outcome == FAIL]
        status = "WARNING" if bad or cls == "WITHIN_TOLERANCE" else "SUCCESS"
    elif p.status == "MISSING":
        msg, status = f"No readable PO reference · {len(p.candidates)} candidate(s) suggested", "WARNING"
    elif p.status == "NOT_FOUND":
        msg, status = f"PO {run_po_ref(ev)} not found · {len(p.candidates)} candidate(s)", "FAILED"
    else:
        msg, status = (f"{p.po['po_number']} belongs to {p.po['vendor_name']} - match refused"
                       + (f" · {len(p.candidates)} candidate(s) from the invoicing vendor" if p.candidates else "")), "FAILED"
    return status, msg, dict(status=p.status, accepted=p.accepted, po_number=p.po["po_number"] if p.po else None,
                             amount=a, candidates=p.candidates, line_matches=p.line_matches,
                             checks=[c.to_dict() for c in p.checks])


def run_po_ref(ev: Evaluation):
    return ev.inv.get("po_reference")


def _s_financial(db, run, state):
    ev = _bind(state, db, run)
    f = ev.financial()
    n = {o: sum(c.outcome == o for c in f) for o in (PASS, WARN, FAIL, UNABLE)}
    msg = f"{n[PASS]} passed, {n[FAIL]} failed, {n[WARN]} warning(s), {n[UNABLE]} unable to validate"
    return ("FAILED" if n[FAIL] else "WARNING" if n[WARN] or n[UNABLE] else "SUCCESS"), msg, dict(
        checks=[c.to_dict() for c in f])


def _s_duplicate(db, run, state):
    ev = _bind(state, db, run)
    d = ev.duplicates()
    if d.status == "CONFIRMED":
        msg, st = f"Confirmed duplicate of {d.original_run_id} ({d.matches[0]['signal']})", "FAILED"
    elif d.status == "POSSIBLE":
        msg, st = f"Possible duplicate of {d.original_run_id} ({d.matches[0]['signal']})", "WARNING"
    else:
        msg, st = "No duplicates found (file hash, vendor + invoice number, content fingerprint)", "SUCCESS"
    return st, msg, dict(status=d.status, original_run_id=d.original_run_id, matches=d.matches,
                         checks=[c.to_dict() for c in d.checks])


def _s_decision(db, run, state):
    ev = _bind(state, db, run)
    rec = ev.decide(1, "pipeline", "Decision engine")
    run.final_status = rec.status
    run.final_status_by = "Decision engine"
    db.flush()
    if rec.status == "APPROVED":
        consume_po(db, run, rec, "Decision engine")
    audit(db, run.id, "Decision engine", "DECISION", rec.headline,
          {"reason_codes": [r["code"] for r in rec.reason_codes]})
    return {"APPROVED": "SUCCESS", "NEEDS_REVIEW": "WARNING", "REJECTED": "FAILED"}[rec.status], \
        rec.headline, dict(
        status=rec.status, headline=rec.headline, reason_codes=rec.reason_codes, explanation=rec.explanation,
        recommended_action=rec.recommended_action, checks_summary=rec.checks_summary)


def _s_report(db, run):
    from .report import build_report
    db.flush()
    db.refresh(run)
    path = build_report(db, run)
    run.report_path = str(path)
    return "SUCCESS", f"Processing report generated ({path.name})", dict(file=path.name)


# ----------------------------------------------------------------------------- re-evaluation
def reevaluate(db: Session, run: ProcessingRun, actor: str) -> DecisionRecord:
    rules = get_rules(db)
    ev = Evaluation(db, run, rules)
    ev.vendor()
    p = ev.po()
    for li in run.line_items:
        li.match = next((m for m in p.line_matches if m["line_no"] == li.line_no), None)
    ev.financial()
    ev.duplicates()
    version = (max((d.version for d in run.decisions), default=0)) + 1
    rec = ev.decide(version, "re-evaluation", actor)
    if rec.status == "REJECTED":
        run.final_status, run.final_status_by = "REJECTED", "Decision engine (re-evaluation)"
    db.flush()
    audit(db, run.id, actor, "REEVALUATED", f"Decision v{version}: {rec.headline}",
          {"version": version, "reason_codes": [r["code"] for r in rec.reason_codes]})
    from .report import build_report
    db.refresh(run)
    run.report_path = str(build_report(db, run))
    return rec
