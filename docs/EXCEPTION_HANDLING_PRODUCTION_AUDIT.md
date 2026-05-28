# Production Exception Handling Audit

Date: 2026-05-27

Mode: read-only source audit. No application code was changed for this pass. This file is the only artifact created for the audit.

Scope: first-party repo files were reviewed from `rg --files`, excluding dependency/runtime/generated folders such as `.git`, `.venv`, `node_modules`, `.pytest_cache`, `__pycache__`, `outputs`, `test-results`, `.paddlex_cache`, `.paddlex_runtime`, and `.local_object_store`. Binary screenshots, OCR sample outputs, PDFs, logs, and JSON fixtures were treated as artifacts unless they affect production behavior.

Parallel review: four parallel agents were used for backend/auth/subscription, pipeline/OCR/workers, frontend, and repo/config/tests/docs. Their findings are merged here.

## Executive Summary

The system has a good base: API error envelopes exist, upload quota checks fail closed, top-ups are attached to the current active subscription, and subscription expiry is lazily enforced in the DB helpers. The production risk is not one missing `try/except`; it is inconsistent exception classification across API, worker, OCR, LLM, object storage, and frontend state.

Before production, prioritize these items:

| Priority | Area | Risk | Required handling |
| --- | --- | --- | --- |
| P0 | Worker cancellation | A cancelled running job can still be completed because the worker can return normally after cancellation checks. | Use a cancellation sentinel/exception and terminal `cancelled` status. Never call `complete_job()` after a cancellation path. |
| P0 | OCR/PaddleOCR | PaddleOCR page failures can be swallowed as empty OCR output. | Separate "no text found" from OCR engine/model/page failure. Persist per-page OCR error metadata and fail or partial the job. |
| P0 | PDF rendering | Corrupt/unrenderable pages can be skipped, producing partial output that looks successful. | Track original page count, failed render pages, and quota release behavior. Fail or mark `partial` explicitly. |
| P0 | DB outage | Login/auth/admin/dashboard/subscription routes often bubble raw DB errors to 500. | Map asyncpg connection, timeout, pool exhaustion, and transaction errors to structured 503 `DATABASE_UNAVAILABLE`. |
| P0 | Startup/readiness | Production config and dependency failures are not fail-fast or readiness-gated enough. | Add production config validation and `/ready` checks for DB, MinIO, required secrets, worker readiness, and scheduler state. |
| P0 | Scheduled workflow | Client/scheduler can move PDFs to success after enqueue, not after terminal extraction. | Move files only after terminal job status, or use processing plus reconciliation. |
| P1 | Quota reservation | Several pre-job failures now release quota, but worker terminal failures/cancel/partial paths need audit coverage. | Release pending quota on all terminal paths and test DB failure during release. |
| P1 | Frontend silent states | Dashboard/history/admin/API keys can show zeros or empty lists after load failures. | Show explicit load failure states and preserve old data instead of misleading empty states. |
| P1 | SSE/realtime | Extraction and config SSE failures have weak reconnect/resume/error handling. | Add reconnect/backoff/resume and terminal status polling. |
| P1 | LLM/object store | LLM, MinIO, and artifact errors are not typed or consistently retryable. | Add typed retryable/permanent exception classes and bounded retry/backoff. |
| P1 | API key/token security | JWTs are in `localStorage`; config SSE uses token query param. | Move toward httpOnly secure cookies or short-lived tokens, and avoid token-in-URL for long-lived streams. |
| P2 | Tests | Negative path coverage is thinner than happy path coverage. | Add outage, timeout, race, corrupted artifact, cancellation, and period-end tests. |

## Recommended Error Taxonomy

Use one structured error contract everywhere:

