# Augmented OCR — System Specification

> **Version** 3.0 · **Updated** 2026-04-06 · **Status** Current implementation

---

## 1. Overview

Augmented OCR is a production document extraction system that converts scanned PDFs and images into structured JSON using a multimodal LLM (Qwen3-VL). Human reviewers can correct the machine output through an interactive visual mapping interface, and corrections feed back into future extractions via gold-example learning.

### Design Principles

| Principle | Implementation |
|---|---|
| Durable processing | Background job queue in Postgres, survives crashes and browser disconnects |
| Immutable machine output | `extractions.result` is never modified after extraction completes |
| Canonical corrections | `extractions.corrected_result` holds human-reviewed truth |
| Real-time feedback | SSE streaming for progress delivery (zero polling) |
| Binary storage outside DB | MinIO for all files; Postgres stores only metadata and object keys |
| Learning loop | Human corrections create gold examples that improve future prompts |

---

## 2. Technology Stack

```
Backend       FastAPI (Python 3.11)
Database      PostgreSQL 16
Cache         Redis 7
Object Store  MinIO (S3-compatible, local Docker)
PDF Render    PyMuPDF (fitz) + Pillow
OCR           PaddleOCR v5 (mobile det + rec)
LLM           Qwen3-VL via OpenAI-compatible HTTP endpoint
Tracing       Arize Phoenix (OpenTelemetry)
Frontend      Vanilla JS SPA served by FastAPI
Container     Docker Compose (multi-service)
```

---

## 3. System Architecture

```mermaid
graph TB
    subgraph Client
        UI["Browser SPA<br/>(qwen_frontend)"]
    end

    subgraph API["FastAPI (port 8000)"]
        INGEST["/ingest/{source_type}"]
        STREAM["/jobs/{id}/stream (SSE)"]
        REVIEW["/extractions/{id}/corrections"]
        STATIC["Static file server"]
    end

    subgraph Workers["Background Workers"]
        W1["normalize-worker"]
        W2["ocr-worker"]
        W3["llm-worker"]
        W4["postprocess-worker"]
        W5["outbound-worker"]
    end

    subgraph Infrastructure
        PG["PostgreSQL 16"]
        RD["Redis 7"]
        MN["MinIO"]
        PX["Phoenix (Tracing)"]
    end

    subgraph External
        LLM["Qwen3-VL<br/>(llama-server:8001)"]
    end

    UI -->|"POST file"| INGEST
    UI -->|"SSE connect"| STREAM
    UI -->|"PUT corrections"| REVIEW
    STATIC -->|"serves"| UI

    INGEST -->|"create job"| PG
    INGEST -->|"store original"| MN
    STREAM -->|"read state"| PG

    W1 -->|"claim job"| PG
    W1 -->|"read/write"| MN
    W1 -->|"enqueue next"| PG
    W2 -->|"claim job"| PG
    W2 -->|"read pages"| MN
    W3 -->|"claim job"| PG
    W3 -->|"call LLM"| LLM
    W4 -->|"claim job"| PG
    W5 -->|"claim job"| PG
    W5 -->|"write Excel"| MN

    W1 & W2 & W3 & W4 & W5 -->|"update progress"| PG
    API -->|"prompt cache"| RD
    W3 & W4 & W5 -.->|"traces"| PX
```

---

## 4. Extraction Pipeline

The pipeline processes each document through 5 sequential stages. Each stage is a separate worker process that claims jobs from Postgres using `FOR UPDATE SKIP LOCKED` for safe concurrency.

```mermaid
flowchart LR
    subgraph "Stage 1: Normalize"
        N1["Download original<br/>from MinIO"]
        N2["Render PDF → JPEG pages<br/>(PyMuPDF + Pillow)"]
        N3["Store pages in MinIO<br/>+ save metadata to DB"]
    end

    subgraph "Stage 2: OCR"
        O1["Download page images"]
        O2["PaddleOCR detect + recognize"]
        O3["Save words + boxes<br/>to extraction.ocr_data"]
    end

    subgraph "Stage 3: LLM"
        L1["Build system prompt<br/>(fields + rules + gold examples)"]
        L2["Extract page-by-page<br/>(Qwen3-VL multimodal)"]
        L3["Merge headers + line items"]
        L4["Save result + page_results"]
    end

    subgraph "Stage 4: Postprocess"
        P1["Match extracted values<br/>to OCR bounding boxes"]
        P2["Save field_locations"]
        P3["Set status = done"]
    end

    subgraph "Stage 5: Outbound"
        E1["Build normalized contract"]
        E2["Generate Excel workbook"]
        E3["Store Excel in MinIO"]
        E4["Record delivery status"]
    end

    N1 --> N2 --> N3 --> O1
    O1 --> O2 --> O3 --> L1
    L1 --> L2 --> L3 --> L4 --> P1
    P1 --> P2 --> P3 --> E1
    E1 --> E2 --> E3 --> E4
```

