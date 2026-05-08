# The Five-Stage Extraction Pipeline

> Source files:
> - Ingest: [backend/main.py](../../backend/main.py) (`/ingest/ui`)
> - Worker stages: [backend/worker.py](../../backend/worker.py)
> - LLM call: [backend/extractor.py](../../backend/extractor.py)
> - Frontend: [frontend/extract.js](../../frontend/extract.js)

When a user uploads a document, **5 background workers** process it in sequence: normalize → ocr → llm → postprocess → outbound. Each stage claims a row from the `jobs` table, does its work, and enqueues the next stage. The frontend watches via SSE.

---

## Why durable jobs (and not in-process async)?

**Resilience.** A worker crash should not lose work. Jobs in `running` status get reset to `queued` after 5 minutes (see [workers.md](workers.md)). Different stages run in different worker processes, so an OOM in OCR doesn't kill the LLM stage.

**Backpressure.** Each stage has its own poll interval and concurrency. The LLM stage is the bottleneck (Qwen3-VL is GPU-bound at ~2 pages/sec), so a single LLM worker is enough; the OCR worker can scale separately.

**Observability.** Every stage transition is logged via `plog.event(...)` with `stage_started` / `stage_completed` markers and surfaces in Phoenix tracing for full LLM call inspection.

---

## Sequence diagram

```
User                Frontend           API (FastAPI)        normalize  ocr   llm   postprocess  outbound
 │                     │                    │                  │       │      │         │           │
 │  Upload PDF         │                    │                  │       │      │         │           │
 │ ──────────────────> │                    │                  │       │      │         │           │
 │                     │  POST /ingest/ui   │                  │       │      │         │           │
 │                     │ ─────────────────> │                  │       │      │         │           │
 │                     │                    │ detect_vendor()  │       │      │         │           │
 │                     │                    │ create_doc()     │       │      │         │           │
 │                     │                    │ create_extr()    │       │      │         │           │
 │                     │                    │ ensure_job(norm) │       │      │         │           │
 │                     │ <── {job_id, ext}─ │                  │       │      │         │           │
 │                     │                    │                  │       │      │         │           │
 │                     │  GET /jobs/{id}/   │                  │       │      │         │           │
 │                     │      stream  (SSE) │                  │       │      │         │           │
 │                     │ ─────────────────> │                  │       │      │         │           │
 │                     │ <── progress ───── │                  │       │      │         │           │
 │                     │                    │                  │       │      │         │           │
 │                     │                    │           claim_job(normalize)  │         │           │
 │                     │                    │                  │       │      │         │           │
 │                     │                    │                  │ pdf2img │    │         │           │
 │                     │                    │                  │ classify│   │         │           │
 │                     │                    │                  │ save_pages│  │         │           │
 │                     │                    │                  │ ensure(ocr) │         │           │
 │                     │                    │                  │ ensure(llm) │         │           │
 │                     │                    │                  │       │      │         │           │
 │                     │                    │                  │       │  PaddleOCR     │           │
 │                     │                    │                  │       │  (scanned only)│           │
 │                     │                    │                  │       │      │         │           │
 │                     │                    │                  │       │      │ bbox_agent│         │
 │                     │                    │                  │       │      │ extract_doc│        │
 │                     │                    │                  │       │      │   per page │        │
 │                     │                    │                  │       │      │ ensure(post)│       │
 │                     │                    │                  │       │      │         │           │
 │                     │                    │                  │       │      │         │ apply_layout│
 │                     │                    │                  │       │      │         │ apply_smem  │
 │                     │                    │                  │       │      │         │ ensure(out)│
 │                     │                    │                  │       │      │         │           │
 │                     │                    │                  │       │      │         │           │ build_xlsx
 │                     │                    │                  │       │      │         │           │ build_csv
 │                     │                    │                  │       │      │         │           │ MinIO put
 │                     │ <── done event ─── │                  │       │      │         │           │
 │ <── result UI ──────│                    │                  │       │      │         │           │
```