| Code | HTTP/status | Retryable | Applies to |
| --- | --- | --- | --- |
| `INVALID_REQUEST` | 400 | No | Bad IDs, bad date ranges, invalid payload shape |
| `UNAUTHORIZED` | 401 | No | Missing/invalid token or API key |
| `FORBIDDEN` | 403 | No | Role or ownership denial |
| `NOT_FOUND` | 404 | No | Missing user, extraction, job, vendor, schedule |
| `CONFLICT` | 409 | Sometimes | Duplicate email/key/alias, resume conflict, stale scheduler state |
| `QUOTA_EXCEEDED` | 402 | No | Subscription page quota exceeded |
| `DATABASE_UNAVAILABLE` | 503 | Yes | Pool outage, connection failure, transaction timeout |
| `STORAGE_UNAVAILABLE` | 503 | Yes | MinIO/object-store failure |
| `LLM_UNAVAILABLE` | 503 | Yes | LLM 429/503/reset/timeout/model overload |
| `OCR_UNAVAILABLE` | job failed/partial, or 503 at sync boundary | Yes | PaddleOCR init/model/device/inference failure |
| `PDF_RENDER_FAILED` | 400/422 or job failed/partial | No for corrupt input, yes for infra | Corrupt or unsupported PDF, per-page render failure |
| `CONFIG_INVALID` | fail startup or 500 sanitized | No | Missing production secret or invalid endpoint |
| `JOB_CANCELLED` | terminal job state | No | User-requested cancellation |
| `JOB_PARTIAL` | terminal extraction state | Depends | Some pages failed but job produced reviewable output |
| `JOB_FAILED_RETRYABLE` | terminal/retry queue | Yes | Temporary backend dependency failure |
| `JOB_FAILED_PERMANENT` | terminal job state | No | Bad input or unrecoverable validation failure |

API responses should keep the existing envelope style:

```json
{
  "error": {
    "code": "DATABASE_UNAVAILABLE",
    "message": "Service temporarily unavailable. Please retry.",
    "retryable": true,
    "request_id": "..."
  }
}
```

Do not leak raw exception messages, secrets, SQL text, object keys, tokens, or stack traces to clients.

## Subscription And Top-up Verdict

Current behavior is mostly correct by design:

- `backend/db.py` expires active subscriptions whose `period_end < NOW()` in `expire_subscriptions`, `get_active_subscription`, `add_topup`, `get_user_quota_v2`, `get_user_history`, and `reserve_quota`.
- Top-ups are attached to a specific `subscription_id`.
- Quota calculation sums top-ups only for the currently active subscription.
- Usage is counted within `[period_start, period_end)`, so the next period starts cleanly.
- If no active subscription exists after period end, `reserve_quota` blocks upload with `reason = no_subscription`.

Remaining production concerns:

- Add boundary tests for exact `period_end`, timezone-aware inputs, and concurrent upload at period transition.
- Add DB outage handling for subscription/top-up routes so admin UI sees a controlled 503 instead of raw 500.
- Add UI states for "expired period", "no active subscription", and "top-up failed because period ended".
- Validate `incoming_pages > 0` in quota reservation even though normal upload paths calculate a positive page count.
- Ensure pending quota is released when worker jobs end as `failed`, `partial`, `cancelled`, or `unverified`.

## File-by-file Audit: Backend

