# LLM Pipeline Stage & Worker

> Source files:
> - [backend/worker.py](../../backend/worker.py) — processing stage orchestrator (`_process_llm`)
> - [backend/extractor.py](../../backend/extractor.py) — Qwen3-VL orchestration interface

The **LLM (Large Language Model)** stage is the third step in the document extraction pipeline. It is responsible for parsing document page images using a vision-language model to extract structured key-value text fields.

---

## What it is

The LLM stage uses the **Qwen3-VL** model (running on a local `llama.cpp` server) to extract invoice and purchase order fields from page images. 

It reads fields, values, and table line items from each page. On **Page 1**, it also requests the model to ground the exact coordinates (bounding boxes) of the field labels. This allows the system to learn the page layout automatically without predefined hardcoded templates.

---

## How it works

The LLM worker runs as a background process polling the PostgreSQL `jobs` table for tasks of type `llm`.

```
           [Claim LLM Job]
                  │
                  ▼
       [Load Template & Hints]
                  │
                  ▼
         [Phase A: Page 1]
    (Request Fields + BBox Coords)
                  │
                  ▼
         [Save Page 1 Boxes]
       (To qwen_layout_boxes)
                  │
                  ▼
        [Phase B: Pages 2-N]
      (Request Fields Only)
      (Parallel Batch Loop)
                  │
                  ▼
           [Merge Results]
         (Consolidate Items)
                  │
                  ▼
         [Save page_results]
                  │
                  ▼
     [Trigger Postprocess Worker]
```

### Step-by-Step Execution Lifecycle

1. **Job Claiming**: Claims a pending job of type `llm` from the database. It locks the row via `FOR UPDATE SKIP LOCKED`.
2. **Template & Gold Corrections Load**:
   - Loads the target vendor's extraction template (header fields, line-item columns, format type, rules).
   - Retrieves verified correction records from `gold_examples` for few-shot prompt hinting.
3. **Prompt Building & Caching**:
   - Builds two distinct system prompts:
     - **Page 1 Prompt**: Instructs Qwen to return both extracted text values and the normalized `[x1, y1, x2, y2]` label bounds.
     - **Pages 2+ Prompt**: Instructs Qwen to return values only (saving prompt window tokens).
   - Computes a hash of the prompt. If it matches the template's cached hash, it loads the prompt from the database rather than rebuilding it.
4. **Phase A — Sequential Page 1 Run**:
   - The worker processes Page 1 alone before processing subsequent pages.
   - If Page 1 succeeds, it extracts the returned `boxes` coordinates (relative 0-1000 grid), normalizes them to `coord / 1000.0`, and upserts them into the `qwen_layout_boxes` table.
5. **Phase B — Batch Parallel Pages 2-N**:
   - Remaining pages (Pages 2-N) are processed in parallel batches of size `LLM_PAGE_BATCH_SIZE` (default 2).
   - Each page is sent to Qwen with the "fields only" system prompt.
6. **Incremental Progress Saving**:
   - The worker runs an `on_page_done` callback as each page finishes.
   - It updates the database incrementally by appending to the `page_results` JSON array, allowing the frontend UI to display live page-by-page progress indicators via Server-Sent Events (SSE).
7. **Consolidation & Merging**:
   - Once all pages finish, the worker calls `merge_results()`.
   - **`single_po_multipage`**: Merges headers from Page 1 with line items collected from all pages.
   - **`po_per_page`**: Flattens pages into a list of separate documents.
   - **`single_page`**: Returns the single page's output directly.
8. **Next Stage Triggering**:
   - If successful, it updates the extraction status to `processing` (awaiting postprocess) and enqueues a `postprocess` job.
   - If cancelled or failed, it releases the page quota reservation via `release_quota_once` and marks status `cancelled` or `partial`.

---

## Rules & Hard Constraints

