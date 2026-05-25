# Augmented OCR — Production Architecture Blueprint

## Product intent
Augmented OCR is a multi-tenant document extraction platform where admins have global access and each client is fully isolated from every other client. A single client can onboard many vendors, and each vendor can have multiple document layouts and extraction rules. The system ingests PDF files or folders, detects the vendor from the first page, extracts structured JSON using Qwen, tracks token/page/latency usage, and stores auditable results.

## Current repo understanding
The repository already contains a substantial backend with database, worker, OCR runner, vendor detector, PDF extraction, object store, exporter, tracing, cache, logging, and text matching modules. The current backend appears feature-rich but too flat in structure, with core concerns spread across large files like `main.py`, `db.py`, `text_matcher.py`, and `worker.py`. There are also multiple operational docs and prompt-related files, which shows the product exists beyond a prototype stage but needs stronger architecture boundaries.

## Main business requirements
1. Admin can see all tenants, jobs, vendors, usage, errors, and system health.
2. Client users can only access their own tenant data.
3. Tenant A must never see Tenant B files, results, vendors, jobs, prompts, metrics, or exports.
4. One client can manage N vendors.
5. Each vendor can have N templates / layouts / prompt strategies.
6. Upload supports single file, bulk folder, API upload, and scheduled ingestion.
7. First page is used for vendor detection.
8. All pages are then processed by Qwen-based extraction.
9. Output must be normalized JSON with confidence and audit metadata.
10. Usage must capture pages, tokens, latency, cost estimates, retries, failures, model version, and prompt version.
11. Concurrent client schedules must run independently.
12. The system must be production-safe, observable, testable, and scalable.

## Proposed production architecture

### Core domains
- Identity and access: users, roles, tenants, policies.
- Tenant management: client account, quotas, vendor configs, schedules.
- Ingestion: file upload, folder upload, object storage registration.
- Preprocessing: PDF split, image render, page metadata, checksum.
- Vendor detection: first-page classifier using OCR/text/layout signals.
- Extraction orchestration: per-document and per-page workflow.
- Qwen inference: structured extraction with prompt versioning.
- Post-processing: validation, normalization, reconciliation, confidence checks.
- Usage and billing telemetry: page counts, tokens, latency, cost.
- Export and integration: JSON, CSV, webhook, ERP connectors.
- Audit and observability: logs, traces, events, metrics.

### Recommended service flow
1. User uploads PDF or folder.
2. API stores file metadata and object location.
3. A document job is created.
4. Scheduler or queue dispatcher sends the document job to preprocessing.
5. First page is rendered and passed to vendor detection.
6. Vendor detector resolves `(tenant_id, vendor_id, template_id)`.
7. Remaining pages are batched for extraction.
8. Qwen extraction returns structured JSON plus raw response metadata.
9. Validation layer checks schema, required fields, totals, data types, hallucination indicators, and confidence rules.
10. Results are persisted with page-level lineage.
11. Usage metrics are written for every stage.
12. Client retrieves results through tenant-scoped APIs.

## Multi-tenant isolation model

### Required isolation rules
- Every business table must carry `tenant_id` except platform-global reference tables.
- Every object-storage key must be tenant-prefixed.
- Every queue message must contain `tenant_id`, `document_id`, and `job_id`.
- Every log line and trace span must include `tenant_id`, `client_id`, `vendor_id`, and `request_id` when available.
- Admin role bypasses tenant filter explicitly; clients never do.
- Row-level security is strongly recommended if using PostgreSQL.

### Access model

| Role | Scope |
|---|---|
| `platform_admin` | Global — all tenants |
| `tenant_admin` | Full control within one client |
| `tenant_operator` | Upload, run, review, export within one client |
| `tenant_viewer` | Read-only within one client |
| `service_worker` | Machine identity with scoped internal permissions |

## Scheduler and concurrency design
When Client A and Client B both schedule jobs at 10 PM, the correct production answer is: **both run independently through a queue-based scheduler**, not a single in-process cron loop.

### Recommended design
- Scheduler service only enqueues due jobs.
- Durable queue: Celery+Redis, Dramatiq, RQ, or cloud alternatives.
- Each scheduled run creates an isolated `batch_run` record.
- Workers pull jobs concurrently.
- Per-tenant fairness limits prevent starvation.
- Global concurrency cap protects GPU/CPU.

### Concurrency policy example
- Global extraction workers: 20
- Per-tenant active documents: 5
- Per-document parallel pages: 4
- Vendor detection: high priority queue
- Export/webhook: low priority queue

## Hallucination controls for Qwen
- Strict JSON schema per vendor template.
- Prompt versioning with immutable template history.
- Vendor-specific extraction contracts, not one generic prompt.
- Bounding-box / OCR grounding for extracted values where possible.
- Confidence scoring by field.
- Reject-or-review rules for impossible totals, missing invoice numbers, invalid dates.
- Store raw model response separately from normalized response.
- Add regression evaluation set per vendor.
- Track hallucination rate per template version and model version.

## Metrics to capture

### Per page
- `page_number`, `render_latency_ms`, `ocr_latency_ms`, `qwen_latency_ms`
- `input_tokens`, `output_tokens`, `total_tokens`
- `prompt_version`, `model_version`, `retry_count`, `status`

