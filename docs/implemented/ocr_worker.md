# OCR Pipeline Stage & Worker

> Source files:
> - [backend/worker.py](../../backend/worker.py) — processing stage orchestrator (`_process_ocr`)
> - [backend/ocr_runner.py](../../backend/ocr_runner.py) — PaddleOCR engine wrapper
> - [backend/geometry.py](../../backend/geometry.py) — geometry normalizer and merger

The **OCR (Optical Character Recognition)** stage is the second step in the document extraction pipeline. It is responsible for detecting word texts and bounding boxes on scanned document pages.

---

## What it is

The OCR stage locates text on scanned pages so that review operators can draw bounding boxes to correct wrong fields in the UI.

To save processing time, memory, and CPU/GPU cycles, **OCR is only run on scanned pages**. Digital pages (which have embedded text structures) skip OCR entirely because their word locations were already extracted during the preceding [Normalize](normalizer.md) stage using `pypdfium2`.

---

## How it works

The OCR worker runs as a background process polling the PostgreSQL `jobs` table for tasks of type `ocr`.

```
           [Claim OCR Job]
                  │
                  ▼
         [Load Page Metadata]
                  │
                  ├── (All pages digital) ───┐
                  │                          ▼
                  │                 [Reuse pypdfium2 words]
                  ▼                          │
         [Load Scanned Images]               │
                  │                          │
                  ▼                          │
         [Run PaddleOCR]                     │
      (Parallel Thread Pool)                 │
                  │                          │
                  ▼                          ▼
         [Merge geometries] ─────────────────┘
                  │
                  ▼
         [Save to ocr_data]
                  │
                  ▼
     [Trigger Postprocess Worker]
```

### Step-by-Step Execution Lifecycle

1. **Job Claiming**: The worker claims a pending job of type `ocr` from the database. It locks the row via `FOR UPDATE SKIP LOCKED`.
2. **Page Source Identification**: The worker loads all page metadata for the extraction from the `pages` table. It reads the job payload's `scanned_page_numbers` list to identify which pages are scanned (i.e., those whose `source` column is not `pypdfium`).
3. **Branching Logic**:
   - **All-Digital Skip**: If no pages require OCR, the worker skips PaddleOCR completely. It maps the `word_geometry` already stored in the `pages` table directly into a unified schema and writes it to `ocr_data`.
   - **Scanned Run**: If scanned pages are present, the worker fetches their images from MinIO/local store using `_load_pages` (which decodes page bytes into base64).
4. **Parallel OCR Execution**:
   - The worker runs `ocr_runner.run_ocr_on_pages()`, which processes page images concurrently in a `ThreadPoolExecutor` with a maximum of 3 threads.
   - For each page, the thread-local PaddleOCR engine runs detection (`predict()`) and outputs a word list containing detected text strings, bounding box arrays `[x0, y0, x1, y1]`, and confidence scores.
5. **Geometry Merging**:
   - The worker calls `geometry.merge_scanned_into_geometry()`. 
   - This keeps `pypdfium` word bounding boxes for digital pages and merges in the freshly computed PaddleOCR word boxes for scanned pages.
6. **Result Persistence**: Saves the merged page-level geometries (words, boxes, sources, character counts, and word counts) as a JSON array in the `ocr_data` column on the `extractions` table.
7. **Enqueue Postprocess**: The worker calls `_maybe_enqueue_postprocess()`. If both `result` (from the LLM worker) and `ocr_data` are present, it enqueues a `postprocess` job.

---

## Rules & Hard Constraints