---

## Step 0 — `POST /ingest/ui` (in `main.py`)

The HTTP entry point. Roughly 70 lines, doing 6 things:

```python
@app.post("/ingest/ui")
async def ingest_ui(
    request: Request,
    file: UploadFile,
    vendor_id: str = Form(None),    # optional manual vendor
    user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
```

### 0.1 — Validate file size

Max 50 MB by default (env `MAX_UPLOAD_MB`). Larger uploads are rejected before reading the body.

### 0.2 — Save raw file to MinIO

```python
object_key = f"documents/uploads/{uuid4().hex}-{filename}"
store.put_bytes(DOCUMENTS_BUCKET, object_key, raw_bytes, mime)
```

The original document lives in object storage. Subsequent stages re-fetch from MinIO instead of passing bytes through the queue (keeps job rows small).

### 0.3 — Detect vendor (if not manually selected)

```python
if not vendor_id:
    page_words = await processor.preview_first_page_words(raw_bytes)
    detect_uid = None if user["role"] == "admin" else user["id"]
    match = await vendor_detector.detect_vendor(pool, page_words, user_id=detect_uid)
    if not match:
        raise HTTPException(409, detail={"reason": "unknown_vendor"})
    vendor_id = match.vendor_id
    detected_payload = {"vendor_id": match.vendor_id, "vendor_name": match.vendor_name}
```

The detector is tenant-scoped via `user_id`. See [vendor_detection.md](vendor_detection.md).

### 0.4 — Defence-in-depth ownership check

```python
await assert_vendor_access(pool, vendor_id, user)
```

Even after a scoped detection, we re-confirm ownership. Belt and braces — protects against any future bug in the detector's filter.

### 0.5 — Create `documents` and `extractions` rows

```python
doc = await db_mod.create_document(pool, vendor_id, ..., object_key=object_key)
ext = await db_mod.create_extraction(pool, doc["id"], vendor_id, ...)
```

Both rows are created with `status='queued'`. The frontend gets the `extraction_id` immediately so it can navigate to the result page even before processing begins.

### 0.6 — Enqueue the first stage

```python
job = await db_mod.ensure_job(
    pool, ext["id"], doc["id"], "normalize",
    {"extraction_id": ext["id"], "trace_context": ...},
)
return {"extraction_id": ext["id"], "job_id": job["id"], "detected_vendor": detected_payload}
```

`ensure_job` is idempotent — see [workers.md](workers.md).

---

## Stage 1 — `normalize` (`worker.py:_process_normalize` lines 179–387)

**Goal**: render the document into per-page images and unified word geometry.

```python
raw = store.get_bytes(DOCUMENTS_BUCKET, document["object_key"])  # re-fetch
is_pdf = filename.endswith(".pdf")
```

### 1.1 — Render to images

```python
if is_pdf:
    rendered_pages = await processor.pdf_to_images(raw)        # pypdfium2
else:
    rendered_pages = await processor.image_file_to_b64(raw)    # passthrough
```

Each `rendered_pages[i]` is `{page_number, image_b64, mime_type, width, height, orig_width, orig_height}`.

### 1.2 — Compute geometry (digital-vs-scanned classification)

```python
if is_pdf:
    page_geometry = geometry.compute_pdf_geometry(raw, page_sizes)
    # Each entry: {page_number, source: 'pypdfium' | 'scanned', char_count, words: [...]}
```

`compute_pdf_geometry` extracts text via pypdfium2 for each page. If a page yields enough characters, it's marked `source='pypdfium'` and PaddleOCR will skip it later. If it's a scanned image embedded in a PDF (low char count), it's marked `source='scanned'` and PaddleOCR runs on the raster.

For pure image uploads (jpg/png), all pages are `source='scanned'` immediately.

