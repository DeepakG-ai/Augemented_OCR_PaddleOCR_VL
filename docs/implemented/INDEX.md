# Implemented Features — Master Index

One line per feature. Read this first. Then open the linked doc for full scenarios, rules, and test coverage.

Status: **✓ doc** = full `implemented/` doc exists. **needs doc** = not yet written.

---

## Core Pipeline

The pipeline is **4 stages only**: normalize → ocr → llm → postprocess. There is no outbound or export stage. Workers coordinate only through the Postgres `jobs` table using `FOR UPDATE SKIP LOCKED`.

| Feature | One-liner | Status |
|---|---|---|
| [Normalize](normalizer.md) | Downloads original PDF from object store, renders all pages to images (32px-aligned), classifies each page as digital (pypdfium2) or scanned, stores page images + word geometry to `pages` table. Enqueues both `ocr` and `llm` in parallel. | **✓ doc** |
| [OCR](ocr_worker.md) | PaddleOCR on scanned pages only. Digital pages reuse pypdfium2 word geometry already in `pages`. Merges both into one unified word-box schema and saves to `ocr_data`. Triggers postprocess when done. | **✓ doc** |
| [LLM](llm_worker.md) | Sends page images to Qwen3-VL. Page 1: system prompt includes bounding-box schema (boxes + values). Pages 2+: line items only. Per-page progress via `on_page_done` callback. Page-1 boxes saved to `qwen_layout_boxes`. Does NOT set status=done — postprocess owns the final status. Triggers postprocess when done. | **✓ doc** |
| [Postprocess](postprocess_worker.md) | Reads `qwen_layout_boxes` for this vendor+template, maps every field to a word-box to build `field_locations`. Applies spatial memory overrides. Applies ERP field mapping (if configured). Sets status=done. Releases quota reservation. | **✓ doc** |

---

## Document Understanding

| Feature | One-liner | Status |
|---|---|---|
| [Vendor Detection](vendor-detection.md) | Renders page 1 of the uploaded PDF, extracts words, matches against `vendor_aliases` using exact match first then rapidfuzz scoring. Unknown vendor → HTTP 409. System never auto-creates a vendor. | **✓ doc** |
| [Templates](templates.md) | One template per vendor. Defines header fields, line item fields, format type, prompt instructions, and extraction rules. System prompt is rebuilt fresh from DB on every LLM run — no caching. | **✓ doc** |
| [Spatial Memory](spatial-memory.md) | Saves WHERE a field is on the page (normalized bbox + page number + layout key `vendor_id:template_id`). On reuse reads current document's text inside the saved region — never replays the old corrected value. Header fields only in phase 1. | **✓ doc** |
| [Gold Corrections](gold-corrections.md) | Per-vendor correction examples injected into the system prompt. Value-redacted so the model learns the field structure, not a stale hardcoded value. Managed via `/vendors/{id}/gold-corrections`. | **✓ doc** |
| [Review / Corrections](review.md) | Three-panel UI (fields / PDF viewer / JSON). User draws bounding boxes on the PDF to correct wrong field extractions. Saving a correction writes to `review_events` and saves to spatial memory. | **✓ doc** |
| [ERP Field Mapping](erp-mapping.md) | Per-vendor field name remapping for downstream ERP systems. Raw `result` is left untouched; mapped copy stored in `mapped_result`. Schemas define canonical field sets. Route: `/vendors/{id}/mapping`. Applied in postprocess. | **✓ doc** |

---

## Auth, Access, and Isolation