### Per document
- `tenant_id`, `vendor_id`, `template_id`, `page_count`
- `total_input_tokens`, `total_output_tokens`, `total_tokens`
- `vendor_detection_latency_ms`, `end_to_end_latency_ms`
- `cost_estimate`, `started_at`, `finished_at`, `failure_reason`

### Per tenant
- daily documents, daily pages, daily tokens
- average latency, failure rate, top vendors
- active schedules, storage consumed

## Recommended backend folder structure

```text
backend/
  src/
    app/
      api/v1/
        admin/
        auth/
        tenants/
        vendors/
        documents/
        jobs/
        schedules/
        exports/
        usage/
      core/
        config.py
        security.py
        logging.py
        tracing.py
        tenancy.py
        exceptions.py
      domain/
        tenants/
        users/
        vendors/
        documents/
        extraction/
        scheduling/
        billing/
        audits/
      services/
        ingestion/
        preprocessing/
        vendor_detection/
        extraction/
        validation/
        exporting/
        notifications/
      workers/
        queue_app.py
        document_tasks.py
        page_tasks.py
        export_tasks.py
        schedule_tasks.py
      infrastructure/
        db/
          models/
          repositories/
          migrations/
        object_store/
        cache/
        queue/
        model_gateway/
        observability/
      schemas/
      tests/
        unit/
        integration/
        contract/
        e2e/
```

## Refactor map: current → target

| Current file | Problem | Target direction |
|---|---|---|
| `main.py` | Mixes routes, logic, orchestration | Routers + service layer + startup wiring |
| `db.py` | All DB logic centralized | Models + repositories + migrations |
| `worker.py` | Coupled workflow, hard to scale | Queue tasks + orchestration services |
| `vendor_detector.py` | Needs contract + versioning | Vendor-detection service with eval set |
| `extractor.py`, `ocr_runner.py` | Missing validation + observability | Preprocessing + inference + post-processing |
| `text_matcher.py`, `spatial_memory.py` | Good logic but buried | Extraction/grounding module with focused tests |

## Document workflow state machine

```
uploaded → stored → preprocessed → vendor_detected → extracting → validating → completed
                                                                       ↓
                                                           failed_retryable → retried
                                                                       ↓
                                                             failed_terminal → manual_review
```

## Queue design

| Queue | Priority | Purpose |
|---|---|---|
| `vendor-detection` | High | Fast first-page classification |
| `extraction-high` | High | Priority jobs |
| `extraction-default` | Normal | Standard jobs |
| `preprocessing` | Normal | PDF split, render |
| `validation` | Normal | Post-extraction checks |
| `export` | Low | JSON/CSV generation |
| `webhook` | Low | ERP/webhook delivery |
| `dead-letter` | — | Failed after all retries |

## Edge cases
- Corrupted or password-protected PDF
- Empty PDF or scanned image with no readable text
- Mixed-vendor pages in one PDF
- First page is a cover page, not an invoice
- Vendor not recognized from first page → manual review
- Page rotation or skew
- Large PDF causing GPU memory spikes
- Duplicate upload detection
- Same schedule triggered twice (idempotency required)
- Client hits quota limit mid-job
- Model timeout or malformed JSON response
- Partial extraction success (some pages ok, some failed)
- Tenant tries to access another tenant's document ID

## Test strategy

### Unit tests
- Vendor detection scoring
- OCR normalization
- Prompt builder
- JSON parser and validation rules
- Token accounting
- Tenant scoping helpers

### Integration tests
- Upload to object store + DB metadata creation
- Queue to worker execution
- PDF to page image conversion
- Vendor detect → extraction handoff
- Result persistence with lineage

### Contract tests
- Qwen response schema per vendor template
- Export payload shape
- Webhook signature format

### End-to-end tests
- Client uploads PDF and receives normalized JSON
- Bulk folder upload
- Schedule fires at defined time
- Two tenants run at same minute without interference
- Admin sees all jobs; client sees only own jobs
- Failed document enters manual review queue

## Scaling plan

### Phase 1 (now)
- Modular monolith
- Single API service + separate worker service
- PostgreSQL + Redis + object storage
- Good enough for early production with proper isolation

### Phase 2
- Dedicated worker pools for preprocessing and extraction
- Horizontal worker autoscaling
- GPU-aware routing
- Better per-tenant throttling

### Phase 3
- Independent model gateway and extraction orchestration services
- Usage analytics pipeline and data warehouse
- Multi-region storage and disaster recovery

## Immediate refactor priorities
1. Freeze features for a short architecture cleanup sprint.
2. Write and commit this architecture to the repo.
3. Split `main.py`, `db.py`, and `worker.py` first.
4. Introduce tenant-aware repositories and service layer boundaries.
5. Replace in-process scheduling with queue-backed scheduler.
6. Add usage-event tables for token/page/latency accounting.
7. Add extraction validation and manual-review workflow.
8. Add regression tests for top vendor templates.
9. Add admin and client dashboards.
10. Then continue feature building.

## Docs to add to workflow folder
- `production-architecture.md` ✅
- `production-architecture.html` ✅
- `tenant-isolation.md`
- `scheduler-design.md`
- `extraction-lifecycle.md`
- `usage-metering.md`
- `testing-strategy.md`
- `refactor-roadmap.md`

## Final guidance
Your project is no longer a 5-file prototype. It is already a real product and should now be treated like a production multi-tenant document platform. The main change you need is not "more code"; it is stronger boundaries: domain separation, queue-driven workflows, tenant isolation, observability, validation, and a documented architecture that your future self can trust.
