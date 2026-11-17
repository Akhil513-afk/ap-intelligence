"""End-to-end and unit tests. Run:  cd backend && python -m pytest -q"""
import json
import os
import tempfile
import time
from pathlib import Path

os.environ["AP_DATA_DIR"] = tempfile.mkdtemp(prefix="aptest_")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import demo  # noqa: E402
from app.config import SAMPLE_DIR  # noqa: E402
from app.main import app  # noqa: E402
from app.pipeline.extract_local import correct_gstin  # noqa: E402
from app.utils import gstin_valid, norm_po, parse_date  # noqa: E402

RV = "Priya Nair (AP Lead)"
MANIFEST = {s["id"]: s for s in json.loads((SAMPLE_DIR / "manifest.json").read_text())}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.put("/api/settings", json={"user": RV, "rules": {"demo": {"stage_pacing_ms": 0}}})
        r = c.post("/api/demo/reset", json={"preload_history": True})
        assert r.status_code == 200 and len(r.json()["history_runs"]) == 6
        yield c


def run_sample(c, sid):
    rid = c.post(f"/api/samples/{sid}/run").json()["run_id"]
    for _ in range(200):
        d = c.get(f"/api/runs/{rid}/live").json()
        if d["run"]["status"] in ("COMPLETED", "FAILED"):
            return c.get(f"/api/runs/{rid}").json()
        time.sleep(0.1)
    raise AssertionError("run did not finish")


# ---------------------------------------------------------------- units
def test_gstin_checksum_and_ocr_correction():
    assert gstin_valid("07AAKCA3157M1ZD")
    assert not gstin_valid("07AAKCA3157M1ZE")
    fixed, changes = correct_gstin("O6AADCD9034H1ZQ")
    assert fixed == "06AADCD9034H1ZQ" and gstin_valid(fixed) and changes


def test_normalisers():
    assert norm_po("P.O. No 1O04") == "PO-1004"
    assert parse_date("03/11/2026").isoformat() == "2026-11-03"   # DD/MM
    assert parse_date("November 2, 2026").isoformat() == "2026-11-02"


# ---------------------------------------------------------------- scenarios (ordered: 07 depends on 01)
ORDER = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14"]
RESULTS = {}


@pytest.mark.parametrize("sid", ORDER)
def test_sample_decision(client, sid):
    d = run_sample(client, sid)
    RESULTS[sid] = d
    assert d["run"]["status"] == "COMPLETED"
    assert d["run"]["final_status"] == MANIFEST[sid]["expected"], d["decision"]["headline"]
    assert all(s["status"] != "PENDING" for s in d["stages"])
    assert Path(client.get(f"/api/runs/{d['run']['id']}/report.pdf").headers["content-type"]).name == "pdf"


def codes(d):
    return {r["code"] for r in d["decision"]["reason_codes"]}


def test_edge1_duplicate_points_to_original(client):
    d = RESULTS["07"]
    assert "DUPLICATE_CONFIRMED" in codes(d)
    assert d["original_run"]["id"] == RESULTS["01"]["run"]["id"]


def test_edge2_variance_exact(client):
    a = RESULTS["06"]["decision"]["context"]["po"]["amount"]
    assert a["difference"] == 8000.0 and a["difference_pct"] == 8.0 and a["classification"] == "OUTSIDE_TOLERANCE"
    assert {"AMOUNT_OVER_TOLERANCE", "LINE_ITEM_UNMATCHED"} <= codes(RESULTS["06"])


def test_within_tolerance_is_visible_not_hidden():
    d = RESULTS["03"]
    assert d["decision"]["context"]["po"]["amount"]["classification"] == "WITHIN_TOLERANCE"
    assert "AMOUNT_WITHIN_TOLERANCE" in codes(d)


def test_edge3_scanned_no_fabrication():
    d = RESULTS["08"]
    f = {x["key"]: x for x in d["fields"]}
    assert d["run"]["extraction_method"] == "ocr"
    assert f["invoice_number"]["original"] is None and f["po_reference"]["original"] is None
    assert f["total_amount"]["value"] == "99120.00"
    assert [c["po_number"] for c in d["decision"]["context"]["po"]["candidates"]][0] == "PO-1004"
    assert d["actions"]["can_approve"] is False


def test_edge4_vendor_po_mismatch():
    d = RESULTS["09"]
    po = d["decision"]["context"]["po"]
    assert po["accepted"] is False and "VENDOR_PO_MISMATCH" in codes(d)
    assert po["candidates"][0]["po_number"] == "PO-1010"


def test_lookalike_not_treated_as_vendor():
    d = RESULTS["11"]
    assert d["run"]["vendor_id"] is None
    assert {"VENDOR_LOOKALIKE", "BANK_DETAILS_MISMATCH"} <= codes(d)


