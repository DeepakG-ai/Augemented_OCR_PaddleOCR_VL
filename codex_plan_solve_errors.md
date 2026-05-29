# Codex Plan To Solve Production Errors

Date: 2026-05-28
Scope: production-readiness fixes for the issues collected in `errors.md`
Mode: implementation plan only; no application code changes in this file

## Executive Decision

The app should not go live until the critical and high findings are fixed and verified. The safest path is two waves:

1. Release-blocker fixes with tightly scoped code changes and regression tests.
2. Deeper accounting/performance hardening after the blocker fixes are stable.

Do not implement the earlier draft plan exactly as written. Some parts are correct, but several proposed changes introduce new race conditions or partial-state bugs. Correctness comes before lock-time optimization.

## Non-Negotiable Rules

- Do not change quota/accounting behavior without regression tests.
- Do not move quota decisions outside their serialization boundary unless accounting is redesigned.
- Do not throw an HTTP error after partially saving template state unless the whole operation is transactional.
- Do not rely on one shared `pending_pages` counter for complex resume/partial flows without strict release guards.
- Keep API contracts backward-compatible unless the user explicitly approves a breaking change.

## Phase 1: Critical And High Release Blockers

### C1: Remove Startup Reset Of 1000-Page Clients

Files:
- `backend/db.py`

Problem:
- Startup currently resets every client with `subscription_limit = 1000` to `0`.
- This can wipe a legitimate admin-configured 1000-page client on every restart.

Implementation:
- Delete the unconditional startup query:
  - `UPDATE users SET subscription_limit = 0 WHERE role = 'client' AND subscription_limit = 1000`
- Do not replace it with another automatic cleanup.
- If historical cleanup is needed, run it manually with an explicit reviewed SQL script.

Tests:
- A client with `subscription_limit = 1000` remains `1000` after `db.init`.
- A client with active subscription remains unchanged after restart.

Acceptance:
- No startup path mutates legitimate existing limits by magic number.

### H1: Release Quota Reservations On Partial LLM Failure

Files:
- `backend/main.py`
- `backend/worker.py`
- `backend/db.py`

Problem:
- Upload reserves `pending_pages`.
- Internal LLM page failure sets extraction status to `partial`.
- No quota release runs for this terminal partial path.
- User can be permanently blocked until manual DB repair or successful resume.

Safer Implementation:
- Add explicit release on all terminal extraction outcomes:
  - `done`
  - `failed`
  - `partial`
  - `cancelled`
- Use a release guard so the same extraction/job cannot release more than once.
- Without a new table, use existing document/extraction metadata carefully:
  - Store `reserved_pages` when submitting the first job.
  - Propagate `reserved_pages` through resume and postprocess job payloads where needed.
  - Release only if `billing_user_id` exists and `reserved_pages > 0`.
- Prefer adding a future `quota_reservations` table, but if schema changes are not allowed, implement defensive best-effort release guards in code.

Important:
- Do not blindly subtract `total_pages` in multiple places.
- Do not double-release on cancel followed by worker exception.

Tests:
- LLM page failure produces `partial` and pending pages return to previous value.
- Cancel before normalize releases once.
- Cancel during LLM releases once.
- Postprocess success releases once.
- Failed normalize/OCR/LLM releases once.
- Double release leaves pending pages unchanged after first release.

Acceptance:
- No terminal job leaves `users.pending_pages` inflated.

### H2: Clean Up `/v1/extract` Idempotency Claims On Pre-Submit Failure

Files:
- `backend/main.py`
- `backend/db.py`

Problem:
- `/v1/extract` creates an idempotency claim before quota/vendor/template/job submission.
- If failure happens before binding to an extraction, retry with the same key returns `202 initializing` until the claim expires.

Implementation:
- Initialize:
  - `incoming_pages = 0`
  - `quota_reserved = False`
  - `_job_submitted = False`
- Wrap all post-claim pre-submit logic in `try/except`.
- On exception:
  - If claim exists and no job was submitted, delete the idempotency claim.
  - If quota was reserved and no job was submitted, release the quota reservation.
  - Use bare `raise`, not `raise exc`, to preserve traceback.
- In `db.claim_idempotency`, handle the expired-claim concurrent reinsert race without HTTP 500.

Tests:
- Quota exceeded with idempotency key does not poison retry.
- Vendor not found with idempotency key does not poison retry.
- Template missing with idempotency key does not poison retry.
- Concurrent expired-key requests return controlled conflict/duplicate behavior, not 500.

