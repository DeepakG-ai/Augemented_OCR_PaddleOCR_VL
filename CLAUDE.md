# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Python executable

Always use the project venv, never a global `python`. The venv lives at `.venv/` in the project root.

```bash
.venv/Scripts/python.exe          # Python interpreter
.venv/Scripts/uvicorn.exe         # uvicorn shortcut (or use python -m uvicorn)
```

If `pytest` is not yet installed in the venv:
```bash
.venv/Scripts/python.exe -m pip install pytest
```

---

## Running the System

### Full stack (Docker — primary workflow)

```bash
# 1. Start the LLM server first (outside Docker, on the host)
llama-server --model /path/to/qwen3-vl-q4.gguf --port 8001 --host 0.0.0.0 --n-gpu-layers 99

# 2. Start all services
docker compose up --build

# App UI:    http://localhost:8000
# MinIO UI:  http://localhost:9001  (minioadmin / minioadmin)
# MLflow:    http://localhost:5000
```

### Without Docker (dev iteration)

```bash
cd backend
.venv/Scripts/python.exe -m pip install -r requirements.txt

# API server (auto-reloads on file change)
.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Each worker in a separate terminal (run from project root)
.venv/Scripts/python.exe -m backend.worker --stage normalize
.venv/Scripts/python.exe -m backend.worker --stage ocr
.venv/Scripts/python.exe -m backend.worker --stage llm
.venv/Scripts/python.exe -m backend.worker --stage postprocess
.venv/Scripts/python.exe -m backend.worker --stage outbound
```

Required env in `backend/.env`:
```
DATABASE_URL=postgresql://augocr:augocr@localhost:5432/augocr
REDIS_URL=redis://localhost:6379/0
LLM_URL=http://localhost:8001/v1/chat/completions
LLM_MODEL=qwen3vl
```

MinIO is optional — falls back to `.local_object_store/` on disk when not available.

### Running tests

```bash
# All tests
.venv/Scripts/python.exe -m pytest tests/

# Single test file
.venv/Scripts/python.exe -m pytest tests/test_vendor_detector.py -v

# Single test case
.venv/Scripts/python.exe -m pytest tests/test_vendor_detector.py::VendorDetectorTests::test_exact_alias_match_runs_before_fuzzy_match -v
```

Tests use `unittest.IsolatedAsyncioTestCase` and mock all DB/store/LLM calls — no live services needed.

---

## Architecture

### Five-stage durable pipeline

A document upload creates a chain of jobs in the `jobs` table, processed by five independent worker processes. Each stage claims a job, does its work, then enqueues the next stage.

```
POST /ingest/ui
  → normalize-worker   — renders PDF pages, classifies digital vs scanned, stores page images in MinIO
  → ocr-worker         — PaddleOCR on scanned pages only; digital pages use pypdfium2 word geometry already stored in the pages table
  → llm-worker         — calls Qwen3-VL for semantic extraction, streams page-by-page progress via SSE
  → postprocess-worker — builds field_locations (bbox mapping), applies spatial memory overrides, marks extraction "done"
  → outbound-worker    — generates Excel/CSV exports via contracts.py, stores to MinIO
```

SSE progress is served from `GET /jobs/{job_id}/stream`. The frontend holds a persistent connection to this stream instead of polling.

### Key backend modules

| File | Role |
|------|------|
| `db.py` | All SQL — asyncpg pool, schema bootstrap, idempotent migrations inline. No ORM. |
| `worker.py` | The five stage handlers + `run_worker()` poll loop with stale-job recovery |
| `main.py` | FastAPI app — REST endpoints, SSE stream, static frontend mount |
| `geometry.py` | Unified page geometry: `pypdfium2` (digital) or PaddleOCR words in the same `{page_number, source, words[{text, box, score}]}` shape |
| `spatial_memory.py` | Save corrected regions (`save_from_corrections`) and apply them to future extractions (`apply_to_extraction`). Stores WHERE a field is, never the old value. |
| `vendor_detector.py` | Matches page-1 words against `vendor_aliases` in DB using exact + fuzzy scoring (rapidfuzz). Unknown vendors return `None` → 409 error. |
| `extractor.py` | System prompt builder, gold-example injection, per-page LLM call loop, result merger |
| `layout_key.py` | Spatial memory grouping key: `vendor_id:template_id` |
| `object_store.py` | MinIO abstraction with local-filesystem fallback |
| `cache.py` | Redis async cache for system prompts and extraction results |
| `qwen_bbox_parser.py` | Parses Qwen v3 anchor/header boxes into `field_locations` |
| `contracts.py` | Normalizes extraction results into a canonical purchase-order contract for export |
| `logging_config.py` | Structured JSON event logging (`plog.event(...)`, `plog.timed(...)`) |

