# Exception Handling Audit — Production Readiness
> Generated 2026-05-27 — full read-only scan of all backend + frontend files  
> Corrected 2026-05-27 after manual code review of stale/wrong findings  
> **READ ONLY — no code was changed**

---

## How to Read This Document

- **CRITICAL** — will cause data loss, silent failures, or uncaught crashes in production. Fix before deploy.
- **HIGH** — billing leaks, user-visible silent failures, or feature silently disabled.
- **MEDIUM** — observability gaps, wrong HTTP status codes, or partial feature degradation.
- **LOW** — code quality, logging gaps, minor inconsistencies.

Line numbers reference the current state of the branch (`backend-reliability-hardening`).

---

## 1. CRITICAL Issues — Fix Before Deploy

### C1 · `main.py:284–289` — folder_ingest_callback finally block swallows quota release failure with no log

The outer try/except at line 302 correctly logs and broadcasts `folder_ingest_error` on unhandled failures. The finally block at line 284 correctly releases quota when no job was submitted. Both are working. The real issue is inside the finally:

```python
finally:
    if not _job_submitted:
        try:
            await db_mod.release_quota_reservation(pool, user_id, pdf_page_count)
        except Exception:
            pass   # no log
```

If `release_quota_reservation` fails (DB blip, pool exhausted), the user's quota is permanently consumed with zero log evidence. It shows up as a phantom page deduction.

**What to add:** `except Exception as exc: logger.warning("folder_ingest: quota release failed path=%s: %s", pdf_path, exc)`

---

### C2 · `ocr_runner.py:43` — PaddleOCR init failure silently returns empty OCR (looks like blank page)

`_get_ocr_engine()` is called at line 93 **inside** the outer `try` block of `_run_ocr_on_page()`. If `PaddleOCR()` raises (CUDA error, corrupted model, disk I/O), the exception is caught by the generic `except Exception` at line ~126, logged at ERROR, and the function returns `{"page_number": N, "words": []}`. This is **worse than a crash** — the worker interprets empty words as a blank page and continues the extraction silently. The user receives an incomplete extraction with no indication OCR failed.

Additionally, `_ocr_local.engine` is never set when init fails, so every subsequent page on the same thread retries the failing constructor, adding latency per page.

**What to add:** In `_get_ocr_engine()`, wrap the `PaddleOCR()` constructor in try/except. On failure, log at ERROR and re-raise — do not let it fall through to the generic page handler where it masquerades as blank-page output.

---

### C3 · `processor.py:187` — `_resize_image_sync()` has zero exception handling

`Image.open(io.BytesIO(file_bytes)).convert("RGB")` can raise `IOError`, `OSError`, or `ValueError` on corrupted or unsupported image formats. None are caught. The error propagates through the executor back to the async caller with no cleanup and no user feedback.

**What to add:** Wrap lines 194–202 in try/except, re-raise as `ValueError("Could not open image for resize: ...")`.

---

### C4 · `object_store.py:74` — MinIO `put_bytes()` and `get_bytes()` paths lack contextual error wrapping

The local-filesystem fallback paths have error handling (OSError caught and re-raised with context). The MinIO `put_object()` and `get_object()` calls are bare — network failure, auth failure, or a missing bucket raises an unhandled exception with no context. The asymmetry with `delete_object()` (which does have a try/except) makes this a latent production outage risk.

**What to add:** Wrap MinIO `put_object` and `get_object` calls in try/except with re-raise and context.

---

### C5 · `contracts.py:45` — silent data loss on malformed extraction results

`contracts.py` has **zero try/except blocks and zero raise statements**. Malformed results silently degrade:

- Non-dict `result` silently converts to `{}` with no log.
- Bad `header` silently skipped (no else clause).
- Bad line item silently skipped.
- Line 45 area: `extraction["id"]` direct key access — `KeyError` if key is absent.

There is no audit trail; callers cannot know their data was truncated. This is a **data loss risk** in the export path.