### Stage Details

| Stage | Worker | Input | Output | Failure Behavior |
|-------|--------|-------|--------|-----------------|
| normalize | `normalize-worker` | Original file (MinIO) | Rendered JPEG pages (MinIO) + page metadata (DB) | Retries up to 3× then marks failed |
| ocr | `ocr-worker` | Page images (MinIO) | `extraction.ocr_data` (words + bounding boxes) | Retries; OCR failure is non-fatal |
| llm | `llm-worker` | Page images + system prompt | `extraction.result` + `extraction.page_results` | Supports resume from partial state |
| postprocess | `postprocess-worker` | Result + OCR data | `extraction.field_locations` (field → bounding box map) | Non-fatal; extraction still marked done |
| outbound | `outbound-worker` | Effective result | Contract JSON + Excel file (MinIO) | Retries; failure recorded in `integration_deliveries` |

---

## 5. Real-Time Progress Delivery (SSE)

The frontend receives progress via **Server-Sent Events**, not polling. This means **1 HTTP connection per extraction** instead of 50+ polling requests.

```mermaid
sequenceDiagram
    participant Browser
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Worker

    Browser->>API: POST /ingest/ui (file + vendor_id)
    API->>DB: INSERT document, extraction, job
    API->>Browser: {job_id, extraction_id}

    Browser->>API: GET /jobs/{job_id}/stream
    Note over Browser,API: Single persistent SSE connection

    loop Every 1 second (server-side)
        API->>DB: SELECT job + extraction status
        alt State changed
            API-->>Browser: data: {"event":"progress", ...}
        end
    end

    Worker->>DB: Claim job → process → update progress
    Worker->>DB: Set extraction.status = "done"

    API->>DB: Detect terminal state
    API-->>Browser: data: {"event":"done", "extraction":{full result}}
    Note over Browser,API: SSE connection closed

    Browser->>Browser: Display result + Review button
```

### SSE Event Types

| Event | When | Payload Size |
|-------|------|-------------|
| `progress` | Worker updates job/extraction progress | **Lightweight** — strips result, ocr_data, page_results |
| `done` | Extraction completed successfully | **Full** — includes result, field_locations, total_pages |
| `failed` | Extraction or job failed | **Full** — includes error details |
| `partial` | Extraction cancelled mid-way | **Full** — includes partial result for resume |
| `error` | Job disappeared or stream error | Minimal error message |

---

## 6. Data Model

```mermaid
erDiagram
    vendors ||--o{ templates : "has one"
    vendors ||--o{ documents : "receives"
    vendors ||--o{ extractions : "processes"
    vendors ||--o{ gold_examples : "learns from"

    documents ||--o{ extractions : "triggers"
    extractions ||--o{ pages : "renders into"
    extractions ||--o{ jobs : "processed by"
    extractions ||--o{ review_events : "audited by"
    extractions ||--o{ integration_deliveries : "delivered via"
    extractions ||--o{ gold_examples : "creates"

    vendors {
        text id PK
        text name
        text status
        timestamptz created_at
    }

    templates {
        serial id PK
        text vendor_id FK
        text format_type
        jsonb header_fields
        jsonb line_item_fields
        text prompt_instructions
        jsonb extraction_rules
        text system_prompt
        text prompt_hash
    }

    documents {
        serial id PK
        text vendor_id FK
        text source_type
        text filename
        text mime_type
        bigint size_bytes
        text object_key
        text status
    }

    extractions {
        serial id PK
        int document_id FK
        text vendor_id FK
        int template_id FK
        text filename
        int total_pages
        jsonb result
        jsonb corrected_result
        jsonb field_locations
        jsonb ocr_data
        jsonb progress
        boolean cancel_requested
        text status
        text export_object_key
        int duration_ms
    }

    pages {
        serial id PK
        int extraction_id FK
        int page_number
        text object_key
        text mime_type
        int width
        int height
    }

    jobs {
        serial id PK
        int extraction_id FK
        int document_id FK
        text job_type
        text status
        jsonb payload
        jsonb progress
        int attempts
        int max_attempts
        text locked_by
        text error
    }

    review_events {
        serial id PK
        int extraction_id FK
        text actor
        text reason_code
        jsonb before_result
        jsonb after_result
        jsonb diff
    }

    gold_examples {
        serial id PK
        text vendor_id FK
        int extraction_id FK
        jsonb original_result
        jsonb corrected_result
        jsonb correction_diff
    }

    integration_deliveries {
        serial id PK
        int extraction_id FK
        text contract_type
        text target_type
        text status
        text object_key
    }
```