| File | Exception handling status | Production action |
| --- | --- | --- |
| `backend/__init__.py` | Package marker only. | No runtime exception handling needed. |
| `backend/auth.py` | Handles malformed password hashes, JWT canonicalization, missing/disabled users, API key auth, and ownership checks. DB outages and encryption/key errors still surface inconsistently. | Map pool/asyncpg failures to 503. Sanitize token decode messages. Treat SECRET_KEY/encryption failures as startup/config errors, not runtime raw 500s. Avoid token query param for SSE long term. |
| `backend/config.py` | Parses some numeric/env values but allows permissive defaults for production-critical values. | Add production validation for `DATABASE_URL`, `SECRET_KEY`, MinIO credentials, `LLM_URL`, upload/page limits, CORS origins, and model settings. Fail startup on invalid production config. |
| `backend/contracts.py` | Central contract definitions. Low direct risk. | Ensure job states include explicit `partial`, `cancelled`, `retryable_failed`, and stage error payloads. |
| `backend/db.py` | Strong quota transaction shape and lazy subscription expiry. Many DB helpers expose raw asyncpg errors to callers. Some invalid IDs return empty values, which can hide bad requests. JSON metadata UUID casts can crash dashboards if dirty data exists. | Add DB exception wrapper or route-level mapper. Classify unique/FK/check/timeouts/pool failures. Guard JSON UUID casts. Validate positive `incoming_pages`. Add tests for period end and concurrent quota reservations. |
| `backend/extractor.py` | LLM request and parse failures are partly represented as `_error`, cancellation cancels local tasks, and JSON repair can make malformed output look valid. | Raise typed LLM exceptions, add bounded retries/backoff for transient 429/503/reset/timeouts, validate repaired output completeness, and await/drain cancelled HTTP tasks. |
| `backend/field_mapper.py` | Mapping logic likely raises validation/key errors on unexpected fields. | Keep failures non-fatal per field when possible, return structured low-confidence mapping warnings, and test malformed extraction payloads. |
| `backend/folder_watcher.py` | Local filesystem watcher can hit permission, missing folder, and move failures. | Treat missing folders as config/user setup errors. Move files through `processing` before terminal folders and make moves idempotent. |
| `backend/geometry.py` | Geometry helpers can receive malformed boxes or zero dimensions. | Validate box shape and numeric ranges at boundaries, return empty/invalid geometry warnings instead of crashing review routes. |
| `backend/layout_key.py` | Layout hashing/keying is low infrastructure risk but depends on normalized page geometry. | Handle empty OCR/layout inputs explicitly and log low-confidence layout matching. |
| `backend/logging_config.py` | Central logging setup. | Ensure structured logs include request/job/extraction IDs and redact tokens, API keys, paths containing credentials, and secrets. |
| `backend/main.py` | Has HTTP/validation/global handlers, quota fail-closed behavior, and recent quota release before job submission. Many route-specific DB/object/scheduler errors still become 500 or misleading empty data. | Add route helper for controlled 503 on DB outages. Harden login/me/admin/dashboard/subscription/top-up/API key/config/scheduler routes. Ensure idempotency bind errors after job submission return accepted job info. Add `/ready`. |
| `backend/mlflow_tracing.py` | Tracing setup can fail during startup or worker boot. | Treat MLflow as optional unless explicitly required. If enabled and unreachable, disable tracing with warning or fail startup based on env flag. |
| `backend/models.py` | Pydantic models cover many input constraints. | Add stronger email validation, numeric bounds for `expires_days`, page limits, top-up pages, schedule counts/times, and string length caps for user-facing text. |
| `backend/object_store.py` | MinIO operations are not typed or consistently retried. | Wrap storage errors in `StorageUnavailable`/`ArtifactMissing` with bucket/key context for logs only. Add retries for transient network errors and readiness probe. |
| `backend/ocr_runner.py` | Recent invalid image/base64 handling exists, but PaddleOCR engine/page failures can still be interpreted as empty OCR. | Distinguish no text from OCR failure. Persist per-page OCR errors. Classify model init/device/runtime errors as retryable infra failures. |
| `backend/page_logger.py` | Error classification is partly string/substr based. | Switch to structured codes from the taxonomy and include job/extraction/user IDs. |
| `backend/pdf_extractor.py` | PDF text extraction can fail on corrupt/encrypted/unsupported files. | Return typed `PDF_RENDER_FAILED` or `PDF_TEXT_EXTRACTION_FAILED` with page context and input classification. |
| `backend/processor.py` | Counts pages and renders PDF. Corrupt page render failures can be skipped and total rendered pages can differ from original page count. | Preserve original page count, record per-page render errors, and choose explicit `failed` or `partial` status. Release reserved quota for pages not actually processable. |
| `backend/qwen_layout_apply.py` | Applies layout decisions from model output. Risk is malformed/partial model JSON. | Validate schema and coordinate ranges. Treat model layout failure as low-confidence or stage failure, not silent best-effort data. |
| `backend/requirements.txt` | Dependency list only. | Pin production-critical dependency versions and document PaddleOCR/PaddlePaddle compatible versions. |
| `backend/scheduler.py` | Schedule execution and lock release exist, but stale locks and terminal-status reconciliation are weak. | Add `executing_since`, stale lock recovery, terminal status polling, and explicit scheduler error states. |
| `backend/spatial_memory.py` | Spatial memory can receive malformed OCR boxes/fields. | Validate source extraction ownership, geometry, and schema. Treat memory update errors as non-blocking warnings where possible. |
| `backend/syteline_connector.py` | External integration risk. | Add timeout/retry/error mapping for connection/auth/validation failures. Do not let external ERP failure corrupt extraction status. |
| `backend/vendor_detector.py` | Vendor detection has fallback behavior. | Return explicit `vendor_unknown` or low-confidence state rather than treating detector errors as no vendor. Validate alias regex/pattern errors. |
| `backend/worker.py` | Highest risk file. Workers process normalize/OCR/LLM/postprocess. Cancellation, retryability, failure marking, and quota release need hardening. | Separate cancelled/partial/failed/retryable states. Protect failure-state DB writes with retries. Do not mark cancelled jobs done. Distinguish transient DB/MinIO/LLM/OCR from permanent input failures. |

## File-by-file Audit: Frontend

