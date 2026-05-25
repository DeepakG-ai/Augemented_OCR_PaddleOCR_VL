# Spatial Memory — Reusable Geometry from Human Corrections

> Source file: [backend/spatial_memory.py](../../backend/spatial_memory.py)
>
> Layout key: [backend/layout_key.py](../../backend/layout_key.py)
>
> Schema: [db.md](db.md) — `spatial_memory` table

---

## The one rule (from AGENTS.md)

> **Store WHERE a field is, never WHAT the old value was.**
>
> On reuse, always read the current document's text inside the saved region.

This rule prevents a class of errors where stored corrections become stale truth. If we stored "the corrected po_number was 12345" and the same vendor sent a new document with "po_number: 67890" in that same region, we'd incorrectly overwrite 67890 with 12345. By storing only the *region*, we learn "values for this field always live in this box" — and we always read fresh values from the new document.

---

## Two phases

```
┌─────────────────────────┐                     ┌──────────────────────────┐
│ Phase 3: WRITE          │                     │ Phase 4: READ            │
│ (review save flow)      │                     │ (postprocess flow)       │
│                         │                     │                          │
│ User drags a box on a   │                     │ For each saved memory:   │
│ field in the review UI  │                     │   1. Convert normalized  │
│        ↓                │                     │      box → pixel coords  │
│ POST /extractions/{id}/ │                     │   2. Find current words  │
│   corrections           │                     │      inside that box     │
│        ↓                │                     │   3. Override field      │
│ save_from_corrections() │                     │      value in result     │
│   normalizes box        │                     │   4. Update field_       │
│   upserts row in        │                     │      locations          │
│   spatial_memory        │                     │                          │
└─────────────────────────┘                     └──────────────────────────┘
       (write once,                                     (read forever,
        rarely)                                           every extraction)
```

---

## The `spatial_memory` row

```sql
CREATE TABLE spatial_memory (
    id                          SERIAL PRIMARY KEY,
    vendor_id                   TEXT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    layout_key                  TEXT NOT NULL,    -- "vendor_id:template_id"
    field_key                   TEXT NOT NULL,    -- e.g. "po_number"
    page_number                 INT  NOT NULL,
    normalized_box              JSONB NOT NULL,    -- {x0,y0,x1,y1} in [0..1]
    source_engine               TEXT NOT NULL,    -- 'pypdfium' or 'paddleocr'
    created_from_extraction_id  INT REFERENCES extractions(id) ON DELETE SET NULL,
    last_verified_at            TIMESTAMPTZ DEFAULT NOW(),
    is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(vendor_id, layout_key, field_key, page_number)
);
CREATE INDEX spatial_memory_lookup_idx ON spatial_memory (vendor_id, layout_key, is_active);
```

**Why `layout_key` instead of just `template_id`?** A vendor may have multiple document layouts that share one template. The layout key is computed from `vendor_id:template_id` and (in some configurations) page geometry signals — see `layout_key.compute_layout_key`. This prevents cross-layout bleed: a memory saved while reviewing Layout A doesn't apply to Layout B even though both are the same vendor.

**`UNIQUE(vendor_id, layout_key, field_key, page_number)`**: one row per field per page per layout. Re-saving with the same key updates the existing row (`upsert_spatial_memory` uses `ON CONFLICT DO UPDATE`).

**Header-only**: line items are explicitly excluded — see `_is_reusable_header_field` below. Per AGENTS.md, line-item row geometry is too volatile to memoize.

---

## Phase 3 — Write (`save_from_corrections` lines 160–302)

Called from the review save endpoint after `save_correction_event` records the audit log.

### 3.1 — Resolve metadata

```python
async def save_from_corrections(pool, extraction_id, field_locations, corrected_result):
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        return 0
    
    vendor_id = extraction["vendor_id"]
    template_id = extraction["template_id"]
    page_results = extraction.get("page_results") or []
    
    if not vendor_id:
        return 0    # cannot save memory without a vendor
    
    lk = compute_layout_key(vendor_id, template_id, page_results)
```

If there's no `vendor_id` (rare — would be a malformed extraction row), bail with 0 saved.

### 3.2 — Load page dimensions

```python
pages = await db_mod.get_pages(pool, extraction_id)
page_dims = {p["page_number"]: (p["width"], p["height"]) for p in pages}
page_sources = {p["page_number"]: p["source"] for p in pages}
```

We need actual page pixel dimensions to normalize the box coordinates. `source` (pypdfium vs paddleocr) is recorded with the memory — when applying later, we can prefer memories whose source matches the current page (less drift).

### 3.3 — Filter to reusable header fields only

```python
configured_header_fields = await _load_configured_header_fields(pool, extraction, vendor_id)
```

Loads the current header fields from the extraction snapshot, falling back to the live template. This means a field that's been removed from the template since the extraction ran won't be saved as memory (defends against orphaned memories for deleted fields).