### Extraction Status Flow

```mermaid
stateDiagram-v2
    [*] --> queued: POST /ingest
    queued --> processing: Worker claims job
    processing --> done: All stages complete
    processing --> failed: Unrecoverable error
    processing --> cancelling: User requests cancel
    cancelling --> partial: Worker stops (has partial results)
    cancelling --> cancelled: Worker stops (no results)
    partial --> queued: POST /resume
    cancelled --> queued: POST /resume
    failed --> queued: POST /resume
    done --> [*]
```

### Result Precedence (Canonical Output)

```mermaid
flowchart TD
    A["Need canonical result?"]
    A -->|Check| B{"corrected_result exists?"}
    B -->|Yes| C["Use corrected_result<br/>(human-reviewed)"]
    B -->|No| D["Use result<br/>(machine-generated)"]

    C --> E["Contract generation"]
    D --> E
    C --> F["Excel export"]
    D --> F
    C --> G["Frontend display"]
    D --> G
```

---

## 7. Review & Learning Loop

The Review page provides a Rossum-style visual field mapping interface for human-in-the-loop validation.

```mermaid
flowchart TB
    subgraph "Review Page (3-column layout)"
        LEFT["Left Panel<br/>Editable field list<br/>+ Line items table"]
        CENTER["Center Panel<br/>PDF viewer with<br/>blue bounding boxes<br/>+ SVG connection lines"]
        RIGHT["Right Panel<br/>Live JSON output"]
    end

    subgraph "Correction Flow"
        EDIT["User edits field<br/>or draws selection box"]
        MATCH["OCR words matched<br/>in drawn rectangle"]
        PREVIEW["Accept/Reject preview"]
        SAVE["PUT /corrections"]
    end

    subgraph "Learning Loop"
        DIFF["Compute correction diff"]
        GOLD["Create gold_example"]
        CACHE["Invalidate prompt cache"]
        NEXT["Next extraction uses<br/>gold example as few-shot"]
    end

    LEFT --> EDIT
    CENTER --> EDIT
    EDIT --> MATCH --> PREVIEW --> SAVE
    SAVE --> DIFF --> GOLD --> CACHE --> NEXT
```

### Text Matching Strategies

The `text_matcher.py` engine maps extracted JSON values to OCR bounding boxes using 4 strategies (tried in order):

| Strategy | Method | Confidence |
|----------|--------|-----------|
| `exact` | OCR text == extracted value (case-insensitive) | High |
| `contains` | One string contains the other | High |
| `multi_span` | Combine consecutive OCR words to match | Medium |
| `fuzzy` | Levenshtein similarity ≥ 0.80 | Low |

---

## 8. API Reference

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |

### Vendors & Templates

| Method | Path | Description |
|--------|------|-------------|
| GET | `/vendors` | List all vendors |
| POST | `/vendors` | Create/update vendor |
| DELETE | `/vendors/{vendor_id}` | Delete vendor (cascades) |
| GET | `/vendors/{vendor_id}/template` | Get vendor template |
| POST | `/vendors/{vendor_id}/template` | Save/update template |
| GET | `/templates` | List all templates |

