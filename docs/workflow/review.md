# Review Page — Manual Correction Workflow

> Source files:
> - Frontend: [frontend/review.js](../../frontend/review.js)
> - Save endpoint: [backend/main.py](../../backend/main.py) (`PUT /extractions/{id}/corrections` lines 1810–2010)
> - Spatial memory write: [backend/spatial_memory.py](../../backend/spatial_memory.py) (`save_from_corrections`)
> - Audit log: `db.create_review_event`

The review page is the **only place humans correct extraction output**. Every save:
1. Updates `extractions.corrected_result` (immutable original `result` stays untouched).
2. Writes a `review_events` audit row.
3. Creates a `gold_examples` row if any header fields changed (used as few-shot examples in future Qwen prompts).
4. Writes new `spatial_memory` rows for any user-drawn boxes (used to override fields in future extractions).
5. Re-enqueues the `outbound` stage to regenerate Excel/CSV exports.

---

## Three-panel layout

```
┌────────────────────┬───────────────────────────┬────────────────┐
│  Field list (left) │  PDF / page viewer (mid)  │  Result JSON   │
│                    │                           │     (right)    │
│  - po_number ✏     │   <document image with    │  {             │
│  - vendor_name     │    overlay boxes>         │    po_number:  │
│  - ship_to ✏        │                          │      "12345",  │
│  - line_items      │   click-to-select +       │    ...         │
│    └ row 1 ✏        │   drag-to-create-box     │  }             │
│    └ row 2          │                           │                │
└────────────────────┴───────────────────────────┴────────────────┘
                              │
                              ▼
                    Save button at bottom
```

---

## End-to-end save flow

```
User clicks Save
   │
   ▼
review.js gathers state:
  - corrected_result (the JSON edited in right panel)
  - field_locations (each field's box, page, strategy)
   │
   ▼
PUT /extractions/{id}/corrections
   │
   ├─ assert_extraction_access(pool, ext_id, user)   ← isolation check
   │
   ├─ db.save_corrections()
   │    UPDATE extractions
   │    SET corrected_result=$1, field_locations=$2,
   │        correction_meta=$3, updated_at=NOW()
   │    WHERE id=$4
   │
   ├─ db.create_review_event()                        ← audit log
   │    INSERT INTO review_events (before_*, after_*, diff)
   │
   ├─ if header fields changed:
   │     db.save_gold_example()                       ← few-shot for future Qwen calls
   │       INSERT INTO gold_examples (vendor_id, original, corrected, diff)
   │
   ├─ if field_locations contains "manual" boxes:
   │     spatial_memory.save_from_corrections()       ← geometry memory
   │       UPSERT spatial_memory (one row per manual box)
   │
   └─ db.ensure_job(outbound)                         ← re-export xlsx/csv
        triggers the outbound worker to rebuild exports
        with the corrected values
```

---

## What the user sees and does

### 1. Open the review page

URL: `/review/{extraction_id}`. The frontend's `renderReviewPage(app, extractionId)` does:

```javascript
const ext = await apiJSON(`/extractions/${extractionId}`);
reviewResult = ext.corrected_result || ext.result;
reviewFieldLocations = ext.field_locations || {};
reviewExtractionId = extractionId;
reviewPages = await apiJSON(`/extractions/${extractionId}/pages`);  // base64 page images
const ocrData = await apiJSON(`/extractions/${extractionId}/ocr`);  // word geometry for click-to-select
```

If any of these calls fail with 401, the catch block re-throws so `apiFetch`'s redirect to `/login` proceeds. Other failures (404, network) fall back to in-memory state captured during extraction.

### 2. Hover a field → highlight on document

For each field in `reviewFieldLocations`, the SVG overlay draws a box on the corresponding page. Hovering the field in the left panel:
- Sets `activeMapField` (top-level state in `core.js`).
- The SVG box for that field gets a brighter outline.
- The page viewer scrolls/zooms to bring the box into view.

### 3. Three ways to correct a value

**Method A — Type in the JSON panel.** Direct edit. The left-panel field list updates as the user types. No box change.

**Method B — Click a word on the document** (click-to-select). The OCR data is loaded; the click coordinates are matched against `ocr_data.words[*].box`. The closest word's text becomes the new field value, and a small box around the word becomes the field's `box` with `strategy: "click_select"`.

