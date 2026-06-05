# CLAUDE.md

Guidance for Claude Code working in this repo. Keep it accurate — if you change how the
system works, update this file.

**What this is:** a multi-tenant web app that extracts data (header fields + line items) from
purchase-order / invoice PDFs using PaddleOCR + a Qwen3-VL vision model. Users upload a PDF, the
system detects the vendor, runs a 4-stage pipeline, and returns structured JSON. Users can correct
results by drawing boxes; the system remembers where each field lives for next time.

---

## Python

Always use the project venv. Never use a global `python`. The venv is at `.venv/`.

```bash
.venv/Scripts/python.exe          # interpreter
.venv/Scripts/python.exe -m pytest tests/        # run tests
.venv/Scripts/python.exe -m pip install pytest   # if pytest is missing
```

---

## Running the system

### Docker (primary)

```bash
# 1. Start the LLM server on the host first (not in Docker)
llama-server --model /path/to/qwen3-vl-q4.gguf --port 8001 --host 0.0.0.0 --n-gpu-layers 99

# 2. Start everything else
docker compose up --build

# App:    http://localhost:8000
# MinIO:  http://localhost:9001  (minioadmin / minioadmin)
# MLflow: http://localhost:5000
```

### Without Docker (dev)

```bash
# API server (auto-reloads)
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Workers — each in its own terminal, run from the project root.
# ocr and llm run in PARALLEL, so you must run both.
.venv/Scripts/python.exe -m backend.worker --stage normalize
.venv/Scripts/python.exe -m backend.worker --stage ocr
.venv/Scripts/python.exe -m backend.worker --stage llm
.venv/Scripts/python.exe -m backend.worker --stage postprocess
```

Minimum `backend/.env`:

```
DATABASE_URL=postgresql://augocr:augocr@localhost:5432/augocr
LLM_URL=http://localhost:8001/v1/chat/completions
LLM_MODEL=qwen3vl
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=use-a-real-password      # must NOT start with "CHANGE_ME"
```

- MinIO is optional — it falls back to `.local_object_store/` on disk.
- Redis is **not** used. There is no `cache.py`. Prompts and results are rebuilt from the DB
  every run.

### Tests

```bash
.venv/Scripts/python.exe -m pytest tests/                       # all
.venv/Scripts/python.exe -m pytest tests/test_vendor_detector.py -v   # one file
```

Tests use `unittest.IsolatedAsyncioTestCase` and mock all DB/store/LLM calls — no live services
needed. Tests must run with `MLFLOW_ENABLED=false` — never emit real traces during tests.

---

## How the pipeline works

A PDF upload creates jobs in the `jobs` table. Independent worker processes claim jobs, do the
work, then queue the next stage. There are **4 stages** (no export/outbound stage — exports are
served on demand from `GET /extractions/{id}/contract`).

```
POST /ingest/{source_type}        source_type = ui | rest | email | s3 | sftp | partner
   │
   ├─ The request does the MINIMUM: validate PDF → reserve quota → store → enqueue
   │  `normalize` → return 201 + job_id fast (<200ms). It does NOT detect the vendor.
   │  If the caller pre-selects a vendor_id, the template is validated here; otherwise
   │  vendor_id is left NULL and the worker detects it.
   │
   ▼
1. normalize   renders all pages, classifies each page digital vs scanned, saves page images +
               word geometry. If vendor_id is NULL, DETECTS THE VENDOR here (read page-1 text:
               digital, else PaddleOCR → match aliases). Unknown vendor → extraction status
               `failed`, error `unknown_vendor`, quota released, pipeline stops (no ocr/llm).
               On a match it persists vendor/template/fields, then queues BOTH ocr and llm
               (they run in parallel).
       ├──▶ 2a. ocr    PaddleOCR on scanned pages only. Digital pages reuse pypdfium2 geometry.
       │               Saves unified word boxes to ocr_data.
       └──▶ 2b. llm    Qwen3-VL, one page at a time. Page 1 also asks for bounding boxes
                       (saved to qwen_layout_boxes). Streams per-page progress over SSE.
                       Does NOT mark the extraction "done" — postprocess does.
   ▼
3. postprocess  runs after BOTH ocr and llm finish. Maps each field to a word box to build
               field_locations, applies spatial-memory overrides, applies ERP field mapping,
               sets status = done, releases the quota reservation.
```

