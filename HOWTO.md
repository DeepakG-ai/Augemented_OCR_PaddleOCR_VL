# How to Run — Augmented OCR

Step-by-step guide to get the full system running locally.

---

## Prerequisites

### Required Software

| Software | Version | Purpose |
|----------|---------|---------|
| **Docker Desktop** | 4.x+ | Runs all services (Postgres, Redis, MinIO, workers) |
| **LLM Server** | — | Qwen3-VL via llama-server, vLLM, or any OpenAI-compatible endpoint |

### Hardware

- **GPU** recommended for the LLM server (Qwen3-VL requires ~8GB VRAM for Q4 quantization)
- **RAM**: 8GB minimum for Docker services
- **Disk**: ~5GB for Docker images + model weights

---

## Quick Start (Docker Compose)

### 1. Start the LLM Server

The extraction pipeline calls an OpenAI-compatible LLM endpoint. Start it **before** Docker Compose:

```bash
# Example using llama-server with Qwen3-VL (adjust path to your model)
llama-server \
  --model /path/to/qwen3-vl-q4.gguf \
  --port 8001 \
  --host 0.0.0.0 \
  --n-gpu-layers 99
```

Or if using vLLM:
```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-VL-7B-Instruct \
  --port 8001
```

The system expects the LLM at `http://localhost:8001/v1/chat/completions` by default.

### 2. Start All Services

From the **project root directory**:

```bash
docker compose up --build
```

This starts **10 containers**:

| Container | Purpose | Port |
|-----------|---------|------|
| `postgres` | Database | 5432 |
| `redis` | Prompt/result cache | 6379 |
| `minio` | Object storage (files, pages, exports) | 9000 (API), 9001 (Console) |
| `api` | FastAPI backend + serves frontend | **8000** |
| `normalize-worker` | PDF rendering / image normalization | — |
| `ocr-worker` | PaddleOCR text detection | — |
| `llm-worker` | Qwen3-VL multimodal extraction | — |
| `postprocess-worker` | Field-to-bounding-box mapping | — |
| `outbound-worker` | Excel export generation | — |
| `phoenix` | LLM observability tracing | 6006, 4317 |

### 3. Wait for Health Checks

Watch the logs until you see all services are healthy:

```bash
docker compose ps
```

All services should show `(healthy)` status. The `api` container will log:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 4. Open the Application

```
http://localhost:8000
```

---

## First Extraction (Step by Step)

### Step 1: Create a Vendor

1. Open http://localhost:8000
2. You'll land on the **Vendors** page
3. Click **"+ Add New Vendor"**
4. Enter a **Vendor ID** (e.g., `ACME`) and **Name** (e.g., `ACME Corporation`)
5. Click **Save**

### Step 2: Configure a Template

1. Click the **"⚙ Template"** button on your vendor card
2. Select the document **Format Type**:
   - `single_po_multipage` — One purchase order spanning multiple pages
   - `po_per_page` — Separate PO per page
   - `single_page` — Everything on one page
3. Add **Header Fields** (e.g., `po_number`, `po_date`, `vendor_name`, `bill_to`, `ship_to`)
4. Add **Line Item Fields** (e.g., `item`, `description`, `qty`, `unit_price`, `amount`)
5. Optionally add **instructions** and **extraction rules**
6. Click **Save Template**

### Step 3: Extract a Document

1. Navigate to the **Extraction** tab
2. Select your vendor from the dropdown
3. Upload a PDF or image file
4. Click **"Extract Fields"** (uses your template) or **"Auto Extract"** (auto-detects fields)
5. Watch the real-time progress: `Page 1/5`, `Page 2/5`, etc.
6. When done, the extracted JSON appears in the result panel
7. Click **"Open Review"** to validate the field mapping

### Step 4: Review & Correct

1. The **Review page** shows three panels:
   - **Left**: Editable field values
   - **Center**: PDF with blue bounding boxes and connection lines
   - **Right**: Live JSON output
2. Click any field in the left panel to highlight its bounding box on the PDF
3. To correct a field:
   - Click the **✎** button next to a field
   - Draw a rectangle on the PDF over the correct text
   - Accept or reject the selection
4. Click **"Confirm"** to save corrections
5. Corrections automatically create a **gold example** that improves future extractions for this vendor

### Step 5: Download Export

From the History page or via API:
```bash
curl -OJ "http://localhost:8000/extractions/1/export.xlsx"
```

---

## Alternative: Run Without Docker

If you prefer running the backend directly:

### 1. Install Dependencies

```bash
# Create a virtual environment
cd backend
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

pip install -r requirements.txt
```

