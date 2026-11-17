"""ORM models. SQLite for the demo; the schema is plain relational and ports to PostgreSQL unchanged."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now():
    return datetime.now()


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    address: Mapped[Optional[str]] = mapped_column(Text)
    state_code: Mapped[Optional[str]] = mapped_column(String(2))
    country: Mapped[str] = mapped_column(String(2), default="IN")
    tax_id: Mapped[Optional[str]] = mapped_column(String(20), unique=True)
    tax_id_type: Mapped[str] = mapped_column(String(10), default="GSTIN")
    email: Mapped[Optional[str]] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(40))
    bank_name: Mapped[Optional[str]] = mapped_column(String(100))
    bank_account: Mapped[Optional[str]] = mapped_column(String(40))
    ifsc: Mapped[Optional[str]] = mapped_column(String(15))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")            # ACTIVE / INACTIVE / BLOCKED
    approval_status: Mapped[str] = mapped_column(String(12), default="APPROVED")  # APPROVED / PENDING / REJECTED
    payment_terms: Mapped[Optional[str]] = mapped_column(String(20))
    onboarded: Mapped[Optional[str]] = mapped_column(String(10))
    notes: Mapped[Optional[str]] = mapped_column(Text)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    po_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True)
    po_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12))           # OPEN / CLOSED / CANCELLED
    currency: Mapped[str] = mapped_column(String(3))
    tax_treatment: Mapped[str] = mapped_column(String(20))    # GST_EXCLUSIVE / NO_TAX
    requester: Mapped[Optional[str]] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text)
    subtotal: Mapped[float] = mapped_column(Float)
    tax_amount: Mapped[float] = mapped_column(Float)
    total_amount: Mapped[float] = mapped_column(Float)
    vendor: Mapped[Vendor] = relationship()
    lines: Mapped[list["POLineItem"]] = relationship(back_populates="po", order_by="POLineItem.line_no",
                                                     cascade="all, delete-orphan")


class POLineItem(Base):
    __tablename__ = "po_line_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(ForeignKey("purchase_orders.po_number"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    unit_price: Mapped[float] = mapped_column(Float)
    tax_rate: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class POConsumption(Base):
    """Amount/quantity billed against a PO by an approved invoice. Drives the remaining balance."""
    __tablename__ = "po_consumptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(ForeignKey("purchase_orders.po_number"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    line_quantities: Mapped[dict] = mapped_column(JSON, default=dict)   # {po_line_id: qty}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    reversed: Mapped[bool] = mapped_column(Boolean, default=False)


class ProcessingRun(Base):
    """One uploaded invoice document and everything that happened to it."""
    __tablename__ = "processing_runs"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="upload")      # upload / sample / history
    sample_id: Mapped[Optional[str]] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), default="QUEUED")      # QUEUED/PROCESSING/COMPLETED/FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    processing_ms: Mapped[Optional[int]] = mapped_column(Integer)          # pure compute time (excludes pacing)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    is_scanned: Mapped[Optional[bool]] = mapped_column(Boolean)
    extraction_method: Mapped[Optional[str]] = mapped_column(String(40))
    # denormalised summary for lists & dashboards (always derived from fields/decision)
    vendor_id: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    vendor_name: Mapped[Optional[str]] = mapped_column(String(200))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(60), index=True)
    invoice_date: Mapped[Optional[str]] = mapped_column(String(10))
    po_number: Mapped[Optional[str]] = mapped_column(String(20))
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    total_amount: Mapped[Optional[float]] = mapped_column(Float)
    content_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    vendor_key: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    system_decision: Mapped[Optional[str]] = mapped_column(String(16))     # engine output
    final_status: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    final_status_by: Mapped[Optional[str]] = mapped_column(String(100))
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[Optional[str]] = mapped_column(String(24))
    error: Mapped[Optional[str]] = mapped_column(Text)
    report_path: Mapped[Optional[str]] = mapped_column(String(500))

    stages: Mapped[list["RunStage"]] = relationship(order_by="RunStage.seq", cascade="all, delete-orphan")
    fields: Mapped[list["ExtractedField"]] = relationship(order_by="ExtractedField.seq", cascade="all, delete-orphan")
    line_items: Mapped[list["InvoiceLineItem"]] = relationship(order_by="InvoiceLineItem.line_no",
                                                               cascade="all, delete-orphan")
    checks: Mapped[list["CheckResult"]] = relationship(order_by="CheckResult.id", cascade="all, delete-orphan")
    decisions: Mapped[list["DecisionRecord"]] = relationship(order_by="DecisionRecord.version",
                                                             cascade="all, delete-orphan")
    reviews: Mapped[list["ReviewAction"]] = relationship(order_by="ReviewAction.id", cascade="all, delete-orphan")


class RunStage(Base):
    __tablename__ = "run_stages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="PENDING")   # PENDING/RUNNING/SUCCESS/WARNING/FAILED/SKIPPED
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSON)


class ExtractedField(Base):
    __tablename__ = "extracted_fields"
    __table_args__ = (UniqueConstraint("run_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    group: Mapped[str] = mapped_column(String(20))           # invoice / vendor / amounts / payment
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[Optional[str]] = mapped_column(Text)        # ORIGINAL extracted value, never overwritten
    raw_text: Mapped[Optional[str]] = mapped_column(Text)     # what the document actually said / OCR read
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[Optional[str]] = mapped_column(String(40))
    source: Mapped[Optional[dict]] = mapped_column(JSON)      # {page, bbox, line, snippet}
    validation_status: Mapped[str] = mapped_column(String(12))   # VALID / UNCERTAIN / INVALID / MISSING
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[list]] = mapped_column(JSON)
    corrected_value: Mapped[Optional[str]] = mapped_column(Text)
    corrected_by: Mapped[Optional[str]] = mapped_column(String(100))
    corrected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    correction_reason: Mapped[Optional[str]] = mapped_column(Text)


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    unit_price: Mapped[Optional[float]] = mapped_column(Float)
    tax_rate: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[Optional[dict]] = mapped_column(JSON)
    match: Mapped[Optional[dict]] = mapped_column(JSON)       # PO line match result


class CheckResult(Base):
    """One validation check. Versioned with the decision it fed into."""
    __tablename__ = "check_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    decision_version: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(20))           # extraction / vendor / po / financial / duplicate
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    outcome: Mapped[str] = mapped_column(String(10))         # PASS / WARN / FAIL / INFO / UNABLE
    message: Mapped[str] = mapped_column(Text)
    expected: Mapped[Optional[str]] = mapped_column(Text)
    actual: Mapped[Optional[str]] = mapped_column(Text)
    difference: Mapped[Optional[str]] = mapped_column(Text)
    rule: Mapped[Optional[str]] = mapped_column(Text)
    reason_code: Mapped[Optional[str]] = mapped_column(String(40))
    evidence: Mapped[Optional[dict]] = mapped_column(JSON)


class DecisionRecord(Base):
    __tablename__ = "decision_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))          # APPROVED / NEEDS_REVIEW / REJECTED
    headline: Mapped[str] = mapped_column(Text)
    reason_codes: Mapped[list] = mapped_column(JSON)         # [{code, effect, message}]
    explanation: Mapped[list] = mapped_column(JSON)          # ordered bullet points
    recommended_action: Mapped[str] = mapped_column(Text)
    checks_summary: Mapped[dict] = mapped_column(JSON)       # {passed, warnings, failed, unable}
    rules_snapshot: Mapped[dict] = mapped_column(JSON)       # config used -> reproducible
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # vendor / PO / duplicate results for display
    blocking: Mapped[list] = mapped_column(JSON, default=list) # codes that must be fixed before approval
    trigger: Mapped[str] = mapped_column(String(40), default="pipeline")   # pipeline / re-evaluation
    created_by: Mapped[str] = mapped_column(String(100), default="Decision engine")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ReviewAction(Base):
    __tablename__ = "review_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    action: Mapped[str] = mapped_column(String(20))   # CORRECT_FIELD/APPROVE/REJECT/REQUEST_INFO/NOTE/REEVALUATE
    reviewer: Mapped[str] = mapped_column(String(100))
    comment: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(24), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Optional[dict]] = mapped_column(JSON)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_by: Mapped[Optional[str]] = mapped_column(String(100))