**What to add:** Log at WARNING when normalizing bad data. Use `.get("id")` with an explicit fallback/error.

---

## 2. HIGH Issues — Data Loss or Silent Feature Failure

### H1 · `worker.py:1177–1178` — silent `except Exception: pass` inside error-handling code

During stage failure recovery, `page_logger.append_log()` failure is swallowed with bare `pass` and no logging. If the billing log write fails during a job failure, the failure itself is silently discarded. The user gets no progress update and the pipeline state is unclear.

**What to fix:** Replace `except Exception: pass` with at minimum `logger.warning(...)`.

---

### H2 · `worker.py:157–169, 984–987, 1210–1221` — quota release failures logged but not retried

Three separate call sites release quota reservations on failure. All log a warning on exception but none retry. If the database is briefly unavailable at job end, pages are permanently consumed from the user's quota with no successful extraction stored.

**What to fix:** At minimum, record failed releases in a dead-letter table for async cleanup.

---

### H3 · `extractor.py:431` — LLM usage recording failure permanently loses billing data

`db_mod.record_llm_usage()` is wrapped in try/except which logs a warning and continues. There is no retry and no dead-letter queue. If the DB is briefly unavailable after any LLM call, token counts are permanently lost.

**What to fix:** Log at ERROR (not WARNING) so ops is alerted. Queue failed usage records for retry.

---

### H4 · `spatial_memory.py:182, 205, 333, 340, 453, 483, 491` — read-path DB failures crash postprocess stage

The write path (`save_from_corrections`) has a broad `except Exception` that logs and continues. The **read path** — `get_extraction()`, `get_pages()`, `get_spatial_memory_for_layout()` — has **no error handling at all**. A DB connection loss during spatial memory apply crashes the postprocess stage, marking the extraction failed and losing all pipeline work.

**What to fix:** Wrap read-path DB calls in try/except; on failure, log and continue without spatial memory rather than crashing.

---

### H5 · `vendor_detector.py:102, 106` — DB calls unprotected; caller rescues quota but error is opaque

> **Partial correction from initial audit:** The caller at `main.py:2515–2518` does catch vendor detection failure and calls `_release_reserved_quota_once()` before returning 503. Quota is not leaked. The real issue is narrower: the DB calls in `_load_detection_aliases()` at lines 102 and 106 are unprotected, so a DB connection error propagates as a generic Python exception. The 503 response the user receives has no useful detail about whether vendor detection failed due to a DB outage vs. bad data.

**What to fix:** Wrap DB calls in `_load_detection_aliases()` in try/except; raise a typed exception (`VendorDetectionUnavailable`) so the caller can return a more informative 503 detail.

---

### H6 · `scheduler.py:213` — scheduler lock deadlock on cleanup failure

If `db_mod.set_schedule_executing(pool, schedule_id, False)` fails inside the finally block, the schedule is permanently locked. No documents will be processed by that schedule until someone manually clears the DB flag. The code documents this but provides no mitigation.

**What to fix:** Add a retry (1–2 attempts with backoff) or an async unlock-stale-schedules pass on startup.

---

### H7 · Frontend: multiple API failures with `console.warn` only — no user feedback

| File | Line(s) | Call | User sees |
|------|---------|------|-----------|
| `dashboard.js` | 480–500 | stats, per-client docs | "Loading usage data…" forever |
| `review.js` | 277, 286, 297 | OCR data, gold corrections, spatial memory | Features silently disabled |
| `vendors.js` | 11, 234, 383, 449 | vendor list, template detail | null/empty data rendered |
| `history.js` | 34–38 | extractions list | Empty page, no explanation |
| `settings.js` | 727 | own config | Page renders with missing data |

**What to fix:** Replace `console.warn` with a toast or inline error banner for these data-loading failures.

---

### H8 · `apikeys.js:349–353, 414–418` — clipboard write has no `.catch()`