| File | Exception handling status | Production action |
| --- | --- | --- |
| `frontend/admin.js` | Admin user, subscription, and top-up workflows have some toasts but also string matching and empty fallback behavior. | Use structured error codes. Show DB/service outage states. Handle expired subscription and top-up period-end conflicts clearly. Preserve current UI data on refresh failure. |
| `frontend/apikeys.js` | API key load/create/reveal/deactivate paths have basic toasts but weak load failure state and string matching. | Show explicit table error. Use error envelope fields for duplicate labels and auth failures. Handle clipboard rejection. |
| `frontend/core.js` | Central `apiFetch`/`apiJSON` exists, but raw fetch/network errors and malformed JSON are not classified. Route cleanup aborts local SSE but not backend job. | Add network timeout/offline/CORS handling, parse success responses safely, add route-level error boundary, and make navigation choices explicit for running jobs. |
| `frontend/dashboard.js` | Dashboard failures can render zeros/empty tables, which is dangerous in production. | Add partial data/error banners and avoid replacing known values with empty states on failed calls. |
| `frontend/extract.js` | Upload/progress flow works but SSE parse errors, disconnects, cancel/resume races, and retry timers are weak. | Add SSE reconnect/backoff/resume, poll terminal status after cancel, clear route timers, and surface stage-specific OCR/LLM/PDF errors. |
| `frontend/history.js` | Load failure can look like "No extractions yet". | Add explicit error state and retry action. |
| `frontend/index.html` | App shell loads main modules. `schedules.js` appears disconnected from routing. | Remove or wire dead scheduler module. Add global error boundary container for fatal route errors. |
| `frontend/login.js` | Login flow stores token/user and handles auth errors basically. Network and malformed response errors are weak, and localStorage token has XSS blast radius. | Validate response shape before storing. Use friendly network errors. Plan httpOnly secure cookie or short-lived token plus CSP. |
| `frontend/mapper.js` | Mapping UI depends on extraction/vendor payloads. | Add malformed payload handling and preserve user edits when background save/load fails. |
| `frontend/review.js` | Review UI depends on OCR/image/fields/artifacts. | Show missing image/OCR/artifact states per page. Do not let review save fail silently. |
| `frontend/schedules.js` | Appears legacy/dead because not loaded by `index.html`. If loaded, it overlaps scheduler logic in settings. | Either remove from production bundle or wire with tests. Avoid duplicate globals. |
| `frontend/settings.js` | Config load/save and config SSE can silently fail or reconnect forever. Token is passed in query string for SSE. | Block save when config load fails. Add SSE backoff/final failure. Avoid token in URL. |
| `frontend/styles.css` | No exception logic. | Add clear visual states for loading, partial failure, terminal failure, cancelled, retryable, and read-only stale data. |
| `frontend/vendors.js` | Vendor list/detection/admin vendor flows need clear failure states. | Add explicit vendor load failure, alias conflict handling, and detector unavailable state. |

## File-by-file Audit: Client Agent

| File | Exception handling status | Production action |
| --- | --- | --- |
| `client/client_agent.py` | Handles login refresh, stable file waits, API calls, SSE wait, folder moves, heartbeat, and scheduler polling. Some broad catches hide root cause, and file terminal move can be too early depending on server behavior. | Move PDFs only after terminal extraction status. Classify API 402/409/503 separately. Persist retry queue for network outages. Make folder moves idempotent and crash-safe. |
| `client/client_agent.json` | Runtime config. | Validate paths, credentials source, base URL, polling intervals, and scheduler behavior before startup. Do not store production passwords in plaintext config. |
| `client/build.ps1` | Build script. | Fail fast on missing dependencies and non-zero packaging result. |
| `client/requirements.txt` | Dependency list. | Pin requests/watchdog versions used in production. |
| `client/logs/client_agent.log` | Runtime artifact. | Do not include production logs/secrets in repo. Ensure token/API key redaction. |

## File-by-file Audit: Root And Deployment

