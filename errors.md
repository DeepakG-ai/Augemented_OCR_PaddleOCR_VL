# errors.md — Combined Bug List for Review
**Date:** 2026-05-28  
**Branch:** `backend-reliability-hardening`  
**Status:** Not go-live ready — fix highs first.

---

## Critical

### C1 — Startup migration silently zeros legitimate 1000-page clients
**File:** `backend/db.py:575`  
**Confirmed:** Yes  
Every server restart unconditionally runs:
```sql
UPDATE users SET subscription_limit = 0
WHERE role = 'client' AND subscription_limit = 1000;
```
Any admin who deliberately set a client's limit to exactly 1000 pages has it zeroed on the next deploy. Blocks all uploads with QUOTA_EXCEEDED until manually repaired. No `IF NOT EXISTS` guard, no version sentinel — runs on every startup.

---

## High

### H1 — Quota permanently stuck after partial LLM failure
**File:** `backend/worker.py:841`, `backend/main.py:2392`  
**Confirmed:** Yes  
Upload reserves `pending_pages` at `main.py:2392`. When batch pages fail internally (`batch_had_failure=True`), the extractor sets `cancelled=True` and status becomes `"partial"`. The quota release at `worker.py:851` only fires when `is_cancel_requested()` returns True — it does not fire for internal batch failures. The postprocess worker (which releases at line 1031) is only enqueued on `status == "processing"`, never on `"partial"`. Result: `pending_pages` is permanently inflated and blocks all future uploads for that user until manual DB repair or a successful resume.

---

### H2 — /v1/extract idempotency claim orphaned on pre-submission failure (24h lock)
**File:** `backend/main.py:4348`, `backend/main.py:4534`  
**Confirmed:** Yes  
`claim_idempotency` is called at line 4348 — before quota check, vendor detection, and job submission. `bind_idempotency_claim` only runs at line 4534 after `_job_submitted = True`. If quota is rejected, vendor not found, or any error occurs in between, the claim is created but never bound and never cleaned up. Retrying with the same `Idempotency-Key` returns `202 initializing` indefinitely — the client cannot retry for up to 24 hours.

---

### H3 — Top-up approval can double-grant pages (concurrent admin race)
**File:** `backend/main.py:1356`, `backend/db.py:4607`  
**Confirmed:** Plausible  
`add_topup` (line 1356) runs before `resolve_topup_request` (line 1371). `resolve_topup_request` has a CAS guard (`WHERE status='pending'`) that prevents double-resolve, but `add_topup` is not wrapped in the same transaction. Two admins simultaneously approving the same request both pass the `status != 'pending'` check, both call `add_topup` granting pages twice, then `resolve_topup_request` returns `None` for the second — no error is raised (line 1382 returns `{"request": None, "topup": <topup>}`) and the double-grant is silent.

---

### H4 — /upload-preview accepts unbounded max_pages (DoS)
**File:** `backend/main.py:3147`, `backend/processor.py:110`  
**Confirmed:** Yes  
`max_pages: int = Form(20)` has no upper-bound validation. Sending `max_pages=0` triggers `if max_pages else total_pages` in `processor.py:110`, rendering the entire PDF. A 500-page scanned document with `max_pages=0` exhausts CPU and memory. Any authenticated user can trigger this — no admin role required.

---

### H5 — Scheduler "is_executing" conflict protection is dead code
**File:** `backend/main.py:2349`, `backend/db.py:4156`  
**Confirmed:** Yes  
`get_user_is_executing` at `main.py:2349` checks whether a schedule is actively running to block concurrent manual uploads. However, `set_schedule_executing` is defined in `db.py:4156` but is **never called** anywhere in backend runtime code — not in `scheduler.py`, `worker.py`, or `main.py`. The flag is always `FALSE`. The overlap protection does nothing; manual uploads can always run concurrent with scheduled batches.

---

### H6 — Resume endpoint uses old non-atomic quota check (concurrent bypass)
**File:** `backend/main.py:2907`  
**Confirmed:** Yes  
The `/extractions/{id}/resume` endpoint calls the old `get_user_billable_pages` soft-limit check, while `/ingest/ui` and `/v1/extract` were migrated to the atomic `reserve_quota`. Five concurrent resume requests all read the same `billable_pages` snapshot, all pass the `used >= limit` check, and all proceed — bypassing the hard quota cap that the other two ingest paths now enforce atomically.

---

### H7 — bcrypt blocks the async event loop on every login (60–100ms stall)
**File:** `backend/auth.py:57`, `backend/main.py:887`  
**Confirmed:** Yes  
`verify_password` (`bcrypt.checkpw`) and `hash_password` (`bcrypt.hashpw`) are synchronous blocking CPU operations called directly inside `async def login` with no `run_in_executor` wrapper. bcrypt at its default cost factor takes 60–100ms. With 5 concurrent login attempts, all other requests — SSE streams, health checks, in-flight uploads — are stalled for up to 500ms.

---

### H8 — reserve_quota holds FOR UPDATE while scanning all of llm_usage (serializes uploads)
**File:** `backend/db.py:3342`  
**Confirmed:** Yes  
`reserve_quota` acquires a `FOR UPDATE` row lock on the `users` table (line 3342), then while holding that lock executes a `COUNT(DISTINCT ...)` correlated scan against the full `llm_usage` table filtered by `user_id`. No pre-aggregated counter exists. As `llm_usage` grows, every concurrent upload from the same user serializes on the lock while the full-table scan completes, creating head-of-line blocking and potential upstream timeouts under load.

---

## Medium