### Database tables (key relationships)

```
vendors → templates (one per vendor)
vendors → vendor_aliases (pattern matching for detection)
documents → extractions (one-to-many)
extractions → pages (one per rendered page)
extractions → jobs (pipeline stage queue)
extractions → review_events + gold_examples + field_locations
spatial_memory (keyed by vendor_id + layout_key + field_key + page_number)
```

Schema is auto-created and migrated in `db.init()` at startup. Migrations are idempotent `DO $$ IF NOT EXISTS ... $$` blocks — safe to run on every startup.

### Frontend

Single-page vanilla JS app (`frontend/app.js`) served as static files by the FastAPI process. Five pages rendered into `#appRoot`: Vendors, Templates, Extraction, History, Review.

- **Styles**: `frontend/styles.css` — Terminal UI theme. `JetBrains Mono` is the primary body font; `Rajdhani` is the display font for headers. Dark theme is the default (`:root`); light theme activates via `[data-theme="light"]` on `<html>`. Do not rewrite this file.
- **Review page**: three-panel layout (fields / PDF viewer / JSON). Users draw bounding boxes to correct fields; saving corrections also writes spatial memory to the DB.

### Extraction result formats

Qwen returns two shapes depending on `format_type`:

- `single_po_multipage` / `single_page` → `result` is a `dict` with header fields + `line_items[]`
- `po_per_page` → `result` is a `list` of dicts (one per page/PO)

`page_results` carries per-page raw output including Qwen v3 `boxes` (anchor boxes). `is_v3` detection in postprocess checks for `"boxes"` in any `page_result`.

### Spatial memory rules (from AGENTS.md)

- Store **where** a field is (normalized box + page + layout key). Never store the old corrected value as reusable truth.
- On reuse: read the current document's text inside the saved region from current OCR/pypdfium geometry.
- Header fields only in phase 1. Line-item row boxes are not persisted as reusable memory.
- Layout key is `vendor_id:template_id` — prevents cross-layout bleed for vendors with multiple document formats.

### Job queue mechanics

- `claim_job` only picks up `queued` status. Jobs left in `running` by a crashed worker are recovered by `db.recover_stale_jobs()`, called on worker startup (5-min threshold) and every 60 s in the poll loop (10-min threshold).
- `ensure_job` is idempotent — checks `has_active_job` before enqueuing to prevent duplicate pipeline stages.
- The unique index `jobs_active_idx` blocks duplicate `(extraction_id, job_type)` pairs in `queued`/`running` state.

### Route ordering constraint

`GET /extractions/count` must stay registered **before** `GET /extractions/{extraction_id: int}` in `main.py`. Starlette matches routes in registration order; placing the static path second causes "count" to hit the int validator and return 422.

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://augocr:augocr@localhost:5432/augocr` | asyncpg connection |
| `REDIS_URL` | `redis://localhost:6379/0` | prompt + result cache |
| `LLM_URL` | `http://localhost:8001/v1/chat/completions` | Qwen3-VL endpoint |
| `LLM_MODEL` | `qwen3vl` | model name sent in API requests |
| `MINIO_ENDPOINT` | `localhost:9000` | object storage |
| `WORKER_POLL_SECONDS` | `1.0` | worker poll interval |
| `MAX_UPLOAD_MB` | `50` | upload size guard |
| `RATE_LIMIT_PER_MINUTE` | `30` | slowapi default limit |
