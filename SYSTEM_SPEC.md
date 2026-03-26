# Augmented OCR — System Specification

**Semantic Document Data Extraction via Vision-Language Models**

| Field | Value |
|-------|-------|
| Version | 1.0.0 |
| Last Updated | 2026-03-24 |
| Stack | Python 3.11 · FastAPI · Celery · Redis · PostgreSQL 16 · MinIO · PyMuPDF · PaddleOCR · vLLM · React 18 · TypeScript · react-konva |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Core Pipeline — Step by Step](#3-core-pipeline--step-by-step)
4. [Infrastructure & Services](#4-infrastructure--services)
5. [Database Schema](#5-database-schema)
6. [API Reference](#6-api-reference)
7. [WebSocket Protocol](#7-websocket-protocol)
8. [Celery Worker & Task Logic](#8-celery-worker--task-logic)
9. [AI Inference Layer](#9-ai-inference-layer)
10. [Frontend Architecture](#10-frontend-architecture)
11. [Zero-Touch Automation](#11-zero-touch-automation)
12. [Multi-Page Conflict Resolution](#12-multi-page-conflict-resolution)
13. [Security Model](#13-security-model)
14. [Deployment Guide](#14-deployment-guide)
15. [Configuration Reference](#15-configuration-reference)
16. [File Map](#16-file-map)

---

## 1. Executive Summary

Augmented OCR is a semantic document data extraction pipeline. Unlike traditional OCR (which extracts all text) or Zonal OCR (which crops fixed regions), Augmented OCR uses **Vision-Language Models** to understand document context and extract specific field values based on semantic meaning and spatial hints.

### Key Differentiators

| Feature | Traditional OCR | Zonal OCR | **Augmented OCR** |
|---------|----------------|-----------|-------------------|
| Template rigidity | N/A | Fixed pixel zones break on layout shifts | Semantic anchors with normalized coordinates adapt to layout variation |
| Context understanding | None | None | VLM understands labels, proximity, and document structure |
| Multi-page handling | Page-by-page dump | Single page only | Searches all pages, collects candidates, resolves conflicts |
| Learning over time | None | None | Stores semantic templates per vendor → zero-touch automation |

### Core Flow (30-Second Summary)

```
User uploads invoice → clicks to place semantic anchors on canvas
    → API accepts (202) → Celery pushes to queue
    → Worker: PDF burst → layout analysis → VLM extraction per page
    → Results saved (JSONB) → delivered via WebSocket
    → Rules stored in templates → next document from same vendor = fully automatic
```

---

## 2. System Architecture

```
┌──────────────┐     ┌───────────────┐     ┌──────────────┐
│   Frontend   │────▶│   FastAPI     │────▶│    Redis     │
│  React 18 +  │ WS  │   (API)       │     │   (Broker)   │
│  react-konva │◀────│   Port 8000   │     │   Port 6379  │
│  Port 5173   │     └──────┬────────┘     └──────┬───────┘
└──────────────┘            │                     │
                            │                     ▼
                     ┌──────▼────────┐     ┌──────────────┐
                     │  PostgreSQL   │     │ Celery Worker │
                     │   16-alpine   │◀────│  (4 workers) │
                     │   Port 5432   │     └──────┬───────┘
                     └───────────────┘            │
                                                  │ HTTP
                     ┌───────────────┐     ┌──────▼───────┐
                     │     MinIO     │     │  vLLM Server │
                     │  (S3 Storage) │◀────│  (WSL2 GPU)  │
                     │  Port 9000    │     │  Port 8001   │
                     └───────────────┘     └──────────────┘
```

### Service Boundaries

| Service | Runs In | Purpose |
|---------|---------|---------|
| `api` | Docker | FastAPI — HTTP + WebSocket gateway |
| `worker` | Docker | Celery — PDF processing, VLM calls, DB writes |
| `postgres` | Docker | Persistent storage — documents, results, templates |
| `redis` | Docker | Celery broker + pub/sub for real-time WebSocket delivery |
| `minio` | Docker | S3-compatible object storage for uploaded documents |
| `vllm-server` | WSL2 (local GPU) | PaddleOCR-VL-1.5 inference via OpenAI-compatible API |
| `frontend` | Docker | React + Konva.js canvas for annotation |
| `flower` | Docker | Celery monitoring dashboard |

---

## 3. Core Pipeline — Step by Step

### Phase 1: Upload

```
Frontend                  API                     MinIO
   │                       │                        │
   │── POST /api/upload ──▶│                        │
   │   (multipart file)    │── put_object() ───────▶│
   │                       │                        │
   │◀── { s3_key,         │                        │
   │      presigned_url } ─│                        │
```

1. User drops a file (PDF, JPEG, PNG) onto the upload zone
2. Frontend sends `POST /api/upload` with the file as multipart form data
3. API validates file type (`image/jpeg`, `image/png`, `application/pdf`) and size (≤ 50MB)
4. File is uploaded to MinIO under `documents/{uuid}/{filename}`
5. API returns `s3_key` and a presigned URL for canvas display

### Phase 2: Annotation

1. The document image loads on the react-konva `<Stage>` canvas
2. User selects a field name from a dropdown (e.g., `invoice_total`, `invoice_date`)
3. User clicks on the document near the field value
4. Click coordinates are **normalized** relative to the fitted image dimensions:
   ```
   norm_x = (clickX - imageOffsetX) / imageWidth
   norm_y = (clickY - imageOffsetY) / imageHeight
   ```
5. A semantic prompt is auto-generated:
   > *"Find the value associated with the label 'invoice total' near coordinates (0.8500, 0.9000)..."*
6. Clicking the same field again **replaces** the existing anchor (dedup by field name)

### Phase 3: Extraction (Async)

```
Frontend              API                Redis              Worker
   │                   │                   │                   │
   │── POST /extract ─▶│                   │                   │
   │   (s3_key,        │── .delay() ──────▶│                   │
   │    vendor_id,     │                   │── task pickup ───▶│
   │    anchors)       │                   │                   │
   │                   │                   │                   │
   │◀── 202 Accepted ─│                   │                   │
   │   { job_id,       │                   │                   │
   │     document_id } │                   │                   │
   │                   │                   │                   │
   │── WS /ws/jobs/{id}│                   │                   │
   │                   │                   │◀── "processing" ──│
   │◀── {"status":     │                   │                   │
   │     "processing"} │                   │                   │
   │                   │                   │                   │
   │                   │                   │  (VLM inference)  │
   │                   │                   │                   │
   │                   │                   │◀── "completed" ───│
   │◀── {"status":     │                   │    + data         │
   │     "completed",  │                   │                   │
   │     "data": {...}}│                   │                   │
```

1. `POST /api/extract` validates the vendor exists and anchors are provided
2. A `Document` row is created with `status = "queued"`
3. A Celery task is dispatched with only `s3_key` (never raw image bytes)
4. API returns **202 Accepted** with `job_id` (= `document_id`)
5. Frontend immediately opens a WebSocket to `/ws/jobs/{job_id}`
6. Worker picks up the task and begins processing (see [Section 8](#8-celery-worker--task-logic))
7. Results are delivered in real-time via Redis pub/sub → WebSocket

### Phase 4: Template Learning

After successful extraction with `save_rules=true`:

1. Each anchor's field, prompt, coordinates, and resolved `page_index` are upserted into `semantic_templates`
2. `sample_count` is incremented on conflict (same vendor + field)
3. Future documents from the same vendor can skip annotation entirely (zero-touch)

---

## 4. Infrastructure & Services

### Docker Compose Services

All infrastructure runs in Docker via `docker-compose.yml`. The vLLM server runs locally on WSL2 with GPU access.

| Service | Image | Notes |
|---------|-------|-------|
| `postgres` | `postgres:16-alpine` | Healthcheck with `pg_isready`, persistent volume `pgdata` |
| `redis` | `redis:7-alpine` | `maxmemory 2gb`, `allkeys-lru` eviction policy |
| `minio` | `minio/minio:latest` | Console on port 9001, persistent volume `miniodata` |
| `api` | `backend/Dockerfile` | Shared Dockerfile with worker, `extra_hosts` for vLLM connectivity |
| `worker` | `backend/Dockerfile` | Same Dockerfile as API, runs Celery with 4 workers on `ocr` queue |
| `flower` | `mher/flower:2.0` | Celery monitoring at port 5555 |
| `frontend` | `frontend/Dockerfile` | Vite dev server at port 5173 |

### vLLM Server (WSL2 Local)

The VLM inference server runs outside Docker on the WSL2 host with direct GPU access:

```bash
source ~/vllm_env/bin/activate
python3 -m vllm.entrypoints.openai.api_server \
  --model PaddlePaddle/PaddleOCR-VL-1.5 \
  --port 8001 \
  --dtype float16 \
  --max-model-len 2048 \
  --gpu-memory-utilization 0.80 \
  --max-num-seqs 4 \
  --trust-remote-code
```

Docker containers reach the host via `host.docker.internal:8001` (configured via `extra_hosts: ["host.docker.internal:host-gateway"]`).

---

## 5. Database Schema

### Entity Relationship Diagram

```mermaid
erDiagram
    vendors ||--o{ documents : "has"
    vendors ||--o{ semantic_templates : "has"
    vendors ||--o{ extraction_results : "has"
    documents ||--o| extraction_results : "produces"

    vendors {
        uuid id PK
        text name UK
        timestamptz created_at
    }

    semantic_templates {
        uuid id PK
        uuid vendor_id FK
        text field_name
        text semantic_prompt
        float norm_x
        float norm_y
        int page_index "nullable"
        int sample_count
        timestamptz created_at
        timestamptz updated_at
    }

    documents {
        uuid id PK
        uuid vendor_id FK
        text s3_key
        text filename
        text status "queued|processing|completed|failed|needs_review"
        text celery_task_id
        timestamptz uploaded_at
        timestamptz completed_at
    }

    extraction_results {
        uuid id PK
        uuid document_id FK_UK
        uuid vendor_id FK
        jsonb raw_data
        jsonb candidate_log "nullable"
        boolean is_verified
        timestamptz extracted_at
    }
```

### Key Constraints & Indexes

| Table | Constraint / Index | Purpose |
|-------|--------------------|---------|
| `semantic_templates` | `UNIQUE(vendor_id, field_name)` | Prevents duplicate rules per vendor |
| `extraction_results` | `UNIQUE(document_id)` | One result per document |
| `extraction_results` | `GIN(raw_data)` | Fast JSONB queries on extracted data |
| `documents` | `INDEX(status)` | Queue polling by status |
| `documents` | `INDEX(vendor_id)` | Vendor-scoped document listing |

### Migrations

Alembic runs automatically on API startup via the FastAPI lifespan event:

```python
subprocess.run(["alembic", "upgrade", "head"], capture_output=True, timeout=30)
```

---

## 6. API Reference

Base URL: `http://localhost:8000/api`

### `POST /upload`

Upload a document to MinIO.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file` | multipart | Yes | JPEG, PNG, or PDF (≤ 50MB) |

**Response** `200 OK`:
```json
{
  "s3_key": "documents/uuid/invoice.pdf",
  "filename": "invoice.pdf",
  "size": 245760,
  "presigned_url": "https://..."
}
```

---

### `POST /extract`

Submit an extraction job. Returns immediately (async).

**Request Body**:
```json
{
  "s3_key": "documents/uuid/invoice.pdf",
  "vendor_id": "550e8400-e29b-41d4-a716-446655440000",
  "anchors": [
    {
      "field": "invoice_total",
      "prompt": "Find the value associated with...",
      "x_pct": 0.85,
      "y_pct": 0.90,
      "page_index": null
    }
  ],
  "save_rules": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `s3_key` | string | MinIO key from upload response |
| `vendor_id` | UUID | Vendor to associate results with |
| `anchors` | array | Semantic anchors (empty = zero-touch path) |
| `save_rules` | boolean | Save/update semantic templates after extraction |

**Response** `202 Accepted`:
```json
{
  "job_id": "document-uuid",
  "document_id": "document-uuid",
  "status": "queued"
}
```

**Zero-touch path**: When `anchors` is empty, the API looks up stored `semantic_templates` for the vendor and converts them to anchors. Returns `422` if no templates exist.

---

### `GET /jobs/{job_id}`

Poll job status and results (fallback for WebSocket).

**Response** `200 OK`:
```json
{
  "job_id": "document-uuid",
  "document_id": "document-uuid",
  "status": "completed",
  "data": {
    "invoice_total": "$1,234.56",
    "invoice_date": "2026-03-15"
  },
  "candidates": null,
  "error": null
}
```

When `status = "needs_review"`, `candidates` contains multi-page conflict data:
```json
{
  "candidates": {
    "invoice_total": [
      { "value": "$42.00", "page_index": 0 },
      { "value": "$1,234.56", "page_index": 2 }
    ]
  }
}
```

---

### `GET /vendors`

List all vendors. **Response**: `VendorResponse[]`

### `POST /vendors`

Create a vendor. **Request**: `{ "name": "Acme Corp" }` — **Response** `201 Created`

### `GET /vendors/{vendor_id}/templates`

List all semantic templates for a vendor.

### `DELETE /vendors/{vendor_id}/templates/{field_name}`

Delete a specific template rule. **Response** `204 No Content`

### `POST /templates/confirm`

Human confirms the correct page for an ambiguous field.

**Request Body**:
```json
{
  "vendor_id": "uuid",
  "field_name": "invoice_total",
  "confirmed_page_index": 2,
  "confirmed_value": "$1,234.56"
}
```

### `GET /documents/{s3_key}/presign`

Generate a presigned URL for viewing a stored document. Path-traversal protected.

---

## 7. WebSocket Protocol

**Endpoint**: `ws://localhost:8000/ws/jobs/{job_id}`

Connect immediately after receiving the `202` response. The server subscribes to Redis channel `job:{job_id}` and forwards messages.

### Message Sequence

```
Client                   Server                  Redis
  │── connect ──────────▶│                         │
  │                      │── subscribe ───────────▶│
  │                      │   "job:{job_id}"        │
  │                      │                         │
  │◀── {"status":        │◀── message ────────────│
  │     "processing"}    │                         │
  │                      │                         │
  │◀── {"status":        │◀── message ────────────│
  │     "completed",     │                         │
  │     "data": {...}}   │                         │
  │                      │                         │
  │── close ────────────▶│── unsubscribe ─────────▶│
```

### Message Shapes

| Status | Payload |
|--------|---------|
| `processing` | `{ status, job_id }` |
| `completed` | `{ status, job_id, data }` |
| `needs_review` | `{ status, job_id, data, candidates }` |
| `failed` | `{ status, job_id, error }` |
| `timeout` | `{ status, job_id, error }` — after 5 minutes |

**Terminal statuses** (`completed`, `failed`, `needs_review`): server closes the connection after sending.

---

## 8. Celery Worker & Task Logic

### Task: `process_document_vqa`

Decorator: `@celery_app.task(bind=True, max_retries=3, queue="ocr")`

### Execution Flow

```python
# 1. Create a single event loop (reused for all async VLM calls)
loop = asyncio.new_event_loop()

# 2. Update status → "processing"
# 3. Fetch file bytes from MinIO (by s3_key, NOT from Redis)
# 4. Burst PDF to pages (PyMuPDF at 300 DPI) or wrap image

# 5. For each anchor:
#    a. If page_index is known (zero-touch) → go directly to that page
#    b. If page_index is None (first-time) → search ALL pages:
#       - Run layout analysis (PP-DocLayoutV3)
#       - Run VLM extraction (PaddleOCR-VL-1.5)
#       - Collect ALL non-null candidates (NO break on first hit)

# 6. Resolve candidates:
#    - 0 candidates → null
#    - 1 candidate  → auto-accept, record page_index
#    - N candidates → use last occurrence, flag for needs_review

# 7. Save extraction result (JSONB)
# 8. Upsert semantic templates (if save_rules=true)
# 9. Publish result via Redis pub/sub

# 10. On failure: retry with exponential backoff (2^n seconds)
#     On max retries exhausted: set status="failed", notify frontend
```

### Retry Strategy

| Retry | Countdown | Behavior |
|-------|-----------|----------|
| 1st | 1 second | Re-queue task |
| 2nd | 2 seconds | Re-queue task |
| 3rd | 4 seconds | Re-queue task |
| 4th failure | — | `MaxRetriesExceededError` caught → status set to `failed` → frontend notified |

### Event Loop Pattern

A single `asyncio.new_event_loop()` is created per task invocation and reused for all `run_vqa()` calls via `loop.run_until_complete()`. The loop is closed in a `finally` block to prevent resource leaks.

```python
loop = asyncio.new_event_loop()
try:
    for page in pages:
        value = loop.run_until_complete(run_vqa(page, anchor, layout))
finally:
    loop.close()
```

---

## 9. AI Inference Layer

### PDF Processing (`pdf_utils.py`)

| Setting | Value | Rationale |
|---------|-------|-----------|
| Library | PyMuPDF (`fitz`) | 10x faster than `pdf2image` |
| DPI | 300 | Required for small-font accuracy |
| Color mode | RGB | RGBA→RGB conversion for 4-channel outputs |
| Max file size | 50 MB | DoS protection |
| Max pages | 100 | Memory protection |

### Layout Analysis (`layout.py`)

| Setting | Value |
|---------|-------|
| Model | `PPStructureV3(layout=True, table=False, ocr=False)` |
| Initialization | Module-level lazy singleton (loaded once per worker process) |
| Failure mode | Graceful — returns empty list, extraction continues without layout context |

Output is formatted into a human-readable summary string:
```
- text region at bbox [100, 200, 500, 250]
- table region at bbox [50, 300, 550, 800]
- title region at bbox [100, 50, 500, 100]
```

### VLM Extraction (`vqa_client.py`)

| Setting | Value | Rationale |
|---------|-------|-----------|
| Model | `PaddlePaddle/PaddleOCR-VL-1.5` | Optimized for document understanding |
| Temperature | `0.0` | **Deterministic** — no randomness in extraction |
| Max tokens | `128` | Field values are short; prevents runaway generation |
| Image format | Full page as base64 JPEG (quality=95) | No cropping — VLM needs full context |
| Timeout | 60 seconds (configurable) | Prevents worker hang on GPU failure |

### VQA Prompt Structure

```
Document structural context:
- text region at bbox [100, 200, 500, 250]
- table region at bbox [50, 300, 550, 800]

Extraction task:
Find the value associated with the label "invoice total"...

Spatial hint: The field is located near normalized coordinates
X: 0.8500, Y: 0.9000
(0,0 = top-left corner, 1,1 = bottom-right corner of the page)

Return ONLY this JSON with no other text:
{"invoice_total": "<extracted_value_or_null>"}
```

### Response Parsing

1. Strip markdown code fences (```json ... ```) if present
2. Parse as JSON
3. Extract the field value
4. Return `None` for null, empty string, or "null" string
5. On any parse failure → return `None` (never crash the task)

---

## 10. Frontend Architecture

### Technology

| Library | Purpose |
|---------|---------|
| React 18 | UI framework |
| TypeScript | Type safety |
| react-konva / Konva.js | HTML5 Canvas with React bindings |
| Vite 6 | Build tool and dev server |
| Tailwind CSS 3 | Styling |
| use-image | Konva image loading hook |
| uuid | Client-side anchor ID generation |

### Component Tree

```
App.tsx
├── VendorSelector       — Vendor dropdown + create new vendor
├── SemanticCanvas       — Document image + annotation layer
│   ├── Layer 1: Image   — Base document (never re-renders on click)
│   └── Layer 2: Markers — Annotation circles + labels (re-renders on state)
└── ResultsPanel         — Extraction results + conflict resolution UI
```

### Canvas Coordinate Math

The image is fitted to the canvas while preserving aspect ratio:

```typescript
// Fit image to stage
if (imgRatio > stageRatio) {
  width = stageSize.width;
  height = stageSize.width / imgRatio;
} else {
  height = stageSize.height;
  width = stageSize.height * imgRatio;
}
offsetX = (stageSize.width - width) / 2;
offsetY = (stageSize.height - height) / 2;
```

Click normalization accounts for zoom and pan transforms:

```typescript
const transformedX = (pointerX - panOffsetX) / zoomScale;
const transformedY = (pointerY - panOffsetY) / zoomScale;
const norm_x = (transformedX - imageOffsetX) / imageWidth;
const norm_y = (transformedY - imageOffsetY) / imageHeight;
```

### Dual-Layer Rendering

| Layer | Content | Re-renders |
|-------|---------|------------|
| Layer 1 | `<KonvaImage>` — the document | Only when image source changes |
| Layer 2 | `<Circle>` + `<Text>` markers | On every anchor add/remove |

This prevents expensive image redraws when annotations change.

---

## 11. Zero-Touch Automation

### How It Works

```mermaid
flowchart TD
    A[Upload new document] --> B{Anchors provided?}
    B -->|Yes| C[First-time path<br>Search all pages]
    B -->|No| D{Templates exist<br>for vendor?}
    D -->|Yes| E[Zero-touch path<br>Use stored templates]
    D -->|No| F[422 Error<br>Annotation required]
    C --> G[Extract values]
    E --> G
    G --> H{save_rules = true?}
    H -->|Yes| I[Upsert templates<br>with page_index]
    H -->|No| J[Skip]
    I --> K[Next document =<br>zero-touch ready]
```

### Template Flywheel

1. **First document**: Human annotates fields manually → extraction runs → templates saved with `page_index` (or `null` if ambiguous)
2. **Second document**: Human clicks "Auto-Extract" → API sends empty `anchors` → templates are loaded → worker goes directly to known pages
3. **Template refinement**: Each successful extraction increments `sample_count` and updates `page_index` if resolved

### Template Schema

```sql
UNIQUE(vendor_id, field_name)  -- one rule per field per vendor
page_index INTEGER NULLABLE    -- null = search all pages, int = go direct
sample_count INTEGER           -- incremented on each upsert (confidence signal)
```

---

## 12. Multi-Page Conflict Resolution

### The Problem

Invoice totals might appear on page 1 as "Total Qty: 42" (a false positive) and on page 3 as "Grand Total: $1,234.56" (the real value). **Breaking on first non-null is wrong.**

### The Solution

```
Page 1: "Total Qty: 42"     → candidate {value: "42", page_index: 0}
Page 2: null                → skipped
Page 3: "Grand Total: $1,234.56" → candidate {value: "$1,234.56", page_index: 2}
```

**Resolution rules:**
- **0 candidates**: Field value = `null`
- **1 candidate**: Auto-accept. Store `page_index` in template.
- **N candidates**: Use **last occurrence** as default (invoice summaries are typically on the last page). Set `status = "needs_review"`. Store all candidates in `candidate_log`.

### Human Review Flow

1. Frontend detects `status = "needs_review"` and renders candidates in the conflict section
2. Human can retry extraction or save the current result manually

---

## 13. Security Model

| Layer | Protection |
|-------|-----------|
| File upload | MIME type validation (`image/jpeg`, `image/png`, `application/pdf`) + 50MB size limit |
| S3 keys | Path traversal prevention (regex validation, `..` blocked, `documents/` prefix required) |
| MinIO | Presigned URLs for frontend access — raw credentials never exposed |
| Database | SQLAlchemy ORM with parameterized queries — no SQL injection risk |
| Celery | JSON-only serialization — no pickle deserialization attacks |
| CORS | Configured on FastAPI (currently `allow_origins=["*"]` for development) |
| Secrets | All credentials in `.env` file, excluded via `.gitignore` |
| Redis | `maxmemory` + `allkeys-lru` — prevents OOM on broker |

---

## 14. Deployment Guide

### Prerequisites

- Docker Desktop with WSL2 integration
- NVIDIA GPU (RTX 4060 or better) with CUDA drivers in WSL2
- vLLM installed in WSL2 Python environment

### Step 1: Start vLLM (WSL2 Terminal)

```bash
source ~/vllm_env/bin/activate
export HUGGING_FACE_HUB_TOKEN=hf_your_token_here
python3 -m vllm.entrypoints.openai.api_server \
  --model PaddlePaddle/PaddleOCR-VL-1.5 \
  --port 8001 \
  --dtype float16 \
  --max-model-len 2048 \
  --gpu-memory-utilization 0.80 \
  --max-num-seqs 4 \
  --trust-remote-code
```

### Step 2: Start Docker Compose (PowerShell)

```powershell
cd Augemented_OCR_PaddleOCR_VL
docker compose up --build
```

### Step 3: Access

| Service | URL |
|---------|-----|
| Frontend + API | http://localhost:8000 |

### Step 4: Verify Connectivity

```bash
# From worker container → vLLM
docker compose exec worker python -c \
  "import httpx; print(httpx.get('http://host.docker.internal:8001/health').status_code)"
```

---

## 15. Configuration Reference

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://ocr:ocr@postgres:5432/ocrdb` | Async DB connection (API) |
| `SYNC_DATABASE_URL` | `postgresql://ocr:ocr@postgres:5432/ocrdb` | Sync DB connection (Worker) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis broker + pub/sub |
| `MINIO_ENDPOINT` | `minio:9000` | MinIO server address |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `MINIO_BUCKET` | `ocr-documents` | Default bucket name |
| `MINIO_SECURE` | `false` | Use HTTPS for MinIO |
| `VLLM_SERVER_URL` | `http://host.docker.internal:8001` | vLLM inference endpoint |
| `VLLM_MODEL` | `PaddlePaddle/PaddleOCR-VL-1.5` | Model name for API calls |
| `VLLM_TIMEOUT` | `60` | HTTP timeout for VLM calls (seconds) |
| `CELERY_CONCURRENCY` | `4` | Number of Celery worker processes |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Frontend → API base URL |
| `VITE_WS_BASE_URL` | `ws://localhost:8000` | Frontend → WebSocket base URL |

---

## 16. File Map

```
Augemented_OCR_PaddleOCR_VL/
├── .env                          # Environment variables (git-ignored)
├── .env.example                  # Template for .env
├── .gitignore                    # Excludes .env, __pycache__, node_modules, etc.
├── docker-compose.yml            # All services (vLLM commented out for WSL2)
├── install_vllm_wsl2.sh          # vLLM installation script for WSL2
├── SYSTEM_SPEC.md                # ← This document
│
├── backend/
│   ├── Dockerfile                # Shared by api + worker services
│   ├── requirements.txt          # Python dependencies
│   ├── main.py                   # FastAPI app, lifespan, CORS, routers
│   ├── alembic.ini               # Alembic config
│   │
│   ├── alembic/
│   │   ├── env.py                # Migration environment (imports all models)
│   │   └── versions/
│   │       └── 001_initial.py    # Initial schema migration
│   │
│   ├── api/
│   │   ├── routes.py             # All HTTP endpoints
│   │   └── websockets.py         # WebSocket handler with Redis pub/sub
│   │
│   ├── core/
│   │   ├── config.py             # Pydantic Settings (loads .env)
│   │   └── database.py           # SQLAlchemy async engine + session factory
│   │
│   ├── models/
│   │   ├── __init__.py           # Re-exports all models
│   │   ├── vendor.py             # Vendor ORM model
│   │   ├── document.py           # Document ORM model
│   │   ├── extraction_result.py  # ExtractionResult ORM model (JSONB)
│   │   └── semantic_template.py  # SemanticTemplate ORM model
│   │
│   ├── schemas/
│   │   ├── extract.py            # ExtractionRequest, AnchorInput, TemplateConfirmRequest
│   │   ├── vendor.py             # VendorCreate, VendorResponse, SemanticTemplateResponse
│   │   └── job.py                # JobStatusResponse, JobResultResponse
│   │
│   ├── services/
│   │   ├── storage.py            # MinIO client singleton, upload, download, presign
│   │   └── template_service.py   # Template CRUD (async, used by API)
│   │
│   └── worker/
│       ├── celery_app.py         # Celery configuration
│       ├── tasks.py              # process_document_vqa task
│       └── inference/
│           ├── pdf_utils.py      # PyMuPDF PDF burst + image conversion
│           ├── layout.py         # PP-DocLayoutV3 layout analysis
│           └── vqa_client.py     # vLLM HTTP client (async)
│
└── frontend/
    ├── Dockerfile                # Node 20 Alpine
    ├── package.json              # React 18, Konva, TypeScript, Vite
    ├── vite.config.ts            # Dev server with API/WS proxy
    ├── tailwind.config.js        # Tailwind configuration
    └── src/
        ├── main.tsx              # React entry point
        ├── index.css             # Global styles, animations, glassmorphism
        ├── App.tsx               # Root component, layout, state management
        ├── types/
        │   └── index.ts          # All TypeScript interfaces + field options
        ├── hooks/
        │   └── useExtraction.ts  # Upload, extraction, WS, vendor, template hooks
        └── components/
            ├── SemanticCanvas.tsx # Konva canvas, annotation, zoom/pan
            ├── ResultsPanel.tsx  # Results display, conflict resolution UI
            └── VendorSelector.tsx # Vendor dropdown + creation form
```

---

*End of specification.*