**Method C — Drag a rectangle on the document** (the powerful one). The user holds shift+drag (or whatever modifier the UI uses) to create a new bounding box. The box is recorded with `strategy: "manual"`. On save, the words inside the drawn box become the field's text. **Only `strategy: "manual"` boxes get persisted as spatial memory.**

### 4. Click Save

```javascript
async function saveReview() {
    const payload = {
        corrected_result: reviewResult,
        field_locations: reviewFieldLocations,
        actor: 'ui',
        reason_code: 'manual_review',
    };
    await apiJSON(`/extractions/${reviewExtractionId}/corrections`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    showToast('Saved');
}
```

The frontend doesn't need to differentiate between "field value changed" and "box position changed" — both go in the same payload. The backend computes the diff.

---

## Backend: `PUT /extractions/{id}/corrections` (lines 1810–2010)

### A) Authorize + parse (lines 1822–1832)

```python
await assert_extraction_access(pool_for_check, extraction_id, user)
body = await request.json()
corrected_result = body.get("corrected_result")
field_locations = body.get("field_locations", {})
actor = body.get("actor") or "ui"
reason_code = body.get("reason_code") or "manual_review"

if corrected_result is None:
    raise HTTPException(400, detail="corrected_result is required")
```

Ownership assertion runs first — rejects 403 before parsing the body.

### B) Compute correction diff (line 1861)

```python
correction_diff = _compute_correction_diff(original_result, corrected_result)
```

Helper compares old vs. new and returns `{field_name: new_value}` for every field that changed. Used to:
- Build the audit log entry.
- Decide whether to create a gold example.
- Annotate the structured log event for debugging.

### C) Persist to `extractions.corrected_result` (lines 1884–1892)

```python
updated = await db_mod.save_corrections(
    pool, extraction_id, corrected_result, field_locations,
    correction_meta={
        "corrected_at": ...,
        "fields_changed": list(correction_diff.keys()),
        "reason_code": reason_code,
        "actor": actor,
    },
)
```

The original `result` column is **never overwritten**. Subsequent reads should always prefer `corrected_result` (with `result` as fallback). The frontend does exactly this:

```javascript
reviewResult = ext.corrected_result || ext.result;
```

This separation lets you "see what Qwen got vs. what humans fixed" — used for evaluating model quality.

### D) Audit log (lines 1895–1906)

```python
review_event_id = await db_mod.create_review_event(
    pool,
    extraction_id=extraction_id,
    actor=actor,                                     # 'ui' (or future: 'api', 'admin', etc)
    reason_code=reason_code,                         # 'manual_review' or custom
    note=note,
    before_result=original_result,                   # full snapshot
    after_result=corrected_result,                   # full snapshot
    before_locations=extraction.get("field_locations") or {},
    after_locations=field_locations,
    diff=correction_diff,
)
```

Append-only — every correction adds a row. To audit "who changed po_number when?", query `SELECT diff, created_at FROM review_events WHERE extraction_id=$1 ORDER BY created_at`.

### E) Auto-create a gold example (lines 1908–1937)

```python
header_only_diff = {
    k: v for k, v in (correction_diff or {}).items()
    if k != "line_items" and not k.endswith("_line_items")
}
if header_only_diff and vendor_id:
    gold_id = await db_mod.save_gold_example(
        pool, vendor_id, extraction_id,
        original_result, corrected_result,
        correction_diff=header_only_diff,
    )
```

**Header-only filter**: line item rows are explicitly excluded. Per AGENTS.md §7, line-item data as few-shot examples adds noise — line items vary too much across documents to teach Qwen anything generalizable.

The gold example feeds back into the next extraction's prompt. `extractor.build_system_prompt` includes a `<verified_examples>` section showing the LLM what a correct correction looked like:

```
<verified_examples>
The following are examples of human corrections...
{
  "po_number": "PO-12345",     ← human-corrected value
  "vendor_address": "..."
}
</verified_examples>
```

Qwen learns the formatting conventions specific to this vendor (e.g. "po_numbers are always uppercase with a hyphen").

### F) Save spatial memory (lines 1939–1969)

```python
spatial_saved = await _sm.save_from_corrections(
    pool, extraction_id, field_locations, corrected_result,
)
```