Acceptance:
- Retrying after a pre-submit failure is possible immediately.

### H3: Make Top-Up Approval Atomic

Files:
- `backend/main.py`
- `backend/db.py`

Problem:
- Approval currently grants pages before resolving the request.
- Two admins can double-grant the same request.

Implementation:
- Add a DB helper such as `approve_topup_request_atomic`.
- In a single transaction:
  - Lock the top-up request row with `SELECT ... FOR UPDATE`.
  - Verify it exists and `status = 'pending'`.
  - Expire stale active subscriptions for the target user.
  - Select active subscription.
  - If no active subscription, return a structured no-subscription result without changing request status.
  - Insert top-up.
  - Update request to `approved`.
  - Return both request and top-up.
- Route should call only this atomic helper.

Do Not:
- Do not mark approved first and try to rollback with `resolve_topup_request`; that helper only updates pending rows and rollback will fail.

Tests:
- Two concurrent approvals grant exactly once.
- Second approval returns 409.
- Approval with no active subscription grants nothing and leaves request pending or returns a clear 409 by design.
- Reject after approve fails.
- Approve after reject fails.

Acceptance:
- One request can produce at most one top-up row.

### H4: Bound `/upload-preview` Rendering

Files:
- `backend/main.py`
- `backend/processor.py`

Problem:
- `max_pages=0` or a huge value can render an entire large PDF.
- Any authenticated user can trigger heavy CPU/memory use.

Implementation:
- Validate in route:
  - minimum: 1
  - maximum: choose a product value, recommended 20 for preview, absolute upper bound 50 if needed.
- Keep this separate from `MAX_DOCUMENT_PAGES`; preview should stay cheaper than extraction.
- Return HTTP 400 for invalid bounds.

Tests:
- `max_pages=0` returns 400.
- `max_pages=-1` returns 400.
- `max_pages=51` returns 400 if cap is 50.
- Default renders no more than default.

Acceptance:
- Preview endpoint cannot render an unbounded page count.

### H5: Restore Scheduler/Manual Upload Interlock

Files:
- `backend/main.py`
- `backend/db.py`
- `client/client_agent.py`

Problem:
- Manual UI upload checks `user_schedules.is_executing`.
- Runtime never sets `is_executing = TRUE`.
- The protection is dead code.

Implementation:
- Add endpoint: `POST /api/scheduler/{schedule_id}/running`.
- It verifies schedule ownership and sets `is_executing = TRUE`.
- Update `mark_schedule_ran` to set:
  - `last_ran_at = NOW()`
  - `is_executing = FALSE`
  - `updated_at = NOW()`
- Client agent:
  - Calls `/running` before scanning/uploading for due schedule ids.
  - Calls `/ran` in `finally`, even on errors.
  - Calls `sched_state.finish_batch()` in the same `finally`.
- Add stale lock behavior:
  - `get_user_is_executing` should ignore or clear rows where `is_executing = TRUE` but `updated_at` is older than a configured timeout.
  - Suggested timeout: 2 hours, or `SCHEDULER_EXECUTION_STALE_MINUTES`.

Tests:
- Manual UI upload blocked while schedule marked running.
- Manual UI upload allowed after `/ran`.
- Manual UI upload allowed after stale timeout.
- Agent failure path still calls `/ran` best-effort.

Acceptance:
- Scheduled batches and manual UI uploads no longer overlap accidentally.

### H6: Use Atomic Quota Reservation For Resume

Files:
- `backend/main.py`
- `backend/worker.py`

Problem:
- Resume uses old non-atomic `get_user_billable_pages` check.
- Concurrent resumes can all pass the same usage snapshot.
- It does not reserve missing pages.

Implementation:
- Validate extraction access and status.
- Load pages and compute `missing_pages` before quota reservation.
- Check inflight jobs before reservation.
- If no missing pages, return 400.
- For non-admin users:
  - Call `reserve_quota(user_id, len(missing_pages))`.
  - Set `quota_reserved = True`.
- After reservation, wrap enqueue/status update in `try/except`.
  - On any failure before job handoff, release the reservation.
- Include `reserved_pages` in the resume job payload.
- Ensure terminal worker paths release that reservation.

Tests:
- Resume at quota limit returns 402.
- Resume with remaining pages succeeds and increments pending.
- Concurrent resume attempts produce one queued job and no leaked pending pages.
- Enqueue failure releases reservation.
- Status update failure releases reservation.

