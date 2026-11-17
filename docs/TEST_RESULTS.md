# Test results

Executed 17 Nov 2026 on the build environment (Python 3.13, Tesseract 5.3.4, Chrome via Playwright).
Automated suite: `cd backend && python -m pytest -q` → **29 passed**.

## Scenario results (actual pipeline output, local extractor, no LLM)

| # | Test scenario | Input | Expected | Actual | Pass |
|---|---|---|---|---|---|
| 1 | Clean digital upload, exact match | 01 Apex APX/26-27/0412 | Approved, PO-1001 balance consumed | Approved; all 4 lines matched; ₹40,179 committed | ✅ |
| 2 | Different layout (labels above values) | 02 Brightline | Approved | Approved; fields read via "label-below" at 92% | ✅ |
| 3 | Variance within tolerance stays visible | 03 Coastal +₹1,568 (1.36%) | Approved with variance shown | Approved; `AMOUNT_WITHIN_TOLERANCE` recorded; line price +1.46% warning | ✅ |
| 4 | Tax-inclusive + bundled line | 04 Evergreen | Approved | Approved; tax back-calculated (₹10,968 exact); bundle reconciles PO lines 2+3 | ✅ |
| 5 | Partial billing | 05 Deccan month 1/3 | Approved, balance ₹1,98,240 left | As expected | ✅ |
| 6 | **Edge 2** amount over tolerance | 06 Falcon ₹1,08,000 vs ₹1,00,000 | Needs review, exact variance | Needs review; +₹8,000 (+8.00%), permitted ₹2,000; unapproved line identified | ✅ |
| 7 | **Edge 1** duplicate, new filename | 07 after 01 | Rejected, link to original | Rejected `DUPLICATE_CONFIRMED`, original run linked | ✅ |
| 8 | **Edge 3** scanned, smudged fields | 08 Deccan scan | OCR, no guessing, Needs review | OCR (mean 93%); invoice no. and PO stored as unreadable; PO-1004 suggested at 90%; approval locked | ✅ |
| 9 | **Edge 4** valid PO, wrong vendor | 09 Coastal → PO-1007 | Match refused, both identities shown | Needs review `VENDOR_PO_MISMATCH`; Coastal's PO-1010 suggested | ✅ |
| 10 | Blocked vendor | 10 Horizon | Rejected | Rejected `VENDOR_BLOCKED` | ✅ |
| 11 | Look-alike vendor | 11 "Apex Office Supply Co." | Not treated as Apex | Needs review: look-alike (89% name), bank mismatch, PO vendor mismatch | ✅ |
| 12 | Closed PO | 12 Jaipur PO-1013 | Rejected | Rejected `PO_CLOSED` | ✅ |
| 13 | Foreign currency, no GST | 13 Greenfield USD | Approved | Approved; EIN recognised; reverse-charge rule applied | ✅ |
| 14 | Readable scan | 14 Coastal scan | Approved via OCR | Approved; all critical fields ≥ 86% | ✅ |
| 15 | Same vendor + same amount ≠ duplicate | 08 vs 05 (both ₹99,120) | Not a duplicate | Info note only: "same amount, different invoice" | ✅ |
| 16 | Human review loop | Correct 2 fields on 08 → re-evaluate → approve | v2 Approved, originals kept, PO consumed | As expected; audit shows correction, re-evaluation, approval, PO update | ✅ |
| 17 | Review guardrails | Approve while blocked; bad date; bank edit; unknown reviewer; reject with no reason | 409 / 422 / 403 / 403 / 422 | As expected | ✅ |
| 18 | Upload validation | .txt, fake PDF, empty file, >10 MB | Rejected with a clear message | 400 / 400 / 400 (413 path covered in code) | ✅ |
| 19 | Upload identical file again | Sample 13 bytes renamed | Duplicate by file hash | Rejected `DUPLICATE_CONFIRMED` | ✅ |
| 20 | Configurable policy | `VENDOR_PO_MISMATCH` → REJECT | Sample 09 becomes Rejected | As expected; invalid policies return 422 | ✅ |
| 21 | Report generation | Any completed run | PDF downloads | 200, PDF with decision, evidence, checks, history | ✅ |
| 22 | Persistence | Restart server | Runs retained | 7 runs before = 7 after | ✅ |
| 23 | Dashboard from real data | /api/metrics vs /api/runs | Counts agree | Agree | ✅ |
| 24 | Live view in browser | Upload scanned PDF via file input | Stages progress live | Screenshot mid-run: stage 3 running, 4–9 pending | ✅ |
| 25 | UI end to end | Browser: open decision → PO tab → "Use this PO" | Re-evaluated v2 | v2 shown, PO-1004 accepted | ✅ |

## Not tested
- The Claude (LLM) extraction path against the live API (no key in the build environment).
- Multi-page invoices with tables spanning pages.
- Load beyond a few dozen invoices.