### M1 — Grace-overage strict `<` blocks grace at exactly the quota limit
**File:** `backend/db.py:3390`  
**Confirmed:** Yes  
The grace-overage condition uses strict `<` (`committed < effective_limit`). When a user is exactly at their limit (`committed == effective_limit`), the condition is `False` and grace is denied. The intent per docstring is to allow small overages near the limit, but at exactly the limit (the most common edge case) grace never fires.

---

### M2 — detect_renames failure silently swallowed — mapping snapshot drifts
**File:** `backend/main.py:1998`  
**Confirmed:** Yes  
The `detect_renames` + `upsert_field_mapping` block is wrapped in `except Exception as map_exc: logger.warning(...)`. Any bug in `detect_renames`, `apply_renames`, or `upsert_field_mapping` silently drops `pending_notices`, leaves the mapping snapshot stale, and returns HTTP 200 to the user who saved the template — no error visible, field renames go undetected.

---

### M3 — po_per_page spatial memory: later pages overwrite earlier page corrections
**File:** `backend/spatial_memory.py:215`  
**Confirmed:** Yes  
`spatial_memory.py:215` uses `merged.update(fl)` to flatten per-page field locations. For `po_per_page` extractions, if page 1 and page 2 both have a `po_number` field, page 2 silently overwrites page 1's spatial correction. Earlier page corrections are permanently lost.

---

### M4 — Review corrections for po_per_page store `doc_0_po_number` keys into prompts
**File:** `backend/main.py:3275`, `backend/extractor.py:83`  
**Confirmed:** Yes  
`main.py:3275` stores correction diffs with keys like `doc_0_po_number`, `doc_1_vendor_id`. `extractor.py:83-90` passes these keys as-is into the gold correction examples dict sent to the LLM. The LLM receives `doc_0_po_number` but the template field name is `po_number` — the correction hints are silently ignored for all `po_per_page` documents.

---

### M5 — POST /api/schedules bypasses the 3-schedule cap
**File:** `backend/main.py:4129`  
**Confirmed:** Yes  
`POST /api/schedules` calls `db_mod.create_user_schedule` directly with no count check. The `_SCHED_MAX = 3` guard only exists in `POST /api/scheduler/start`. Any client that calls the CRUD endpoint directly can create unlimited schedules.

---

### M6 — Concurrent requests on expired idempotency key → uncaught second UniqueViolationError → HTTP 500
**File:** `backend/db.py:998`  
**Confirmed:** Plausible  
Two simultaneous requests with the same expired `Idempotency-Key` both get `UniqueViolationError` on INSERT, both fetch `existing=None` (expired), both enter the DELETE+reinsert transaction. The first commits; the second's INSERT raises a second `UniqueViolationError` that is not caught inside `claim_idempotency` — propagates to `global_exception_handler` as HTTP 500 instead of 409.

---

### M7 — Failed LLM page calls not counted in usage/billing
**File:** `backend/extractor.py:497`, `backend/extractor.py:530`  
**Confirmed:** Yes  
`_record_usage_if_needed()` is called only on successful JSON parse or recovery. The `raise ValueError` at line 530 (all fallbacks exhausted) exits without recording tokens or cost. Pages that fail with unrecoverable LLM JSON are invisible to billing and MLflow tracing.

---

### M8 — Settings SSE uses hardcoded path, breaks split deployments
**File:** `frontend/settings.js:59`  
**Confirmed:** Yes  
`fetch('/api/config/stream', ...)` ignores the `API` base URL constant used everywhere else in the frontend. Split frontend/API deployments (API on a different host or port) will silently fail to connect to the config SSE stream.

---

### M9 — Legacy subscription-limit endpoint writes a column that quota ignores
**File:** `backend/main.py:1097`  
**Confirmed:** Yes  
`PATCH /admin/users/{user_id}/subscription-limit` writes `users.subscription_limit`. Actual quota enforcement (`reserve_quota`) reads from the `subscriptions` + `topups` tables. The endpoint has no effect on live quota — an ops mistake (setting the limit here) produces no quota change and no error.

---

### M10 — JWT verification block copy-pasted across 3 auth functions (already diverged)
**File:** `backend/auth.py:161`, `backend/auth.py:239`, `backend/auth.py:271`  
**Confirmed:** Yes  
The 15-line JWT decode + DB-lookup + role-check block is duplicated in `get_current_user`, `get_current_user_or_api_key`, and `get_current_user_sse`. Copies have already silently diverged: `get_current_user_or_api_key` omits `auth_method`, `get_current_user_sse` drops it entirely. Any future token validation change must be applied in all three places.

---

### M11 — Redundant get_user_by_id DB call after reserve_quota already fetched user row
**File:** `backend/main.py:2400`  
**Confirmed:** Yes  
After `reserve_quota` (which SELECT…FOR UPDATE's the user row), a second `get_user_by_id` is issued at lines 2400, 2430, and 2451 solely to read the user's email for `log_limit_alert`. The email is already available from the JWT `user` dict. Doubles DB round-trips on every quota-exceeded or quota-warning upload.

---

## Notes

- **`--no-access-log` clarification:** This uvicorn flag only disables uvicorn's raw request-line access log. The application-level `AccessLogMiddleware` still logs safe paths via `request.url.path`. All app logs, worker logs, errors, and security logs are unaffected.
- **Test suite reliability:** 18 route tests currently fail due to auth-override isolation pollution; one test passes alone but fails in suite. The test gate is not trustworthy for release readiness — a clean full-suite run is required before go-live.
- **Recommended smoke test before go-live:** login, upload, quota exhaustion, partial/resume flow, top-up approval, scheduler batch trigger, review correction save.
