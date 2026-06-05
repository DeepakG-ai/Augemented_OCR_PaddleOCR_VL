# Web App & API Router

This document details the configuration, lifespans, middleware, and route groupings of the central FastAPI application.

---

## What it is

The Web App & API Router (`backend/main.py`) is the entry point and coordinator of the server application. It configures the FastAPI instance, manages database connection pools and client sessions during its startup/shutdown lifespans, registers logging and rate-limiting middlewares, validates incoming files and payload bounds, and exposes all REST API endpoints for user authentication, dashboard administration, document ingestion, job scheduling, and manual review.

---

## Key States

- **STARTING**: The server is initializing resources (creating Postgres connection pools, connecting to MinIO object storage, verifying environment configurations, and bootstrapping default admin accounts).
- **UP**: The server is running and accepting REST/SSE requests. Uvicorn access log filtering suppresses high-frequency polling telemetry noise.
- **SHUTTING DOWN**: The server is closing active database connection pools and finalizing log writers.

---

## How it works

The FastAPI application orchestrates requests through a series of global middlewares and endpoint groups.

```
                   [HTTP Request Received]
                              │
                              ▼
                   [AccessLogMiddleware]
                   (Injects tracing context)
                              │
                              ▼
                     [slowapi Limiter]
                   (Rate limits per endpoint)
                              │
                              ▼
                     [CorsMiddleware]
                   (Validates origin headers)
                              │
                              ▼
                       [Router Paths]
                              │
         ┌────────────────────┼────────────────────┐
         ▼ Ingest Paths       ▼ Admin Paths        ▼ Telemetry Paths
  [/ingest/{source_type}]  [/admin/users]       [/health]
  [/v1/extract]            [/admin/topups]      [/live]
  [/upload-preview]        [/admin/api-keys]
         │
         ▼
[Execution Hand-off]
(Durable Postgres job)
```

### 1. Lifespan Lifecycle
1. **Startup**:
   - Initializes the `asyncpg` connection pool and bootstraps the schema.
   - Instantiates the object store client (MinIO or local filesystem fallback).
   - Configures MLflow tracing endpoints.
   - Bootstraps the system admin account using credentials from `.env` (provided the password is not a `CHANGE_ME` placeholder).
   - Registers a quiet filter in the logger to prevent the high-frequency `/health` route from flooding logs.
2. **Shutdown**:
   - Closes the active Postgres connection pool asynchronously.

### 2. Global Middlewares
- **AccessLogMiddleware**: Computes request timing, records structured JSON log lines via `plog.event`, and assigns context identifiers (`extraction_id`, `filename`, `stage`) to keep log groups cohesive.
- **slowapi Limiter**: Enforces dynamic request rate limits per-route (e.g., maximum 30 requests/minute default, or strict limits like 10/minute for resource-heavy operations).
- **CorsMiddleware**: Authorizes incoming HTTP requests from configured origins to allow cross-origin browser requests.

### 3. File Verification & Upload Guards
- **File Format Guard**: All ingestion endpoints call `_require_pdf()`. This checks that filenames end in `.pdf` or `.PDF` and that the file binary stream starts with the PDF magic header `%PDF`. Non-conforming files are rejected with HTTP 400.
- **Page Count Guard**: Uses `processor.count_pdf_pages()` to extract the physical page count before job submission. If the document exceeds `MAX_DOCUMENT_PAGES` (default 100), the request is rejected with HTTP 400 (`DOCUMENT_TOO_LARGE`).
- **Payload Size Guard**: Restricts raw multipart request sizes to `MAX_UPLOAD_BYTES` (default 50MB) via custom streaming request validators.

### 4. Idempotency Flow (`/v1/extract`)
For programmatic API clients carrying an `Idempotency-Key` header:
1. Calls `db_mod.claim_idempotency()` using the user ID, idempotency key, and a SHA-256 hash of the PDF file.
2. **Status Branches**:
   - `conflict`: The same key was submitted with a different file hash. Returns HTTP 409.
   - `duplicate`: The key was already processed. If complete, returns the cached mapped result. If in-flight, returns HTTP 202 `"initializing"`. If the prior attempt failed, evicts the claim and re-runs.
   - `new`: Resolves vendor, checks subscription quotas, uploads the PDF, enqueues the extraction job, and calls `bind_idempotency_claim` to link the key.