### 2. Start Infrastructure Services

You still need Postgres and Redis running. Use Docker for just those:

```bash
docker run -d --name augocr-postgres \
  -e POSTGRES_DB=augocr -e POSTGRES_USER=augocr -e POSTGRES_PASSWORD=augocr \
  -p 5432:5432 postgres:16-alpine

docker run -d --name augocr-redis \
  -p 6379:6379 redis:7-alpine
```

MinIO is optional — the system falls back to a local `.local_object_store/` directory.

### 3. Configure Environment

Edit `backend/.env`:

```env
DATABASE_URL=postgresql://augocr:augocr@localhost:5432/augocr
REDIS_URL=redis://localhost:6379/0
LLM_URL=http://localhost:8001/v1/chat/completions
LLM_MODEL=qwen3vl
```

### 4. Start the API Server

```bash
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Start Workers (each in a separate terminal)

```bash
# Terminal 1
python -m backend.worker --stage normalize

# Terminal 2
python -m backend.worker --stage ocr

# Terminal 3
python -m backend.worker --stage llm

# Terminal 4
python -m backend.worker --stage postprocess

# Terminal 5
python -m backend.worker --stage outbound
```

---

## API Usage (cURL)

### Submit a Document

```bash
curl -X POST "http://localhost:8000/ingest/ui" \
  -F "file=@invoice.pdf" \
  -F "vendor_id=ACME" \
  -F "format_type=single_po_multipage" \
  -F 'header_fields=["po_number","po_date","vendor_name"]' \
  -F 'line_item_fields=["item","qty","unit_price","amount"]'
```

Response:
```json
{"job_id": 1, "extraction_id": 1, "status": "queued"}
```

### Stream Progress (SSE)

```bash
curl -N "http://localhost:8000/jobs/1/stream"
```

Events arrive as Server-Sent Events:
```
data: {"event":"progress","job":{...},"extraction":{"status":"processing","progress":{"page":2,"total_pages":5}}}
data: {"event":"progress","job":{...},"extraction":{"status":"processing","progress":{"page":3,"total_pages":5}}}
data: {"event":"done","job":{...},"extraction":{"result":{...},"field_locations":{...}}}
```

### Fetch Result

```bash
curl "http://localhost:8000/extractions/1"
```

### Download Excel

```bash
curl -OJ "http://localhost:8000/extractions/1/export.xlsx"
```

### Get Normalized Contract

```bash
curl "http://localhost:8000/extractions/1/contract"
```

---

## Service URLs

| Service | URL | Credentials |
|---------|-----|-------------|
| **Application** | http://localhost:8000 | — |
| **MinIO Console** | http://localhost:9001 | `minioadmin` / `minioadmin` |
| **Phoenix Tracing** | http://localhost:6006 | — |
| **PostgreSQL** | `localhost:5432` | `augocr` / `augocr` |
| **Redis** | `localhost:6379` | — |

---

## Troubleshooting

### Container won't start

```bash
# Check logs for a specific container
docker compose logs api
docker compose logs llm-worker

# Rebuild from scratch
docker compose down -v   # Warning: deletes all data
docker compose up --build
```

### LLM worker fails

The LLM worker needs to reach your LLM server. Inside Docker, it connects via `host.docker.internal`:

```
LLM_URL=http://host.docker.internal:8001/v1/chat/completions
```

Verify your LLM server is running and accessible:
```bash
curl http://localhost:8001/v1/models
```

### OCR worker crashes with CUDA errors

PaddleOCR tries to use GPU by default. In Docker (CPU-only), it falls back to CPU. If you see CUDA errors, ensure `paddlepaddle` (not `paddlepaddle-gpu`) is installed in requirements.txt.

### MinIO connection refused

If MinIO isn't running, the system falls back to local file storage at `.local_object_store/`. This is fine for development. For production, ensure MinIO is healthy:

```bash
docker compose ps minio
```

### Excel export returns 404

The Excel export is generated asynchronously by the `outbound-worker` after extraction completes. Wait a few seconds after extraction finishes, then retry. Check the outbound worker logs:

```bash
docker compose logs outbound-worker
```

### Database schema issues

The database schema is auto-created and migrated on startup. To reset completely:

```bash
docker compose down
docker volume rm augemented_ocr_paddleocr_vl_pgdata
docker compose up --build
```


Email: admin@augocr.com
Password: admin

User: augocr
Password: augocr
Database: augocr




# All tests (headless)
npm run test:e2e

# With visible browser
npm run test:e2e:headed

# Interactive UI
npm run test:e2e:ui