- **Scanned Pages Only**: PaddleOCR must never execute on digital pages. Running OCR on digital text is a waste of resources and can reduce coordinate snapping accuracy.
- **Multithreading Executor**: The OCR runner uses a `ThreadPoolExecutor(max_workers=3)` to process up to 3 pages in parallel. Higher limits are restricted to prevent CPU thrashing or CUDA out-of-memory crashes.
- **Thread-Local Lazy Loading**: The PaddleOCR engine is large and slow to boot. It is lazily initialized on a thread-local basis (`threading.local()`) so that a thread only loads the model into memory the first time it claims a page run.
- **Fail-Fast Boundary**: If OCR fails on any page (e.g. image decoding issue, memory exhaustion), the runner throws `OCRUnavailable`. The job is marked `failed`, quota reservations are released immediately, and the entire extraction status is set to `failed` to prevent postprocessing on partial data.
- **Coordinate Space Alignment**: Bounding boxes returned by OCR are in the pixel dimensions of the final page image (which is pre-aligned to a multiple of 32px to match Qwen's grid).

---

## All Scenarios in Plain English

### Scenario 1 — All pages in the PDF are digital
- The PDF contains vector text. During normalization, all pages were classified as `source = 'pypdfium'`.
- The OCR job is claimed. The payload contains an empty `scanned_page_numbers` list.
- The worker skips PaddleOCR completely.
- It queries the `pages` table, copies the pre-extracted `word_geometry` directly into the unified shape, and saves to `ocr_data`.
- It attempts to enqueue the postprocess stage.

### Scenario 2 — All pages in the PDF are scanned images (scanned PDF or uploaded photo)
- The PDF contains scanned images. During normalization, all pages were classified as `source = 'paddleocr'`.
- The OCR job payload lists all page numbers as scanned.
- The worker downloads the page images and feeds them into the OCR thread pool.
- PaddleOCR processes the pages concurrently, returns the detected word boxes, and writes them to `ocr_data`.
- It attempts to enqueue the postprocess stage.

### Scenario 3 — Mixed digital and scanned pages
- The PDF contains some vector text pages (e.g. terms sheet) and some scanned invoice images.
- The payload lists only the scanned page numbers.
- The worker downloads images *only* for the scanned pages.
- PaddleOCR runs *only* on those pages.
- The merging logic takes digital words from the `pages` table for pages 1 and 3, and gets scanned words from PaddleOCR for page 2, producing a single merged `ocr_data` block.
- It attempts to enqueue the postprocess stage.

### Scenario 4 — PaddleOCR failure
- A scanned page image is corrupted in the object store.
- The worker claims the OCR job, loads the image, and sends it to the OCR engine.
- OpenCV fails to decode the image array, causing `predict()` to raise an exception.
- The runner catches the error, sets the `_ocr_error` key, and throws `OCRUnavailable`.
- The worker intercepts this, sets the extraction status to `failed`, releases the page quota reservation via `_release_failed_job_quota()`, and marks the job status as `failed` with the error trace.

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_ocr_runner_hardening.py`](../../tests/test_ocr_runner_hardening.py) | `test_ocr_failure_raises_ocr_unavailable` | Verify that OCR engine exceptions raise `OCRUnavailable` and fail the job rather than logging empty text silently. |
| | `test_run_ocr_empty_pages` | Verify that passing an empty list returns immediately. |
| [`test_ocr_multithread.py`](../../tests/test_ocr_multithread.py) | `test_concurrent_ocr_execution` | Proves that the thread-local lazy engine initialization and thread pool process pages concurrently without race conditions. |
| [`test_word_pdlocr.py`](../../tests/test_word_pdlocr.py) | `test_ocr_merges_scanned_geometry` | Verifies that scanned text geometries align correctly with the digital base layouts in `geometry.merge_scanned_into_geometry`. |

---

## Quick Reference

| Query / Operation | Source Field | Target Table/Field | Notes |
|---|---|---|---|
| Check if page is digital/scanned | `pages.source` | n/a | `pypdfium` (digital) vs `paddleocr` (scanned) |
| Save combined word coordinates | `ocr_pages.words` | `extractions.ocr_data` | Format: `[{page_number, source, words: [{text, box: [x0,y0,x1,y1]}]}]` |
| Check if postprocess is ready | `extractions.result`, `extractions.ocr_data` | `jobs` | Enqueues postprocessing if both fields are NOT null |
| Recover stuck OCR jobs | `jobs.status` | `jobs.status` | Recovered back to `queued` if stuck in `running` > 10 min |