### Ingestion & Jobs

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ingest/{source_type}` | Submit document for extraction. Source types: `ui`, `rest`, `email`, `s3`, `sftp`, `partner` |
| GET | `/jobs/{job_id}` | One-shot job status check |
| GET | `/jobs/{job_id}/stream` | **SSE stream** — real-time progress via Server-Sent Events |
| POST | `/jobs/extractions/{id}/cancel` | Request cancellation |
| POST | `/jobs/extractions/{id}/resume` | Resume from partial/failed state |

### Extractions

| Method | Path | Description |
|--------|------|-------------|
| GET | `/extractions` | Global extraction history (limit 50) |
| GET | `/vendors/{vendor_id}/extractions` | Vendor-specific history |
| GET | `/extractions/{id}` | Full extraction record |
| GET | `/extractions/{id}/pages` | Rendered page images (base64) |
| GET | `/extractions/{id}/ocr` | PaddleOCR word data for click-to-select |

### Review & Export

| Method | Path | Description |
|--------|------|-------------|
| PUT | `/extractions/{id}/corrections` | Save human corrections → creates gold example → invalidates cache |
| GET | `/extractions/{id}/reviews` | Review event audit trail |
| GET | `/extractions/{id}/contract` | Normalized output contract |
| GET | `/extractions/{id}/export.xlsx` | Download Excel workbook |

### Preview

| Method | Path | Description |
|--------|------|-------------|
| POST | `/upload-preview` | Render PDF/image pages without extraction |

### Deprecated

| Method | Path | Status |
|--------|------|--------|
| POST | `/extract` | `410 Gone` — use `/ingest/ui` |
| POST | `/extract/cancel/{id}` | `410 Gone` — use `/jobs/extractions/{id}/cancel` |
| POST | `/extract/resume/{id}` | `410 Gone` — use `/jobs/extractions/{id}/resume` |

---

## 9. Frontend Pages

The SPA uses hash-based routing (`#/vendors`, `#/extract`, `#/review/123`, etc.):

| Route | Page | Purpose |
|-------|------|---------|
| `#/vendors` | Vendors | Create, delete, configure vendors |
| `#/template/{vendor_id}` | Template Editor | Define header/line-item fields, instructions, rules |
| `#/saved-templates` | Saved Templates | Browse all vendor templates |
| `#/extract` | Extraction | Upload file → auto/manual extract → view result |
| `#/history` | History | Browse all past extractions |
| `#/review/{id}` | Review | 3-column visual field mapping + correction |

### Extract Page Flow

```mermaid
flowchart TD
    A["Upload PDF/Image"] --> B["Select vendor + format"]
    B --> C{"Extract Fields<br/>or Auto Extract?"}
    C -->|"Extract Fields"| D["Auto-save template"]
    C -->|"Auto Extract"| E["Skip template"]
    D --> F["POST /ingest/ui"]
    E --> F
    F --> G["Open SSE stream"]
    G --> H["Show page progress<br/>(Page 2/5)"]
    H --> I{"Terminal event?"}
    I -->|done| J["Show result + Review button"]
    I -->|failed| K["Show error + Retry button"]
    I -->|partial| L["Show Resume button"]
```

---

## 10. Docker Compose Services

```mermaid
graph LR
    subgraph Infrastructure
        PG["postgres:16<br/>port 5432"]
        RD["redis:7<br/>port 6379"]
        MN["minio<br/>ports 9000/9001"]
        PX["phoenix<br/>port 6006"]
    end

    subgraph Application
        API["api<br/>port 8000<br/>(FastAPI + Frontend)"]
        NW["normalize-worker"]
        OW["ocr-worker"]
        LW["llm-worker"]
        PW["postprocess-worker"]
        OBW["outbound-worker"]
    end

    API --> PG & RD & MN
    NW & OW & LW & PW & OBW --> PG & MN
    LW -->|"HTTP"| LLM["LLM Server<br/>host.docker.internal:8001"]
    API -.-> PX
```

All worker containers use the same Docker image with different `command:` overrides:
```
api:               python -m uvicorn qwen_backend.main:app --host 0.0.0.0 --port 8000
normalize-worker:  python -m qwen_backend.worker normalize
ocr-worker:        python -m qwen_backend.worker ocr
llm-worker:        python -m qwen_backend.worker llm
postprocess-worker: python -m qwen_backend.worker postprocess
outbound-worker:   python -m qwen_backend.worker outbound
```

---