Walks through `field_locations`, filters to `strategy == "manual"` header fields only, normalizes the boxes, and upserts into `spatial_memory`. See [spatial_memory.md](spatial_memory.md) for the full algorithm.

This is the **most important side effect of saving** — it teaches the system *where* a field lives so future extractions don't need correction.

### G) Re-trigger outbound (lines 1996–2005)

```python
outbound_payload = {"extraction_id": extraction_id, "trigger": "review"}
await db_mod.ensure_job(pool, ext_id, doc_id, "outbound", outbound_payload)
```

Excel and CSV exports are rebuilt with the corrected values. The user's `Download Excel` button serves the new file (the outbound worker overwrites the existing `purchase_order.xlsx` MinIO key, and `upsert_delivery` updates the row).

The `trigger: "review"` payload field is informational — visible in worker logs as the reason this outbound ran.

---

## Field-locations strategies (the values you'll see)

| `strategy` | Source | Reusable? | Description |
|---|---|---|---|
| `qwen_layout` | BBox Agent + qwen_layout_apply | No | Auto-mapped from learned label layout |
| `manual` | User drag-drew the box in review | **Yes** — saved as spatial memory | Human-verified region |
| `click_select` | User clicked a word | No | Single-word selection from OCR |
| `spatial_memory` | Auto-applied from saved memory | No (already memory) | This field came from a stored memory hit |

Only `manual` boxes become spatial memory. `qwen_layout` is already reproducible from the BBox Agent's stored layout, so re-saving would be redundant. `spatial_memory` re-saving is a no-op (the memory is already there).

This filtering happens in `_is_reusable_header_field` and the `if strategy != "manual": continue` guard in `save_from_corrections`.

---

## What's NOT saved when you save

- **Raw click coordinates.** The intermediate clicks/drags during interaction never reach the server; only the final `field_locations` snapshot does.
- **Undo history.** No undo stack on the server. The user can press Ctrl+Z client-side until the next reload, but server only sees the final state.
- **Selection focus.** Which field the user was editing isn't recorded.
- **Cursor position in the JSON panel.** Same as above.

---

## Failure modes and recovery

| Symptom | Cause | Fix |
|---|---|---|
| `400 corrected_result is required` | Empty body or missing field | Frontend bug; check payload construction |
| `403 Access denied` | User trying to correct another tenant's extraction | Expected — isolation working |
| `404 Extraction not found` | Stale tab; extraction was deleted | Reload, navigate elsewhere |
| Save succeeds but next extraction still wrong | Gold example not yet picked up — system prompt rebuilt fresh on every extraction, so should take effect immediately. If not, check `gold_examples` table for the new row | Verify `save_gold_example` succeeded; check log for `gold_correction_saved` event |
| Save succeeds but spatial memory not applied | The box was drawn with strategy ≠ "manual", or the saved region has no current text in the new doc (staleness guard in `apply_to_extraction`) | Confirm `strategy` was `manual` in payload; re-draw if not |
| Outbound files not updated after save | Outbound worker not running, or the `outbound` job got stuck | `docker compose logs -f outbound-worker`; check `jobs` table for failed outbound rows |

---

## Why `corrected_result` is separate from `result`

Three reasons:

1. **Auditability**: you can query "what did Qwen originally output?" forever.
2. **Quality evaluation**: comparing `result` vs. `corrected_result` across thousands of extractions tells you Qwen's accuracy on production data.
3. **Re-extraction safety**: if a user accidentally corrupts `corrected_result` (e.g. deletes important fields), `result` is intact and the fallback `corrected_result || result` recovers.

---

## What this workflow does NOT do

- **No multi-user collaboration.** Two reviewers editing the same extraction race; whoever saves last wins. There's no lock or merge.
- **No partial save.** Save is all-or-nothing — `corrected_result` is replaced wholesale.
- **No revert button.** To revert, the user reloads the page (which fetches the latest `corrected_result`, not the original `result`). To revert to original, an admin runs `UPDATE extractions SET corrected_result = NULL WHERE id = X`.
- **No diff view.** The UI doesn't currently highlight which fields differ between Qwen and the human. (It could be added — `correction_diff` is already computed server-side.)
- **No bulk review.** One extraction at a time.