Acceptance:
- Resume follows the same hard quota model as initial ingest.

## Phase 2: Medium Correctness Fixes

### M3: Preserve Per-Page Spatial Memory For `po_per_page`

Files:
- `backend/spatial_memory.py`

Problem:
- List-form `field_locations` are flattened with `dict.update`.
- Same field on later pages overwrites earlier page corrections.

Implementation:
- Convert input to a list of `(field_key, loc)` tuples.
- Process every tuple independently.
- Keep existing validation for configured header field, manual strategy, box bounds, and page number.

Tests:
- Page 1 and page 2 both correct `po_number`.
- Two spatial memory rows are stored, one per page.

Acceptance:
- Same field name can be learned separately for multiple pages.

### M4: Strip `doc_i_` Prefixes From Gold Correction Examples

Files:
- `backend/extractor.py`
- optionally `backend/main.py`

Problem:
- `po_per_page` correction diffs produce keys like `doc_0_po_number`.
- Prompt field is `po_number`, so the hint is weak or ignored.

Implementation:
- In `_gold_correction_examples`, normalize keys with:
  - `doc_0_po_number -> po_number`
- Keep raw audit history unchanged.
- Optionally improve future diff creation to store clean field keys plus page metadata.

Tests:
- Gold example with `doc_0_po_number` becomes prompt key `po_number`.
- Non-prefixed keys remain unchanged.

Acceptance:
- Human correction hints use template-facing field names.

### M5: Enforce Schedule Cap In CRUD Route

Files:
- `backend/main.py`

Problem:
- `POST /api/schedules` bypasses `_SCHED_MAX`.

Implementation:
- Before creating a schedule, count current user schedules.
- If count >= `_SCHED_MAX`, return 400.
- Keep same behavior as `/api/scheduler/start`.

Tests:
- Fourth schedule through `/api/schedules` returns 400.
- Third schedule succeeds.

Acceptance:
- All schedule creation paths enforce the same cap.

### M7: Record LLM Usage On Unrecoverable JSON Parse Failure

Files:
- `backend/extractor.py`

Problem:
- LLM provider can charge tokens even when response JSON is invalid.
- Current code records usage only after parse/recovery success.

Implementation:
- After receiving LLM response and extracting usage metadata, ensure `_record_usage_if_needed()` runs before raising unrecoverable parse errors.
- Guard against double recording on successful fallback paths.

Tests:
- Valid JSON records once.
- Repaired JSON records once.
- Invalid JSON records once then raises.

Acceptance:
- Token/cost accounting includes failed parse responses.

### M8: Use API Base URL For Settings SSE

Files:
- `frontend/settings.js`

Problem:
- Settings stream hardcodes `/api/config/stream`.
- Split frontend/API deployments fail.

Implementation:
- Change fetch target to:
  - `` `${API}/api/config/stream` ``
- Since `API` is normalized to empty string for same-origin, this works for both same-origin and split deploys.

Tests:
- Same-origin still connects.
- API base configured through `window.__AUGMENTED_OCR_API__` connects.

Acceptance:
- Settings SSE follows the same API base behavior as other frontend calls.

### M9: Retire Or Fix Legacy Subscription-Limit Endpoint

Files:
- `backend/main.py`
- `backend/db.py`

Problem:
- Legacy endpoint writes `users.subscription_limit`.
- Real quota uses active `subscriptions` plus `topups`.

Preferred Implementation:
- Return `410 Gone` or `409 Conflict` with a clear message directing admins to `/admin/users/{id}/subscriptions`.
- Do not silently update a column that quota ignores.

Alternative:
- Make endpoint update active subscription if one exists.
- If no active subscription exists, return 409.
- Do not create arbitrary subscription periods from this endpoint.

Tests:
- Legacy endpoint cannot claim success while live quota is unchanged.

Acceptance:
- Admin cannot accidentally believe quota changed when it did not.

## Phase 3: Auth And Performance Hardening

### H7: Offload Bcrypt From Async Event Loop

Files:
- `backend/auth.py`
- `backend/main.py`

Problem:
- `bcrypt.checkpw` and `bcrypt.hashpw` block the event loop when called inside async routes.

Implementation:
- Add:
  - `hash_password_async`
  - `verify_password_async`
- Use `await asyncio.to_thread(...)`.
- Update async route usage:
  - login verification
  - admin create user
  - admin reset password
  - bootstrap can remain sync if desired, but async wrapper is fine.

