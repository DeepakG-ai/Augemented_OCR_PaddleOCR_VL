# Spatial Memory

This document covers the design and operations of spatial memory, which enables the system to remember where a field is located on a document layout and reuse its coordinates.

---

## What it is

Spatial memory stores the physical coordinates of header fields that were corrected by human review operators. 

The system prompt does not have templates for document geometry. Instead, spatial memory captures **where** the field belongs on the page (in normalized bounding coordinates). 

**The Golden Rule of Spatial Memory**: The database stores the region coordinates, **never** the historical text values. When processing a new document of the same layout, the system overlays this coordinate box on the current page's OCR/pdfium coordinates and reads the text fresh. This ensures dynamic values like invoice numbers or dates are read correctly.

---

## How it works

Spatial memory operates in two phases: the **Write Path** (saving manual corrections) and the **Read Path** (applying saved coordinates to new extractions).

```
[Write Path: Human Review UI]
              │
              ▼
   (Strategy == "manual" ?)
              │
              ├─── No ────► [Skip: Not a manual region correction]
              │
              ▼ Yes
    [Normalize to 0..1 scale]
              │
              ▼
    [Upsert to spatial_memory]
   (Keyed: vendor_id + layout_key)

============================================================

[Read Path: Postprocess Worker]
              │
              ▼
    [Load Active Memories]
   (Keyed: vendor_id + layout_key)
              │
              ▼
    [Denormalize to Pixel Box]
 (Using current page width & height)
              │
              ▼
     [Snap Words Inside Box]
   (OCR & pdfium word intersection)
              │
              ▼
  (Length >= 2 chars & field matches?)
              │
              ├─── No ────► [Skip: Keep LLM value / empty]
              │
              ▼ Yes
    [Override result value &]
  [Set strategy = 'spatial_memory']
```

### 1. The Write Path (Phase 3)
When a review operator saves changes on the manual Review page, `/extractions/{id}/corrections` triggers `spatial_memory.save_from_corrections()`:
- **Eligible Fields**: It scans the submitted `field_locations`. It only processes dynamic header fields that belong to the active vendor template. Line items are excluded.
- **Manual Filter**: The coordinate box is saved only if its strategy is `"manual"` (drawn by a human user). Bounding boxes returned by Qwen anchors or previous spatial memories are ignored.
- **Coordinate Normalisation**: The system retrieves the actual width and height of the page. It translates the pixel-space box `[x0, y0, x1, y1]` into normalized float boundaries `[0..1]` and upserts the row into `spatial_memory`.

### 2. The Read Path (Phase 4)
During the postprocessing stage, the worker calls `spatial_memory.apply_to_extraction()`:
- **Layout Selection**: It loads the vendor and computes the current document's `layout_key` (derived from `vendor_id` and the page classification).
- **Coordinate Denormalisation**: For each active memory record, the normalized box `[0..1]` is multiplied by the current document's page width and height to yield pixel boundaries.
- **Text Intersection**: It queries the merged page geometry (`ocr_data`). It selects all word-boxes that intersect (even partially) with the pixel boundaries.
- **Reading Order Sorting**: Words are sorted top-to-bottom, then left-to-right, and joined with spaces.
- **Value Override**: If the extracted text is at least 2 characters, the text replaces the LLM-extracted value in `result`, and the field's strategy is updated to `"spatial_memory"` in `field_locations`.

---

## Rules & Hard Constraints

- **Re-Read, Never Replay**: Values must never be cached or copy-pasted. The system must always read the current document words intersecting the saved coordinates.
- **Strategy Guard**: Only `"manual"` locations are stored. This prevents feedback loops where the system records its own Qwen-grounded layout predictions as ground-truth memory.
- **Header Fields Only**: Spatial memory is restricted to top-level header fields (e.g. `invoice_number`). Tables and line items are too dynamic and are skipped.
- **Staleness Guard**: If the snapped text within a memory region contains fewer than 2 characters, the memory is skipped. The LLM value is kept, ensuring that if a field moves entirely on a new document, the system does not overwrite it with whitespace.
- **Template Synchronization**: If a field is removed from a vendor's template, all corresponding `spatial_memory` rows for that field are immediately hard-deleted from the database during `save_template()`.

---

## All Scenarios in Plain English

### Scenario 1 — Successful region snapping
- A vendor's invoice has the PO number in a box at `[100, 200, 250, 220]` on page 1.
- In a previous review, a human corrected the PO number position, saving the normalized region to `spatial_memory`.
- During a new upload, Qwen fails to extract the PO number.
- In postprocessing, the system overlays the coordinates, finds the OCR words `"PO-9901"` inside, overrides the result value with `"PO-9901"`, and maps the field strategy as `"spatial_memory"`.

### Scenario 2 — Skipped due to lack of text (Staleness Guard)
- On a new invoice from the same vendor, the layout changed and the PO number field was moved to the bottom of the page. The old coordinate region is now white space.
- Postprocessing snaps the coordinates of the old region and finds 0 words.
- Since the text length is 0 (< 2), the system skips the override. The LLM value (or null) is preserved.

### Scenario 3 — Multi-page list format (po_per_page)
- A multi-page document is split into one PO per page.
- A user corrects the PO number on page 2, creating a manual correction box.
- The system saves the spatial memory, explicitly binding it to `page_number = 2`.
- When processing the next multi-page document, the region override is applied strictly to page 2's result object, leaving page 1 and page 3 untouched.

---

## Error Responses

Because spatial memory runs asynchronously inside the postprocess worker or is called as a side effect during manual corrections, database failures are logged defensively. If `apply_to_extraction` encounters a database exception, it logs the error and falls back gracefully to the raw LLM output, ensuring the pipeline does not crash.

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_spatial_memory_management.py`](../../tests/test_spatial_memory_management.py) | `test_get_spatial_memory_by_id_returns_entry` | Verifies single spatial memory retrieval. |
| | `test_delete_spatial_memory_by_id_can_remove_prompt_correction` | Verifies deletion cleans up associated gold examples. |
| | `test_delete_gold_correction_field_removes_all_versions` | Verifies clean-up deletes all historic diff versions. |
| | `test_list_vendor_spatial_memory_returns_entries` | Verifies API GET lists all saved memories for a vendor. |
| | `test_delete_spatial_memory_entry_success` | Verifies API DELETE soft/hard removes the memory record. |

---

## Quick Reference

| Action / Operation | Key Fields | Table / Location | Notes |
|---|---|---|---|
| Normalized region keys | `x0`, `y0`, `x1`, `y1` | `spatial_memory.normalized_box` | Float coordinates in `[0..1]` |
| Snapping strategy key | `strategy` | `field_locations[field].strategy` | Set to `"spatial_memory"` |
| Exclude field | `line_items` | n/a | Line items are skipped |
| Cleanup on template change | `field_key` | `spatial_memory` | Deleted if missing in template |
