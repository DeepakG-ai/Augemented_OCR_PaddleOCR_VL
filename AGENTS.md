# AGENTS.md

This file is the working brief for the next branch cut from `box-vis`.

The goal is to implement a new hybrid PDF pipeline that:

- uses `pypdfium2` for digital PDFs/pages
- uses `PaddleOCR` for scanned PDFs/pages
- auto-detects the vendor/client from the uploaded document
- blocks unknown vendors and asks the user to create a vendor with vendor id
- keeps the current review UI and manual drag-drop correction flow
- turns manual corrections into reusable spatial memory for future documents

Do not discard or overwrite existing `box-vis` work while implementing this.

## Mission

Build a vendor-aware extraction flow where the user can upload any PDF first, the system determines whether each page is digital or scanned, detects the vendor from current document text, then runs extraction and review using the correct text geometry source.

The feature must support known vendors such as:

- Robert Scott
- RJ Schinner
- American Paper and Twine
- Ferguson

If a PDF belongs to a new or unknown client, the system must stop early and raise a clear error telling the user to create the vendor and vendor id first.

## Non-Negotiable Rules

### 1. Digital vs scanned must be decided from the current file

- Run `pypdfium2` first on PDF pages.
- If a page has usable embedded text, treat that page as `digital`.
- If `pypdfium2` returns no usable text, zero words, or text below the configured threshold, treat that page as `scanned`.
- For scanned pages, run `PaddleOCR`.
- This decision should be page-level, not only document-level, so mixed PDFs are supported.

### 2. Use current text, not old corrected values

Manual review reuse must become reusable spatial memory.

The critical rule is:

- store where the field is
- do not store the old field value as the future answer

Example:

- Robert Scott PDF 1:
  - `supplier` region is corrected manually
  - OCR text inside that region = `Fresh Products`
- Robert Scott PDF 2:
  - same layout, same `supplier` region
  - OCR text inside that region = `Bluechip`

Correct behavior:

- reuse the saved `supplier` region as the first-pass search area
- read the new OCR or PDF text inside that region from the current document
- return `Bluechip`, not `Fresh Products`

### 3. Spatial memory is geometry memory, not answer memory

- Save corrected box/region, page, field key, layout identity, and geometry source.
- Do not treat prior corrected text as the next answer.
- If prompt examples are kept, they are only hints for recurring model mistakes.
- The real reused answer must come from the current document text inside the reused region.

### 4. Keep the current review UX

- The existing drag-draw-select review flow must keep working.
- The current review behavior in `frontend/app.js` already writes manual `field_locations`.
- Reuse that interaction pattern instead of building a second review UI.

### 5. Do not persist row-specific line-item values as reusable memory

- Header fields are the first-class target for spatial memory.
- For line items, reuse header or column anchors first.
- Do not assume row `0` from one document maps to row `0` in the next document.

## Current Repo Baseline

These files already contain relevant behavior:

- `backend/pdf_extractor.py`
  - contains a working `pypdfium2` prototype for digital detection and word extraction
- `backend/ocr_runner.py`
  - current `PaddleOCR` word extraction
- `backend/worker.py`
  - normalize, OCR, LLM, postprocess, outbound stages
- `backend/extractor.py`
  - system prompt builder and gold-example injection
- `backend/main.py`
  - review save endpoint and OCR fetch endpoint
- `backend/db.py`
  - tables for vendors, templates, extractions, review events, gold examples
- `backend/qwen_bbox_parser.py`
  - current Qwen anchor/header bbox parser
- `frontend/app.js`
  - current review page, drag-box selection, manual field location save

Important current behaviors:

- Gold examples already exist and are injected into the system prompt.
- Review already stores manual `field_locations`.
- Qwen v3 already returns anchor/header boxes, not value-level boxes.
- PaddleOCR is currently the source of exact value word boxes for review.

What is missing today:

- unified digital/scanned geometry pipeline
- vendor detection before extraction
- unknown-vendor stop flow
- reusable spatial memory table and lookup logic
- reuse of manual geometry across future documents of the same vendor/layout

## Target Architecture

### A. Unified text geometry output

Create one shared page geometry format for both `pypdfium2` and `PaddleOCR`.

Preferred shape:

```json
{
  "page_number": 1,
  "source": "pypdfium" ,
  "char_count": 1234,
  "word_count": 240,
  "words": [
    {"text": "ROBERT", "box": [10, 20, 80, 40], "score": 1.0}
  ]
}
```

Rules:

- `source` must be `pypdfium` or `paddleocr`
- boxes must be in the same image-space coordinates used by review UI
- review and spatial memory lookup should consume the same word-box schema regardless of source

### B. Vendor detection flow

The system should allow upload before vendor is known.

Preferred flow:

1. User uploads PDF.
2. Run early text extraction on **Page 1 only** (to prevent ingestion bottlenecks on large scanned PDFs):
   - `pypdfium2` first
   - if scanned fallback is needed, use `PaddleOCR`
3. Detect vendor from current Page 1 text.
4. Match against known vendors and aliases in DB.
5. If matched:
   - continue extraction using that vendor's template
6. If not matched:
   - stop with structured error
   - tell user to create vendor and vendor id first

Unknown vendor behavior must be explicit. Do not silently auto-create vendors.

### C. Page routing

Page routing must be:

- digital page -> `pypdfium2` words are the geometry source
- scanned page -> `PaddleOCR` words are the geometry source

For mixed PDFs:

- page 1 can be digital
- page 2 can be scanned
- both must still end up in one unified extraction/review payload

### D. Qwen role after this change

Keep Qwen for semantic extraction and anchor/header understanding.

Recommended split:

- Qwen = semantic extraction and anchor/header boxes
- `pypdfium2` or `PaddleOCR` = exact word-level text geometry
- review UI = manual correction and human confirmation
- spatial memory = reusable region hints for future docs

Do not ask Qwen to memorize old corrected values as future truth.

## Spatial Memory Design

### What to store

Add a new persistence layer for reusable manual geometry, for example:

- `vendor_id`
- `layout_key`
- `field_key`
- `page_number`
- `normalized_box`
- `source_engine`
- `created_from_extraction_id`
- `last_verified_at`
- `is_active`

Store normalized coordinates so the same region can be reused on different render sizes.

### What not to store as reusable truth

Do not use this as future answer memory:

- old `matched_text`
- old corrected field value
- old line-item row values

Those can be stored for auditing, but must not be treated as the next answer.

### How reuse should work

For the next document of the same vendor and matching layout:

1. load saved spatial memory for the field
2. convert normalized box to current page coordinates
3. search current page words inside that region
4. read current text from the current document
5. use that as the first-pass candidate
6. if lookup fails, fall back to normal matching or Qwen flow

### Layout identity

Do not key reuse only by `vendor_id`.

Create a `layout_key` or equivalent fingerprint. **Do not use page count**, as a 1-page and 2-page invoice from the exact same vendor should share the same memory. Compute it from a combination of:

- vendor id
- `template_id`
- stable anchor/header positions (hashing the most prominent anchor text)

This prevents one vendor's multiple layouts from sharing the wrong memory.

## Gold Examples vs Spatial Memory

Both can exist, but they serve different purposes.

### Gold examples

Use gold examples for:

- recurring Qwen formatting mistakes
- systematic extraction mistakes
- field naming or normalization guidance

Gold examples already exist in the repo and can stay.

### Spatial memory

Use spatial memory for:

- manual drag-drop corrected header fields
- recurring field regions in repeat vendor layouts
- first-pass region narrowing before normal matching

Preferred rule:

- use spatial memory algorithmically first
- use gold examples as prompt hints only

## Review Flow Requirements

The current review page already supports:

- drawing a rectangle
- collecting matched words
- updating field values
- saving `field_locations`

