# Interview preparation: AP Intelligence

## 1. The business problem
AP teams match each invoice to a PO and a vendor record, then decide. Cost shows up three ways: **labour** (every invoice touched), **leakage** (duplicates, over-billing, paying a look-alike or fraudulent vendor), and **delay** (missed early-payment terms, vendor friction). The hard part isn't reading a clean invoice. It's the exceptions, and being able to *explain* every decision to an auditor.

## 2. The solution in one paragraph
A nine-stage pipeline turns a PDF into a decision. Extraction (text layer, or OCR for scans; optionally Claude) produces fields with confidence and provenance. Deterministic modules then verify the vendor, match the PO, validate the arithmetic and tax, and check for duplicates. A rules engine maps findings to reason codes, and each reason code to an effect (reject / review / note) through a configurable policy table. Humans handle the exceptions with full evidence, and every action is audited.

## 3. Process architecture
Upload → analyse PDF (text layer or scan?) → extract → vendor → PO → financial → duplicates → decision → report. Each stage is persisted before and after it runs, which drives the live view and the audit trail. Re-evaluation re-runs stages 4–8 on the "effective" invoice (originals overlaid with reviewer corrections) and creates a new decision version.

## 4. Technology choices
- **FastAPI/Python**: the PDF and OCR libraries live in Python; Pydantic validates inputs.
- **SQLite**: zero-setup demo with a relational schema; PostgreSQL is a connection-string change.
- **pdfplumber**: gives word coordinates and font sizes; the layout matters (label → value to the right or below).
- **Tesseract**: free, local, no data leaves the machine. I added deskew, a median filter and binarisation because raw OCR on a 0.8°-skewed scan split table rows.
- **rapidfuzz**: transparent similarity scores I can show a reviewer.
- **React + Tailwind**: fast to build a polished, consistent UI.

## 5. Why AI for extraction
Layouts vary endlessly: "Invoice No", "Bill No.", "Inv. No.", labels above values, tax-inclusive retail formats. Reading documents is a perception problem, which is where models are strong. What I *built and tested* is a deterministic layout-aware extractor plus OCR, as the guaranteed fallback. The Claude path is wired in with two safeguards: **grounding** (a value not found in the document text is capped at 50% confidence) and **agreement** (if model and parser disagree, the field goes to review). I'm upfront that the Claude path wasn't run against the live API in my build.

## 6. Why deterministic rules decide
Payment is a financial control. It has to be reproducible, auditable and explainable to an auditor: "rejected because rule X with threshold Y". An LLM can be confidently wrong and isn't reproducible. So the decision is a pure function of the checks plus the policy, and each decision stores a snapshot of the rules it used.

## 7. Duplicate detection
Three signals, compared only against earlier runs that aren't themselves duplicates: identical file hash; same vendor + normalised invoice number (confirmed if the amount also matches, "possible" if it differs, since it might be a revised invoice); same vendor + date + total + line amounts under a different number. **Same vendor and same amount alone is not a duplicate.** Monthly retainers look exactly like that, so the system shows it as information and explains why it didn't flag it.

## 8. PO matching
Exact lookup on the normalised reference (OCR fixes like O→0). The PO is accepted **only if it belongs to the verified vendor**. The amount is compared with the *remaining* balance, because partial billing is normal. Lines are matched by description similarity, then checked on quantity and unit price (GST-inclusive prices are back-calculated). One invoice line can reconcile several PO lines as a bundle. If the reference is missing, wrong or another vendor's, candidates are scored with reasons and never auto-selected.

## 9. Confidence scores
Field confidence = placement score (inline / right / below the label) × OCR word confidence. It's then adjusted by independent cross-checks: GSTIN checksum, subtotal + tax = total, Σ lines = subtotal, header name = signature block. Critical fields under the threshold (80%, configurable) route to review. A reviewer correction has confidence 1.0 and is marked human-verified; the original stays.

## 10. The edge cases and why I chose them
1. **Duplicate re-sent under a new filename**: the most common source of AP leakage.
2. **Amount over tolerance**: shows tolerance policy, and that variances inside tolerance are still displayed.
3. **Scanned invoice with unreadable fields**: tests the "never fabricate" principle and the human loop.
4. **Valid PO, wrong vendor**: a fraud and mistake pattern that simple matching misses.
Plus: look-alike vendor with different bank details (impersonation), blocked vendor, closed PO, bundled lines with tax-inclusive pricing, and USD.

## 11. Human review
The queue shows each exception with its reason and next step. Reviewers correct header fields (originals preserved), pick a candidate PO, re-run validation, then approve / reject (reason required) / request info / add notes. Approval over judgement calls requires a written justification. Identity/control failures *lock* approval until they're fixed.

## 12. Security and privacy
Local OCR by default. API keys stay in server environment variables. Bank account numbers are masked in the UI, reports and logs. Bank details can't be edited from an invoice. Uploads are validated (extension, PDF magic bytes, size). Run IDs are pattern-checked. Reviewer actions are attributed. The audit log is append-only by design (no delete endpoint). Gaps for production: real authentication/RBAC, encryption at rest, retention policy, and a DPA if a hosted LLM is used.

## 13. Current limitations
Heuristic extractor tested on 5 layouts; LLM path not live-tested; no stitching of tables across pages; reviewer picker instead of auth; single worker; no email ingestion; no GST-portal or e-invoice IRN verification; header-only corrections; no FX conversion.