If `compute_pdf_geometry` raises (corrupt PDF), the worker falls back to treating all pages as scanned — defensive, never lose data due to a parse error.

### 1.3 — Save page artifacts

```python
for page in rendered_pages:
    object_key = f"extractions/{ext_id}/pages/page_{page_num}.jpg"
    store.put_bytes(ARTIFACTS_BUCKET, object_key, page_bytes, mime)
    page_rows.append({page_number, object_key, ..., source, char_count, word_geometry})

await db_mod.save_pages(pool, ext_id, page_rows)
```

Page images live in MinIO. The `pages` table holds metadata + `word_geometry` (JSONB). `image_b64` is no longer stored in the DB (legacy column kept for compatibility).

### 1.4 — Enqueue OCR + LLM in parallel

```python
await db_mod.ensure_job(pool, ext_id, doc_id, "ocr",
                        {"scanned_page_numbers": [...]})
await db_mod.ensure_job(pool, ext_id, doc_id, "llm",
                        {"extraction_id": ext_id})
```

Both stages start at the same time. The LLM stage doesn't wait for OCR because:
- LLM works on **page images**, not OCR text.
- Postprocess (the next stage) needs both LLM results AND OCR words to map fields to bounding boxes.
- So LLM and OCR run in parallel, then postprocess waits for **both** before starting.

This parallelism is what `_maybe_enqueue_postprocess` (line 164) ensures — see [workers.md](workers.md).

---

## Stage 2 — `ocr` (`worker.py:_process_ocr` lines 389–534)

**Goal**: produce unified per-page word geometry combining digital and OCR'd pages.

### 2.1 — Detect which pages need OCR

```python
scanned_page_numbers = payload.get("scanned_page_numbers")
if scanned_page_numbers is None:
    # Fallback for old jobs: read source column from pages table
    scanned_page_numbers = [p["page_number"] for p in all_pages if p["source"] != "pypdfium"]
```

### 2.2 — All-digital fast path

```python
if not scanned_page_numbers:
    # Skip PaddleOCR entirely. Build unified ocr_data from existing word_geometry.
    unified = [{page_number, source: 'pypdfium', char_count, word_count, words: [...]} for p in all_pages]
    await db_mod.save_ocr_data(pool, ext_id, unified)
    return  # done
```

Saves ~30-60 seconds per multi-page digital PDF.

### 2.3 — Run PaddleOCR on scanned pages only

```python
scanned_pages = [p for p in all_pages_loaded if p["page_number"] in scanned_set]
ocr_pages = await ocr_runner.run_ocr_on_pages(scanned_pages)
```

`ocr_runner` wraps PaddleOCR. Each output: `{page_number, words: [{text, box: [x0,y0,x1,y1], score}, ...]}`.

### 2.4 — Merge into unified geometry

```python
base_geometry = [...]  # built from pages.word_geometry (digital pages only have data)
unified = geometry.merge_scanned_into_geometry(base_geometry, ocr_pages)
await db_mod.save_ocr_data(pool, ext_id, unified)
```

After this stage, `extractions.ocr_data` has the same shape for every page regardless of source. Downstream code (postprocess, spatial memory, qwen_layout_apply) doesn't care whether the words came from pypdfium2 or PaddleOCR — same `{text, box, score}` triplet.

### 2.5 — Try to enqueue postprocess

```python
await _maybe_enqueue_postprocess(pool, ext_id, doc_id, trace_context)
```

Will run **only if** `is_postprocess_ready()` returns True — i.e. both `result` (from LLM) and `ocr_data` (just saved) exist. If LLM hasn't finished yet, this is a no-op; the LLM worker will call `_maybe_enqueue_postprocess` when it's done.

---

## Stage 3 — `llm` (`worker.py:_process_llm` lines 537–817)

**Goal**: send each page image to Qwen3-VL, parse JSON, accumulate page_results, store final result.

### 3.1 — Load template + build system prompt