`navigator.clipboard.writeText()` returns a Promise that rejects if clipboard access is denied (non-HTTPS or browser permission denied). No `.catch()` on either call site. User clicks "Copy" and nothing happens.

**What to fix:** Add `.catch(() => showToast('Could not copy to clipboard'))`.

---

## 3. MEDIUM Issues

### M1 · `geometry.py:93` — `pdfium.PdfDocument(pdf_bytes)` not protected

PDF initialization is outside the `try/finally` that protects page iteration. A malformed PDF raises an unhandled exception that propagates to the caller. The caller (worker normalize stage) catches it, but the error message is generic.

---

### M2 · `extractor.py:286, 294, 306, 570` — DB queries in `get_or_build_system_prompt()` unprotected

Connection failures abort the LLM stage with no context about which query failed. At minimum, the exception should include the query name.

---

### M3 · `ocr_runner.py:126` — empty words list is ambiguous (blank page vs. OCR failure)

OCR page failure returns `{"page_number": ..., "words": []}`. Indistinguishable from a legitimately blank page. The worker cannot tell the difference.

**What to fix:** Return `{"page_number": ..., "words": [], "_ocr_error": str(exc)}` on failure.

---

### M4 · `main.py:1803–1811` — schema creation catches broad `Exception`, returns 400 for DB errors

Database connection errors are caught and converted to 400 "Bad Request" because the catch is `except Exception` instead of `except ValueError`. Clients retry with the same payload.

**What to fix:** Catch `ValueError` specifically; let other exceptions become 500.

---

### M5 · `config.py:54–126` — string env vars accept any value with no format validation

Numeric env vars raise `ValueError` fast-fail on invalid input (good). String env vars (`DATABASE_URL`, `LLM_URL`, `MINIO_ENDPOINT`) accept any string silently. A typo in `LLM_URL` is not caught until the first LLM call during extraction.

**What to fix:** Add basic `urlparse` validation for `DATABASE_URL`, `LLM_URL`, `REDIS_URL`.

---

### M6 · `logging_config.py:242–280` — log directory creation and handler setup unprotected at startup

`os.makedirs()`, `RotatingFileHandler()`, and `QueueListener.start()` have no error handling. If the log directory is unwritable at startup, the app crashes at import time with an `OSError` rather than a useful message.

---

### M7 · `logging_config.py:157–161, 189–193` — bare `except` swallows file handle cleanup failures

Handle `.flush()` and `.close()` failures are silently swallowed; could leak file descriptors under load.

---

### M8 · `scheduler.py:72, 113, 122, 145` — silent job removal failures

`except Exception: pass` on four APScheduler job removal sites with no logging. If a job enters a bad state there is no signal to investigate.

**What to fix:** `except Exception as exc: logger.warning("scheduler: job removal failed: %s", exc)`.

---

### M9 · `field_mapper.py` — zero exception handling, silent data drops

No try/except anywhere. Malformed line items silently skipped (line 59). Misconfigured mappings silently produce empty output. No logging at any point.

**What to fix:** Add `logger.warning(...)` when items are skipped or mappings produce no output.

---

### M10 · `pdf_extractor.py` — pypdfium2 API calls bare, errors unhandled

`textpage.count_chars()`, `textpage.get_text_range()`, `textpage.get_charbox()` can all raise. Caller (`geometry.py`) has a broad catch, but the error message is opaque.

---

### M11 · `qwen_layout_apply.py` — all failures are implicit `None` returns, no logging

Missing keys raise `KeyError`. Non-numeric page dimensions raise `TypeError`. None are handled. Callers receive `None` with no diagnostic information.

---

### M12 · `core.js:172` — `router()` has no try/catch

The SPA router calls all `renderXXXPage()` functions with `await` but no surrounding try/catch. If any render function throws, the error is an unhandled promise rejection and the user sees a blank/stale page.

---

### M13 · `extract.js:815` / `core.js:77` — `fetch()` missing network-error catch