## 11. Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://augocr:augocr@localhost:5432/augocr` | Postgres connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `LLM_URL` | `http://localhost:8001/v1/chat/completions` | LLM API endpoint |
| `LLM_MODEL` | `qwen3vl` | Model name for LLM requests |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO server |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO credentials |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO credentials |
| `MINIO_SECURE` | `false` | Use HTTPS for MinIO |
| `WORKER_POLL_SECONDS` | `1.0` | Worker job claim interval |
| `RATE_LIMIT_PER_MINUTE` | `30` | API rate limit (per IP) |
| `MAX_UPLOAD_MB` | `50` | Max upload file size |
| `PHOENIX_ENABLED` | `true` | Enable Arize Phoenix tracing |
| `PHOENIX_COLLECTOR_ENDPOINT` | `http://localhost:4317` | Phoenix gRPC endpoint |

### MinIO Buckets

| Bucket | Contents |
|--------|----------|
| `augocr-documents` | Original uploaded files |
| `augocr-artifacts` | Rendered page images (JPEG) |
| `augocr-exports` | Generated Excel workbooks |

---

## 12. Local Development

### Prerequisites

- Docker Desktop with Compose
- LLM server running at `LLM_URL` (Qwen3-VL via llama-server or vLLM)

### Quick Start

```bash
# Start everything
docker compose up --build

# Or run backend locally (requires Postgres + Redis running)
cd qwen_backend
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Service URLs

| Service | URL |
|---------|-----|
| Application | http://localhost:8000 |
| MinIO Console | http://localhost:9001 (minioadmin/minioadmin) |
| Phoenix Tracing | http://localhost:6006 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### Test Extraction (cURL)

```bash
# 1. Submit document
curl -X POST "http://localhost:8000/ingest/ui" \
  -F "file=@invoice.pdf" \
  -F "vendor_id=ACME" \
  -F "format_type=single_po_multipage" \
  -F 'header_fields=["po_number","po_date","vendor_name"]' \
  -F 'line_item_fields=["item","qty","unit_price","amount"]'

# Response: {"job_id": 1, "extraction_id": 1, "status": "queued"}

# 2. Stream progress (SSE)
curl -N "http://localhost:8000/jobs/1/stream"

# 3. Fetch result
curl "http://localhost:8000/extractions/1"

# 4. Download Excel
curl -OJ "http://localhost:8000/extractions/1/export.xlsx"
```

---

## 13. Source Files

### Backend (`qwen_backend/`)

| File | Lines | Purpose |
|------|-------|---------|
| `main.py` | ~1290 | FastAPI app, all REST + SSE endpoints, lifespan management |
| `worker.py` | ~360 | Background job processor — 5 stages |
| `db.py` | ~1250 | All SQL queries, schema bootstrap, migrations |
| `extractor.py` | ~530 | LLM prompt building, page extraction, result merging |
| `processor.py` | ~170 | PDF rendering (PyMuPDF) + image normalization (Pillow) |
| `ocr_runner.py` | ~180 | PaddleOCR wrapper for page images |
| `text_matcher.py` | ~430 | 4-strategy field-to-bounding-box matching engine |
| `object_store.py` | ~100 | MinIO client with local filesystem fallback |
| `contracts.py` | ~40 | Normalized output contract builder |
| `excel_exporter.py` | ~50 | Openpyxl workbook generator |
| `cache.py` | ~65 | Redis prompt + extraction caching |
| `models.py` | ~170 | Pydantic request/response models |
| `phoenix_tracing.py` | ~670 | Full pipeline OpenTelemetry instrumentation |
| `logging_config.py` | ~50 | Central logging configuration |

### Frontend (`qwen_frontend/`)

| File | Purpose |
|------|---------|
| `index.html` | Shell HTML with theme flash prevention |
| `app.js` | Full SPA — router, 6 pages, SSE streaming, review page |
| `styles.css` | Dark/light theme, review layout, mapping overlays |

### Infrastructure

| File | Purpose |
|------|---------|
| `docker-compose.yml` | 10-service orchestration with health checks |
| `Dockerfile` | Python 3.11 slim with system deps for PaddleOCR |
| `.env.example` | Reference environment configuration |

---

## 14. Known Limitations

- `email`, `s3`, `sftp`, `partner` are source-type labels on the same upload API — not background connector daemons
- Outbound delivery is Excel export only — no ERP-specific API adapters yet
- No zombie job cleanup (jobs stuck in `running` if worker crashes)
- No MinIO garbage collection for orphaned blobs
- Frontend uses `innerHTML` in some places — should be reviewed for XSS hardening
- `ocr_pipeline.py` is a dead file from an earlier iteration — not used by the current pipeline