| Feature | One-liner | Status |
|---|---|---|
| [Auth / JWT](auth.md) | `POST /auth/login` → JWT (8h TTL, HS256, bcrypt). Ownership assertions (`assert_vendor_access`, `assert_extraction_access`, etc.) always walk back to `vendors.user_id`. Admin role short-circuits all ownership checks. | **✓ doc** |
| [Multi-tenancy](auth.md#multi-tenancy) | All data chains through `vendors.user_id`. Extractions and jobs have no `user_id` column — ownership is always resolved by walking back to the vendor. Client A can never see Client B's data. | **✓ doc** |
| [API Keys](auth.md#api-key-admin-management) | Admin-managed per-user API keys for programmatic access. Key hash stored (SHA-256), prefix stored for display, full key shown once at creation. Routes: `/admin/api-keys`. | **✓ doc** |
| [Rate Limiting](auth.md#rate-limiting) | slowapi middleware on all routes. Configurable via `RATE_LIMIT_PER_MINUTE` env var (default 30/min). Returns HTTP 429 on breach. | **✓ doc** |

---

## Billing and Quota

| Feature | One-liner | Status |
|---|---|---|
| [Subscription / Quota](subscription.md) | `subscriptions` + `topups` tables. Quota math via `get_user_quota_v2`. One active subscription per user (partial unique index). Top-ups attach to the active subscription. Everything expires at `period_end` (use-it-or-lose-it). Pages reserved at upload, released by postprocess on completion or by LLM on cancel/partial. | **✓ doc** |
| [Topup Requests](subscription.md) | Users request extra pages from the UI (`/me/topup-requests`). Admins see a queue and approve or reject. Approved requests call `admin_add_topup` and attach pages to the current active subscription. | **✓ doc** |

---

## Observability

| Feature | One-liner | Status |
|---|---|---|
| [SSE Progress](sse-progress.md) | `GET /jobs/{job_id}/stream` keeps a persistent connection open. Frontend never polls — it receives live events (progress, done, failed). | **✓ doc** |
| [MLflow Tracing](mlflow-tracing.md) | 1 PDF = 1 hierarchical `field_extraction` trace. Each pipeline stage is a child span. `MLFLOW_ENABLED=false` in all tests — never emit real traces during tests. Trace accessible from UI at `/extractions/{id}/trace`. | needs doc |
| [Page Logger](logging.md) | Per-extraction human-readable run log written to `PIPELINE_LOG_DIR`. Records pages, billable pages, digital/scanned split, Qwen success/fail/skip counts, field count, duration, errors. Admin reads via `/admin/logs/pipeline`. | needs doc |
| [Structured Logging](logging.md) | `plog.event(...)` and `plog.timed(...)` emit structured JSON log lines. Context vars track `extraction_id`, `filename`, `stage`, `worker` per log line. | needs doc |

---

## Infrastructure

| Feature | One-liner | Status |
|---|---|---|
| [FastAPI Router](main.md) | Central FastAPI routing layer managing lifespans, global middlewares (CORS, slowapi rate-limiting, custom AccessLogMiddleware), file payload constraints, and HTTP endpoints. | **✓ doc** |
| [Database Layer](db.md) | PostgreSQL schema definitions and asyncpg query functions (no ORM, advisory-locked migrations, JSONB structures). | **✓ doc** |
| [Job Queue](job-queue.md) | Postgres `jobs` table. `claim_job` uses `FOR UPDATE SKIP LOCKED`. Stale job recovery on worker startup (5 min threshold) and every 60s in the poll loop (10 min threshold). `ensure_job` is idempotent — unique index blocks duplicate `(extraction_id, job_type)` in queued/running state. | needs doc |
| [Object Storage](object-storage.md) | MinIO abstraction in `object_store.py`. Falls back to `.local_object_store/` on disk when MinIO is unavailable. No code change needed to switch. Buckets: `documents` (originals), `artifacts` (page images). | **✓ doc** |
| [Redis Cache](redis-cache.md) | Async Redis cache for extraction results. System prompts are NOT cached — rebuilt live from DB on every run. `REDIS_URL` env var. | needs doc |
| [Idempotency](auth.md#how-idempotency-works) | Upload requests carry an optional `Idempotency-Key` header (API path only). Server checks `idempotency_claims` table before creating a new extraction. Duplicate key + same file → return existing result. Duplicate key + different file → HTTP 409. | **✓ doc** |
| [Contract Endpoint](contract.md) | `GET /extractions/{id}/contract` returns the extraction result normalized into a canonical purchase-order shape via `contracts.py`. JSON only — not Excel/CSV. Used by API clients. | needs doc |

---

## Document Template

Every file in `docs/implemented/` follows this section order:

```
# Feature Name
## What it is          — 1 paragraph, no jargon
## Key States          — only if the feature has distinct modes
## How it works        — numbered steps, end to end
## Rules               — hard constraints, bullet list
## All Scenarios       — numbered, plain English, happy path + edge cases
## Error Responses     — table: situation → HTTP code → message
## Test Coverage       — table: test name → what it proves
## Quick Reference     — table: situation → answer
```

`subscription.md` is a good reference implementation of this template.