| File | Exception handling status | Production action |
| --- | --- | --- |
| `AGENTS.md` | Local agent instruction. | No runtime effect. |
| `CLAUDE.md` | Assistant/development notes. | No runtime effect. Keep production runbooks separate. |
| `Dockerfile` | Sets Paddle cache/env and installs dependencies. | Add startup checks for Paddle model cache availability and fail clearly if model files are missing. |
| `docker-compose.yml` | DB and MinIO healthchecks exist. API/workers do not have first-class healthchecks/readiness. | Add API `/ready` healthcheck and worker health/heartbeat checks. Ensure service restart policy is production appropriate. |
| `package.json` | Frontend/e2e package metadata. | Ensure test scripts include failure modes before release. |
| `package-lock.json` | Locked JS deps. | Keep committed and scan for security issues before deploy. |
| `playwright.config.js` | Test config. | Add negative-path UI tests for API outage, SSE outage, quota exceeded, expired subscription. |
| `pgadmin-servers.json` | Tooling config. | Avoid production credentials in repo. |
| `exceptions_2_agy.md` | Older audit document. | Use as historical context only. Replace with this production checklist as current source. |
| `test_trace.py` | Local trace/test utility. | Keep out of production path. |
| `backend/.env` | Environment file exists locally. Values not reviewed or copied here. | Do not commit production secrets. Add `.env.example` with required keys and production validation. |

## File-by-file Audit: Scripts

| File | Exception handling status | Production action |
| --- | --- | --- |
| `scripts/qwen.py` | Manual LLM test script with minimal JSON/error handling. | Keep non-production. Add timeout/status handling if used in diagnostics. |
| `scripts/qwen_mul.py` | Manual multi-page LLM test with HTTP/JSON catches. | Keep non-production. Do not share production prompts/secrets in output. |
| `scripts/qwen_raw.py` | Raw LLM test script. | Keep non-production. Handle streaming/truncated output explicitly if reused. |
| `scripts/rd_qwen.py` | Manual Qwen extraction script. | Keep non-production. |
| `scripts/rj_schinner_qwen.py` | Manual vendor-specific script with request/JSON catches. | Keep non-production. |
| `scripts/test_api.py` | Manual API smoke script. | Add expected error code checks if used for release smoke. |
| `scripts/test_vllm.py` | Manual vLLM test. | Add status/timeout/retry diagnostics. |
| `scripts/test_vllm_basic.py` | Basic vLLM test. | Add clear non-zero exit on unavailable model/server. |
| `scripts/extracted_po.json` | Sample output artifact. | No exception handling. |
| `scripts/rj_schinner_extracted_po.json` | Sample output artifact. | No exception handling. |

## File-by-file Audit: E2E

| File | Exception handling status | Production action |
| --- | --- | --- |
| `e2e/playwright.config.js` | E2E config. | Add failure scenario projects or tags for production gating. |
| `e2e/helpers/auth.js` | Test auth stubs use localStorage token injection. | Add tests matching production auth behavior if cookies replace localStorage. |
| `e2e/tests/01_ingestion.spec.js` | Covers happy-path ingestion and quota UI pieces. | Add OCR failure, render failure, SSE disconnect, and quota release tests. |
| `e2e/tests/02_review_ui.spec.js` | Covers review UI basics. | Add missing image/OCR/artifact failure views. |
| `e2e/tests/03_spatial_memory.spec.js` | Covers spatial memory UI. | Add bad geometry and save failure tests. |
| `e2e/tests/04_client_isolation.spec.js` | Covers client isolation. | Add admin/client DB outage and forbidden access cases. |
| `e2e/fixtures/dummy.pdf` | Test fixture. | No exception handling. |
| `e2e/report/index.html` | Generated report artifact. | Do not rely on as source. |

## File-by-file Audit: Tests

Existing tests provide useful coverage, but production exception coverage needs more outage and race tests.

