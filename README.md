# AP Intelligence: AI invoice processing and decision engine

**From invoice upload to an explainable AP decision.** Zamp AI Solutions Analyst case study, PS-1 (Finance / Accounts Payable).

A vendor invoice PDF goes in (digital or scanned). Nine persisted stages run: intake, PDF analysis, OCR/extraction, vendor verification, PO matching, financial validation, duplicate detection, decision, report. Out comes **APPROVED / NEEDS REVIEW / REJECTED**, with reason codes, evidence, the rule applied, and a recommended next action. Reviewers can correct fields (originals are preserved), pick a suggested PO, re-run validation, approve, reject or request information. Every step goes into an append-only audit log.

---

## 1. Run it

**Requirements:** Python 3.11+, Node 18+, Tesseract OCR and Poppler (`pdftoppm`).

```bash
# Ubuntu/Debian: sudo apt install tesseract-ocr poppler-utils
# macOS:         brew install tesseract poppler
./start.sh                      # installs deps, builds UI, serves http://localhost:8000
```

Manual equivalent:

```bash
pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
cd backend && uvicorn app.main:app --port 8000
```

Open **http://localhost:8000**. On first start the vendor master and PO register load automatically.
**Load the demo history:** go to *Rules & settings → Reset and load demo history*. This processes 6 older invoices through the real pipeline so the dashboard has data.

UI development with hot reload: `cd frontend && npm run dev` (port 5173, proxies `/api` to 8000).

**Docker / hosted link** (Render, Railway, Fly etc.): `docker build -t ap-intelligence . && docker run -p 8000:8000 -v apdata:/data ap-intelligence`. Mount `/data` as a persistent volume so the SQLite DB and uploads survive redeploys.

**Optional AI extraction:** set `ANTHROPIC_API_KEY` (see `.env.example`). Without a key the app runs fully on the local parser + Tesseract. *Settings* shows which engine is active.

**Tests:** `cd backend && python -m pytest -q` (29 tests, about 16 s).

**Regenerate sample PDFs:** `python sample_data/generate_invoices.py`.

---

## 2. Architecture

```
 React + TS + Tailwind (Vite)  ──poll /api/runs/{id}/live──►  FastAPI
   Overview · Process · Review queue · History · POs · Vendors · Audit · Settings
                                                             │
          ┌──────────────────────── pipeline (single worker thread) ────────────────────────┐
          │ 1 upload → 2 analyse PDF → 3 extract (text layer | deskew+denoise+Tesseract OCR) │
          │          [optional Claude extraction, grounded + cross-checked]                  │
          │ 4 vendor → 5 PO match → 6 financial → 7 duplicates → 8 DECISION (rules) → 9 report│
          └───────────────────────────────────────────────────────────────────────────────────┘
                                                             │
                     SQLite (WAL) via SQLAlchemy: vendors, POs, PO lines, PO consumptions, runs,
                     stages, extracted fields, invoice lines, checks, decisions, reviews, audit, settings
```

| Layer | Choice | Why |
|---|---|---|
| Backend | Python FastAPI | Python owns the PDF/OCR ecosystem; typed request validation; one process serves API + UI |
| Storage | SQLite + SQLAlchemy 2 | Zero setup for the demo; plain relational schema, so moving to PostgreSQL is a connection-string change |
| PDF text | pdfplumber | Word-level coordinates and font sizes, needed for layout-aware extraction |
| OCR | Tesseract 5 + Poppler, with projection-profile deskew, median denoise and binarisation | Free, local, no data leaves the machine; word-level confidences feed field confidence |
| Matching | rapidfuzz | Fast, explainable similarity scores |
| AI (optional) | Claude via tool-use JSON schema | Reads arbitrary layouts; output is grounded and cross-checked, never trusted blindly |
| Reports | ReportLab | Deterministic, offline PDF generation |
| Frontend | React 18, TypeScript, Tailwind 4, Recharts, IBM Plex | Fast build; Plex has tabular figures for financial data |

**Key principles as implemented**
- **AI reads, rules decide.** `pipeline/decision.py` is a pure function of check results plus a policy table. No model output can approve or pay an invoice.
- **No fabrication.** If a label is found but its value is unreadable, the field is stored as missing with the raw OCR text attached. Totals are never back-filled from subtotal + tax.
- **Originals are immutable.** Corrections live in `corrected_value`; the extracted `value` never changes. Every decision version keeps its own checks and a snapshot of the rules used.
- **Visible, recoverable failures.** A stage exception marks the run FAILED with the error; later stages are marked SKIPPED and *Retry* reprocesses the run. Runs interrupted by a restart are marked failed on startup.
- **Stage pacing is honest.** A configurable pause between stages (default 550 ms) makes the live view watchable. It is excluded from all reported timings, and set to 0 it disappears.
- **Controls.** No payment execution; bank details cannot be edited from an invoice; reviewers are named and every action is audited; account numbers are masked in the UI and reports.

Code map: `backend/app/pipeline/` holds `document.py` (PDF analysis, OCR), `extract_local.py`, `extract_llm.py`, `vendor.py`, `po_match.py`, `financial.py`, `duplicates.py`, `decision.py`, `report.py`, `runner.py` (orchestration and re-evaluation). `rules.py` holds the configurable rules and reason codes. `main.py` is the API.

---

