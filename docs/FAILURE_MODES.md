# System Failure Modes & Bug Register

> Generated: 2026-05-21  
> Scope: Full codebase audit (backend + tests). Docs in `docs/` folder intentionally excluded as source of truth.  
> GPU constraint: RTX 4060 8 GB VRAM (current). AWS 32 GB GPU notes called out separately.

---

## Contents

1. [CRITICAL — System-Breaking](#1-critical--system-breaking)
2. [HIGH — Data Integrity & Race Conditions](#2-high--data-integrity--race-conditions)
3. [MEDIUM — Silent Edge-Case Failures](#3-medium--silent-edge-case-failures)
4. [LOW — Minor & Unlikely](#4-low--minor--unlikely)
5. [Test Suite Defects](#5-test-suite-defects)
6. [Missing Test Coverage](#6-missing-test-coverage)
7. [AWS 32 GB GPU — Forward Notes](#7-aws-32-gb-gpu--forward-notes)

---

## 1. CRITICAL — System-Breaking

These cause data loss, permanent stuck state, or hard crashes in normal operation.

---

### C-01 · `db.py` — `get_vendor_object_keys()` NameError

**File**: `backend/db.py` ~line 1953  
**Bug**: References undefined variable `delivery_rows` inside `get_vendor_object_keys()`.  
**Failure**: `NameError` at runtime whenever any code path calls this function (e.g., vendor deletion).  
**Fix**: Audit the query; ensure the variable is defined before use.

---

### C-02 · `object_store.py` — Silent Write Failure on Local Fallback

**File**: `backend/object_store.py` — `put_bytes()`  
**Bug**: When MinIO is unavailable, the local fallback calls `target.write_bytes(data)` with no try/except. Disk-full or permission errors raise `OSError` that is uncaught.  
**Failure**: Document artifacts silently lost — caller receives no error signal. PDF is ingested, jobs are queued, but the backing bytes never exist. Every subsequent stage fails reading them.  
**Fix**: Wrap `write_bytes` in try/except, surface as explicit storage exception, propagate to caller.

---

### C-03 · `object_store.py` — Retrieval Failure Unlogged & Ambiguous

**File**: `backend/object_store.py` — `get_bytes()`  
**Bug**: `target.read_bytes()` raises `FileNotFoundError` with no logging context when the fallback file doesn't exist.  
**Failure**: Pipeline crashes mid-stage. Caller cannot distinguish "object never written" (bug in C-02) from "MinIO temporarily down". Both look identical.  
**Fix**: Catch and re-raise with context (`key`, `fallback_path`, reason).

---

### C-04 · `scheduler.py` — Permanent Schedule Lock on Exception

**File**: `backend/scheduler.py` — `_run_schedule()` ~line 191  
**Bug**: The `finally` block that calls `db_mod.set_schedule_executing(pool, schedule_id, False)` swallows its own exceptions. If the DB call fails, the schedule remains permanently locked in `executing=True`.  
**Failure**: That schedule never runs again without manual DB intervention. No alert is raised.  
**Fix**: Log the unlock failure; implement a watchdog query that resets stuck schedules older than N minutes (similar to the job stale-recovery pattern already in `db.py`).

---

### C-05 · `scheduler.py` — Race: File Deleted Between Glob and Ingest

**File**: `backend/scheduler.py` — `_run_schedule()` ~lines 161–181  
**Bug**: Files are globbed, then iterated. If the user deletes a PDF between the glob and `ingest_cb()`, `FileNotFoundError` is unhandled.  
**Failure**: Scheduler crashes mid-run. PDFs before the deleted file are ingested; those after are silently skipped.  
**Fix**: Wrap `ingest_cb()` per-file in try/except; log and continue on missing-file errors.

---

### C-06 · `extractor.py` — Malformed LLM Response Causes AttributeError in Handler

**File**: `backend/extractor.py` — `call_llm()` ~lines 405–457  
**Bug**: If `resp.json()` itself raises (`JSONDecodeError` or `httpx.ResponseError`), the exception is caught by an outer handler that tries to reference `resp_json` — which was never assigned.  
**Failure**: `AttributeError: name 'resp_json' is not defined` inside the exception handler. The real error is masked. LLM stage job crashes.  
**Fix**: Initialize `resp_json = {}` before the try block; handle the decode error explicitly.

---

### C-07 · `extractor.py` — Empty `{}` Returned as Successful Extraction

**File**: `backend/extractor.py` — `call_llm()` ~line 457  
**Bug**: When LLM returns `{"choices": []}` or a missing `message`, the function returns `{}`. Downstream `_process_page()` treats `{}` as a valid page result (no `_error` key), and it gets merged into final output.  
**Failure**: Silent data loss. A page that produced nothing appears to have succeeded. The extraction completes, the document is marked done, but all fields for that page are empty.  
**Fix**: Return `{"_error": "empty_choices", ...}` instead of `{}` so `merge_results` excludes the page.

---

### C-08 · `extractor.py` — `merge_results` Returns `{}` When All Pages Fail

**File**: `backend/extractor.py` — `merge_results()` ~line 777  
**Bug**: `if not valid_pages: return {}` — returns empty dict when all pages errored.  
**Failure**: Caller cannot distinguish "all pages failed" from "no pages were given". Extraction marked done with completely empty result. No error visible to user.  
**Fix**: Return a structured error sentinel like `{"_all_pages_failed": True, "errors": [...]}` and propagate to postprocess-worker to set extraction status to `failed`.

---

### C-09 · `mlflow_tracing.py` — MLflow Connection Lost Mid-Pipeline, Silent

**File**: `backend/mlflow_tracing.py` — `span()`  
**Bug**: MLflow connection errors during tracing are silently swallowed. `setup_mlflow()` at startup does not validate connectivity, so tracing appears enabled but emits nothing after connection drops.  
**Failure**: Complete loss of observability — no spans, no traces, no alerting. Only discovered by manually checking MLflow UI.  
**Fix**: Periodically probe the MLflow endpoint (or check on `span()` failure); emit a single structured warning log per N failures rather than silencing all.

---

### C-10 · `scheduler.py` — Concurrent UI Upload + Scheduler Creates Duplicate Jobs

**File**: `backend/main.py` — `ingest_document()` ~lines 1372–1377  
**Bug**: The advisory `get_user_is_executing()` check is non-atomic. Scheduler can start between the check and enqueue.  
**Failure**: Both UI upload and scheduled job run simultaneously for the same user. Two extraction jobs compete, writing to the same DB row, producing corrupted or doubled results.  
**Fix**: Enforce exclusion at enqueue time via a DB-level advisory lock or a `SELECT ... FOR UPDATE` guard on the user's active-job count.

---

## 2. HIGH — Data Integrity & Race Conditions

---

### H-01 · `db.py` — `delete_stale_*` Functions Crash on Empty Result String

**File**: `backend/db.py` — `delete_stale_qwen_layout_boxes()`, `delete_stale_spatial_memory()` ~lines 2246, 2270  
**Bug**: `int(result.split()[-1])` assumes asyncpg returns `"DELETE N"`. If result is `None` or empty, this raises `IndexError`/`AttributeError`.  
**Failure**: Template updates that trigger stale-cleanup crash, leaving stale layout/spatial data in DB.

---

### H-02 · `db.py` — User CRUD Parses asyncpg Status String Incorrectly

**File**: `backend/db.py` — `deactivate_user()`, `reactivate_user()`, `hard_delete_user()`, `update_user_subscription_limit()` ~lines 1229–2696  
**Bug**: All use `.endswith(" 1")` on the asyncpg status string instead of `== "UPDATE 1"`.  
**Failure**: Silent false-negatives if status string has unexpected formatting. Deactivation appears to succeed but the caller marks it complete anyway.

---

### H-03 · `db.py` — Billing Race: Vendor Deleted Between Owner Query and INSERT

**File**: `backend/db.py` — `record_llm_usage()` ~line 672  
**Bug**: Vendor owner is fetched in a separate query before billing INSERT. If vendor is deleted in the window, `billing_user_id` is wrong or `NULL`.  
**Failure**: LLM usage billed to wrong user, or to nobody. Revenue reporting incorrect; no error raised.  
**Fix**: Use a single `INSERT ... SELECT` that fetches the owner atomically.

---

### H-04 · `main.py` — Resume Extraction Race Condition

**File**: `backend/main.py` — `queue_resume_extraction()` ~lines 1862–1880  
**Bug**: Extraction status is checked at line 1808, but state can change before `enqueue_job()` at line 1870.  
**Failure**: Job enqueued for a now-deleted or already-completed extraction. Orphaned job sits in `queued` state indefinitely.

---

### H-05 · `db.py` — DB Pool Exhaustion Has No Graceful Degradation

**File**: `backend/db.py` — `create_pool()` — pool max_size=10  
**Bug**: At 10 concurrent connections, new requests block for 30 s then raise `asyncpg.TooManyConnectionsError` or timeout.  
**Failure**: Under load, requests return 500 with no user-visible explanation. No circuit breaker or queue-depth signal.  
**Fix**: Add `command_timeout` and a pool `max_cached_statement_lifetime`; surface a `503 Service Temporarily Unavailable` with `Retry-After`.

---

### H-06 · `spatial_memory.py` — Inverted Bounding Boxes Stored Without Validation

**File**: `backend/spatial_memory.py` — `_normalize_box()`, `save_from_corrections()`  
**Bug**: No check that `x0 < x1` and `y0 < y1`. An inverted box (user draws right-to-left) is stored as-is.  
**Failure**: Future `apply_to_extraction()` reads the inverted box, `_words_in_box()` finds no words, spatial memory silently returns nothing. Field appears blank with no error.

---

### H-07 · `spatial_memory.py` — `_words_in_box` Misses Words That Overlap But Don't Have Centre Inside Box

**File**: `backend/spatial_memory.py` — `_words_in_box()` ~line 137  
**Bug**: Tests `x0 ≤ cx ≤ x1` on word centre. A wide word that overlaps the box but has its centre outside is excluded.  
**Failure**: Short spatial memory regions (e.g., tight correction around a vendor code) miss half the token. Re-read value is truncated.

---

### H-08 · `vendor_detector.py` — Blank Page 1 Silently Fails Vendor Detection

**File**: `backend/vendor_detector.py` — `detect_vendor()` ~lines 246–248  
**Bug**: Returns `None` if `page_words` is empty with only a warning log. Worker proceeds with no vendor context.  
**Failure**: Extraction continues with no `vendor_id`. Template matching fails; LLM prompt is generic. Result is garbage. No 409 raised, no job failed.  
**Fix**: Raise an explicit exception (or return a typed error) so the worker fails the job cleanly.

---

### H-09 · `extractor.py` — JSON Repair Invents Missing Data

**File**: `backend/extractor.py` — `call_llm()` ~line 487  
**Bug**: `json_repair(raw, return_objects=True)` repairs truncated LLM output (e.g., partial JSON) by inventing closing braces. The repaired structure parses but may be missing fields.  
**Failure**: Extraction appears successful. `po_number`, `line_items`, or other fields are silently absent. Only detected at review time.  
**Fix**: After repair, validate the resulting dict against the expected schema (required keys presence check). Flag repaired results with `"_repaired": True` in `page_results`.

---

### H-10 · `contracts.py` — No `format_type` Validation Before Contract Build

**File**: `backend/contracts.py` — `build_purchase_order_contract()`  
**Bug**: Called from `worker.py` without checking `format_type`. Unknown or malformed type falls through to the `else` branch, wrapping a list as a single dict.  
**Failure**: Contract has wrong `document_count` and wrong field nesting. Export (Excel/CSV) silently contains incorrect data.

---

### H-11 · `ocr_runner.py` — One Bad Page Fails Entire PDF Batch

**File**: `backend/ocr_runner.py` — `run_ocr_on_pages()` ~line 149  
**Bug**: `asyncio.gather(*tasks)` without `return_exceptions=True`. One page raising any exception propagates and cancels all other pages in the batch.  
**Failure**: A single corrupted JPEG kills OCR for the entire PDF. Extraction job fails hard.  
**Fix**: Use `return_exceptions=True`; filter exceptions and flag per-page errors.

---

### H-12 · `ocr_runner.py` — Corrupted Base64 or JPEG Not Caught Before PaddleOCR

**File**: `backend/ocr_runner.py` — `_run_ocr_on_page()` ~lines 87–91  
**Bug**: `base64.b64decode()` can raise `binascii.Error`. `cv2.imdecode()` returns `None` on bad JPEG — no null check before passing to `ocr.predict()`.  
**Failure**: PaddleOCR crashes on `None` input. Job fails with unhelpful exception.

---

### H-13 · `processor.py` — PDF Constructor Outside Try Block Causes `UnboundLocalError`

**File**: `backend/processor.py` — `_render_pdf_sync()` ~line 89–96  
**Bug**: `pdf = pdfium.PdfDocument(file_bytes)` is called before the try block. If constructor throws, `pdf` is uninitialized. The finally block then tries `pdf.close()` → `UnboundLocalError`, masking the real error.  
**Failure**: Corrupted PDFs produce a confusing `UnboundLocalError` instead of a meaningful "PDF corrupt" message.

---

### H-14 · `logging_config.py` — File Handle Evicted While Another Thread Is Writing

**File**: `backend/logging_config.py` — `ExtractionLogHandler.emit()` ~line 135  
**Bug**: LRU cache of file handles evicts oldest when `_MAX_OPEN` is exceeded. A concurrent `emit()` call for that extraction writes to a closed file.  
**Failure**: `ValueError: I/O operation on closed file`. Log lines lost for older concurrent extractions.

---

### H-15 · `page_logger.py` — Cross-Process Log Corruption

**File**: `backend/page_logger.py` — `append_log()` ~lines 91–94  
**Bug**: Uses `threading.Lock()` (per-process) to guard file writes. Multiple worker processes (normalize, OCR, LLM, etc.) all write to the same log file with no cross-process coordination.  
**Failure**: Log lines interleave and corrupt each other under concurrent extraction load.  
**Fix**: Use `fcntl.flock()` on Linux / `msvcrt.locking()` on Windows for cross-process file locking, or funnel log writes through the API server.

---

### H-16 · `scheduler.py` — Invalid Cron Expression Accepted Silently

**File**: `backend/scheduler.py` — `sync_job()` ~lines 74–92  
**Bug**: No validation of cron syntax before creating `CronTrigger`. Invalid values like `"60 * * * *"` don't raise at creation time — APScheduler may accept or silently skip.  
**Failure**: User creates a schedule that never fires, with no feedback. Only discovered manually.  
**Fix**: Parse cron string and validate each field range (0–59, 0–23, etc.) before saving.

---

### H-17 · `scheduler.py` — PDF Stuck in Input Folder if Move Fails

**File**: `backend/scheduler.py` — `_move_pdf()` ~line 133  
**Bug**: `shutil.move()` fails on cross-filesystem moves or if destination already exists (Windows). Exception is logged but PDF stays in `input_folder`.  
**Failure**: PDF re-ingested on every subsequent schedule run, creating unlimited duplicate extractions.  
**Fix**: Check destination before move; rename with timestamp suffix on collision.

---

### H-18 · `extractor.py` — Line-Item Template / Return Schema Mismatch

**File**: `backend/extractor.py` — `build_user_message()` ~line 220  
**Bug**: When `header_fields` is empty, the template JSON becomes ambiguous about whether `line_items` is top-level or nested under `"fields"`. LLM returns top-level; `merge_results` expects it nested.  
**Failure**: `line_items` silently disappears from merged result. Extraction appears complete with empty line items.

---

## 3. MEDIUM — Silent Edge-Case Failures

---

### M-01 · `main.py` — `Content-Length` Parse Crash

**File**: `backend/main.py` — `MaxUploadSizeMiddleware.dispatch()` ~line 268  
**Bug**: `int(content_length_header)` with no try/except. Malformed header (e.g., `"chunked"`) raises `ValueError`.  
**Failure**: 500 instead of 413.

---

### M-02 · `main.py` — Quota Overage Calculation Inverted

**File**: `backend/main.py` — `ingest_document()` ~line 1410  
**Bug**: `overage = u_limit - u_used` should be `u_used - u_limit`. Currently returns a negative number.  
**Failure**: API response shows negative overage. Client-side logic that acts on overage may behave incorrectly.

---

### M-03 · `main.py` — JSON Form Fields Parsed Without Try/Except

**File**: `backend/main.py` — `ingest_document()` ~lines 1385–1386  
**Bug**: `json.loads()` on `header_fields` / `line_item_fields` form fields with no error handling.  
**Failure**: Malformed JSON in form submission → 500 instead of 400.

---

### M-04 · `main.py` — SSE Serializer Crashes on Decimal Types

**File**: `backend/main.py` — `stream_job_status_sse()` ~lines 1707–1710  
**Bug**: Custom `_serialize` only handles `datetime` → `isoformat()`. Any `Decimal` column in extraction rows raises `TypeError` and kills the SSE stream mid-response.  
**Failure**: Client SSE connection dies silently. Frontend shows spinner forever.

---

### M-05 · `main.py` — 0-Page PDF Queued as Valid Extraction

**File**: `backend/main.py` — `ingest_document()` ~lines 1482–1491  
**Bug**: `processor.pdf_to_images()` can return empty list without exception. No guard before enqueueing.  
**Failure**: Pipeline runs 5 stages on an empty extraction. LLM stage processes nothing. Extraction silently completes with empty result.

---

### M-06 · `db.py` — Alias Pattern Lowercased, Breaking Case-Sensitive Matching

**File**: `backend/db.py` — `insert_vendor_alias()` ~line 1301  
**Bug**: `.lower().strip()` applied unconditionally. Regex or case-sensitive alias patterns are destroyed.  
**Failure**: Vendor aliases using uppercase intent (`^ACME Corp` regex) fail to match after storage.

---

### M-07 · `db.py` — `client_seq` Migration Can Produce Duplicate Sequence Numbers

**File**: `backend/db.py` — `init()` schema migration ~lines 283–289  
**Bug**: Backfill uses `ROW_NUMBER()` but doesn't advance the sequence to `max(client_seq) + 1`. New vendors can collide with backfilled numbers.  
**Failure**: Two vendors under the same user get the same `client_seq`, breaking client-facing display numbers.

---

### M-08 · `spatial_memory.py` — `apply_to_extraction` Async Race on Concurrent Corrections

**File**: `backend/spatial_memory.py` — `apply_to_extraction()` ~line 510  
**Bug**: No locking around concurrent async tasks that read page geometry and write to the same result dict.  
**Failure**: Two spatial overrides for the same field produce non-deterministic last-write-wins output.

---

### M-09 · `geometry.py` — Page Count Mismatch Causes Index Misalignment

**File**: `backend/geometry.py` — `compute_pdf_geometry()` ~lines 96–118  
**Bug**: Silently breaks out of loop if `i >= len(pdf)`. Downstream code assumes 1:1 mapping between page geometry list and page list.  
**Failure**: Wrong geometry applied to wrong page. Bounding boxes are off. Spatial memory reads incorrect text.

---

### M-10 · `qwen_layout_apply.py` — `None` Conflates Two Different Failure Modes

**File**: `backend/qwen_layout_apply.py` — `_make_loc()` ~lines 72–77  
**Bug**: Returns `None` for both "page not found" and "page has zero dimensions". Caller creates `field_locations` with `"box": None` and strategy `"qwen_column_header_missing"` for both.  
**Failure**: Review UI shows field as present but with no box. User cannot correct it because there's no box to edit.

---

### M-11 · `processor.py` — All Corrupted Pages Returns Empty List Without Error

**File**: `backend/processor.py` — `_render_pdf_sync()` ~lines 101–162  
**Bug**: Skips pages with 0×0 dimensions but doesn't check if `results` is empty at return.  
**Failure**: Totally corrupted PDF returns `[]`. Downstream treats it as valid 0-page document (see M-05).

---

### M-12 · `config.py` — Env Var Type Errors Crash App With Cryptic Message

**File**: `backend/config.py` ~lines 20–89  
**Bug**: `int(os.getenv("JPEG_QUALITY", "92"))` and similar conversions raise `ValueError` if env var is set to a non-integer (typo, quotes, etc.).  
**Failure**: App fails to start. Error message points to Python line in config.py rather than the env var name.  
**Fix**: Wrap all env-var conversions in try/except with clear message: `"JPEG_QUALITY must be an integer, got: 'invalid'"`.

---

### M-13 · `scheduler.py` — Relative `input_folder` Path Resolves to Wrong Directory

**File**: `backend/scheduler.py` — `_run_schedule()` ~line 161  
**Bug**: `input_folder` from config is used as-is. If it's a relative path, it resolves relative to whatever the process's cwd is at run time.  
**Failure**: Scheduler watches wrong directory. No PDFs ingested. No error.

---

### M-14 · `extractor.py` — Single-Page Handler Applied to Wrong Page on Retry

**File**: `backend/extractor.py` — `extract_document()` ~lines 674–708  
**Bug**: `format_type == "single_page"` check triggers when `len(page_results) == 1`, but on retry starting from page 2, this is page 2 not page 1. Wrong merge logic applied.  
**Failure**: Wrong result structure used. Field mapping fails.

---

### M-15 · `vendor_detector.py` — Fuzzy Margin Threshold Not Adaptive

**File**: `backend/vendor_detector.py` — `_detect_fuzzy()` ~line 209  
**Bug**: `FUZZY_MIN_MARGIN = 5.0` is flat. At `best_score=50, second=45` (margin=5) it incorrectly passes; at `best_score=95, second=90` (margin=5) it incorrectly rejects.  
**Failure**: Low-confidence vendor matches accepted; high-confidence matches rejected. Wrong vendor detected or 409 fired on real vendor.

---

### M-16 · `auth.py` — SSE Auth Fails If DB Pool Is Temporarily Down

**File**: `backend/auth.py` — `get_current_user()` ~line 146  
**Bug**: Auth always queries DB even if the JWT token alone could confirm identity. If `pool.acquire()` times out, SSE connections fail even for valid tokens.  
**Failure**: All live extraction streams drop when DB has a brief hiccup. Users see the stream die.

---

### M-17 · `worker.py` — `store.get_bytes()` Returns `None`/Corrupt Without Check

**File**: `backend/worker.py` — `_load_pages()` ~lines 104–120  
**Bug**: No validation that `store.get_bytes()` returns non-None, non-empty bytes.  
**Failure**: `base64.b64encode(None)` → `TypeError`. Worker job crashes. Page images lost.

---

### M-18 · `extractor.py` — Normalizer Does Not Recurse Into Nested Line-Item Fields

**File**: `backend/extractor.py` — `normalize_header_values()` ~line 758  
**Bug**: Line items are passed through unchanged. If any line-item field is a nested dict (LLM sometimes returns `{"value": "...", "confidence": 0.9}`), `contracts.py` serializes it as a dict instead of a scalar.  
**Failure**: Excel export shows `{'value': '100.00'}` instead of `100.00`.

---

## 4. LOW — Minor & Unlikely

---

### L-01 · `auth.py` — Fire-and-Forget API Key Touch Can Silently Fail

**File**: `backend/auth.py` ~line 186  
`asyncio.ensure_future(db_mod.touch_api_key(...))` — no error callback. `last_used_at` tracking unreliable.

---

### L-02 · `models.py` — Password Validation Allows Whitespace-Only Strings

**File**: `backend/models.py` — `LoginRequest` ~line 32  
`min_length=1` allows `" "` as a valid password. Add `validator` to strip and re-check.

---

### L-03 · `extractor.py` — Circular References in LLM Result Cause `RecursionError`

**File**: `backend/extractor.py` — `_strip_newlines()` ~lines 42–50  
Unlikely with LLM output but possible if result is manually modified. No depth guard.

---

### L-04 · `vendor_detector.py` — Pure-Punctuation Aliases Silently Ignored

**File**: `backend/vendor_detector.py` — `_alias_variants()` ~lines 41–43  
Pattern normalizes to empty string → returns `[]` silently. No warning.

---

### L-05 · `spatial_memory.py` — Inverted Boxes Persisted to DB (Discovered Later on Read)

**File**: `backend/spatial_memory.py` — `save_from_corrections()`  
No pre-save validation that `x0 < x1`. Data silently saved; only fails when applied.

---

### L-06 · `logging_config.py` — Two Extractions With Long Names Get Same Log Filename

**File**: `backend/logging_config.py` — `_safe_stem()` ~line 152  
Clips filenames at 60 chars. Two files differing only past char 61 share a log file.

---

### L-07 · `ocr_runner.py` — Global ThreadPoolExecutor Never Shut Down

**File**: `backend/ocr_runner.py` ~line 39  
`ThreadPoolExecutor(max_workers=3)` global; no shutdown on worker exit. Threads hang on process restart.

---

### L-08 · `processor.py` — Tiny Images Upscaled Past VLM Budget

**File**: `backend/processor.py` — `_resize_to_vlm_budget()` ~lines 63–64  
`max(32, 0) = 32` can upscale sub-32px images beyond `MAX_PIXELS`. Marginal for current hardware.

---

### L-09 · `scheduler.py` — Destination PDF Collision on Windows Not Handled

**File**: `backend/scheduler.py` — `_move_pdf()` ~line 133  
`shutil.move()` on Windows raises if destination file already exists (same filename re-processed). PDF left in source dir.

---

### L-10 · `db.py` — `enqueue_job` `ON CONFLICT DO NOTHING` Ambiguous to Caller

**File**: `backend/db.py` — `enqueue_job()` ~line 2314  
Returns `None` on collision — caller must know to check `if job is None`. No typed response. Currently handled in `main.py` but fragile.

---

## 5. Test Suite Defects

Tests that are **wrong** (not just missing) and would pass even when the production code is broken.

---

### T-01 · `test_extractor_merge.py` — Wrong Assertion Duplicated

**Test**: `test_merge_results_preserves_duplicate_line_items`  
**Bug**: Line 34 asserts `vendor_name` twice instead of asserting `line_items` equality.  
**Missed**: Silent deduplication of identical line items on the same page.

---

### T-02 · `test_vendor_detector.py` — Test Documents Wrong Behaviour as Correct

**Test**: `test_weak_single_word_alias_is_rejected`  
**Bug**: Name says "rejected" but asserts `assertIsNotNone(match)`. The test passes by asserting the bug.  
**Missed**: The weak alias filter is never actually validated.

---

### T-03 · `test_admin_billing_reporting_isolation.py` — Cases 2–4 Never Run Assertions

**Test**: `test_assert_extraction_access_respects_billing_user`  
**Bug**: Sequential mock pool objects cause only case 1's assertions to execute. Cases 2–4 are dead code.  
**Missed**: Access control bugs where non-admin clients read admin-uploaded docs.

---

### T-04 · `test_pipeline_integration_flow.py` — No Negative Path for Malformed LLM Response

**Test**: `test_full_pipeline_flow_populates_extraction_artifacts`  
**Bug**: All 28 DB mocks return fixed, happy-path rows. No test for bad LLM response.  
**Missed**: Worker crash on malformed LLM output (see C-06, C-07).

---

### T-05 · `test_page_limits.py` — Ingest Logic Never Actually Executed

**Tests**: `test_allowed_one_page_below_limit`, `test_allowed_well_under_limit`  
**Bug**: `_submit_ingestion_job` mocked to return fake success. Ingest body never runs.  
**Missed**: Silent failures in actual ingest when quota check passes.

---

### T-06 · `test_scheduler_edge_cases.py` — Fragile Absence Assertion

**Tests**: `test_no_pdfs_never_sets_executing_true`, `test_missing_folder_never_sets_executing_true`  
**Bug**: Asserts `true_calls == []` which passes if `set_schedule_executing` is never called (e.g., entire function removed).  
**Missed**: Logic branch inversion in `_run_schedule`.

---

### T-07 · `test_llm_usage.py` — `assert_not_awaited` Checked at Wrong Point

**Test**: `test_call_llm_does_not_record_usage_on_json_failure`  
**Bug**: `mock_record.assert_not_awaited()` is checked before the exception propagates.  
**Missed**: LLM usage recorded on JSON parse errors.

---

### T-08 · `test_auth.py` — Mock Doesn't Verify Scoping Parameters

**Test**: `test_client_a_cannot_read_extraction_owned_by_client_b`  
**Bug**: `get_vendor_owner` is mocked once but never asserted it was called with the correct `vendor_id`.  
**Missed**: Auth function accepting wrong params still passes test.

---

### T-09 · `test_review_api.py` — Call Order Not Asserted

**Test**: `test_save_corrections_returns_200_on_success`  
**Bug**: `save_corrections → create_review_event → ensure_job` order not verified.  
**Missed**: Jobs enqueued before corrections saved.

---

### T-10 · `test_extractor_concurrency.py` — No Sleep in Fake LLM

**Test**: Concurrency tests  
**Bug**: `fake_call_llm()` returns instantly. No actual concurrency pressure. Setting `max_active=1` would still pass.  
**Missed**: Real semaphore bugs under concurrency.

---

## 6. Missing Test Coverage

Critical scenarios that have **zero test coverage** anywhere in the suite:

| # | Scenario | Risk if Untested |
|---|----------|-----------------|
| 1 | Concurrent `call_llm()` with shared `pipeline_context` — billing_user_id race | Wrong billing under concurrent extractions |
| 2 | Cross-tenant data leakage via ID enumeration at API level | Security — Client A reads Client B's extraction |
| 3 | PDF with 0 pages submitted via API | Silent empty extraction (see M-05) |
| 4 | MinIO down + local fallback disk full simultaneously | C-02 / C-03 — total data loss |
| 5 | Worker crash mid-transaction; resume endpoint with partial state | Stuck extraction with partial data forever |
| 6 | Document exceeding LLM context window across pages | Silent truncation, partial extraction |
| 7 | Admin audit trail created on admin vendor access | Compliance / audit risk |
| 8 | API key deactivated then re-activated | Key state machine untested |
| 9 | Quota check race: two concurrent uploads both pass before either is recorded | Both exceed quota silently |
| 10 | `test_processor.py` does not exist | Entire PDF rendering module untested |

---

## 7. AWS 32 GB GPU — Forward Notes

> These are not bugs in the current codebase. Notes for when the hardware upgrade happens.

**Batch size**: Current `asyncio.gather` parallelism in `ocr_runner.py` is limited to 3 threads. A 32 GB GPU can run larger batches — increase `ThreadPoolExecutor(max_workers=...)` and Paddle batch size accordingly.

**llama.cpp `--parallel`**: With 32 GB VRAM, increase `--parallel` from 1 to 4–8 to serve multiple concurrent Qwen requests. This makes the concurrency bugs in `extractor.py` (H-09 JSON repair, M-08 spatial memory race) more likely to surface under real load — fix those first.

**`MAX_PIXELS` in `processor.py`**: Current budget is tuned for 8 GB VRAM. On 32 GB you can raise the pixel cap and reduce JPEG compression without OOM risk. Measure token throughput before raising.

**Pool size**: `asyncpg` pool `max_size=10` (H-05) becomes a bottleneck at higher concurrency. Raise to 20–30 to match the wider GPU parallelism.

**No vLLM** (confirmed): Continue using llama.cpp — vLLM does not support GGUF VL models. This constraint carries forward to AWS.

---

*This document was generated by automated codebase audit. All findings require manual triage before acting.*