| File | Current role | Add before production |
| --- | --- | --- |
| `tests/conftest.py` | Test fixtures and env defaults. | Add fixtures for DB outage, MinIO outage, LLM outage, and bad production config. Ensure default `SECRET_KEY` does not hide missing secret tests. |
| `tests/test_admin_billing_reporting_isolation.py` | Billing report isolation. | Add bad `billing_user_id` metadata and DB query failure tests. |
| `tests/test_admin_billing_scoping.py` | Admin billing scoping. | Add malformed user ID and DB outage cases. |
| `tests/test_admin_client_scoping.py` | Client/admin scoping. | Add dashboard route invalid-target handling. |
| `tests/test_admin_dashboard.py` | Dashboard behavior. | Add partial DB failure and bad metadata tests. |
| `tests/test_admin_users.py` | Admin user CRUD behavior. | Add duplicate email race, UniqueViolation mapping, and DB outage mapping. |
| `tests/test_api_keys.py` | API key routes and auth. | Add encryption failure, missing SECRET_KEY, DB touch failure, expiry bounds. |
| `tests/test_auth.py` | Auth/token behavior. | Add asyncpg failure in `get_current_user`, sanitized JWT expiry/malformed responses, and disabled user DB outage. |
| `tests/test_client_agent.py` | Client agent unit tests. | Add terminal polling, move failure, 503 retry queue, and scheduler period transition. |
| `tests/test_config_api.py` | Config API. | Add config load failure blocks save and SSE failure behavior. |
| `tests/test_db_vendor_filtering.py` | Vendor filtering. | Add invalid UUID and DB exception classification. |
| `tests/test_default_page_limit.py` | Page limit defaults. | Add production config validation cases. |
| `tests/test_extractor_concurrency.py` | Extractor concurrency. | Add LLM cancellation drain and transient retry tests. |
| `tests/test_extractor_merge.py` | LLM result merge. | Add malformed repaired JSON and incomplete schema tests. |
| `tests/test_extractor_no_boxes.py` | No-box extraction behavior. | Add OCR failure vs no-text distinction. |
| `tests/test_field_mapper.py` | Field mapping. | Add malformed fields and missing schema tests. |
| `tests/test_immutable_usage_after_delete.py` | Usage immutability and quota. | Add quota release on failed/partial/cancelled worker states. |
| `tests/test_infrastructure_safety.py` | Infrastructure expectations. | Add API and worker healthcheck assertions, not only service presence. |
| `tests/test_ingest_hardening.py` | Ingest hardening. | Add idempotency bind failure after job submission and object-store failure before submit. |
| `tests/test_llm_usage.py` | LLM usage accounting. | Add no-usage, bad-usage, and partial page accounting tests. |
| `tests/test_logging_config.py` | Logging setup. | Add redaction tests for token/API key/secret fields. |
| `tests/test_ocr_multithread.py` | OCR threading. | Add model init failure under concurrency. |
| `tests/test_ocr_runner_hardening.py` | OCR hardening. | Add PaddleOCR init failure, per-page inference failure, bad device/model cache tests. |
| `tests/test_output_schemas.py` | Output schema expectations. | Add stage error and partial job schema cases. |
| `tests/test_page_limits.py` | Page upload limits. | Add corrupt page count and render count mismatch tests. |
| `tests/test_pdf_only_upload_guard.py` | Upload content-type/PDF guard. | Add encrypted/corrupt PDF cases. |
| `tests/test_pipeline_adversarial.py` | Pipeline adversarial cases. | Add MinIO missing artifact and LLM malformed response cases. |
| `tests/test_pipeline_hardening.py` | Pipeline hardening. | Add cancellation terminal-state regression. |
| `tests/test_pipeline_integration_flow.py` | Happy-path integration. | Add failure-path integration matrix. |
| `tests/test_processor.py` | PDF processor. | Add partial page render failure and original page count preservation. |
| `tests/test_qwen_layout_decision.py` | Qwen layout decisions. | Add malformed/low-confidence layout payloads. |
| `tests/test_reliability_hardening.py` | Reliability hardening. | Add `/ready`, DB outage mapping, and worker failure marking outage. |
| `tests/test_review_api.py` | Review API. | Add artifact missing/corrupt and save failure tests. |
| `tests/test_scheduler.py` | Scheduler core behavior. | Add stale lock recovery and terminal move tests. |
| `tests/test_scheduler_api_routes.py` | Scheduler API routes. | Add DB outage and conflict tests. |
| `tests/test_scheduler_edge_cases.py` | Scheduler edge cases. | Add exact time boundary and timezone cases. |
| `tests/test_scheduler_isolation.py` | Scheduler isolation. | Add cross-user stale lock and failure isolation. |
| `tests/test_single_agent_bbox.py` | BBox single-agent behavior. | Add invalid geometry and OCR mismatch tests. |
| `tests/test_spatial_memory_management.py` | Spatial memory management. | Add DB outage and malformed memory payload cases. |
| `tests/test_subscriptions.py` | Subscription behavior. | Add exact `period_end`, top-up expiry, no active subscription upload, concurrent reservation, and pending release tests. |
| `tests/test_template_prompt_visibility.py` | Prompt/template visibility. | Add missing template and invalid schema tests. |
| `tests/test_tracing.py` | Tracing behavior. | Add MLflow unavailable/degraded mode tests. |
| `tests/test_usage_aggregation.py` | Usage aggregation. | Add bad metadata and partial page accounting tests. |
| `tests/test_vendor_dashboard.py` | Vendor dashboard. | Add DB outage and bad vendor ID tests. |
| `tests/test_vendor_detector.py` | Vendor detector. | Add alias regex failure and detector exception cases. |
| `tests/test_word_pdlocr.py` | Word/Paddle OCR tests. | Add PaddleOCR dependency unavailable case. |
| `tests/review/__init__.py` | Test package marker. | No exception handling. |
| `tests/review/console_output.txt` | Fixture/artifact. | No exception handling. |
| `tests/review/text_extraction_output.json` | Fixture/artifact. | No exception handling. |
| `tests/paddle_ocr/ocr_pipeline.py` | Experimental/test OCR pipeline. | Keep out of production or add explicit dependency checks. |
| `tests/paddle_ocr/pd.py` | Experimental/test OCR helper. | Keep out of production. |
| `tests/paddle_ocr/pdl_ocr.py` | Experimental/test Paddle OCR helper. | Keep out of production. |
| `tests/paddle_ocr/processor.py` | Experimental/test processor. | Keep out of production. |
| `tests/paddle_ocr/text_matcher.py` | Experimental/test matcher. | Keep out of production. |
| `tests/paddle_ocr/word_pdlocr.py` | Experimental/test Word/Paddle script. | Keep out of production. |
| `tests/paddle_ocr/output/*` | OCR sample artifacts. | No exception handling. Avoid using stale artifacts as truth for production. |
| `tests/paddle_ocr/output_word/*` | OCR sample artifacts. | No exception handling. |