```python
def _is_reusable_header_field(field_key, configured_header_fields) -> bool:
    if not isinstance(field_key, str) or not field_key.strip():
        return False
    if field_key == "line_items" or field_key.startswith("line_item_"):
        return False  # never save line items as memory
    if configured_header_fields:
        return field_key in configured_header_fields
    return True
```

### 3.4 — Filter to manual review boxes only

```python
strategy = str(loc.get("strategy") or "").lower()
if strategy != "manual":
    plog.event("spatial_memory_save_skipped", reason="not_manual_review_box")
    continue
```

`field_locations` may contain entries with various `strategy` values:
- `"manual"`: human dragged a box in the review UI.
- `"qwen_layout"`: auto-mapped from BBox Agent. Already deterministic from the layout — no need to memoize.
- `"spatial_memory"`: applied from a previous memory. Re-saving would be a no-op.

Only `"manual"` is reusable. This is the explicit human-in-the-loop signal — the user looked at the document and confirmed "this region is where this field belongs."

### 3.5 — Normalize and upsert

```python
box = loc["box"]                     # pixel coords
page_num = loc.get("page", 1)
dims = page_dims.get(page_num)
if not dims or dims[0] <= 0 or dims[1] <= 0:
    continue

normalized = _normalize_box(box, dims[0], dims[1])
if not normalized:
    continue

source_engine = "pypdfium" if page_sources.get(page_num) == "pypdfium" else "paddleocr"

await db_mod.upsert_spatial_memory(
    pool, vendor_id=vendor_id, layout_key=lk, field_key=field_key,
    page_number=page_num, normalized_box=normalized, source_engine=source_engine,
    created_from_extraction_id=extraction_id,
)
```

`_normalize_box` (lines 87–115) accepts both list `[x0,y0,x1,y1]` and dict `{x0,y0,x1,y1}` shapes. Output is always a dict with values in `[0..1]`, rounded to 6 decimals.

The `source_engine` is recorded but currently informational only — the apply step doesn't filter by source. Future enhancement: prefer memories whose source matches the current page.

---

## Phase 4 — Read (`apply_to_extraction` lines 466–643)

Called from `_process_postprocess` (worker.py line 920) after `field_locations` are built.

### 4.1 — Load extraction + memories

```python
async def apply_to_extraction(pool, extraction_id, result, field_locations, page_geometry=None):
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        return result, field_locations, 0
    
    if isinstance(result, list):
        return await _apply_to_po_per_page(...)    # special-case for po_per_page format
    
    vendor_id = extraction["vendor_id"]
    template_id = extraction["template_id"]
    lk = compute_layout_key(vendor_id, template_id, extraction.get("page_results") or [])
    
    memories = await db_mod.get_spatial_memory_for_layout(pool, vendor_id, lk)
    if not memories:
        return result, field_locations, 0
```

Querying by `(vendor_id, layout_key)` returns only the memories for this exact layout — no cross-vendor or cross-layout bleed.

### 4.2 — Iterate memories

```python
configured_header_fields = await _load_configured_header_fields(pool, extraction, vendor_id)
pages = await db_mod.get_pages(pool, extraction_id)
page_dims = {p["page_number"]: (p["width"], p["height"]) for p in pages}

if page_geometry is None:
    page_geometry = extraction.get("ocr_data") or []

words_by_page = {entry["page_number"]: entry["words"] for entry in page_geometry}

applied = 0
for mem in memories:
    field_key = mem["field_key"]
    if configured_header_fields and field_key not in configured_header_fields:
        continue   # field no longer in template — skip stale memory
    
    page_num = mem["page_number"]
    normalized = mem["normalized_box"]
    
    dims = page_dims.get(page_num)
    if not dims or dims[0] <= 0 or dims[1] <= 0:
        continue   # page doesn't exist in current document
```

Two safeguards:
1. **Stale memory for removed field**: skip silently. The memory row stays; if the field is re-added later it will reactivate.
2. **Missing page**: a single-page document but a memory was saved for page 3 (because the original was multi-page). Skip silently.

### 4.3 — The core operation: read current text inside the saved region

```python
pixel_box = _denormalize_box(normalized, dims[0], dims[1])
matched_words = _reading_order(_words_in_box(words_by_page.get(page_num, []), pixel_box))
current_text = " ".join(w.get("text", "") for w in matched_words).strip()
```

This is the single most important block in the file. We:
1. **Denormalize** the saved box to pixel coords using *this document's* page dimensions.
2. **Find words** whose center falls inside that box (`_words_in_box` uses center-point containment, which handles partial overlaps gracefully).
3. **Read in reading order** (top-to-bottom, then left-to-right within rows of ~10px tolerance).
4. **Join with spaces** to form the current text.

**`_words_in_box` (lines 132–144)**:

```python
def _words_in_box(words, box):
    x0, y0, x1, y1 = box
    matched = []
    for w in words:
        wb = w.get("box", [])
        if len(wb) != 4: continue
        cx = (wb[0] + wb[2]) / 2
        cy = (wb[1] + wb[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            matched.append(w)
    return matched
```

Center-point containment over edge-overlap is intentional. A word that's half inside / half outside a box gets included if its center is inside. This forgives slight box drift and handles wide words (long emails, addresses).

