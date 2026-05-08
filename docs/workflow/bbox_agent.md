# BBox Agent + Fields Agent (Two-Agent Split)

> Source files:
> - BBox Agent: [backend/bbox_agent.py](../../backend/bbox_agent.py) — learns label positions
> - Fields Agent: [backend/extractor.py](../../backend/extractor.py) — extracts values
> - Apply layer: [backend/qwen_layout_apply.py](../../backend/qwen_layout_apply.py) — maps boxes to values

---

## The problem this solves

Earlier versions of the system asked Qwen3-VL to do two things in one prompt:
1. Extract the **values** for each requested field.
2. Return the **bounding box** of each value (so the review UI can highlight where each field came from).

Two issues with this approach:

1. **Cost and latency**: every page got the bbox request, but boxes only need to be detected once per template (the layout doesn't change between documents from the same vendor).
2. **Reliability**: combining "what is the value" and "where is the label" in one prompt produced flaky boxes — Qwen often hallucinated coordinates or returned boxes for the value (which moves) instead of the label (which is fixed).

**Solution: split into two agents.**

```
Vendor configures: header_fields = [po_number, vendor_name, ship_to]
                   line_item_fields = [no, description, qty, unit_price]

         ┌───────────────────────────────────────────┐
         │ BBox Agent — runs ONCE per (vendor, tmpl) │
         │ on page 1 only, when fields change        │
         │                                           │
         │ "Where is the LABEL 'PO Number:' on this  │
         │  page? Where are the column headers       │
         │  'No', 'Description', etc.?"              │
         │                                           │
         │ Output: {field_key: {normalized_box, type}│
         │         saved to qwen_layout_boxes table  │
         └───────────────────────────────────────────┘
                              │
                              ▼  (run for first ext, reused for all subsequent)
         ┌───────────────────────────────────────────┐
         │ Fields Agent — runs PER PAGE              │
         │ for every extraction                      │
         │                                           │
         │ "Extract the values for these fields. No  │
         │  bounding boxes — just the data."         │
         │                                           │
         │ Output: {fields: {po_number: "...", ...}} │
         └───────────────────────────────────────────┘
                              │
                              ▼  (postprocess step)
         ┌───────────────────────────────────────────┐
         │ qwen_layout_apply.build_field_locations   │
         │                                           │
         │ Combines:                                 │
         │  • Stored layout boxes (where labels are) │
         │  • Current OCR words (text on this page)  │
         │  • Fields Agent values (what to find)     │
         │                                           │
         │ Output: field_locations = {field_key: {   │
         │           page, box, strategy, ... }}     │
         └───────────────────────────────────────────┘
```

---

## When does the BBox Agent run?

`worker.py:_process_llm` lines 599–653:

```python
if tmpl and pages:
    vendor_id_llm = extraction_row["vendor_id"]
    template_id_llm = tmpl["id"]
    known = await db_mod.get_qwen_layout_boxes(pool, vendor_id_llm, template_id_llm)
    missing_header = [f for f in req_header if f not in known]
    missing_columns = [f for f in req_items if f not in known]
    
    if missing_header or missing_columns:
        # New fields detected → re-run with ALL fields, then overwrite DB
        learned = await bbox_agent.learn_layout_for_vendor(
            page1_image_b64=pages[0]["image_b64"],
            page1_width=pages[0].get("width") or 0,
            page1_height=pages[0].get("height") or 0,
            header_field_keys=req_header,           # ALL fields, not just missing
            line_item_column_keys=req_items,
            llm_url=LLM_URL,
            model=LLM_MODEL,
            ...
        )
        if learned:
            await db_mod.upsert_qwen_layout_boxes(pool, vendor_id, template_id, ext_id, learned)
```

**Trigger condition**: any header field or line-item column in the current template that doesn't yet have a row in `qwen_layout_boxes` for this `(vendor, template)`.

**When triggered, send ALL fields, not just the missing ones.** Why? Because the LLM needs to see the full layout context — knowing where `qty`, `description`, etc. are helps it correctly identify a new column like `material_no`. Asking for one column in isolation often gives worse results than asking for all of them.

**The output overwrites all existing rows for this (vendor, template).** A `DELETE WHERE vendor_id=$1 AND template_id=$2` followed by an `INSERT` of the new boxes. Old fields that are still in the request get re-learned; old fields that have been removed simply disappear from `qwen_layout_boxes`.

This trigger fires:
- The first time any extraction runs for a (vendor, template).
- Every time a user adds a new field to the template.

It does NOT fire on every extraction — once layout is learned, all subsequent extractions skip the BBox Agent entirely. **Saves ~3–6 seconds per extraction after the first one.**

---

## `bbox_agent.learn_layout_for_vendor` (lines 103–303)

Detailed walkthrough.

### The system prompt (lines 45–65)

```python
def _build_bbox_system_prompt(total_fields: int) -> str:
    return f"""\
You are a Document Layout Analyzer.
Your ONLY task: locate the exact position of where specific label text and
table column header text appears in the document image.

<field_types>
1. HEADER FIELDS — standalone label-value pairs (e.g., "Ship To:", "PO Number:").
   Return the bounding box of the LABEL text only — NOT the value.
2. LINE ITEM COLUMNS — column headers in the item table.
   Return the bounding box of each column header cell text.
</field_types>

<rules>
- Strictly return ONLY the bounding box of the LABEL or HEADER text region.
- bbox_2d format: [x1, y1, x2, y2] in a 0-1000 normalized grid.
- If a label is not visible on this page, return null for that entry.
- Strictly return ONLY valid JSON. No markdown fences, no explanation.
- If multiple occurrences of the same field name exist, map to the contextually
  correct one (e.g. material_no header vs line item).
</rules>

<critical> The user requested EXACTLY {total_fields} fields. You MUST return
exactly {total_fields} keys in the "boxes" object. </critical>
"""
```

The prompt is structured with XML tags (project convention — see `MEMORY.md:project_conventions.md`). The `<critical>` block forces Qwen to count its outputs against the request count, reducing dropped fields.

### The user message (lines 67–100)

Lists all requested header fields and line item columns. Provides an example output template with `null` placeholders so Qwen knows the exact JSON shape:

```json
{
  "boxes": {
    "po_number": null,
    "vendor_name": null,
    "ship_to": null,
    "no": null,
    "description": null,
    "qty": null
  },
  "all_6_requested_fields_returned": true
}
```

The `all_N_requested_fields_returned` boolean is a self-check the model has to answer truthfully — empirically improves completeness.

### The HTTP call (lines 156–222)

```python
payload = {
    "model": model,
    "messages": messages,
    "temperature": LLM_TEMPERATURE,    # 0.6 — same as Fields Agent
    "top_p": LLM_TOP_P,
    "max_tokens": LLM_MAX_TOKENS_BBOX,
}

async with httpx.AsyncClient(timeout=120.0) as client:
    resp = await client.post(llm_url, json=payload)
    resp.raise_for_status()
```

**`max_tokens=LLM_MAX_TOKENS_BBOX`** (configured in `config.py`) — enough for ~30 bounding box objects. Set lower than the Fields Agent's max because BBox output is short and structured.

Token usage is recorded with `call_type="bbox_agent"` so it shows up separately in the admin usage dashboard.

### The coordinate mapping (lines 244–280)

This is the subtlest part. Qwen3-VL returns coordinates in a **0-1000 normalized grid** relative to a **32px-aligned** image (Qwen pads images so dimensions are multiples of 32). To convert back to original-image normalized coords:

```python
FACTOR = 32
w_bar = max(FACTOR, int(round(page1_width / FACTOR) * FACTOR))   # aligned width
h_bar = max(FACTOR, int(round(page1_height / FACTOR) * FACTOR))  # aligned height

for field_key in all_requested:
    box_raw = raw_boxes.get(field_key)
    if not isinstance(box_raw, list) or len(box_raw) != 4:
        continue
    
    # Step 1: 0-1000 grid → pixel coords on the aligned image
    x0_px = (box_raw[0] / 1000.0) * w_bar
    y0_px = (box_raw[1] / 1000.0) * h_bar
    x1_px = (box_raw[2] / 1000.0) * w_bar
    y1_px = (box_raw[3] / 1000.0) * h_bar
    
    # Step 2: normalize relative to ORIGINAL image dimensions
    nx0 = x0_px / page1_width
    ny0 = y0_px / page1_height
    nx1 = x1_px / page1_width
    ny1 = y1_px / page1_height
    
    nx0, ny0 = max(0.0, nx0), max(0.0, ny0)
    nx1, ny1 = min(1.0, nx1), min(1.0, ny1)
    
    if nx1 <= nx0 or ny1 <= ny0:
        continue  # malformed
    
    result[field_key] = {
        "normalized_box": {"x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1},
        "field_type": "header" if field_key in header_field_keys else "line_item_column",
    }
```

The full explanation of the FACTOR=32 alignment is in `docs/qwen_bbox_github_issue.md` — it's a quirk of how Qwen3-VL processes images. Without this correction, all bounding boxes would be off by a few percent — small enough to look "almost right" but enough to miss the label entirely on edge cases.

After this step, every box is `{x0, y0, x1, y1}` in `[0..1]` relative to the original page dimensions. Storage in `qwen_layout_boxes.normalized_box` is in this normalized form, so it's resolution-independent.

---

## The Fields Agent (in `extractor.py`)

After the BBox Agent runs (or is skipped), the Fields Agent processes every page:

```python
# extractor.build_user_message — sent for each page
"""Extract the header fields AND all visible line item rows from this purchase
order page (page {page_num} of {total_pages}).

If any field is empty or not visible, return null.
<header_fields>
  - po_number
  - vendor_name
  ...
</header_fields>
<line_item_columns>
  - no
  - description
  ...
</line_item_columns>

Return JSON in exactly this shape:
{
  "fields": {
    "po_number": null,
    "vendor_name": null,
    ...
    "line_items": [{"no": null, "description": null, ...}]
  }
}
"""
```

**Notice: no boxes requested.** The system prompt sets `Return one top-level key: fields` — only values. Boxes are computed downstream from the BBox Agent's stored layout.

**`PROMPT_VERSION = "v4.0"`** in `extractor.py` line 89 — version bumped from v3 (which had boxes). The prompt hash includes the version, so old cached prompts are invalidated when the version changes.

---

## Combining the two: `qwen_layout_apply.build_field_locations_from_layout`

This is the postprocess step that turns "labels at these coords" + "current page words" + "Fields Agent values" into "this value is at this box".

For header fields:
1. Take the label box (e.g. `po_number → {x0, y0, x1, y1}` from `qwen_layout_boxes`).
2. Project right or below the label (configurable per layout) to find the value region.
3. Find the OCR words inside that region.
4. The first matching word run becomes the field's `box` in `field_locations`.

For line-item columns:
1. Take the column header box.
2. For each line item, project down to find the cell that contains that line's value.
3. Match against the value from Fields Agent (text comparison) to confirm.
4. Record the box.

The output is the `field_locations` JSONB column on the extraction:

```json
{
  "po_number": {
    "page": 1,
    "box": [120, 340, 280, 365],
    "strategy": "qwen_layout",
    "confidence": "high",
    "matched_text": "PO-12345"
  },
  "line_items": [
    {
      "no": {"page": 1, "box": [...]},
      "description": {"page": 1, "box": [...]},
      "qty": {"page": 1, "box": [...]}
    },
    ...
  ]
}
```

The review UI uses this to draw highlight boxes on the document image when the user hovers a field.

---

## Why store `qwen_layout_boxes` separately from `spatial_memory`?

Both store geometry. They differ in source:

| Table | Source | Trigger | Lifecycle |
|---|---|---|---|
| `qwen_layout_boxes` | Qwen3-VL (BBox Agent) | New field added to template | Re-run on next extraction |
| `spatial_memory` | Human review (user drags a box) | User saves corrections | Survives until vendor delete |

`qwen_layout_boxes` represents Qwen's understanding of the layout. `spatial_memory` represents human-verified ground truth. The two can be combined in postprocess — spatial memory takes precedence over qwen-layout for the same field.

See [spatial_memory.md](spatial_memory.md) for the human-correction side.

---

## Common failure modes

1. **`learned = {}` returned**: Qwen returned malformed JSON or zero usable boxes. The worker logs `BBox agent: learned 0/N fields` and continues; field_locations will be empty until a user manually corrects in review.
2. **Wrong label box for similar fields**: e.g. "po_number" maps to a line-item column header instead of the document header. The `<critical>` rule in the prompt addresses this; if it still happens, edit the field name to be more specific (`document_po_number`).
3. **FACTOR=32 misalignment**: if box coordinates look "almost right but off by a few pixels", verify `page1_width` and `page1_height` are the rendered dimensions sent to the LLM, not the original PDF dimensions.
4. **Missing fields in output**: the prompt asks for null when a label isn't visible. Genuinely missing labels (e.g. asked for `vendor_id` but the document only has `vendor_name`) end up as null/missing from `qwen_layout_boxes` and the field has no auto-mapped box. User must add it via review.

---

## What this design does NOT do

- **No box correction post-LLM.** No OCR snapping, no IoU-based refinement. The Qwen-returned box is used as-is (after FACTOR=32 alignment).
- **No multi-page label detection.** Only page 1 is sent. If a vendor has different layouts across pages, only page 1 is learned.
- **No confidence score from Qwen.** All learned boxes are stored with implicit "Qwen said so"; downstream `field_locations` uses `confidence: high` if present.
- **No retry on parse failure.** A single JSON-parse error → empty result. The trigger condition will fire again on the next extraction with new fields, giving Qwen another chance.