3. If an error occurs *before* job submission (e.g., quota exceeded or unknown vendor), the exception handler catches it, releases the quota reservation, and deletes the idempotency claim to allow subsequent retries.

---

## Rules & Hard Constraints

- **Admin Bootstrap Override**: Bootstrap admins must not be created if the password contains the placeholder prefix `CHANGE_ME`.
- **PDF Headers Enforcement**: Every upload path must assert both name and binary magic bytes matching the PDF specification.
- **Telemetry Log Filtering**: Heartbeat, schedule checks, and health endpoints must have their uvicorn request logs filtered out to keep logs clean.
- **Concurrency Rate Limiting**: Limiters must be configured on all operational paths using the global environment variable `RATE_LIMIT` (default 30/minute).
- **Idempotency Key Scope**: Idempotency keys are scoped strictly to the calling `user_id`. An idempotency key clash for User A must never conflict with User B.

---

## All Scenarios in Plain English

### Scenario 1 — Ingestion through Web UI
- A logged-in user drops an invoice on the Web UI. The browser posts to `/ingest/ui`.
- The server validates the PDF format and counts the pages (3).
- It verifies the user's subscription quota and locks the page reservation.
- It detects the vendor using exact/fuzzy name matching on Page 1 text.
- It enqueues the `normalize` job in Postgres, returning the job ID.
- The UI listens to the SSE stream `/jobs/{job_id}/stream` to render status changes.

### Scenario 2 — API client with idempotency cache hit
- An integration script POSTs to `/v1/extract` carrying `Idempotency-Key: key_abc` and `invoice.pdf`.
- The database finds `key_abc` exists with status `done` and matching file hash.
- The server retrieves the mapped output JSON from the `extractions` table and returns it immediately. The extraction pipeline is not executed, and no extra pages are billed.

### Scenario 3 — File size limit breached
- A client attempts to upload a 60MB scanned PDF to `/ingest/rest`.
- The server's size guard middleware intercepts the request stream before loading it into memory.
- The connection is closed with an HTTP 413 (Payload Too Large) error, protecting the application memory from DoS.

---

## Error Responses

| Situation | HTTP Code | Error Message / Code |
|---|---|---|
| Non-PDF file uploaded | 400 | `"Only PDF files are allowed."` |
| Document page count exceeds cap | 400 | `{"code": "DOCUMENT_TOO_LARGE", "message": "..."}` |
| File size exceeds limit | 413 | `"Request Entity Too Large"` |
| Rate limit threshold breached | 429 | `"Too Many Requests"` |
| Database connection failure | 503 | `"Service temporarily unavailable. Please retry."` |
| Idempotency Key payload clash | 409 | `{"code": "CONFLICT", "message": "Idempotency key conflict..."}` |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_auth.py`](../../tests/test_auth.py) | `test_login_success` | Verifies `/auth/login` validates credentials and returns a valid JWT. |
| [`test_api_keys.py`](../../tests/test_api_keys.py) | `test_authenticate_with_valid_api_key` | Verifies `/v1/extract` accepts API keys in headers. |
| | `test_idempotency_duplicate_returns_cached` | Verifies idempotency cache hits return stored mapped results. |
| | `test_idempotency_conflict_returns_409` | Verifies clashing files with the same key return HTTP 409. |
| [`test_page_limits.py`](../../tests/test_page_limits.py) | `test_upload_exceeding_pages_returns_400` | Verifies page count guards reject files exceeding the page cap. |
| | `test_quota_check_db_failure_blocks_upload_with_503` | Verifies DB connection exceptions map cleanly to HTTP 503. |

---

## Quick Reference

| Route Path | Allowed Methods | Auth Type | Primary Table |
|---|---|---|---|
| `/auth/login` | POST | None | `users` |
| `/v1/extract` | POST | API Key / Bearer | `idempotency_claims` / `jobs` |
| `/ingest/{source_type}` | POST | Bearer | `jobs` / `extractions` |
| `/jobs/{job_id}/stream` | GET | Bearer / Query Token | `jobs` |
| `/extractions/{id}/corrections` | PUT | Bearer | `extractions` / `review_events` |