### 4.4 — Staleness guard

```python
if len(current_text) < 2:
    plog.event("spatial_memory_skipped", reason="no_current_text_in_saved_box")
    continue
```

If the box is empty in the current document (vendor changed their template, layout moved, page rotated), don't override — keep Qwen's answer. This is the safety net: a stale memory that no longer points at any text is functionally inert.

### 4.5 — Override the result

```python
if isinstance(result, dict) and field_key in result:
    old_val = result[field_key]
    result[field_key] = current_text
    plog.event("spatial_memory_overrode_field", old_value=old_val, new_value=current_text)
elif isinstance(result, dict):
    result[field_key] = current_text   # add field if missing from result

# Update field_locations to reflect the spatial memory hit
field_locations[field_key] = {
    "page": page_num,
    "box": pixel_box,
    "strategy": "spatial_memory",   # marks it as memory-derived
    "confidence": "high",
    "matched_text": current_text,
}
applied += 1
```

Three things happen:
1. The result value is replaced with `current_text` (read from the saved box).
2. The `field_locations` entry is overwritten with `strategy: "spatial_memory"` — the review UI shows this strategy, helping the user understand the value came from memory.
3. The counter is incremented for logging.

### 4.6 — Persist if anything changed

Back in `worker.py:_process_postprocess`:

```python
result, field_locations, sm_applied = await spatial_memory.apply_to_extraction(...)
if sm_applied:
    await db_mod.update_extraction_result(pool, extraction_id, result, page_results, "processing", None)
```

Only re-write `extractions.result` if at least one memory hit. Avoids unnecessary writes for documents with no applicable memory.

---

## `po_per_page` special case (lines 307–463)

When `result` is a list (each page = independent PO), memory is applied page-by-page:

```python
async def _apply_to_po_per_page(pool, ext_id, extraction, result, field_locations, page_geometry):
    # field_locations becomes a list, one dict per page
    fl_list = [dict(fl) if isinstance(fl, dict) else {} for fl in field_locations]
    
    for mem in memories:
        page_num = mem["page_number"]
        idx = page_num - 1
        if idx < 0 or idx >= len(result):
            continue
        
        page_result = result[idx]
        # ... same denormalize / words_in_box / override logic ...
        page_result[field_key] = current_text
        fl_list[idx][field_key] = {...}
```

A memory saved for page 2 only touches `result[1]`. Memory for page 1 only touches `result[0]`. No cross-page bleed.

---

## Layout key — what it is and why

```python
def compute_layout_key(vendor_id: str, template_id: int | None, page_results: list) -> str:
    if template_id is not None:
        return f"{vendor_id}:{template_id}"
    return f"{vendor_id}:_no_template"
```

(Simplified — see `backend/layout_key.py` for the actual implementation, which can also include layout signals from `page_results`.)

The composite key prevents cross-template memory pollution. If a vendor uses two different document layouts (e.g. invoice vs purchase order), each gets its own template and its own set of memories.

---

## When to invalidate memory

Currently, memories are never auto-invalidated. They live forever (until vendor delete). Cleanup options the system does **not** implement:

- **Time-based decay**: memories older than 6 months → flag for re-verification.
- **Hit-rate tracking**: memories that never produce text (always skip the staleness guard) → mark inactive.
- **A/B comparison**: compare Qwen's value to memory's value, flag disagreements for review.

These are TODOs — not blockers since the rule "always read current text" inherently prevents the worst kind of staleness (stale value taking effect).

---

## Common debugging events

```python
plog.event("spatial_memory_save_started",       # write begins
           layout_key=..., submitted_locations=...)
plog.event("spatial_memory_save_skipped",       # one field skipped during save
           reason="not_manual_review_box", field_key=...)
plog.event("spatial_memory_saved",              # one row written
           field_key=..., normalized_box=...)
plog.event("spatial_memory_save_completed",     # write done
           saved_count=...)

plog.event("spatial_memory_loaded",             # apply begins
           memory_count=...)
plog.event("spatial_memory_skipped",            # apply: stale memory
           reason="no_current_text_in_saved_box")
plog.event("spatial_memory_overrode_field",     # apply: value replaced
           old_value=..., new_value=...)
plog.event("spatial_memory_added_field",        # apply: value injected
           new_value=...)
```

Grep the worker logs for these to debug. They're emitted at INFO level via `logging_config.event` (structured JSON logging).

---

## What this design does NOT do

- **No fuzzy box overlap.** A memory at exactly box X applies only to words whose centers fall inside box X. No "close enough" matching.
- **No memory for line items.** Per AGENTS.md rule. Line-item row geometry is too volatile.
- **No cross-vendor sharing.** Each vendor's memories are isolated. No "vendors with similar templates can share memory" optimization.
- **No automatic conflict resolution.** Two memories for the same field on the same page would conflict — but the `UNIQUE(vendor_id, layout_key, field_key, page_number)` constraint makes that impossible at the DB level.
- **No memory export/import.** Vendor memories live only in your DB. No backup format.