```python
tmpl = await db_mod.get_template(pool, vendor_id)
req_header = extraction_row.get("header_fields") or tmpl.get("header_fields", [])
req_items = extraction_row.get("line_item_fields") or tmpl.get("line_item_fields", [])
req_format = tmpl.get("format_type") or extraction_row.get("format_type") or "single_po_multipage"
```

The extraction row holds a snapshot of which fields were requested at extraction time. Falls back to the current template if the snapshot is empty (legacy/edge case).

```python
gold_examples = await db_mod.get_gold_examples(pool, vendor_id)
system_prompt = extractor.build_system_prompt(
    req_header, req_items,
    tmpl["prompt_instructions"], tmpl["extraction_rules"],
    req_format,
    gold_examples=gold_examples,
)
```

**The system prompt is built fresh from current DB fields on every extraction** — no caching. (Project convention; per-`MEMORY.md` `project_conventions.md`: "dynamic prompt rebuild from DB at runtime, no Redis cache for prompts going forward".) This way:
- A template edit takes effect on the very next extraction.
- Gold examples updated yesterday are included today.
- No stale-cache surprises.

### 3.2 — BBox Agent (one-shot label detector) — see [bbox_agent.md](bbox_agent.md)

```python
known = await db_mod.get_qwen_layout_boxes(pool, vendor_id, template_id)
missing_header = [f for f in req_header if f not in known]
missing_columns = [f for f in req_items if f not in known]

if missing_header or missing_columns:
    # Re-run with ALL fields (not just missing ones)
    learned = await bbox_agent.learn_layout_for_vendor(...)
    if learned:
        await db_mod.upsert_qwen_layout_boxes(pool, vendor_id, template_id, ext_id, learned)
```

Runs only when new fields have been added. Costs one extra LLM call, but only when fields change. The result is reusable across all future extractions for this (vendor, template).

### 3.3 — Per-page LLM extraction

```python
output = await extractor.extract_document(
    pages=pages,
    header_fields=req_header,
    line_item_fields=req_items,
    system_prompt=system_prompt,
    format_type=req_format,
    on_page_done=on_page_done,    # streams progress to SSE
    cancel_event=cancel_event,    # racing flag for instant cancel
    start_from_page=...,          # resume support
    existing_page_results=...,    # resume support
)
```

`extract_document` (in `extractor.py` lines 501–672):
1. Computes which pages still need processing (skipping already-done pages on resume).
2. Iterates pages in batches of 1 (sequential) — `PARALLEL_BATCH = 1`. Sequential keeps GPU memory predictable on the llama-server.
3. For each page, builds a `user_message` with the field template and calls `call_llm`.
4. After each page, calls `on_page_done(page_num, total, page_result)` which:
   - Updates `jobs.progress` (drives the SSE stream).
   - Persists the partial result to `extractions.page_results` immediately.
   - Checks `cancel_requested` and sets `cancel_event` if so.
5. After all pages, **merges** based on format:
   - `single_po_multipage`: header from page 1, line_items concatenated from all pages.
   - `po_per_page`: list of dicts, one per page.
   - `single_page`: just the one page's fields.

### 3.4 — `extractor.call_llm` (lines 333–496)

Sends the request to llama-server (Qwen3-VL):

```python
payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            {"type": "text", "text": user_message},
        ]},
    ],
    "temperature": LLM_TEMPERATURE,    # 0.6 (per project convention — official Qwen3-VL value)
    "top_p": LLM_TOP_P,
    "presence_penalty": LLM_PRESENCE_PENALTY,
    "max_tokens": LLM_MAX_TOKENS_FIELDS,
}
```

**Cancel race**: if `cancel_event` is provided, the HTTP POST is raced against the event:

```python
done, pending = await asyncio.wait(
    [asyncio.create_task(_do_post()), asyncio.create_task(_wait_cancel())],
    return_when=asyncio.FIRST_COMPLETED,
)
```