Whichever of `ocr`/`llm` finishes second triggers `postprocess`. A unique DB index
(`jobs_active_idx`) makes sure it is only queued once.

**Progress (SSE):** `GET /jobs/{job_id}/stream` is a long-lived connection. The server checks the
DB about once per second and pushes changes. The browser never polls.

---

## Backend modules

> ⚠️ `main.py` (~4,700 lines, ~90 routes) and `db.py` (~4,800 lines, ~180 functions) are large
> monoliths. When adding a route or query, prefer pulling out an `APIRouter` or a small db module
> instead of making these files bigger.

| File | What it does |
|------|------|
| `main.py` | FastAPI app: all REST routes, SSE streams, auth dependencies, upload-size limit, serves the frontend. |
| `db.py` | All SQL (asyncpg, no ORM). Creates and migrates the schema on startup in `_init_db`. |
| `worker.py` | The 4 stage handlers + the poll loop (`run_worker`) with stale-job recovery and DB reconnect. |
| `config.py` | All env vars / constants live here. |
| `models.py` | Pydantic request/response models. |
| `auth.py` | JWT login (HS256, 8h, bcrypt) + API keys. `get_current_user*`, `require_admin`, and `assert_*_access` ownership checks. |
| `processor.py` | Renders PDF pages to images (runs in a thread pool). Page counting, 32px alignment. |
| `pdf_extractor.py` | Reads digital text + word boxes from PDFs (pypdfium2). |
| `ocr_runner.py` | PaddleOCR wrapper (thread pool). |
| `geometry.py` | Puts digital and OCR words into one shape: `{page_number, source, words[{text, box, score}]}`. |
| `vendor_detector.py` | Matches page-1 words to `vendor_aliases` (exact, then fuzzy/rapidfuzz). Unknown → `None`. Called from the `normalize` worker (auto-detect) and the standalone `/detect-vendor` endpoint. Always scoped to a user. |
| `extractor.py` | Builds the LLM prompt, calls the model per page, merges page results. |
| `qwen_layout_apply.py` | Builds `field_locations` from the learned page-1 boxes (`qwen_layout_boxes`) + current OCR words. |
| `spatial_memory.py` | `save_from_corrections` (save where a field is) and `apply_to_extraction` (reuse it). |
| `layout_key.py` | The spatial-memory grouping key: `vendor_id:template_id`. |
| `field_mapper.py` | Optional ERP mapping — renames extracted fields to a vendor's canonical schema. |
| `contracts.py` | Normalizes a result into the canonical purchase-order JSON (`/extractions/{id}/contract`). |
| `object_store.py` | MinIO storage, with local-disk fallback. |
| `page_logger.py` | Per-extraction human-readable run log + quota/usage alerts. |
| `logging_config.py` | Structured JSON logging (`plog.event(...)`, `plog.timed(...)`). |
| `mlflow_tracing.py` | MLflow tracing helpers. No-op when `MLFLOW_ENABLED=false`. |
| `syteline_connector.py` | Stub for a Syteline ERP connector. |

---

## Database

This is multi-tenant. **Every piece of data belongs to a user through `vendors.user_id`.**
Extractions and jobs have no `user_id` — you find the owner by walking back to the vendor. One
client can never see another client's data.

```
users
  ├─ vendors ─ templates (1 per vendor)
  │         ├─ vendor_aliases   (text patterns used to detect the vendor)
  │         └─ field_mappings   (optional ERP rename rules) ─ schemas
  ├─ subscriptions (one active per user) ─ topups
  ├─ topup_requests   (user asks for more pages, admin approves)
  ├─ api_keys
  └─ llm_usage        (per-page token + billing rows)

documents ─ extractions ─┬─ pages, ocr_data, qwen_layout_boxes
                         ├─ jobs              (pipeline stage queue)
                         └─ review_events, gold_examples, field_locations

spatial_memory      keyed by vendor_id + layout_key + field_key + page_number
idempotency_claims  backs the Idempotency-Key header on the API upload path
```

The schema is created and migrated automatically in `db.init()` at startup. Migrations are
idempotent (safe to run every time).