## 3. Decision rules (defaults; editable in *Rules & settings*)

Final status: **any REJECT-effect reason → REJECTED; else any REVIEW-effect reason → NEEDS REVIEW; else APPROVED.**

| Area | Rule | Default effect |
|---|---|---|
| Duplicates | Same file hash, or same vendor + invoice number + amount | REJECT |
| | Same vendor + number but different amount; or same vendor/date/total/lines under another number | REVIEW |
| | Same vendor + same amount only | not a duplicate (shown as info) |
| Vendor | Identity = tax ID (GSTIN checksum-validated / EIN). Blocked or inactive vendor | REJECT |
| | Not in master; look-alike name with a different tax ID; onboarding pending; no tax ID on invoice | REVIEW |
| | Bank details differ from vendor master (never updated from an invoice) | REVIEW |
| PO | Missing / unknown / unreadable PO: candidates suggested with scores, **never auto-selected** | REVIEW |
| | PO belongs to another vendor: match refused | REVIEW (configurable to REJECT) |
| | PO closed or cancelled | REJECT |
| Amount | Compared with the PO's **remaining** balance. Excess allowed: lower of **2% of PO value** or **₹5,000** | within → approve, variance shown |
| | Beyond tolerance | REVIEW |
| | Beyond **15%** | REJECT |
| Lines | Description similarity ≥ 70; qty ≤ remaining qty; unit price within ±2% (GST-inclusive prices are back-calculated); one invoice line may reconcile several PO lines (bundle) | REVIEW on failure |
| Financial | qty × price = line; Σ lines = subtotal (or total if inclusive); subtotal + tax = total; tax recomputed from rates; IGST vs CGST+SGST from vendor/buyer state codes; date sanity | REVIEW on failure |
| Extraction | Critical field below the **80%** confidence threshold or missing | REVIEW |

Outcome vocabulary on every check: **exact match / within tolerance / outside tolerance / unable to validate**, each with expected, actual, difference and rule.

**Approval lock:** a reviewer can approve over judgement-call items (tolerance, low confidence) with a written justification. Identity and control failures (wrong-vendor PO, missing PO, look-alike, bank mismatch, unapproved vendor, missing mandatory field) must be fixed and re-evaluated first.

---

## 4. Demo data (all fictional)

- 10 vendors, including a blocked one, one pending onboarding, and one US vendor billing in USD with an EIN
- 17 POs with line items, including one closed PO; 4 POs are used only by the history set
- 14 demo invoices plus 6 history invoices across **5 vendor layouts**, 2 of them scanned (no text layer)

The invoices are generated from the vendor side by `sample_data/generate_invoices.py`; the backend never reads expected answers. Data files: `sample_data/vendors.csv`, `purchase_orders.csv`, `master_data.json`, `invoices/manifest.json`.

| # | Scenario | Expected |
|---|---|---|
| 01 | Clean digital, exact PO match (Apex, PO-1001) | Approved |
| 02 | Different layout, labels above values (Brightline, PO-1002) | Approved |
| 03 | +1.36% variance, inside tolerance and shown | Approved |
| 04 | GST-inclusive prices + bundled line (Evergreen, PO-1005) | Approved |
| 05 | Partial billing, month 1 of 3 (Deccan, PO-1004) | Approved |
| **06** | **Edge 2:** ₹1,08,000 vs PO ₹1,00,000 (8%) plus an unapproved line | Needs review |
| **07** | **Edge 1:** sample 01 re-sent with a new filename and a stamp (run after 01) | Rejected |
| **08** | **Edge 3:** scanned; invoice no. and PO smudged | Needs review |
| **09** | **Edge 4:** Coastal quotes PO-1007, which belongs to Brightline | Needs review |
| 10 | Blocked vendor | Rejected |
| 11 | Look-alike vendor: different GSTIN and bank | Needs review |
| 12 | Closed PO | Rejected |
| 13 | USD invoice, no GST | Approved |
| 14 | Readable scan, OCR happy path | Approved |

---

## 5. Known limitations (honest list)

- **The local extractor is heuristic.** It is label-anchored and layout-aware, and tested on 5 layouts plus 2 scan qualities. Unseen layouts may be read with lower confidence or missed fields. They then go to review rather than being guessed. The Claude path exists for that case but **was not exercised against the live API in this build** (no key available in the build environment); its grounding/merge logic is code-reviewed, not live-tested.
- Single-page focus: multi-page invoices are read page by page, but line tables that continue across pages are not stitched.
- Reviewer identity is a picker, not real authentication or role-based access. Production needs SSO/RBAC and segregation of duties.
- One worker thread processes runs sequentially, which keeps duplicate detection deterministic. Scale-out needs a queue plus DB-level uniqueness/locking.
- No email inbox ingestion: intake is upload or sample. An IMAP or Graph webhook would feed the same `create_run`.
- GST logic covers IGST vs CGST+SGST by state code and recomputes tax. It does not cover TDS, e-invoice IRN/QR validation, or GST-portal lookups of GSTIN status.
- Line-item corrections are not editable in the UI (header fields are).
- FX conversion is not applied: USD invoices are compared with USD POs only, and dashboard totals are per currency.

See `docs/TEST_RESULTS.md`, `docs/DEMO_SCRIPT.md` and `docs/INTERVIEW_PREP.md`.