Build on that.

When a manual correction is confirmed:

- keep saving `corrected_result`
- keep saving `field_locations`
- also write or update spatial memory for eligible fields

Eligible first-pass fields:

- vendor
- supplier
- bill_to
- ship_to
- deliver_to
- po_number
- invoice_number
- date-like header fields

Avoid persisting row-specific line-item cell memory in phase 1.

## Suggested Backend Changes

### 1. Add unified geometry abstraction

Introduce a backend layer that returns page word geometry from either:

- `pypdfium2`
- `PaddleOCR`

This should replace the assumption that review data always comes from PaddleOCR.

### 2. Replace or extend `/extractions/{id}/ocr`

Current review endpoint returns only OCR pages.

The review UI will need either:

- a renamed unified endpoint such as `/extractions/{id}/geometry`
- or the existing endpoint extended to return unified page word geometry from either engine

### 3. Vendor detection before extraction

Current durable ingest path still expects `vendor_id`.

This feature likely requires one of:

- making `vendor_id` optional and detecting it during ingest
- or adding a pre-ingest detection endpoint that resolves vendor before job creation

The new design must support upload-first, detect-second behavior.

### 4. DB additions

Add a new table for spatial memory instead of overloading:

- `gold_examples`
- `review_events`
- `field_locations`

`field_locations` are per-extraction snapshots.
Spatial memory must be reusable across future extractions.

## Suggested Execution Order

1. Create the new feature branch from the current `box-vis` baseline.
2. Promote `backend/pdf_extractor.py` from prototype into reusable production code.
3. Build a unified geometry service for `pypdfium2` and `PaddleOCR`.
4. Add vendor detection from current document text.
5. Change ingest flow so vendor can be detected before extraction starts.
6. Add unknown-vendor blocking behavior.
7. Add spatial memory DB table and CRUD helpers.
8. Save spatial memory from confirmed manual review actions.
9. Reuse spatial memory on the next extraction for the same vendor/layout.
10. Keep gold examples, but do not let them override current-region text.
11. Update tests and review endpoint payloads.

## Acceptance Criteria

The task is complete only when all of the following are true.

### Digital PDF

- A digital Robert Scott PDF uses `pypdfium2` words and boxes.
- Vendor is detected from current PDF text.
- Extraction continues without requiring PaddleOCR for those digital pages.

### Scanned PDF

- A scanned Robert Scott PDF falls back to `PaddleOCR`.
- Vendor is detected from OCR text when `pypdfium2` has no usable text.

### Unknown vendor

- A new unseen client fails with a clear "create vendor and vendor id first" error.
- The system does not silently guess or auto-create a vendor.

### Manual correction reuse

- After correcting a header field in review, a future same-layout document reuses the saved region.
- The next document returns the current text inside that region, not the old corrected value.

### Review compatibility

- Existing draw-box correction UX still works.
- Field overlays still render on the right page.

### Layout safety

- Reuse does not leak across different layouts from the same vendor.

## Good Defaults

- Start with header-field spatial memory first.
- Keep line-item reuse limited to column/header anchors until header-field reuse is stable.
- Prefer deterministic region lookup over prompt-only reuse.
- Normalize saved boxes to page size before persisting.
- Keep page association with every saved region.

## Pitfalls to Avoid

- Storing old corrected values as future answers
- Reusing memory for the wrong layout
- Assuming the whole PDF is digital or scanned when only some pages are
- Breaking the current review page while adding the new geometry source
- Tying spatial memory only to vendor without a layout discriminator
- Persisting line-item row boxes as reusable truth across documents

## Short Version

Implement a hybrid vendor-aware PDF pipeline:

- digital page -> `pypdfium2`
- scanned page -> `PaddleOCR`
- detect vendor from current document text
- unknown vendor -> stop and ask for vendor creation
- manual review -> save geometry memory
- next same-layout doc -> reuse region, read current text, never reuse old value