- **Deterministic Page 1 Grounding**: Page 1 must run sequentially and alone. This ensures layout-box grounding is completed and written to `qwen_layout_boxes` before the postprocess stage runs.
- **Progressive JSON Parsing Fallbacks**: The LLM output is text that must be parsed as JSON. If the initial parse fails, the engine applies these recovery rules:
  1. **Leading-Zero Repair**: Detects unquoted numbers with leading zeros (e.g. `0070`) and quotes them to prevent JSON parser exceptions.
  2. **json-repair library**: Uses the `json_repair` tool to fix missing brackets, unquoted keys, or trailing commas.
  3. **Usage Telemetry Permanence**: If parsing fails after all repairs, the engine still records the token metrics via `record_llm_usage` before throwing a `ValueError`. Tokens were consumed by the GPU server even if the output was unparseable.
- **LLM Usage buffer**: If a database connection error occurs while saving token usage, metrics are queued in an in-memory `_failed_usage_buffer` cache and retried during subsequent page runs.
- **In-flight Cancellation Race**: If a user cancels an extraction, the HTTP request to the llama-server is aborted immediately using `asyncio.wait(cancel_event.wait())` to save GPU cycles on the inference server.

---

## All Scenarios in Plain English

### Scenario 1 — Normal multi-page purchase order run
- A 3-page PDF is processed.
- Page 1 is sent sequentially. Qwen returns fields and bounding boxes for the labels.
- The worker saves Page 1 boxes to the layout store.
- Pages 2 and 3 are sent in parallel. Qwen extracts fields only.
- The merger aggregates Page 1 header fields and Page 1, 2, and 3 line items into a single document structure.
- The worker enqueues a `postprocess` job.

### Scenario 2 — Extraction with page-level failure
- A 4-page PDF is processed. Page 1 and 2 batches succeed.
- Page 3 encounters a model generation timeout (HTTP 500).
- The worker catches the error, appends an error object to `page_results` indicating that Page 3 failed, and halts the extraction pipeline. Page 4 is never sent to the LLM.
- The worker sets status to `partial` (because some pages succeeded), releases the quota reservation, and triggers no further stages.

### Scenario 3 — Aborted mid-extraction
- A user clicks the "Cancel" button in the UI while page 4 of a 5-page PDF is processing.
- The server sets the `cancel_requested` flag to `TRUE` in the database.
- During the `on_page_done` callback, the worker polls the cancel flag, triggers the cancel event, and aborts the in-flight HTTP request.
- The worker updates the status to `partial` (since pages 1-3 were already saved), releases outstanding page quotas, and raises `JobCancelled`.

### Scenario 4 — JSON Repair success
- Qwen generates a result page but fails to close a bracket at the end of the JSON object.
- Standard `json.loads` fails.
- The engine runs progressive repair using the `json_repair` library.
- The bracket is repaired, the JSON is successfully parsed, and a warning is logged.
- The results are persisted, and the pipeline continues normally.

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_extractor_merge.py`](../../tests/test_extractor_merge.py) | `test_merge_results_multipage` | Proves that the merger consolidates header fields from Page 1 with line items from all pages correctly. |
| | `test_po_per_page_no_merge` | Verifies that `po_per_page` formatting keeps pages separate instead of merging them. |
| [`test_extractor_no_boxes.py`](../../tests/test_extractor_no_boxes.py) | `test_extractor_handles_missing_boxes` | Proves that if Page 1 Qwen output contains no bounding boxes, the system handles it gracefully instead of crashing. |
| [`test_llm_usage.py`](../../tests/test_llm_usage.py) | `test_usage_saved_on_llm_failure` | Proves that if LLM output fails to parse, token counters are still recorded to `llm_usage` for billing. |
| | `test_usage_buffer_retry` | Verifies that database errors during usage recording queue payloads to the in-memory recovery buffer. |

---

## Quick Reference

| Config Parameter | Default Value | Notes |
|---|---|---|
| `LLM_PAGE_BATCH_SIZE` | `2` | Number of concurrent LLM requests for pages 2-N |
| `LLM_MAX_TOKENS_FIELDS` | Custom | Maximum output token generation limit |
| `LLM_TIMEOUT` | `120.0s` | HTTP timeout for vision generation calls |
| Caching table | `templates` | Stores `system_prompt` and `prompt_hash` |
| BBox storage table | `qwen_layout_boxes` | Stores normalized coordinates for Page 1 labels |