## File-by-file Audit: Docs

| File | Notes |
| --- | --- |
| `docs/FAILURE_MODES.md` | Keep, but update to match current worker/API behavior after hardening. |
| `docs/failue_modes_antigravit.md` | Typo in filename and likely historical. Merge useful points into this file or `FAILURE_MODES.md`. |
| `docs/workflow/PRODUCTION_READINESS.md` | Already mentions config validation and readiness. Treat missing implemented readiness as an open production blocker. |
| `docs/workflow/workers.md` | Notes failed jobs are not automatically retried. This is risky for transient DB/MinIO/LLM/OCR failures. |
| `docs/workflow.md` | Mentions `outbound-worker`; compose/dispatch appear to use normalize/OCR/LLM/postprocess. Fix documentation drift. |
| `docs/workflow/auth.md` | Align with token/cookie decision and SSE auth changes. |
| `docs/workflow/db.md` | Add DB outage/error taxonomy and quota period-end behavior. |
| `docs/workflow/extraction.md` | Add terminal states, partial failure, and cancellation semantics. |
| `docs/workflow/frontend.md` | Add UI failure-state expectations. |
| `docs/workflow/login.md` | Add malformed login response and network outage behavior. |
| `docs/workflow/review.md` | Add missing artifact/OCR/image behavior. |
| `docs/workflow/scheduler.md` | File not present; scheduler content is spread elsewhere. Consider adding one. |
| `docs/workflow/vendor_detection.md` | Add detector failure and unknown vendor behavior. |
| `docs/workflow/spatial_memory.md` | Add malformed geometry and ownership failure behavior. |
| `docs/workflow/isolation.md` | Add error behavior for isolation violations. |
| `docs/workflow/ARCHITECTURE.md` | Update after readiness and worker retry changes. |
| `docs/workflow/architecture.html` | Generated/reference artifact. Regenerate only if architecture docs change. |
| `docs/workflow/README.md` | Add link to this audit and current failure mode docs. |
| `docs/workflow/bbox_agent.md` | Add bbox failure and no-layout fallback behavior. |
| `docs/bbox_fix.md` | Historical fix doc. Keep as context. |
| `docs/mapping.md` | Add mapping failure behavior if not already included. |
| `docs/prompt_examples.md` | Ensure examples do not encourage accepting malformed model JSON silently. |
| `docs/prompts` | Prompt artifact/file. Add validation expectations around model output. |
| `docs/qwen_bbox_github_issue.md` | Historical issue context. |
| `docs/qwen_prompt_bbox.md` | Add malformed/partial output handling guidance. |
| `docs/scaling.md` | Add queue retry/backoff and health/readiness scaling notes. |
| `docs/vendor_id_renumber_plan.md` | Migration doc. Add rollback/error behavior if executed. |
| `docs/bottleneck/performance_notes.md` | Performance context. Add whether bottlenecks cause retryable user-facing errors. |
| `docs/bottleneck/llama cpp log.txt` | Log artifact. Do not include secrets or production tokens. |
| `docs/Screenshot 2026-03-24 161806.png` | Visual artifact. No exception handling. |
| `docs/Screenshot 2026-03-26 145357.png` | Visual artifact. No exception handling. |

