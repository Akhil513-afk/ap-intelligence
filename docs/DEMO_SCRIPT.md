# Five-minute demo script

**Before you start (2 min before the call)**
1. Start the app and open http://localhost:8000.
2. *Rules & settings → Reset and load demo history.* This gives 6 runs on the dashboard and 1 item in the review queue.
3. Leave stage pacing at 550 ms. Open *Process invoice* in one tab and *Overview* in another.
Do **not** run sample 01 beforehand, or it becomes the duplicate.

---

### 0:00–1:00 Problem and product (on Overview)
> "An AP team gets hundreds of PDF invoices a month. For each one, somebody reads it, finds the PO, checks the numbers and the vendor, and decides. It's repetitive, and that's where expensive mistakes happen: duplicate payments, paying the wrong vendor, paying over the PO.
> AP Intelligence takes the PDF and produces one of three decisions (approve, review, reject) with the evidence behind it. The design rule I held myself to: **AI reads the document, deterministic rules make the financial decision**, and a human stays in control of anything ambiguous. Everything on this dashboard is computed from the runs; there are 6 historical invoices loaded already."

### 1:00–2:00 Happy path: sample 01
Click **Run** on *01 Clean digital invoice*.
> "Each stage you see is actually executing and being written to the database. I've added a half-second pause between stages so you can follow it; real compute here is about 150 ms.
> Extraction: 17 fields, each with a confidence score. Vendor verified by **GSTIN, checksum-validated**, not by name. PO-1001 matched, the amount equals the remaining balance, all four lines matched on quantity and price. Tax recomputed, IGST is correct because Delhi to Haryana is inter-state. No duplicates. **Approved.** Nothing has been paid; it's eligible for the payment run."
Open the decision → show the *All checks* tab briefly and the *Processing report* download.

### 2:00–3:00 Edge case: amount over tolerance (sample 06)
Run **06 Falcon**.
> "PO-1006 is ₹1,00,000; the invoice is ₹1,08,000. The engine computes +₹8,000, 8% of PO value. The policy is 2% or ₹5,000, whichever is lower, so ₹2,000 is allowed. Outside tolerance, so it goes to **Needs review** rather than reject, because 15% is the hard-reject line. More useful than the number: it found *why*. There's a line, 'additional design revisions', that's not on the PO. The recommended action is to get budget-owner approval or a credit note."
Show the *PO match* tab: PO value, remaining, difference, permitted excess, and the rule text.

### 3:00–4:00 Second edge case: duplicate (07), then scanned (08)
Run **07**: "Same invoice re-sent as a payment reminder, different filename, different file. Matched on vendor, invoice number and amount → **Rejected**, and it links to the original run."
Run **08** (scanned): "No text layer, so it goes through OCR: deskew, denoise, Tesseract. Most fields read at 90%+. The invoice number and PO are smeared, and the system **doesn't guess**: they're recorded as unreadable. It still suggests PO-1004 with reasons: same vendor, same service line, same unit price. It does not auto-select it."
On the run page: *PO match → Use this PO*, then in *Extracted data* correct the invoice number to `DFS/NOV/2026/131`, then **Re-run validation** → v2 approved by the engine → **Approve**. Show the *Audit trail*: original value kept, correction, re-evaluation, approval, PO balance updated.

### 4:00–5:00 Business value and controls
Back to **Overview**, then **Audit log**.
> "Clean invoices go straight through. People only see exceptions, and each exception arrives with its reason and next step. Controls are built in: identity by tax ID, wrong-vendor POs refused, bank details never changed from an invoice, no payments initiated, every action attributed. Rules are configuration: in Settings I can switch the wrong-vendor policy from review to reject without code.
> I'd describe the impact as fewer touches per invoice and fewer leakage errors. I haven't measured time savings against a real AP team, so I won't put a number on it. That's what a pilot would measure."

### Backup runs if asked
09 (wrong-vendor PO), 11 (look-alike vendor), 04 (bundled + GST-inclusive), 13 (USD), 14 (readable scan).

### Expected outputs
| Sample | Final status | Key reason |
|---|---|---|
| 01 | Approved | exact match |
| 06 | Needs review | AMOUNT_OVER_TOLERANCE, LINE_ITEM_UNMATCHED |
| 07 | Rejected | DUPLICATE_CONFIRMED (links to the 01 run) |
| 08 | Needs review → Approved after correction | MISSING_FIELD, PO_MISSING |
| 09 | Needs review | VENDOR_PO_MISMATCH |
