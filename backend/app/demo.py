"""Demo data management: load master data, reset the environment, pre-load a realistic history."""
from __future__ import annotations

import json
import shutil

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import HISTORY_DIR, REPORT_DIR, SAMPLE_DIR, UPLOAD_DIR
from .db import Base, engine, session_scope
from .models import (AuditEvent, CheckResult, DecisionRecord, ExtractedField, InvoiceLineItem, POConsumption,
                     POLineItem, ProcessingRun, PurchaseOrder, ReviewAction, RunStage, Setting, Vendor)
from .seed_data import PURCHASE_ORDERS, VENDORS
from .utils import money


def init_db():
    Base.metadata.create_all(engine)


def load_master_data(db: Session):
    for v in VENDORS:
        db.merge(Vendor(**v))
    db.flush()
    for po in PURCHASE_ORDERS:
        po = dict(po)
        lines = po.pop("lines")
        sub = sum(money(l["quantity"] * l["unit_price"]) for l in lines)
        tax = sum(money(money(l["quantity"] * l["unit_price"]) * money(l["tax_rate"]) / 100) for l in lines)
        obj = PurchaseOrder(**po, subtotal=float(sub), tax_amount=float(money(tax)), total_amount=float(money(sub + tax)))
        for i, l in enumerate(lines, start=1):
            obj.lines.append(POLineItem(line_no=i, description=l["description"], quantity=l["quantity"], unit=l["unit"],
                                        unit_price=l["unit_price"], tax_rate=l["tax_rate"],
                                        amount=float(money(l["quantity"] * l["unit_price"]))))
        db.add(obj)


def ensure_seeded():
    init_db()
    with session_scope() as db:
        if not db.scalar(select(Vendor).limit(1)):
            load_master_data(db)
            db.add(AuditEvent(actor="System", event_type="DEMO_SEEDED",
                              message=f"Loaded {len(VENDORS)} vendors and {len(PURCHASE_ORDERS)} purchase orders"))


def reset_demo(actor: str, keep_settings: bool = True) -> None:
    with session_scope() as db:
        for model in (CheckResult, DecisionRecord, ReviewAction, RunStage, ExtractedField, InvoiceLineItem,
                      POConsumption, AuditEvent):
            db.execute(delete(model))
        db.execute(delete(ProcessingRun))
        db.execute(delete(POLineItem))
        db.execute(delete(PurchaseOrder))
        db.execute(delete(Vendor))
        if not keep_settings:
            db.execute(delete(Setting))
    for d in (UPLOAD_DIR, REPORT_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    with session_scope() as db:
        load_master_data(db)
        db.add(AuditEvent(actor=actor, event_type="DEMO_RESET",
                          message=f"Demo environment reset: {len(VENDORS)} vendors, {len(PURCHASE_ORDERS)} POs loaded; "
                                  f"all runs cleared" + ("" if keep_settings else "; rules reset to defaults")))


def preload_history(actor: str):
    """Process the historical invoices through the REAL pipeline (no pacing) so dashboards have data."""
    from .pipeline.runner import create_run, process_run
    manifest = json.loads((HISTORY_DIR / "manifest.json").read_text())
    ids = []
    for item in manifest:
        data = (HISTORY_DIR / item["file"]).read_bytes()
        rid = create_run(item["file"], data, source="history", sample_id=item["id"], actor="AP Inbox (history)")
        process_run(rid, pacing_ms=0)
        ids.append(rid)
    with session_scope() as db:
        db.add(AuditEvent(actor=actor, event_type="HISTORY_PRELOADED",
                          message=f"Processed {len(ids)} historical invoices through the live pipeline",
                          data={"runs": ids}))
    return ids


def samples() -> list[dict]:
    m = json.loads((SAMPLE_DIR / "manifest.json").read_text())
    return sorted(m, key=lambda x: x["order"])