**Source of truth for behavior:** `docs/implemented/` (kept up to date). Read it for full details
on auth, billing/quota, ERP mapping, spatial memory, and gold corrections.

---

## Frontend

Plain vanilla JavaScript, served as static files by FastAPI. No build step, no framework.

- There is **no `app.js`**. `index.html` loads 10 scripts. `core.js` is the router and shared
  layer (fetch wrapper, auth, escaping); the rest are one per screen: `login`, `vendors`,
  `extract`, `history`, `review`, `dashboard`, `admin`, `apikeys`, `mapper`.
- Auth: the JWT and user object are stored in `localStorage` and read by `core.js`. Admin UI is
  hidden client-side, so **the server must re-check admin rights on every admin endpoint.**
- Styles: `frontend/styles.css` — terminal theme, `JetBrains Mono` body font, `Rajdhani` headers,
  dark by default, light via `[data-theme="light"]` on `<html>`. **Do not rewrite this file.**
- Review screen: 3 panels (fields / PDF / JSON). Drawing a box to fix a field also saves spatial
  memory to the DB.

---

## Extraction result shapes

The LLM result depends on `format_type`:

- `single_po_multipage` / `single_page` → `result` is a **dict** (header fields + `line_items[]`).
- `po_per_page` → `result` is a **list** of dicts (one per page).

Page 1's response also returns a `boxes` dict (label and column-header boxes on a 0–1000 grid).
The llm worker saves these to `qwen_layout_boxes`; postprocess maps them onto the current OCR words
(`qwen_layout_apply`) to build `field_locations`.

---

## Important rules and gotchas

- **Spatial memory stores WHERE a field is, never the old value.** On reuse it reads the current
  document's text inside the saved region. Header fields only (phase 1); line-item row boxes are
  not saved as reusable memory. Layout key `vendor_id:template_id` keeps different document formats
  from the same vendor separate.
- **Job recovery:** `claim_job` only picks `queued` jobs. Jobs stuck in `running` (crashed worker)
  are reset by `recover_stale_jobs` — on worker start (5 min) and every 60s in the loop (10 min).
  `ensure_job` is idempotent.
- **Route order:** `GET /extractions/count` must stay registered **before**
  `GET /extractions/{extraction_id:int}` in `main.py`. Starlette matches in order; otherwise
  "count" hits the int route and returns 422.
- **Quota:** pages are reserved at upload (`reserve_quota`, row-locked) and released by postprocess
  on success, or on cancel/failure. Don't double-count.
- **Dead code to be aware of (don't build on it):** `extractor.get_or_build_system_prompt` and
  `compute_prompt_hash` are not called by anything — the worker builds the prompt fresh each run.
- **Known rough edges:** OCR is currently all-or-nothing (one failed scanned page fails the whole
  OCR job).

---

## Environment variables

All defined in `backend/config.py`. Most-used:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://augocr:augocr@localhost:5432/augocr` | Postgres connection |
| `LLM_URL` | `http://localhost:8001/v1/chat/completions` | Qwen3-VL endpoint |
| `LLM_MODEL` | `qwen3vl` | model name sent to the LLM |
| `LLM_TEMPERATURE` / `LLM_TOP_P` / `LLM_PRESENCE_PENALTY` | `0.7` / `0.8` / `1.5` | Qwen3-VL sampling |
| `LLM_PAGE_BATCH_SIZE` | `1` | pages 2+ processed per batch; match `--parallel N` on llama-server |
| `LLM_TIMEOUT` | `300.0` | per-call LLM timeout (seconds) |
| `WORKER_POLL_SECONDS` | `1.0` | worker poll interval |
| `MAX_UPLOAD_MB` | `50` | upload size limit |
| `MAX_DOCUMENT_PAGES` | `100` | hard per-PDF page cap |
| `RATE_LIMIT_PER_MINUTE` | `30` | API rate limit |
| `DEFAULT_SUBSCRIPTION_LIMIT` | `0` | default page quota for new users |
| `SUBSCRIPTION_WARNING_THRESHOLD` | `0.9` | usage % that triggers a near-limit warning |
| `MINIO_ENDPOINT` | `localhost:9000` | object storage (falls back to local disk) |
| `MLFLOW_ENABLED` | `true` | set `false` in tests |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | — | bootstrap admin on first startup |