def test_bundle_and_inclusive_tax():
    lm = RESULTS["04"]["decision"]["context"]["po"]["line_matches"]
    assert any(m["status"] == "BUNDLED" for m in lm)


def test_review_flow_correct_reevaluate_approve(client):
    rid = RESULTS["08"]["run"]["id"]
    assert client.post(f"/api/runs/{rid}/review", json={"action": "APPROVE", "reviewer": RV, "comment": "x" * 20}).status_code == 409
    assert client.post(f"/api/runs/{rid}/fields/invoice_date/correct",
                       json={"value": "not a date", "reason": "test", "reviewer": RV}).status_code == 422
    assert client.post(f"/api/runs/{rid}/fields/bank_account/correct",
                       json={"value": "1234567890", "reason": "test", "reviewer": RV}).status_code == 403
    client.post(f"/api/runs/{rid}/fields/invoice_number/correct", json={"value": "DFS/NOV/2026/131", "reason": "paper copy", "reviewer": RV})
    client.post(f"/api/runs/{rid}/fields/po_reference/correct", json={"value": "PO-1004", "reason": "candidate", "reviewer": RV})
    d = client.post(f"/api/runs/{rid}/reevaluate", json={"reviewer": RV}).json()
    assert d["decision"]["version"] == 2 and d["decision"]["status"] == "APPROVED"
    f = {x["key"]: x for x in d["fields"]}
    assert f["invoice_number"]["original"] is None and f["invoice_number"]["corrected"]
    d = client.post(f"/api/runs/{rid}/review", json={"action": "APPROVE", "reviewer": RV, "comment": ""}).json()
    assert d["run"]["final_status"] == "APPROVED"
    po = next(p for p in client.get("/api/purchase-orders").json() if p["po_number"] == "PO-1004")
    assert po["consumed"] == 2 * 99120.0      # Oct (sample 05) + Nov
    types = [a["event_type"] for a in d["audit"]]
    assert {"FIELD_CORRECTED", "REEVALUATED", "APPROVED_BY_REVIEWER", "PO_BALANCE_UPDATED"} <= set(types)


def test_reject_requires_reason_and_unauthorised_blocked(client):
    rid = RESULTS["06"]["run"]["id"]
    assert client.post(f"/api/runs/{rid}/review", json={"action": "REJECT", "reviewer": RV, "comment": ""}).status_code == 422
    assert client.post(f"/api/runs/{rid}/review", json={"action": "NOTE", "reviewer": "Mallory", "comment": "hi"}).status_code == 403
    d = client.post(f"/api/runs/{rid}/review", json={"action": "REQUEST_INFO", "reviewer": RV, "comment": "Need credit note for design revisions"}).json()
    assert d["run"]["final_status"] == "INFO_REQUESTED"


def test_upload_validation(client):
    assert client.post("/api/runs/upload", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400
    assert client.post("/api/runs/upload", files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    assert client.post("/api/runs/upload", files={"file": ("x.pdf", b"", "application/pdf")}).status_code == 400
    data = (SAMPLE_DIR / MANIFEST["13"]["file"]).read_bytes()
    rid = client.post("/api/runs/upload", files={"file": ("renamed.pdf", data, "application/pdf")}).json()["run_id"]
    for _ in range(100):
        d = client.get(f"/api/runs/{rid}/live").json()
        if d["run"]["status"] == "COMPLETED":
            break
        time.sleep(0.1)
    assert d["run"]["final_status"] == "REJECTED"   # identical file to sample 13 -> confirmed duplicate


def test_metrics_from_real_data(client):
    m = client.get("/api/metrics").json()
    runs = client.get("/api/runs").json()
    assert m["total_processed"] == len([r for r in runs if r["status"] == "COMPLETED"])
    assert m["duplicates"] >= 3


def test_settings_validation_and_policy_change(client):
    assert client.put("/api/settings", json={"user": RV, "rules": {"policies": {"DUPLICATE_CONFIRMED": "NONE"}}}).status_code == 422
    r = client.put("/api/settings", json={"user": RV, "rules": {"policies": {"VENDOR_PO_MISMATCH": "REJECT"}}})
    assert r.status_code == 200
    client.post("/api/demo/reset", json={"preload_history": False})
    assert run_sample(client, "09")["run"]["final_status"] == "REJECTED"
    client.put("/api/settings", json={"user": RV, "rules": {"policies": {"VENDOR_PO_MISMATCH": "REVIEW"}}})


def test_persistence_survives_new_session(client):
    from app.db import SessionLocal
    from app.models import ProcessingRun
    with SessionLocal() as s:
        assert s.query(ProcessingRun).count() >= 1