## 14. What I'd do for production
Email/SFTP ingestion; LLM extraction with grounding plus an evaluation set of real invoices and accuracy tracked per field; GSTIN status lookup and IRN QR validation; three-way match with goods receipts; ERP integration (export approved invoices to the payment run); SSO/RBAC with approval limits by amount; queue workers with DB uniqueness on (vendor, invoice number); monitoring on exception rates and extraction confidence drift.

## 15. Scaling to hundreds a month
Hundreds a month is about 20–40 a day. The current single worker processes a digital invoice in about 0.15 s and a scan in about 2.5 s of compute, so volume isn't the constraint at that scale. The real constraint is *exception rate*, and that's what the dashboard tracks. For thousands: a job queue with N workers, PostgreSQL, object storage for files, and a unique constraint for duplicate safety under concurrency.

---

## Likely questions and answers

1. **Why not just let GPT/Claude decide?** Payment decisions must be reproducible and auditable. I use the model where it's strong (reading) and keep the decision in rules I can show an auditor, with the exact threshold that fired.
2. **What happens if extraction is wrong but confident?** Independent cross-checks catch most of it: the arithmetic must reconcile, the GSTIN checksum must pass, the vendor must match by tax ID, and the PO must match on amount and lines. A wrong total breaks subtotal + tax = total or the PO comparison and goes to review.
3. **How did you set the 2% / ₹5,000 tolerance?** It's an illustrative, common style of policy: percentage for small POs, an absolute cap for large ones, whichever is lower. It's configurable, because the real number belongs to the finance controller.
4. **Why is an 8% overage "review" and not "reject"?** Small overages often have legitimate causes (extra scope, freight). Above the hard limit (15%) it's rejected. Both thresholds and effects are configurable.
5. **How do you avoid false duplicate rejections?** Confirmation requires the invoice number *and* the amount for the same vendor, or an identical file. Same amount alone is just noted, as with Deccan's two monthly invoices of ₹99,120.
6. **What if the vendor legitimately re-issues a corrected invoice with the same number?** Different amount + same number → "possible duplicate" → review, not reject.
7. **Why identify vendors by tax ID, not name?** Names vary ("COASTAL LOGISTICS LLP" vs "Coastal Logistics") and can be imitated. My look-alike sample scores 89% on name but has a different GSTIN and bank. Name-only matching would pay a fraudster.
8. **What's the GSTIN checksum doing for you?** It's self-verifying: a 15-character code with a check digit. OCR read "O6AADCD…" (letter O). I correct by position, since the first two characters must be digits, and accept the fix only if the checksum then passes.
9. **How does the scanned edge case work end to end?** No text layer is detected → page rendered at 300 dpi → deskew, denoise, binarise → Tesseract with word confidences → the label-anchored extractor. The smudged fields come back as junk with confidence near 0 and are recorded as unreadable. Candidates are suggested; a human confirms.
10. **Can a reviewer just approve anything?** No. Judgement-call items can be overridden with a written justification. Identity and control failures lock approval until fixed and re-evaluated.
11. **How do you know your extractor generalises?** I don't, beyond the 5 layouts I tested, and I'd rather say so. In production I'd use the LLM path, build a labelled set of the client's real invoices, and track per-field accuracy. The design makes failure safe: low confidence means review, not a wrong payment.
12. **Where's the human in the loop, exactly?** Anything not provably clean, plus payment release itself. The system never initiates payment.
13. **What's "automation rate" on your dashboard?** The share of completed runs decided by the engine without a human: auto-approved or auto-rejected. It's computed from the runs, not estimated.
14. **What did you deliberately not build?** Email ingestion, ERP integration, auth. All are integration work that doesn't change the decision logic, and I prioritised a reliable end-to-end flow.
15. **Biggest technical challenge?** OCR on skewed scans breaking table rows. Projection-profile deskew fixed row grouping; then calibrating confidence so smudged fields fall below the threshold while clean ones stay above it.
16. **How would you integrate with an ERP?** Read POs and vendors from the ERP (or a nightly sync). Push approved invoices as payable vouchers with the reason codes attached. The PO "consumption" table becomes the ERP's invoiced-quantity data.
17. **How would you handle 3-way matching?** Add a goods-receipt table, and compare invoice quantity with *received* quantity rather than ordered quantity. It's the same check structure with a new data source.
18. **How do rule changes affect history?** They don't rewrite it. Each decision stores the rules snapshot it used, and the change itself is in the audit log with before/after values.
19. **What about data privacy with an LLM?** Off by default; the local path sends nothing out. If enabled, only the invoice text/image is sent, under the provider's data terms; I'd want a DPA and zero-retention settings.
20. **What would you measure in a pilot?** Touch rate, exception reasons, time from receipt to decision, duplicates and overpayments prevented, and per-field extraction accuracy against reviewer corrections. I'd measure these rather than claim savings upfront.
21. **Why SQLite?** It's zero-setup for the demo. The schema is plain relational and maps one-to-one to PostgreSQL.
22. **How does partial billing work?** Each approved invoice records a PO consumption (amount and line quantities). The next invoice is compared against the remaining balance, so the third month of a 3-month PO is exact, and a fourth would exceed it.

## Follow-ups to expect
- "Show me the code for the decision." → `backend/app/pipeline/decision.py`, about 100 lines.
- "Change a rule live." → Settings → set the policy for "PO belongs to a different vendor" to Reject → re-run 09.
- "What breaks it?" → An unseen layout with no recognisable labels: fields come back missing and the invoice goes to review. It's safe, but not automated; that's the case for the LLM path.
