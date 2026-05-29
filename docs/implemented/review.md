# Review / Corrections

This document covers the Review page corrections submission flow, how manual modifications are saved, audited, and propagated to the prompt guidance and spatial memory stores.

---

## What it is

The Review page is a three-panel UI showing the original PDF document viewer, the extracted JSON fields, and a list of editable field inputs. 

If Qwen extracts incorrect values, review operators can correct the values in the inputs and draw bounding box overlays directly on the PDF to correct the extraction geometry. 

Saving corrections sends the updated JSON values and coordinate boundaries to the server. The server logs the corrections in an immutable audit table, updates the extraction, and triggers the gold corrections and spatial memory learning loops.

---

## How it works

Manual corrections are saved via `PUT /extractions/{extraction_id}/corrections`.

```
                    [PUT /extractions/{id}/corrections]
                                     │
                                     ▼
                      [Verify User Extraction Access]
                                     │
                                     ▼
                     [Compute Correction Diff Object]
                     (Compare original vs corrected)
                                     │
                                     ▼
                        [Save to extractions Table]
                      (Writes to corrected_result &
                       updates field_locations column)
                                     │
                                     ▼
                         [Log audit review_event]
                                     │
                                     ▼
                     [Trigger Gold Correction Example]
                     (If header fields actually changed)
                                     │
                                     ▼
                           [Save Spatial Memory]
                     (If manual strategies are present)
```

### Step-by-Step Corrections Save Flow

1. **Access Control**: The system calls `assert_extraction_access()` to verify that the calling user owns the extraction's vendor.
2. **Diff Computation**:
   - The server fetches the current extraction and reads the primary `result` (the initial LLM output).
   - It runs `_compute_correction_diff()` to compare `result` against the incoming `corrected_result`.
   - String values are stripped and compared case-insensitively to generate a nested `correction_diff` JSON object:
     ```json
     {
       "field_key": {
         "original": "value_before",
         "corrected": "value_after"
       }
     }
     ```
3. **Persist Corrections**:
   - The server calls `db_mod.save_corrections()`.
   - The `corrected_result` JSON and the `field_locations` coordinate mapping are saved to their respective columns on the `extractions` table.
   - The primary `result` column remains **immutable** for audit logs.
4. **Log Review Event**:
   - The server writes a detailed audit row to the `review_events` table using `db_mod.create_review_event()`.
   - It stores the before/after results, the before/after field locations, the diff object, the actor type (e.g. `"ui"`), the reason code (e.g. `"manual_review"`), and optional reviewer notes.
5. **Gold Example Compilation**:
   - If any top-level header fields were changed (excluding `line_items` and keys ending with `_line_items`), the system automatically compiles a few-shot training example.
   - The diff is saved to `gold_examples` using `db_mod.save_gold_example()`.
6. **Spatial Memory Compilation**:
   - If `field_locations` contains keys with strategy `"manual"` (drag-boxes drawn by the operator), the system normalizes the coordinates and saves them to `spatial_memory`.

---

## Rules & Hard Constraints

- **Immutability**: The initial `result` column on the `extractions` table is immutable. Once the LLM writes it, it is never changed. User corrections are stored strictly in `corrected_result`.
- **Auditing**: Every correction submission must create a `review_event` record. 
- **Telemetry context lookup**: When saving, the server attempts to retrieve any tracing context (`trace_context`) from the document metadata and wraps the manual review log trace in that context to keep logs grouped.
- **Line-item exclusion for Gold Examples**: Prompt gold examples are header-only. Line items and tables are stripped before saving to `gold_examples` to prevent prompt bloat.
- **po_per_page Key Prefix Limitation**: For `po_per_page` split extractions, manual review corrections are stored with prefixed keys (e.g., `doc_0_po_number`). Because these do not match the base template keys (e.g. `po_number`), they are currently ignored by the Vision-LLM prompt compiler and do not function as active few-shot prompt corrections. This is a known current limitation of the prompt-hints system.

---

## All Scenarios in Plain English

### Scenario 1 — Normal correction with value and region updates
- A user changes the invoice number value from `"INV-01"` to `"INV-100"` and draws a new box on the PDF.
- The server saves `corrected_result` and `field_locations` (the invoice number gets strategy `"manual"`).
- A `review_events` row is inserted.
- A `gold_examples` row is inserted showing `"INV-01"` vs `"INV-100"`.
- A `spatial_memory` row is inserted for the new box coordinates.

### Scenario 2 — Save without changes
- A user opens the review page, checks the values, and clicks "Save" without changing any fields.
- The server runs `_compute_correction_diff` and yields an empty dictionary `{}`.
- `corrected_result` is saved.
- A `review_events` row is inserted with `diff` set to `{}`.
- Gold examples and spatial memory writes are skipped (since no changes/manual boxes were added).

### Scenario 3 — Line-item-only correction
- A user fixes a quantity typo inside the line items table. No header fields are touched.
- The server computes the diff.
- `corrected_result` is saved, and a `review_events` row is written.
- Since the diff only contains line items, the gold example write is skipped.

---

## Error Responses

| Situation | HTTP Code | Error Message |
|---|---|---|
| Request missing corrected_result | 400 | `"corrected_result is required"` |
| Target extraction does not exist | 404 | `"Extraction 123 not found"` |
| User does not own extraction | 403 | `"Access denied"` |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_review_api.py`](../../tests/test_review_api.py) | `test_get_extraction_ocr_returns_200_with_payload` | Verifies OCR coordinates retrieval route for the Review page rendering. |
| | `test_save_corrections_requires_corrected_result` | Verifies body validation constraint. |
| | `test_save_corrections_returns_404_for_missing_extraction` | Verifies missing resource handling. |
| | `test_save_corrections_returns_200_on_success` | Verifies that a valid corrections PUT successfully updates DB, logs the event, and writes gold/spatial memory. |

---

## Quick Reference

| Endpoint / Operation | HTTP Method | Target DB Table | Notes |
|---|---|---|---|
| Load PDF OCR geometry | `GET /extractions/{id}/ocr` | `extractions.ocr_data` | Used by review UI canvas |
| Save corrections | `PUT /extractions/{id}/corrections` | `extractions.corrected_result` | Saves JSON + locations |
| Review audit logs | `GET /extractions/{id}/reviews` | `review_events` | List all edits per document |