A network failure before the response object is received raises an exception that bypasses the `res.ok` check. In `extract.js:815` the SSE stream never starts; in `core.js:77` the `apiFetch()` helper is unprotected at the network level.

---

### M14 · `settings.js:45, 49` — SSE `onerror` inadequate

The `onerror` handler updates a status badge but shows no toast. Line 49 silently swallows JSON parse errors on SSE messages. If the SSE connection drops mid-extraction, the user sees no real-time progress and no explanation.

---

## 4. LOW Issues

### L1 · `main.py:593–603` — `_peek_user()` swallows all exceptions with no debug log

Returns `"-"` on any exception, including malformed tokens that indicate a bug. Add `logger.debug(...)` to help diagnose token issues in production logs.

---

### L2 · `main.py:618–649` — AccessLogMiddleware swallows logging exceptions silently

The inner `except Exception: pass` in the finally block makes access log failures completely invisible. At minimum, write to `sys.stderr`.

---

### L3 · `db.py:2959–2962` — idempotency claim cleanup failure swallowed silently

`except Exception: pass` with no log. The idempotency table grows unbounded on repeated failures. Should log at WARNING.

---

### L4 · `auth.py:61–64` — bcrypt error masked as authentication failure

`bcrypt.checkpw()` errors return `False` silently. A misconfigured password hash format appears as "wrong password" rather than a configuration error. Add `logger.debug(...)`.

---

### L5 · `extractor.py:498` — bare `except Exception: pass` in json_repair fallback

Swallows all errors from `json_repair()` silently. No visibility into why repair failed before the page is marked as a parse error.

---

### L6 · `mlflow_tracing.py:34–36` — `setup_mlflow()` has no error handling

If MLflow server is unreachable at startup, `mlflow.set_tracking_uri()` blocks and raises unhandled. If MLflow is non-essential (it is), wrap in try/except.

---

### L7 · `layout_key.py` — no defensive handling for unexpected input types

Low risk given current callers, but `str(vendor_id).strip()` assumes a working `__str__`.

---

### L8 · `schedules.js:141` — POST body sent as string `'{}'` instead of `JSON.stringify({})`

Valid JSON string, not a crash risk, but inconsistent with the rest of the codebase.

---

### L9 · `login.js:58–59` — JWT stored in localStorage; response shape not validated

Marked "clean" in initial audit — **too generous**. Line 58 stores the JWT in `localStorage`, which is accessible to any JS on the page (XSS risk). Line 57 accesses `data.access_token` and `data.user` directly without checking the response shape; a malformed server response raises a TypeError that propagates out of the catch block.

---

### L10 · `admin.js:525, 788–791` — error type detected via `e.message.includes('409')`

Marked "clean" in initial audit — **too generous**. Two call sites detect conflict errors by string-matching `e.message`. If the backend changes its error message format, these silently break and show a generic error instead of the intended "already exists" message.

**What to fix:** Use HTTP status code (`e.status === 409`) from the `apiFetch()` error object rather than string-matching the message.

---

## 5. Per-File Summary

### Backend

