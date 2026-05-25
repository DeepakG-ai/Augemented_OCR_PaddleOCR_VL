# Failure Modes & System Auditing Report (Antigravity)

This document provides a highly detailed, professional audit of the Augmented OCR codebase (`PaddleOCR + Qwen-VL` pipeline) located at `c:\Users\aigroup5\PycharmProjects\Augemented_OCR_PaddleOCR_VL`. It compiles structural code bugs, test suite defects, key system exceptions, and hardware/VRAM transition strategies.

---

## Contents
1. [🚨 Executive Summary of Critical Findings](#-executive-summary-of-critical-findings)
2. [🔍 Detailed Technical Findings (Backend Bugs & Logical Flaws)](#-detailed-technical-findings-backend-bugs--logical-flaws)
3. [🧪 Test Suite Defects & False Positives](#-test-suite-defects--false-positives)
4. [📊 Core Exceptions & Failure Modes Register](#-core-exceptions--failure-modes-register)
5. [🖥️ Hardware Sizing & VRAM Strategy: 8GB vs. 32GB VRAM](#️-hardware-sizing--vram-strategy-8gb-vs-32gb-vram)

---

## 🚨 Executive Summary of Critical Findings

1. **Subscription Quota Bypass on Zero Limit (Critical Logical Bug):** The condition `u_limit > 0 and u_used >= u_limit` enables new users with a default limit of `0` to bypass the quota check entirely and obtain infinite page extractions.
2. **Namespace Collision with Local `bottleneck` Folder (Import Shadowing):** A local `bottleneck/` folder shadows the third-party pip module `bottleneck`, causing PaddleOCR/pandas initialization to raise an `ImportError` and breaking the global test collection.
3. **Dead Cancellation Logic (In-Flight Abort Impossible):** The in-flight VLM request cancellation event is local to `_process_llm` and is only set inside `on_page_done`. But `on_page_done` is only triggered *after* the task completes, rendering it unreachable and impossible to cancel active requests.
4. **Permanent Ingestion Lock Vulnerability (Stuck Schedule State):** A crash, restart, or DB timeout during cron folder scanning leaves the `is_executing` flag in `user_schedules` stuck as `True` forever with no recovery sweepers, permanently blocking manual UI uploads with HTTP `409 Conflict`.
5. **Muted Ingestion Lock Design Flaw:** The lock intended to prevent concurrent scheduled and manual UI uploads is reset immediately after jobs are enqueued in `jobs`, rather than waiting for the worker to finish the actual pipeline steps.
6. **Hardcoded Sequential VLM Processing (`PARALLEL_BATCH = 1`):** In contrast to documentation detailing multi-page parallel batches to utilize `llama-server` slots, batch sizes are hardcoded to `1`, locking high-performance servers into slow sequential runs.
7. **Hardcoded CPU-Only PaddleOCR Execution:** PaddleOCR is hardcoded to run on `device="cpu"` to prevent VRAM competition in 8GB local setups, underutilizing available GPU acceleration on powerful AWS servers.
8. **Aggressive Image Downscaling Cap (`960px`):** Dense tables and small fonts on high-resolution invoices are aggressively downscaled with no hardware-adaptive threshold, harming extraction accuracy.

---

## 🔍 Detailed Technical Findings (Backend Bugs & Logical Flaws)

### 2.1 Subscription Quota Bypass on Zero Limit
* **Locations**: 
  * [`backend/main.py:L1400`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L1400)
  * [`backend/main.py:L1817`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L1817)
* **Bug description**: 
  The quota enforcement guard is implemented as:
  ```python
  if u_limit > 0 and u_used >= u_limit:
  ```
  If a new user is created with a subscription limit of `0` (which is the default fallback for new accounts), the check `u_limit > 0` evaluates to `False`. As a result, the entire conditional block is skipped, bypassing the quota guard.
* **System Impact**:
  * Users with a `0` limit are granted **infinite page extractions** instead of being blocked.
  * **Test Failure Demonstration**: The test `test_blocked_zero_limit_zero_used` inside `tests/test_page_limits.py` fails. Because the quota check is bypassed, the mock server attempts to proceed with the ingest request. The test supplies a dummy PDF bytes (`b"%PDF-fake"`), which bypasses quota verification and crashes downstream during PDF rendering with `pypdfium2._helpers.misc.PdfiumError: Failed to load document (PDFium: Data format error)`, throwing an HTTP `500 Internal Server Error` instead of the expected HTTP `402 Payment Required`.
* **Fix**:
  Modify the condition to enforce quota blocking whenever `u_limit == 0` or standard limits are exceeded:
  ```python
  if u_limit == 0 or (u_limit > 0 and u_used >= u_limit):
  ```

---

### 2.2 Namespace Collision with Local `bottleneck` Folder (Import Shadowing)
* **Location**: Root directory folder `/bottleneck`
* **Bug description**:
  The project contains a local folder named `bottleneck/` (used to store text/markdown notes). However, the Python package `pandas` (which is imported by PaddleOCR) relies on the third-party pip library `bottleneck` for optimized calculations. 
  Because Python puts the current working directory (`""`) at the front of `sys.path` during execution, importing `paddleocr` resolves the package name `bottleneck` to the local directory instead of the installed pip package.
* **System Impact**:
  * This namespace collision raises a cryptic `ImportError: Can't determine version for bottleneck` when initializing PaddleOCR.
  * **Global Test Impact**: Any scripts importing `paddleocr` (e.g., `scripts/test_vllm.py`) immediately crash at import time. This import collision breaks the global test collection, forcing developers to restrict tests to manual, specific paths.
* **Fix**:
  Rename the local directory `bottleneck/` to a non-colliding folder name like `docs/performance_bottlenecks/` or `performance/`.

---

### 2.3 Dead Cancellation Code — In-Flight HTTP Requests Cannot Be Cancelled
* **Location**:
  * [`backend/extractor.py:L378-L393`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/extractor.py#L378-L393)
  * [`backend/worker.py:L567-L589`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/worker.py#L567-L589)
* **Bug description**:
  The backend attempts to abort in-flight VLM HTTP requests using a `cancel_event` inside `call_llm` to save GPU/VRAM cycles when a user cancels. However, the event is local to `_process_llm` and is set only inside the `on_page_done` callback:
  ```python
  if await db_mod.is_cancel_requested(pool, extraction_id):
      cancel_event.set()
  ```
  But `on_page_done` is only called **after** a page's `call_llm` task completes in `extract_document`!
* **System Impact**:
  Since nothing checks the database for cancellation *while* `call_llm` is actively waiting on the HTTP call, `cancel_event` is never set during the in-flight request. The `asyncio.wait` racing block inside `call_llm` will always run to completion.
* **Fix**:
  Introduce a lightweight background polling task in `_process_llm` that periodically checks `db_mod.is_cancel_requested` and sets `cancel_event` while pages are processing.

---

### 2.4 Permanent Ingestion Lock Vulnerability (Stuck Schedule State)
* **Location**:
  * [`backend/scheduler.py:L167-L193`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/scheduler.py#L167-L193)
  * [`backend/db.py:L2972-L2981`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/db.py#L2972-L2981)
* **Bug description**:
  When the cron schedule runs, it sets `is_executing = True` on the schedule row. If the worker process or backend container crashes, restarts, gets OOM-killed, or encounters a database deadlock during `_run_schedule`, the `finally` block that resets `is_executing = False` never runs. 
* **System Impact**:
  There is **no recovery or sweeper mechanism** for stale `is_executing` flags in the `user_schedules` table (unlike the `jobs` table, which has a robust startup/periodic sweeper `recover_stale_jobs`). It remains stuck in `True` forever, and the user is permanently blocked from uploading documents via the UI (receiving an indefinite `409 Conflict` "Scheduler is currently running...").
* **Fix**:
  Implement a periodic sweeper or startup recovery task in `scheduler.py` that resets any `is_executing` flag to `False` if it has been running for a stale period (e.g., >30 minutes).

---

### 2.5 Ingestion Lock Muted by Async Enqueueing
* **Location**:
  * [`backend/scheduler.py:L167-L193`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/scheduler.py#L167-L193)
  * [`backend/main.py:L138-L210`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L138-L210)
* **Bug description**:
  The lock `is_executing` is wrapped around the `pdfs` loop in `_run_schedule`. However, the `ingest_cb` (which is `_folder_ingest_callback`) only enqueues the initial `normalize` job in the `jobs` table and returns immediately.
* **System Impact**:
  The `_run_schedule` finishes in a few seconds and resets `is_executing` to `False` while the background worker is still actively running the CPU-intensive OCR, VLM, and postprocess steps. The ingestion lock is released long before the worker actually finishes processing the scheduled documents, completely failing to prevent concurrent schedule/UI operations.
* **Fix**:
  Instead of wrapping `is_executing` around the scheduler's enqueueing loop, track whether there are any active jobs in `queued` or `running` state with `source_type = 'folder'` for that user in the `jobs` table.

---

### 2.6 Hardcoded Sequential VLM Processing
* **Location**: [`backend/extractor.py:L535`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/extractor.py#L535)
* **Bug description**:
  The comments and documentation state that pages are processed in parallel batches of 2 to match `--parallel 2` on `llama-server`. However, `PARALLEL_BATCH` is hardcoded to `1` in `extract_document`:
  ```python
  PARALLEL_BATCH = 1  # Sequential: process page 1 before page 2
  ```
* **System Impact**:
  Multi-page documents are processed purely page-by-page, leading to slow processing times. High-end hardware and multiple LLM slot allocations are heavily underutilized.
* **Fix**:
  Make `PARALLEL_BATCH` configurable via `config.py` (e.g., default to `2` or `4` on systems with high VRAM), allowing parallel in-flight VLM calls.

---

## 🧪 Test Suite Defects & False Positives

### 3.1 Obsolete Bounding Box Normalization in `test_single_agent_bbox.py`
* **Location**: [`tests/test_single_agent_bbox.py`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/tests/test_single_agent_bbox.py)
* **Bug description**:
  * The test class `Factor32NormalizationTests` asserts coordinate alignment adjustments based on a `FACTOR = 32` normalization adjustment.
  * However, the test file **redefines a local duplicate copy** of the old `_normalize_box` logic within the test itself, rather than importing and verifying the actual code from the production files.
  * The actual codebase in `backend/worker.py` (lines 660-663) has **completely removed** the `FACTOR = 32` correction because Qwen3-VL relative coordinate ranges operate on 0–1000 scales without llama.cpp GGUF 32px absolute boundary restrictions.
* **System Impact**:
  The test passes successfully but is a **false positive**. It verifies an obsolete coordinate alignment that no longer exists in the production pipeline, potentially masking alignment mismatches on actual inputs.
* **Fix**:
  Remove the local duplicate redefinition and update the tests to import and verify the actual bounding box transformation function from `backend/spatial_memory.py` or `backend/worker.py` (i.e. `raw_box[i] / 1000.0` relative coordinates).

---

### 3.2 Global FastAPI `dependency_overrides` State Leakage
* **Location**: [`tests/test_page_limits.py`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/tests/test_page_limits.py)
* **Bug description**:
  The test classes in `test_page_limits.py` override the `get_current_user` dependency globally by writing directly to `main.app.dependency_overrides`. However, they **do not restore or clear** these overrides during `tearDown`.
* **System Impact**:
  FastAPI's dependency overrides are global. When running the entire test suite, subsequent test files run in the same process will inherit these mock credentials (e.g., matching users with the mock email `admin@test.com` instead of the standard fixtures defined in `conftest.py`). This creates flaky tests, side effects, and state leakage across the suite.
* **Fix**:
  Safeguard the global state inside `setUp` and restore it in `tearDown`:
  ```python
  def setUp(self):
      self._saved_overrides = dict(main.app.dependency_overrides)

  def tearDown(self):
      main.app.dependency_overrides.clear()
      main.app.dependency_overrides.update(self._saved_overrides)
  ```

---

### 3.3 Hardcoded Sequential Constraint in Concurrency Tests
* **Location**: [`tests/test_extractor_concurrency.py`](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/tests/test_extractor_concurrency.py)
* **Bug description**:
  The concurrency tests hardcode assertions checking that `max_active == 1` inside `extract_document`. Meanwhile, production documentation in `backend/extractor.py` indicates a target performance batching of `PARALLEL_BATCH = 2` to optimize llama-server throughput. The actual code is constrained to sequential execution to keep the test green.
* **System Impact**:
  Performance optimizations (such as batching multiple page extractions concurrently) are artificially blocked because the test suite hardcodes sequential batch assertions.
* **Fix**:
  Refactor the test assertions to accept configurable parallel batch bounds, letting the pipeline run in parallel under high VRAM servers.

---

## 📊 Core Exceptions & Failure Modes Register

The following register details failure modes under various system exceptions, including how they propagate and mitigation strategies:

| Component | Failure Scenario | Triggering Condition | Current System Behavior | Impact & Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| **Database Pool** | Connection Depletion | Concurrency reaches `max_size=10` limit under load. | Unhandled `asyncpg` timeout raising HTTP 500 error. | **Mitigation**: Wrap pool acquisition; catch pool exhaustion and return HTTP `503 Service Temporarily Unavailable` with a `Retry-After` header. |
| **Scheduler** | Permanent Thread Lock | Database exception occurs inside the cleanup `finally` block of `_run_schedule`. | The `is_executing` flag remains set to `True` forever. | **Mitigation**: Add try-except within the `finally` block; log failures and introduce an automated watchdog query to break stale scheduler locks older than 10 minutes. |
| **PDF Parser** | Damaged / Malformed PDF | Client uploads corrupted PDF bytes via UI or API. | Downstream `pypdfium2` raises `PdfiumError: Data format error` inside executor. | **Mitigation**: Wrap `pdfium.PdfDocument` instantiation in a try-except block; return HTTP `400 Bad Request` with description rather than crash with HTTP 500. |
| **PDF Parser** | Page Index Mismatch on skipped pages | Page 2 of a 3-page PDF fails to render and is skipped. | Total pages becomes 2 (`len(rendered)`), but page 3 is indexed as page 3, prompting the VLM with `"page 3 of 2"`. | **Mitigation**: Re-index page numbers of successfully rendered pages sequentially, or pass the absolute total PDF pages to the LLM prompt. |
| **PaddleOCR** | Image Decoding Failures | Base64 string corrupted or invalid JPEG bytes processed. | `cv2.imdecode` returns `None` silently; PaddleOCR crashes downstream. | **Mitigation**: Add explicit `None` verification for decoded image matrices before passing inputs to `ocr.predict()`. |
| **Spatial Memory** | Inverted Bounding Boxes | User draws coordinates right-to-left (`x0 > x1` or `y0 > y1`). | Saved coordinates successfully in database but fails silent overlap calculations. | **Mitigation**: Add validation at saving time to swap coordinates if `x0 > x1` or `y0 > y1` (`x0, x1 = min(x0, x1), max(x0, x1)`). |
| **VLM Extraction** | JSON Repair Halting | Model returns truncated JSON that is repaired by appending missing braces. | The repaired JSON validates structurally but lacks required fields. | **Mitigation**: Compare repaired JSON fields against requested schema contract; flag outcomes in database with a `_repaired: True` attribute for visual audit. |

---

## 🖥️ Hardware Sizing & VRAM Strategy: 8GB vs. 32GB VRAM

The transition from a local developer machine (8GB VRAM) to an AWS Production Server (32GB VRAM) requires careful parameter tuning to avoid crashes or sub-optimal resource use.

### 4.1 Local Developer Environment (8GB VRAM RTX 4060)
* **Failure Vectors**:
  * **Simultaneous Models OOM**: Running both `PaddleOCR` and the `Qwen3-VL` model server concurrently on a single 8GB GPU will trigger Out-Of-Memory (OOM) failures or force active paging into system RAM, degrading processing speed.
  * **VLM Context Overflow**: High-resolution image coordinates, multi-page document prompt histories, and "Gold Examples" swollen by corrections will cause the LLM context window to expand, exceeding the 8GB limit.
* **Mitigation / Rules**:
  * Use small context window constraints (e.g., maximum context size of 4096 tokens).
  * Enforce sequential processing (`PARALLEL_BATCH = 1`) to ensure only one page is parsed at a time.
  * Limit `max_size=10` on the PostgreSQL connection pool to prevent concurrent worker scaling.
  * Force PaddleOCR to CPU execution using `device="cpu"` to leave VRAM for the VLM.
  * Cap `MAX_LONG_SIDE_PX` at `960px` to minimize context VRAM usage.

### 4.2 AWS Production Server (32GB VRAM)
* **New Scaling Failure Modes**:
  * **Starlette Thread Pool Saturation**: With 32GB VRAM, the backend can support concurrent executions. However, without strict queue limits in `worker.py`, multiple parallel ingestion jobs might submit parallel requests simultaneously, running into concurrent thread spikes and sudden GPU VRAM spikes.
  * **Database Bottlenecks**: As concurrent workers process more documents in parallel, the database connection pool limit of 10 (`max_size=10`) will exhaust, causing jobs to fail on connection timeouts.
* **AWS Optimization Checklist**:
  1. **Enable llama.cpp Parallelism**: Increase `--parallel` parameter in the llama-server to `4` or `8` to enable concurrent VLM request handling.
  2. **Enable Parallel Page Extraction**: Increase `PARALLEL_BATCH` inside `backend/extractor.py` to `2` or `4` to extract multiple document pages in parallel.
  3. **Scale Database Connection Pool**: Increase `max_size` in the `asyncpg` pool configuration to `25` or `30` to match worker concurrency.
  4. **Raise Image Resolutions**: Adjust pixel budget `MAX_LONG_SIDE_PX` inside `backend/processor.py` (e.g., up to `1600px` or `2048px`) to allow higher quality document rendering, improving VLM matching accuracy for small fonts and dense invoices.
  5. **Enable GPU PaddleOCR**: Expose and enable `device="gpu"` or `gpu_id` configs to run OCR on the GPU, dropping processing time from ~2 seconds per page to under 100ms.