If cancel wins, `_wait_cancel` raises `asyncio.CancelledError` which short-circuits the call. The pending POST is `.cancel()`'d, freeing the GPU.

**JSON parsing with three-tier fallback**:
1. Strip markdown fences (\`\`\`json ... \`\`\`).
2. `json.loads(raw)` — happy path.
3. **Fallback 1**: regex-fix leading-zero numbers (`0070` → `"0070"`) and re-parse.
4. **Fallback 2**: `json_repair.repair_json` (third-party best-effort fixer).
5. If all fail, raise `ValueError`.

**Token usage recording**:
```python
await db_mod.record_llm_usage(pool, doc_id=..., extraction_id=..., page_num=..., 
                              call_type="extraction", model=model,
                              prompt_tokens=..., completion_tokens=..., total_tokens=...,
                              duration_ms=...)
```

Every call lands in the `llm_usage` table. Drives the admin billing dashboard.

### 3.5 — Persist result + enqueue postprocess

```python
await db_mod.update_extraction_result(pool, ext_id, output["result"], output["page_results"], status, elapsed_ms)
if status == "processing":
    await _maybe_enqueue_postprocess(pool, ext_id, doc_id, trace_context)
```

Status is left at `processing`, **not** `done`. Postprocess sets the final `done` status — keeps the SSE stream open until field_locations are computed.

---

## Stage 4 — `postprocess` (`worker.py:_process_postprocess` lines 819–986)

**Goal**: build `field_locations` (the field-to-bbox mapping) and apply spatial memory overrides.

### 4.1 — Apply Qwen layout boxes

```python
qwen_boxes = await db_mod.get_qwen_layout_boxes(pool, vendor_id, template_id)
if qwen_boxes:
    field_locations = qwen_layout_apply.build_field_locations_from_layout(
        qwen_boxes, pages_with_words, result, page_results=page_results,
    )
```

`qwen_layout_apply` (separate module) takes:
- The learned label boxes from Qwen (where each LABEL is on the page).
- Current page words from `ocr_data`.
- The extraction result (which fields have values).

It returns `{field_key: {page, box, strategy: 'qwen_layout', confidence: ..., matched_text: ...}}` — for each header field, the box that contains the VALUE adjacent to its label, and for each line-item column, the column box containing the cell values for each line item.

If `qwen_boxes` is empty (no BBox Agent run yet), `field_locations = {}`. The review UI still works; users can drag boxes manually.

### 4.2 — Apply spatial memory — see [spatial_memory.md](spatial_memory.md)

```python
result, field_locations, sm_applied = await spatial_memory.apply_to_extraction(
    pool, ext_id, result, field_locations, page_geometry=ocr_data,
)
if sm_applied:
    await db_mod.update_extraction_result(pool, ext_id, result, page_results, "processing", None)
```

For each saved memory region, read the current document's text inside the region and **override** the corresponding field in `result`. Never use the old corrected value — the rule from AGENTS.md.

Spatial memory reflects past corrections. If a user fixed `vendor_address` last week by drawing a box, this week's extraction reads from that same box and overrides whatever Qwen guessed.

### 4.3 — Persist + enqueue outbound

```python
await db_mod.save_field_locations(pool, ext_id, field_locations)
await db_mod.set_extraction_status(pool, ext_id, "done", progress=...)
await db_mod.ensure_job(pool, ext_id, doc_id, "outbound", ...)
```

Status becomes `done`. The SSE stream emits the terminal `done` event; the frontend re-fetches the full extraction (with `corrected_result`, `field_locations`, etc.) and renders the result panel.

---

## Stage 5 — `outbound` (`worker.py:_process_outbound` lines 989–1076)

**Goal**: render the contract to Excel + CSV, store in MinIO, write `integration_deliveries` rows.

```python
contract = build_purchase_order_contract(extraction_row)
excel_bytes = build_excel_bytes(contract)
csv_bytes = build_csv_bytes(contract)
store.put_bytes(EXPORTS_BUCKET, xlsx_key, excel_bytes, ...)
store.put_bytes(EXPORTS_BUCKET, csv_key, csv_bytes, ...)
await db_mod.save_export_artifact(pool, ext_id, xlsx_key)
await db_mod.upsert_delivery(pool, ext_id, ..., target_type="excel", object_key=xlsx_key)
await db_mod.upsert_delivery(pool, ext_id, ..., target_type="csv", object_key=csv_key)
```

See [export.md](export.md) for the contract shape and how the Excel/CSV are built.

This stage is "fire and forget" relative to the user — by the time outbound finishes, the user has already seen the result. The exports are downloadable on demand via:
- `GET /extractions/{id}/export.xlsx`
- `GET /extractions/{id}/export.csv`

If outbound fails (e.g. MinIO unreachable), the extraction stays `done` (correct, the result is good); the delivery row is marked `failed` with an error message; the user can retry from the UI.

---

## Frontend: how the user experiences this

`frontend/extract.js`:

1. User picks a file → `handleFile()` shows a preview via `POST /upload-preview` (synchronous, separate endpoint that returns first 5 pages as base64 — for instant feedback).
2. User clicks **EXTRACT** → `runExtract()`:
   - Uploads via `POST /ingest/ui`.
   - Receives `{job_id, extraction_id, detected_vendor}`.
   - Calls `streamJob(jobId)` which opens a `fetch` (with AbortController for cancel) at `/jobs/{job_id}/stream`.
   - For each SSE event, updates the pipeline visualization (`updatePipelineFromSSE`).
   - On terminal events (`done`, `failed`, `partial`), settles the UI.
3. While streaming, the **STOP** button posts to `/jobs/extractions/{id}/cancel` which sets `cancel_requested=TRUE`. The next page boundary in the LLM worker exits with `status='partial'` if any pages succeeded, `cancelled` otherwise.
4. **Resume**: if status is `partial`, a button calls `/jobs/extractions/{id}/resume` which re-enqueues the LLM stage with `start_from_page=last_completed_page+1` and `existing_page_results=[...]`.

Pipeline UI stages are slightly more granular than the backend stages (it shows `upload`, `detect`, `normalize`, `ocr`, `llm`, `json`, `postprocess`); these are visual labels mapped from `progress.stage`.

---

## Format types — what they mean

| `format_type` | Description | Result shape |
|---|---|---|
| `single_po_multipage` | One PO across many pages. Header on page 1; line items continue. | `dict` (header + line_items) |
| `po_per_page` | Each page is a self-contained PO. | `list[dict]` (one per page) |
| `single_page` | One-page document. | `dict` |

The merger in `extractor.merge_results` switches on this. Postprocess and spatial memory both check `isinstance(result, list)` to handle `po_per_page`.

---

## Three modes of cancellation

1. **User clicks STOP**: `cancel_requested=TRUE` → next page boundary exits.
2. **Worker crash mid-job**: job is in `running`. Stale-job recovery (every 60s) resets to `queued`. A live worker re-claims and either resumes or restarts (depending on stage idempotency).
3. **`asyncio.CancelledError` mid-LLM-call**: the `cancel_event` race in `call_llm` cancels the in-flight HTTP request, freeing GPU immediately.

---

## What does NOT happen here

- **No automatic retries on LLM failure.** A page that errors three times in a row results in `status='partial'`. The user retries manually.
- **No quality gates.** The pipeline doesn't validate fields against expected formats (date parsers, currency parsers). It produces what Qwen returns, normalized for whitespace.
- **No multi-vendor splitting.** A document is assumed to belong to a single vendor. If it contains POs from two different vendors, the user gets one detected vendor and Qwen extracts what it can.
- **No PII redaction or scrubbing.** Whatever's on the document lands in the result.