## Production Fix Order

### Phase 1: Must fix before deploy

1. Add production config validation and `/ready`.
2. Add API/worker healthchecks in deployment.
3. Map DB outage to 503 across login/auth/admin/dashboard/subscription/top-up/config/scheduler.
4. Fix worker cancellation so cancelled jobs cannot become `done`.
5. Fix OCR/PaddleOCR error classification so engine/page failures cannot appear as empty text.
6. Fix PDF render partial-page handling and original page count tracking.
7. Ensure quota reservation is released on failed, partial, cancelled, and unverified worker terminal paths.
8. Fix scheduler/client file movement so files move to success only after terminal extraction success.
9. Add frontend explicit failure states for dashboard/history/admin/API keys/settings/extract.

### Phase 2: Strongly recommended before deploy if time allows

1. Add typed storage and LLM exceptions with bounded retries.
2. Add SSE reconnect/resume and cancel terminal polling.
3. Add object-store artifact missing/corrupt diagnostics.
4. Add dashboard bad metadata protection for JSON UUID casts.
5. Replace frontend string-matching of errors with structured `error.code`.
6. Add period-end/top-up boundary tests.

### Phase 3: Post-deploy hardening

1. Move from localStorage JWT to httpOnly secure cookie or short-lived access token with refresh controls.
2. Remove token-in-query SSE or limit it to very short-lived stream tokens.
3. Add circuit breakers for LLM/PaddleOCR/model dependency outages.
4. Add operator runbook entries for each error code.
5. Add metrics for exception code counts by route/stage/job.

## Minimum Production Test Matrix

Run these before release:

| Scenario | Expected result |
| --- | --- |
| DB down during `/auth/login` | 503 `DATABASE_UNAVAILABLE`, no raw stack in client |
| DB down during `/auth/me` | 503 or controlled auth failure, no frontend blank page |
| Duplicate user creation race | 409 `CONFLICT`, no raw asyncpg unique error |
| Missing/invalid SECRET_KEY in production | Startup fails with config error |
| MinIO unavailable during upload | Job not submitted or marked retryable, quota released |
| PaddleOCR model init failure | Job failed/retryable with `OCR_UNAVAILABLE`, not empty OCR |
| PaddleOCR fails on one page | Extraction partial or failed with page error, not silent success |
| Corrupt PDF page | `PDF_RENDER_FAILED` or explicit partial, original page count preserved |
| LLM timeout/503 | Retry bounded, then retryable job failure |
| LLM malformed JSON | Schema validation failure or low-confidence state, not trusted output |
| User cancels running job | Terminal `cancelled`, no later `done` transition |
| Route changes during extraction | UI tells whether job continues and can resume status |
| SSE disconnect | Reconnect/backoff or terminal poll, no stuck spinner |
| Subscription expired yesterday | Upload blocked with no active subscription |
| Top-up added after period end | 409 no active subscription |
| Top-up from old period | Not counted in new period |
| Concurrent uploads near quota | One serialized reservation result, no quota overrun beyond intended grace |
| Worker DB failure while marking failed | Worker retries state update or leaves retryable observable state |
| Dashboard bad metadata UUID | Route returns controlled partial/error, not 500 |
| Frontend dashboard API failure | Visible error, not zeros |

## Notes On Current Strengths

- API error envelopes already exist and should be reused.
- Upload quota check fails closed on DB failure, which is correct for billing safety.
- `reserve_quota` uses row locking and pending pages for concurrent upload protection.
- Subscriptions and top-ups are modeled correctly around active subscription periods.
- Input models already include several useful constraints.
- Tests already cover many happy paths and some hardening paths.

## Final Recommendation

Yes, exception handling can be made production-safe this week, but do not treat it as adding random `except Exception` blocks. The correct fix is a small number of typed error boundaries:

1. API boundary: convert known dependency and validation exceptions into structured client errors.
2. Worker boundary: convert stage failures into explicit terminal or retryable job states.
3. Dependency boundary: DB, MinIO, LLM, PaddleOCR, PDF renderer each get typed exceptions.
4. Frontend boundary: show real failure states and consume structured error codes.
5. Test boundary: prove period-end, top-up, cancellation, OCR, and outage behavior.