| File | Critical | High | Medium | Low | Notes |
|------|----------|------|--------|-----|-------|
| `main.py` | C1 | — | M4 | L1, L2 | Quota release in folder watcher finally swallowed silently |
| `auth.py` | — | — | — | L4 | Specific exceptions throughout; bcrypt swallow minor |
| `db.py` | — | — | — | L3 | Strong retry logic; idempotency cleanup swallowed |
| `models.py` | — | — | — | — | Pure Pydantic; no logic |
| `worker.py` | — | H1, H2 | — | — | Good outer handler; quota release not retried |
| `ocr_runner.py` | C2 | — | M3 | — | Init failure returns blank page instead of error |
| `extractor.py` | — | H3 | M2 | L5 | Good JSON recovery; DB calls unprotected; usage loss |
| `geometry.py` | — | — | M1 | — | PDF init outside try/finally |
| `spatial_memory.py` | — | H4 | — | — | Read-path DB calls fully unprotected |
| `vendor_detector.py` | — | H5 | — | — | DB failure = opaque 503; caller does release quota |
| `object_store.py` | C4 | — | — | — | MinIO put/get bare; local path protected |
| `scheduler.py` | — | H6 | — | M8 | Lock deadlock; silent job removal failures |
| `field_mapper.py` | — | — | M9 | — | Zero exception handling throughout |
| `syteline_connector.py` | — | — | — | — | Static constants only; no logic |
| `folder_watcher.py` | — | — | — | — | Empty file (removed) |
| `contracts.py` | C5 | — | — | — | Silent data loss on malformed export results |
| `config.py` | — | — | M5 | — | Numeric vars validated; string vars unchecked |
| `logging_config.py` | — | — | M6, M7 | — | Startup unprotected; handle cleanup swallowed |
| `mlflow_tracing.py` | — | — | — | L6 | Span handling defensive; setup_mlflow unprotected |
| `layout_key.py` | — | — | — | L7 | Simple utility; low risk |
| `qwen_layout_apply.py` | — | — | M11 | — | Zero exception handling; implicit None returns |
| `pdf_extractor.py` | — | — | M10 | — | pypdfium2 calls bare; error messages opaque |
| `page_logger.py` | — | — | — | — | `_get_log_file()` IS inside try; contract holds |
| `processor.py` | C3 | — | — | — | `_resize_image_sync` fully unprotected |

### Frontend

| File | Critical | High | Medium | Low | Notes |
|------|----------|------|--------|-----|-------|
| `core.js` | — | — | M12, M13 | — | Router unprotected; fetch missing network catch |
| `extract.js` | — | — | M13 | — | SSE fetch unprotected; loadPages warn-only |
| `dashboard.js` | — | H7 | — | — | Stats/docs fail silently |
| `login.js` | — | — | — | L9 | JWT in localStorage; response shape not validated |
| `admin.js` | — | — | — | L10 | `e.message.includes('409')` fragile string matching |
| `apikeys.js` | — | H8 | — | — | Clipboard write no .catch() |
| `mapper.js` | — | — | — | — | Comprehensive handling; no issues found |
| `settings.js` | — | H7 | M14 | — | Config load silent; SSE errors not surfaced |
| `review.js` | — | H7 | — | — | Three feature loads silent |
| `vendors.js` | — | H7 | — | — | Multiple silent API failures |
| `history.js` | — | H7 | — | — | Extractions load silent |
| `schedules.js` | — | — | — | L8 | Minor body formatting inconsistency |

---

## 6. Recommended Fix Order for This Week

**Day 1 — Crash and Data Loss Risks**
1. `C2` — wrap PaddleOCR init in `ocr_runner.py`; re-raise instead of returning empty words
2. `C3` — wrap `_resize_image_sync` in `processor.py`
3. `C4` — wrap MinIO put/get in `object_store.py`
4. `C5` — add logging and safe key access in `contracts.py`

**Day 2 — Silent Quota and Billing Leaks**
5. `C1` — add log to quota release in folder_ingest finally
6. `H1` — fix bare `pass` in worker error-handling path
7. `H3` — escalate LLM usage recording failure to ERROR; consider dead-letter

**Day 3 — User-Facing Silent Failures**
8. `H4` — wrap spatial memory read-path DB calls
9. `H6` — add scheduler lock recovery on startup
10. `H7` — add toasts/banners for silent API failures (dashboard, review, vendors, history, settings)

**Post-Deploy (this week)**
- `M3` — distinguish OCR failure from blank page (add `_ocr_error` key)
- `H8` — clipboard `.catch()` in `apikeys.js`
- `M12/M13` — protect router and fetch calls in `core.js` / `extract.js`
- `L9/L10` — login.js response validation; admin.js status-code-based error detection

---

*End of audit. No code was modified during this review.*