Tests:
- Login success/failure still works.
- Password reset still works.
- Optional concurrency smoke: `/health` responds during login burst.

Acceptance:
- Auth behavior unchanged; event loop no longer blocked by bcrypt.

### M10: Centralize JWT Authentication

Files:
- `backend/auth.py`

Problem:
- JWT validation and DB user refresh logic is duplicated.

Implementation:
- Add `_authenticate_jwt(request, credentials)`.
- Return consistent shape:
  - `id`
  - `role`
  - `email`
  - `auth_method = "jwt"`
  - `api_key_id = None`
- Use helper in:
  - `get_current_user`
  - `get_current_user_sse`
  - JWT fallback path of `get_current_user_or_api_key`

Tests:
- Normal JWT route works.
- SSE JWT route works.
- API-key route still works.
- JWT fallback for `/v1/extract` still works.

Acceptance:
- One JWT code path controls all JWT auth behavior.

### H8: Quota Query Performance

Files:
- `backend/db.py`

Problem:
- `reserve_quota` counts usage while holding a user row lock.
- This may become slow under large `llm_usage`.

Safe Near-Term Implementation:
- Do not move the usage count outside the quota decision unless accounting is redesigned.
- Add or verify indexes that support the existing query:
  - `(user_id, call_type, ts, extraction_id, page_num)`
- Keep correctness over micro-optimization.

Long-Term Implementation:
- Add a durable usage aggregate table or quota reservation table.
- Make quota decisions based on maintained counters rather than scanning raw usage rows.

Tests:
- Existing quota concurrency tests still pass.
- Query plan uses the intended index.

Acceptance:
- No quota overrun introduced for performance.

## Phase 4: Template Mapping Rename Handling

### M2: Avoid Silent Mapping Drift

Files:
- `backend/main.py`
- `backend/db.py`

Problem:
- Mapping rename sync exceptions are swallowed.
- But simply raising after template save can leave partial state.

Implementation Options:

Option A, preferred:
- Wrap template save and mapping rename sync in a single DB transaction.
- If mapping sync fails, roll back template update too.
- Return 400/500 with clear message.

Option B, lower-risk:
- Keep template save successful.
- Return a response warning that mapping sync failed.
- Surface warning in UI.
- Add admin-visible pending notice.

Do Not:
- Do not save template, fail mapping, then throw HTTP 400 as if nothing changed.

Tests:
- Mapping sync failure cannot silently drift without visible warning.
- Template state is either fully rolled back or response clearly reports warning.

Acceptance:
- User/admin can see when ERP mapping needs attention.

## Test Plan

Run focused tests first:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_auth.py `
  tests\test_api_keys.py `
  tests\test_subscriptions.py `
  tests\test_topup_requests.py `
  tests\test_page_limits.py `
  tests\test_review_api.py `
  tests\test_spatial_memory_management.py `
  tests\test_scheduler_api_routes.py `
  -q
```

Before trusting the full suite:
- Fix dependency override pollution in tests.
- Several route tests can pass alone but fail in mixed order because auth overrides are mutated globally.

Then run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Manual Docker smoke tests:
- Login as admin and client.
- Create client subscription with exactly 1000 pages; restart API; verify it remains 1000.
- API-key `/v1/extract` sync upload.
- `/v1/extract` idempotency retry after quota/vendor failure.
- UI upload quota exceeded.
- LLM partial failure releases pending pages.
- Resume partial extraction near quota limit.
- Top-up approval double-click/concurrent approval.
- Scheduler running blocks manual UI upload and releases after done.
- Review correction save for `po_per_page`.
- Settings SSE on same-origin and split API base.

## Suggested Implementation Order

1. C1 startup reset removal.
2. H4 upload-preview bounds.
3. H2 idempotency cleanup.
4. H3 atomic top-up approval.
5. H1 terminal quota release guard.
6. H6 resume atomic reservation.
7. M3/M4 review and spatial memory fixes.
8. H5 scheduler running/stale lock.
9. M5/M8/M9 route/frontend cleanup.
10. H7/M10 auth cleanup.
11. H8 quota performance indexing or aggregate design.
12. M2 template mapping transactional/warning design.

## Go-Live Gate

Go-live is acceptable only when:

- All critical/high findings have regression tests.
- Full focused test suite passes.
- Full pytest pass is clean or known unrelated failures are explicitly signed off.
- Docker smoke test passes.
- No client has stale `pending_pages`.
- No active subscription was modified unexpectedly during startup.
