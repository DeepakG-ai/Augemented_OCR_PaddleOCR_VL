# Postprocess Pipeline Stage & Worker

> Source files:
> - [backend/worker.py](../../backend/worker.py) — processing stage orchestrator (`_process_postprocess`)
> - [backend/qwen_layout_apply.py](../../backend/qwen_layout_apply.py) — Qwen3-VL layout box mapper
> - [backend/spatial_memory.py](../../backend/spatial_memory.py) — spatial memory text-snapper
> - [backend/field_mapper.py](../../backend/field_mapper.py) — ERP key mapper

The **Postprocess** stage is the fourth and final step in the document extraction pipeline. It is responsible for mapping layout boundaries, applying spatial memory overrides, remapping keys for ERP integration, and updating the final extraction status and page quota.

---

## What it is

The Postprocess stage transforms raw extraction text into a fully mapped document. It:
- Aligns text values to their actual bounding boxes on the PDF page so the review page can render overlays.
- Re-reads regions corrected by users in past extractions (**spatial memory**) to override wrong LLM outputs.
- Translates vendor-specific key names to canonical ERP field structures (e.g. renaming `invoice_no` to `invoice_number`).
- Atomically releases the tenant's page quota lock.

---

## How it works

The postprocess worker runs as a background process polling the PostgreSQL `jobs` table for tasks of type `postprocess`.

```
        [Claim Postprocess Job]
                  │
                  ▼
         [Load Qwen Layouts]
                  │
                  ▼
      [Build field_locations]
     (Translate relative 0-1.0
      coords to image pixels)
                  │
                  ▼
       [Apply Spatial Memory]
    (Re-read text inside regions
      saved from prior reviews)
                  │
                  ▼
         [Save field_locations]
                  │
                  ▼
        [Apply ERP Remapping]
     (Translate keys, save copy
       to mapped_result column)
                  │
                  ▼
        [Release Page Quota]
                  │
                  ▼
      [Set status = 'done']
```

### Step-by-Step Execution Lifecycle

1. **Job Claiming**: Claims a pending job of type `postprocess` from the database. It locks the row via `FOR UPDATE SKIP LOCKED`.
2. **Layout Application**:
   - Queries `qwen_layout_boxes` for the active layout boundaries learned on Page 1.
   - Denormalizes Qwen's float coordinates `[x0, y0, x1, y1]` (0.0 to 1.0) into pixel bounds using the page width and height.
   - **Header Fields**: Maps layout bounds directly, setting strategy to `"qwen_anchor"`.
   - **Line Items**: Maps column headers. It assigns row indices to page numbers by counting items sequentially from `page_results`, setting strategy to `"qwen_column_header"`.
3. **Spatial Memory Correction Override**:
   - The worker invokes `spatial_memory.apply_to_extraction()`.
   - It searches the `spatial_memory` table for active correction boxes saved from prior reviews of this vendor layout.
   - For each active override, it overlays the correction box on the current page's OCR/pdfium word boxes and re-reads the text within those boundaries.
   - If text is found, it overrides the LLM value in `result` with the re-read text.
4. **ERP Field Mapping**:
   - The worker calls `field_mapper.apply_mapping()`.
   - It reads ERP mapping configurations (`header_map`, `line_map`, and target `output_schemas`) for the vendor.
   - It translates the keys of the extraction result into target canonical fields. The original `result` stays untouched for the Review UI; the renamed copy is saved to `mapped_result`.
5. **Quota Release**:
   - Calls `db_mod.release_quota_once()`.
   - This checks the document's `reserved_pages` metadata, decrements the user's `pending_pages` counter, and deletes `reserved_pages` inside a single transaction to prevent double-releases.
6. **Completion**: Sets extraction status to `done`, progress to `Field mapping complete`, and logs final processing metrics.

---

## Rules & Hard Constraints

- **Re-read, Never Replay**: Spatial memory must never copy the text value corrected in the past. It must store the coordinate bounds, overlay them on the *current* document, and snap words inside the box to read the value fresh. This ensures dynamic fields (like date or PO number) are not overwritten with stale data.
- **Quota Release Idempotency**: Quota release must run inside a transaction with a `FOR UPDATE` lock on the document row. If the job restarts after releasing but before completion, a second release must find `reserved_pages` missing and behave as a no-op, preventing negative page counters.
- **Isolation Boundaries**: ERP field mapping renames keys but does not strip unmapped keys from the primary `result` object. This ensures data is not lost for human review if mapping schemas change.

---

## All Scenarios in Plain English

### Scenario 1 — Postprocessing with active layouts
- Extraction finishes. Page 1 bounding boxes are present in `qwen_layout_boxes`.
- The worker denormalizes boxes into pixel dimensions.
- Header fields get pixel bounds. Line items cells get column header bounds.
- The worker saves results to `field_locations` and marks status `done`.

### Scenario 2 — Postprocessing with empty layout boxes
- The template is new, and Qwen failed to locate labels on Page 1 (boxes were empty).
- `qwen_layout_boxes` queries return nothing.
- The worker skips layout application, sets `field_locations` to an empty dictionary, and continues.
- Fields are still editable in the Review UI, but coordinate overlays are not drawn.

### Scenario 3 — Spatial memory override triggers
- A user previously corrected the invoice number location because Qwen consistently missed it. The correction saved the bounding coordinates to `spatial_memory`.
- During postprocessing, the worker detects the active spatial memory record.
- It overlays the coordinates on the new document's page 1, finds the text words inside (e.g. `"INV-9901"`), updates the extraction result value for `invoice_number`, and sets the field location strategy to `"spatial_memory"`.
- The updated value is saved to the database.

### Scenario 4 — ERP mapping configured
- The vendor template is assigned to the "AP Automation" output schema.
- The vendor has an ERP mapping that maps `po_no` to target `po_number`.
- The postprocess worker renames `po_no` to `po_number`, packages it into the AP schema layout, and writes the resulting JSON to the `mapped_result` column on `extractions`.

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_field_mapper.py`](../../tests/test_field_mapper.py) | `test_apply_mapping_dict` | Verifies that header and line item keys are renamed to schema targets, and unmapped keys are omitted or set to null. |
| | `test_detect_renames` | Verifies that name updates by position are detected cleanly when schema definitions change. |
| [`test_spatial_memory_management.py`](../../tests/test_spatial_memory_management.py) | `test_apply_to_extraction_overrides_value` | Proves that spatial memory re-reads words from the current page OCR/pdfium coordinates and overrides LLM text values. |
| | `test_deactivate_spatial_memory` | Verifies that user corrections can deactivate layout region snaps. |
| [`test_quota_release_idempotent.py`](../../tests/test_quota_release_idempotent.py) | `test_release_quota_once_is_idempotent` | Verifies that double-release calls within a transaction are caught, preventing double quota reductions. |

---

## Quick Reference

| Snapping Strategy | Source Engine | Resulting Action |
|---|---|---|
| `"qwen_anchor"` | Qwen3-VL | Pixel box matching the label word coordinates |
| `"qwen_column_header"` | Qwen3-VL | Pixel box matching the table column header |
| `"spatial_memory"` | Review UI | Region coordinates snaps words from OCR/pdfium bounds |
| `"qwen_column_header_missing"` | n/a | Target column header not found on page |
