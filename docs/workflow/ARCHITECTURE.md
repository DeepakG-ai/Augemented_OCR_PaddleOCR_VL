# Augmented OCR — Production Architecture & Engineering Reference

> **Audience:** engineers, the architect, and the product owner.
> **Purpose:** the single document that explains *what is built*, *how it connects*,
> *how it scales*, *where it breaks*, and *what is tested*. Read alongside
> [architecture.html](architecture.html) for the visual diagrams (rendered Mermaid).
>
> Status as of 2026-05-15. This is descriptive of the current `feat/qwen-two-agent-bbox-fields`
> branch, with forward-looking sections clearly marked **PLANNED**.

---

## 1. Product in one paragraph

Augmented OCR is a multi-tenant SaaS that turns vendor PDFs (invoices / purchase
orders) into structured JSON. An **admin** provisions clients and quotas. Each
**client** is fully isolated: client A can never see client B's data. A client
owns N **vendors**; a vendor is one distinct PDF *format*. A document is uploaded
(manually or by a folder scheduler), the system reads page 1 to **detect the
vendor**, renders and classifies every page, sends page images to **Qwen3-VL**
for extraction, maps every value to a bounding box, and lets the client correct
mistakes in a review UI. Every page, every token, and every millisecond of LLM
time is metered per client for billing.

---

## 2. The two invariants (memorize these)

Everything in this system is a consequence of two rules. If a change violates
either, it is a bug regardless of what else it does.

### Invariant 1 — Tenant isolation chains through `vendors.user_id`

```
users.id ──owns──> vendors.user_id
                     ├── templates.vendor_id
                     ├── vendor_aliases.vendor_id
                     ├── spatial_memory.vendor_id
                     ├── qwen_layout_boxes.vendor_id
                     ├── gold_examples.vendor_id
                     └── extractions.vendor_id ── documents
                                               ├── pages
                                               ├── jobs
                                               ├── llm_usage
                                               └── review_events
```

There is **no `user_id` on extractions, jobs, or llm_usage**. Ownership is always
resolved by walking back to a vendor. Every route is exactly one of:

| Route class | Enforcement | Examples |
|---|---|---|
| List | `WHERE vendor.user_id = :me` | `/vendors`, `/extractions`, `/templates` |
| Item | `assert_*_access()` ownership walk | `/extractions/{id}`, `/jobs/{id}` |
| Admin | `require_admin` (role bypass) | `/admin/users`, `/admin/usage` |
| Public | none | `/health` |

`auth.py` — `assert_vendor_access` → `assert_extraction_access` →
`assert_job_access` → `assert_alias_access`. Admin role short-circuits every
assert (`_is_admin`). This is the **entire** isolation contract.

### Invariant 2 — Page 1 is a hard sequential dependency

Page 1's Qwen call produces the bounding-box layout (`qwen_layout_boxes`) that
postprocess and every later page depend on. Pages within a document are processed
**strictly in order** (`extractor.py`, `PARALLEL_BATCH = 1`). This must never be
parallelized — not for speed, not for anything. It is also physically impossible
on the 8 GB GPU (two page contexts won't fit).

---

## 3. Repository structure (production layout)

```
Augmented_OCR_PaddleOCR_VL/
├── backend/                     # FastAPI app + 4 worker stages (asyncpg, no ORM)
│   ├── main.py                  # REST + SSE + static mount  (≈2900 LOC — the API surface)
│   ├── db.py                    # ALL SQL: pool, schema bootstrap, idempotent migrations
│   ├── worker.py                # 4 stage handlers + run_worker() poll loop
│   ├── extractor.py             # system-prompt builder, per-page Qwen loop, merge
│   ├── scheduler.py             # APScheduler wrapper — per-user folder ingest cron
│   ├── folder_watcher.py        # (manual/legacy) directory poll ingest
│   ├── vendor_detector.py       # page-1 text → vendor (exact alias + rapidfuzz)
│   ├── geometry.py              # unified word-box schema (pypdfium2 | PaddleOCR)
│   ├── ocr_runner.py            # PaddleOCR on scanned pages only
│   ├── processor.py             # PDF/image → page images (32px-aligned)
│   ├── pdf_extractor.py         # pypdfium2 digital text + word geometry
│   ├── qwen_layout_apply.py     # apply learned page-1 boxes → field_locations
│   ├── spatial_memory.py        # save/apply human-corrected regions (geometry, not values)
│   ├── layout_key.py            # spatial-memory grouping key (vendor_id:template_id)
│   ├── contracts.py             # canonical purchase-order export contract
│   ├── object_store.py          # MinIO with local-filesystem fallback
│   ├── cache.py                 # Redis async cache (results; prompts now rebuilt live)
│   ├── auth.py                  # JWT + bcrypt + ownership asserts
│   ├── config.py                # env → typed config
│   ├── models.py                # pydantic request/response models
│   ├── mlflow_tracing.py        # span/trace context for the pipeline
│   ├── logging_config.py        # structured JSON events (plog.event/timed)
│   └── page_logger.py           # per-extraction human-readable run log + billing summary
├── frontend/                    # vanilla-JS SPA, hash routing, served by FastAPI
│   ├── core.js                  # router, apiFetch, auth guard, global state
│   ├── login.js  vendors.js  extract.js  review.js  history.js
│   ├── dashboard.js  admin.js  schedules.js  settings.js
│   └── styles.css  index.html   # terminal theme — DO NOT rewrite styles.css
├── docs/
│   ├── workflow/                # ← line-by-line study guides + THIS doc + .html
│   ├── workflow.md              # full screen-by-screen product walkthrough
│   └── scaling.md               # the authoritative scaling plan (10–100 customers)
├── tests/                       # unittest.IsolatedAsyncioTestCase — all deps mocked
├── e2e/                         # Playwright end-to-end specs
├── bottleneck/                  # finalized llama-server perf flags + notes
├── docker-compose.yml           # api + 4 workers + postgres + redis + minio + mlflow
└── CLAUDE.md / AGENTS.md        # agent operating instructions
```

**Rule of thumb for where code goes:** SQL → `db.py` only. Anything that calls
Qwen → `extractor.py`. Anything that decides *where a value is on the page* →
`geometry.py` / `qwen_layout_apply.py` / `spatial_memory.py`. HTTP shape →
`main.py` + `models.py`. Never put SQL in `main.py` or worker stage files.

---

## 4. Runtime topology

Six processes (Docker Compose), one external dependency (the GPU LLM server):

| Process | Bound by | Replicas today | Scales by |
|---|---|---|---|
| `api` (uvicorn) | CPU / IO | 1 | horizontal, stateless |
| `normalize-worker` | CPU (PDFium) | 1 | **add replicas** |
| `ocr-worker` | CPU (PaddleOCR) | 1 | **add replicas** |
| `llm-worker` | **GPU (serial)** | 1 | *not* by replicas — GPU is the wall |
| `postprocess-worker` | CPU / DB | 1 | rarely needed |
| `postgres` / `redis` / `minio` / `mlflow` | IO | 1 | managed services in prod |
| `llama-server` (Qwen3-VL GGUF) | **GPU** | external host | `--parallel N` / more GPUs |

Workers never talk to each other. They coordinate **only** through the Postgres
`jobs` table using `FOR UPDATE SKIP LOCKED`. This is the most important
architectural decision in the system: it makes every stage independently
scalable and crash-recoverable with zero distributed-systems code.

---

## 5. The end-to-end workflow

### 5.1 Ingest (two entry points, one path)

1. **Manual:** client uploads on `#/extract` → `POST /ingest/ui` (multipart).
2. **Scheduled:** APScheduler fires → scans the client's `input_folder` → calls
   the same `ingest_cb` per PDF. *Same code path from here down.*

Ingest does, in order: quota check → store original to object store → create
`documents` row → **detect vendor from page 1** (unless vendor pre-selected) →
create `extractions` row → enqueue the `normalize` job → return
`{job_id, extraction_id, detected_vendor}`. The frontend then opens
`GET /jobs/{job_id}/stream` (SSE) and watches the pipeline live.

If vendor detection fails the request is rejected (HTTP 409) — the system
**never auto-creates a vendor**. The client must create the vendor + template
first. This is deliberate: an unknown format with no template produces garbage.

### 5.2 The four durable stages

```
normalize ──┬──> ocr ───────┐
            └──> llm ────────┴──> postprocess ──> done
```

`normalize` fans out to **both** `ocr` and `llm` (they run concurrently —
PaddleOCR on CPU while Qwen works on the GPU). Whichever finishes second triggers
`postprocess` via the idempotent `_maybe_enqueue_postprocess` /
`is_postprocess_ready` guard.

| Stage | Input | Work | Output | SSE |
|---|---|---|---|---|
| **normalize** | original bytes | render pages (32px-aligned), classify digital vs scanned via pypdfium2 char count, store page images | `extraction_pages`, per-page `source` + `word_geometry` | "Rendered N pages (X digital, Y scanned)" |
| **ocr** | scanned page numbers | PaddleOCR on *scanned only*; digital pages reuse pypdfium2 geometry | unified `ocr_data` (one word-box schema for both engines) | "Running OCR on N scanned pages" / "OCR skipped" |
| **llm** | page images + template | build system prompt fresh from DB; page-1 prompt includes box schema + vendor verify; pages 2+ are line-items only; record token usage per page | `result`, `page_results`, `qwen_layout_boxes` (from page-1 boxes) | "Extracting page X/Y" |
| **postprocess** | result + ocr_data + layout boxes | map every field to a word box, apply spatial memory (reads *current* text in saved regions), build `field_locations` | final `result`, `field_locations`, status=`done` | "Field mapping complete" |

The LLM stage does **not** set status `done` — it leaves `processing` so the SSE
stream stays open until postprocess has the bounding boxes. Final status
transition is owned by postprocess alone.

### 5.3 Two-agent split & the box lifecycle (current branch)

Page 1 carries two jobs: (a) extract header field **values** *and* (b) emit the
layout **boxes** for every field. Pages 2+ only need line items. So the page-1
system prompt is built with `include_boxes=True` + vendor verification; pages 2+
with `include_boxes=False` (`extractor.py` — `effective_prompt` /
`include_boxes=is_page1`).

**Every PDF's page 1 goes to the model fresh, every time, and returns both the
box and the value.** The page-1 boxes are then upserted into `qwen_layout_boxes`
keyed `(vendor_id, template_id, field_key)` — **last-write-wins, overwritten on
every run**. Postprocess reads them back *within the same extraction* to build
`field_locations`. So the table is a **per-run handoff from the LLM stage to
postprocess**, not a long-lived cache. There is no "learned once, reused
forever" path and therefore no mechanism for a bad page-1 box to be inherited by
future documents — each document re-derives its own geometry from its own page 1.

> The only thing that *is* reused across documents is **spatial memory**
> (§5.4) — and that stores human-corrected *regions*, never values, and is read
> against the current document's text.

### 5.3a Model strategy (Qwen3-VL primary, Gemini 3 Flash optional)

The backend pipeline is **Qwen3-VL only** (`llama.cpp`, OpenAI-compatible
endpoint). There is no Gemini code in `backend/`. Gemini 3 Flash lives in the
standalone `gemini/` experiment folder as an **optional alternative extractor**,
not a runtime-pluggable model. Both produce the same JSON shape, so the choice
is a *deployment* decision, not an architectural one:

| Model | When | Trade-off |
|---|---|---|
| **Qwen3-VL (preferred, in prod)** | We control the GPU / customer provides adequate GPUs | Self-hosted, no per-call cost, GGUF on llama.cpp, scales via `--parallel` |
| **Gemini 3 Flash (optional)** | Customer cannot provide adequate GPU; scale must be trivial | Hosted API, per-call cost, near-zero infra, scales instantly; swap-in only |

Decision rule: prefer Qwen3-VL while we have GPU; fall back to Gemini 3 Flash
only if the customer's GPU situation makes self-hosting impractical. Wiring
Gemini into the `llm` stage as a selectable backend is a **future task**, not
current behavior.

### 5.4 Review & spatial memory (human-in-the-loop)

The client opens `#/review/{id}`, sees blue dotted rectangles where each value
was found, and drags a box to correct any field. Saving a correction writes
**geometry, never the value**: `spatial_memory` stores
`(vendor_id, layout_key, field_key, page_number, normalized_box)`. On the next
document of the same layout, postprocess reads whatever text currently sits in
that saved region of the *new* document. Storing the old value as truth would be
a correctness bug — the rule is "remember *where*, never *what*".

---

## 6. Metering: pages, tokens, latency (the billing spine)

Billing is derived, not stored as a running total. Source of truth is the
`llm_usage` table — **one row per LLM call** (i.e. per page):

```
llm_usage(extraction_id, vendor_id, page_num, total_pages, call_type,
          model, prompt_tokens, completion_tokens, total_tokens,
          duration_ms, llm_url, ts)
```

- **Per-page tokens & latency:** written by the LLM stage as each page completes
  (`db.record_llm_usage`). `prompt_tokens` / `completion_tokens` come straight
  from the model server's usage block.
- **Per-PDF totals:** `get_extraction_token_totals` aggregates rows by
  `extraction_id` (logged at "EXTRACTION COMPLETE").
- **Per-client billable pages:** `COUNT(DISTINCT (extraction_id, page_num))`
  where `extraction_id IS NOT NULL`, compared against `users.subscription_limit`.
- **Quota gate:** at ingest, if `billable_pages >= subscription_limit` the upload
  is rejected (HTTP 402). It is a **soft limit** — the last allowed PDF may run
  slightly over because pages are counted *after* extraction; the *next* upload
  is what gets blocked. `subscription_limit = 0` blocks all uploads.
- **Dashboard:** `/dashboard` (client = self, admin = all + per-client
  breakdown) renders token trend, input/output split, daily table, and
  expandable per-PDF per-page detail — all read-side aggregations over
  `llm_usage`.

> **Known billing leak (carried from audit):** `llm_usage.extraction_id` is
> `ON DELETE SET NULL`. Deleting an extraction drops its pages out of the
> billable count — a client can lower their bill by deleting history. Fix
> requires snapshotting billable pages at extraction completion into an
> append-only ledger that delete cannot touch. **Tracked, not yet fixed.**

---

## 7. The scheduler & concurrency (the "two clients at 10 PM" question)

This is the question that matters most for multi-tenant correctness, so it gets
its own section.

### 7.1 How scheduling works

`scheduler.py` wraps **APScheduler `AsyncIOScheduler`** with a
**`SQLAlchemyJobStore`** (its own `apscheduler_jobs` table). Our
`user_schedules` table holds the user-facing cron metadata
(`cron_expr`, `timezone`, `enabled`, `is_executing`, `last_ran_at`). Each
enabled schedule becomes one APScheduler job id `user_sched_{id}` with
`max_instances=1, coalesce=True`. On startup `reload_all_schedules` re-registers
every enabled row from the DB, so schedules survive restarts.

### 7.2 What happens when Client A and Client B both fire at 22:00

**They run independently and correctly. There is no conflict. Here is exactly why:**

1. **Different jobs.** A's `user_sched_A` and B's `user_sched_B` are distinct
   APScheduler jobs. Both fire at 22:00 in the single asyncio event loop. They
   are scheduled concurrently as coroutines — neither blocks the other (the work
   is `await`-heavy IO).
2. **Per-schedule serialization, not cross-schedule.** `max_instances=1` +
   `coalesce=True` only prevent *the same schedule* from overlapping itself
   (e.g. a slow 22:00 run still going when the 23:00 run is due → the later one
   is coalesced, not stacked). It has no effect across different clients.
3. **`is_executing` is a per-schedule UI guard, not a global lock.**
   `_run_schedule` sets `user_schedules.is_executing = TRUE` for *its own*
   schedule_id while scanning that client's folder, then `FALSE` in a `finally`.
   Its only purpose is to stop the *same client's* manual UI upload from racing
   the *same client's* scheduled run. Client A's flag has nothing to do with
   client B.
4. **The real serialization point is the GPU, and it's a queue, not a lock.**
   Both runs call `ingest_cb` per PDF, which just enqueues `normalize` jobs in
   the Postgres `jobs` table. Workers claim jobs with `FOR UPDATE SKIP LOCKED`.
   So A's and B's documents interleave through the pipeline. The GPU processes
   them one page at a time (it is serial today), but **fairly by queue order**,
   not "A fully drains before B starts" — *provided per-customer fair queueing
   is enabled* (see next point).
5. **Head-of-line blocking is the actual risk, and it is known.** Today
   `claim_job` orders strictly `priority ASC, created_at ASC`. If A's 22:00 run
   enqueues 500 pages one second before B's 5 pages, B waits behind all 500
   (~30+ min on one GPU). This is **not a deadlock or a data-safety issue** —
   isolation and correctness hold — it is a *fairness/latency* issue. The fix is
   **per-customer fair queueing** (`scaling.md` Step 2): stamp `jobs.priority`
   at enqueue time from the customer's current backlog so customers interleave.
   It is designed, tested in `tests/test_scheduler_isolation.py` /
   `test_scheduler_edge_cases.py`, and deliberately **not deployed during demo
   week** (queue-ordering changes are demo-sensitive).

**Summary:** simultaneous schedules are *safe and isolated by design*. The only
thing that degrades under simultaneous large batches is *latency fairness*, and
that has a designed, tested, gated fix. No client can ever see, block, or corrupt
another client's data through the scheduler.

### 7.3 Scheduler failure semantics

- Folder missing / no PDFs → logged, no-op, no error surfaced to the client.
- Per-PDF: success → moved to `success_folder`; failure → moved to
  `failed_folder`; `_move_pdf` never raises (logs only).
- `is_executing` is always cleared in `finally` even if the whole run throws —
  no permanent "stuck executing" state.
- Scheduler context (`pool`, `ingest_cb`) is injected post-startup
  (`set_context`) to avoid circular imports; if unset the run logs an error and
  returns rather than crashing the event loop.

---

## 8. Edge cases (the things that actually break in production)

| # | Edge case | Current behavior | Status |
|---|---|---|---|
| 1 | Unknown vendor (no alias match) | HTTP 409, no auto-create, no pipeline | ✅ correct by design |
| 2 | Ambiguous alias (`scott` shared by 2 vendors) | substring match → wrong vendor possible | ⚠️ known, aliases must be unique |
| 3 | Digital page with >50 chars but 0 words | classified digital → OCR skipped → no geometry → review has no boxes for that page | 🔴 known HIGH |
| 4 | Malformed LLM response (missing `choices`) | unhandled KeyError can crash the page | 🔴 known CRITICAL — wrap + JSON-repair fallback |
| 5 | Quota check hits a DB error | fails *open* (continues without check) | 🔴 known HIGH — should fail closed |
| 6 | Delete extraction | `llm_usage.extraction_id`→NULL, jobs/pages not cascaded | 🔴 billing leak + DB bloat |
| 7 | Delete vendor | aliases / spatial_memory / layout_boxes / templates orphan | ⚠️ MEDIUM (templates *do* cascade; others do not) |
| 8 | Worker crashes mid-job | job stuck `running` → `recover_stale_jobs` resets it (5 min on startup, 10 min every 60 s) | ✅ handled |
| 9 | Duplicate pipeline stage enqueued | `jobs_active_idx` unique `(extraction_id, job_type)` in queued/running blocks it; `ensure_job` checks `has_active_job` | ✅ handled |
| 10 | Same client: scheduled run races manual upload | `is_executing` flag blocks the UI upload while the schedule runs | ✅ handled |
| 11 | Two different clients fire at the same time | independent, isolated; only latency fairness at risk | ✅ safe (see §7) |
| 12 | Cancel mid-pipeline | `_stop_if_cancelled` after each stage → status `cancelled` or `partial` (if partial results exist) | ✅ handled |
| 13 | Vendor verification fails on page 1 (`vendor_confirmed:false`) | status `unverified`, pipeline stops | ⚠️ SSE doesn't treat `unverified` as terminal → UI spinner can hang |
| 14 | PDF text-geometry extraction throws | whole document falls back to scanned (PaddleOCR) | ✅ graceful degrade |
| 15 | Empty template (no header/line fields) | extraction proceeds with weak prompt → poor output | ⚠️ no save-time validation |
| 16 | SSE client disconnects | server stream ends; pipeline continues; client can re-attach via `/jobs/{id}/stream` | ✅ durable by design |
| 17 | Soft-limit overshoot | last PDF may exceed limit (counted post-hoc); next upload blocked | ✅ documented, intentional |

🔴 = fix before charging real money. ⚠️ = known, has a workaround or is low-blast-radius.

---

## 9. Test coverage map

`tests/` uses `unittest.IsolatedAsyncioTestCase` with all DB/store/LLM calls
mocked — no live services. Coverage by concern:

| Concern | Test files |
|---|---|
| Tenant isolation / admin scoping | `test_admin_client_scoping`, `test_admin_billing_scoping`, `test_db_vendor_filtering`, `test_auth` |
| Vendor detection | `test_vendor_detector` |
| Pipeline correctness | `test_pipeline_integration_flow`, `test_processor`, `test_extractor_merge`, `test_extractor_concurrency`, `test_extractor_no_boxes` |
| Pipeline hardening / adversarial | `test_pipeline_hardening`, `test_pipeline_adversarial`, `test_ingest_hardening`, `test_infrastructure_safety` |
| Scheduler (incl. concurrency) | `test_scheduler`, `test_scheduler_api_routes`, `test_scheduler_isolation`, `test_scheduler_edge_cases` |
| Billing / metering | `test_llm_usage`, `test_usage_aggregation`, `test_page_limits` |
| Layout / bbox | `test_qwen_layout_decision`, `test_single_agent_bbox`, `test_word_pdlocr` |
| Review HITL | `test_review_api`, `review/` |
| Admin UX | `test_admin_users`, `test_admin_dashboard`, `test_vendor_dashboard`, `test_template_prompt_visibility`, `test_config_api` |
| Observability | `test_tracing` |
| OCR threading | `test_ocr_multithread`, `paddle_ocr/` |

**Gaps worth adding** (recommended, not yet present): a test that asserts
`quota fails *closed*` on DB error (edge #5); a test that the digital-zero-word
page still gets OCR geometry (edge #3); an end-to-end billing-leak test that a
deleted extraction does **not** reduce a frozen billable ledger (edge #6, after
the ledger fix).

Run: `.venv/Scripts/python.exe -m pytest tests/`

---

## 10. Scaling roadmap (summary — full detail in `docs/scaling.md`)

Target: 10–100 customers, 10–15 formats each, ~100 docs each.

**The one decision that matters:** the model is GGUF → stay on **llama.cpp** and
raise `--parallel 1` → `--parallel 8–16` on a 24–48 GB AWS GPU. *Zero Python
changes, 5–10× throughput.* vLLM is **not** an option (no GGUF vision support).

Single RTX 4060 today ≈ 240 pages/hour (serial, the hard wall). Ordered plan:

1. **CPU-stage replicas** (free, low risk) — 3× normalize + 3× ocr so the GPU
   never starves. Keep llm-worker at 1.
2. **Per-customer fair queueing** (medium risk, tested, *not* demo week) — stamp
   `jobs.priority` from per-customer backlog. Fixes §7.2 head-of-line blocking.
3. **DB connection ceiling** — shrink CPU-worker pool sizes; add pgbouncer
   before >8 workers.
4. **Second GPU / bigger card** — the *only* true throughput multiplier; the
   queue already load-balances two `llm-worker`s with no code change.
5. **Shorter LLM output** — compact JSON, omit nulls, cap max tokens (~15–25%).

Decision rule: overnight batch acceptable → steps 1, 3a, 5 (1 GPU is enough).
Same-hour turnaround required → step 4 is mandatory.

---

## 11. Operational runbook (essentials)

```bash
# Local dev (no Docker)
.venv/Scripts/python.exe -m uvicorn backend.main:app --port 8000 --reload
.venv/Scripts/python.exe -m backend.worker --stage normalize   # + ocr / llm / postprocess

# Full stack
docker compose up --build         # UI :8000  MinIO :9001  MLflow :5000

# Health checks
psql "$DATABASE_URL" -c "SELECT job_type,status,count(*) FROM jobs GROUP BY 1,2;"
psql "$DATABASE_URL" -c "SELECT count(*) FROM pg_stat_activity; SHOW max_connections;"
```

- **Stuck extraction:** check the `jobs` row status. `running` >10 min →
  `recover_stale_jobs` will reset it; if it never recovers, the worker for that
  stage is down.
- **GPU down:** llm-worker keeps claiming jobs and failing; extractions go
  `failed`. Bring `llama-server` back; failed jobs are retryable up to
  `max_attempts=3`.
- **Schema:** auto-created and migrated idempotently in `db.init()` on every
  startup. Migrations are `DO $$ IF NOT EXISTS $$` — safe to re-run. **Do not
  run manual migrations during demo week without explicit sign-off.**
- **Secrets:** `SECRET_KEY` must be set in prod (JWT signing fails closed with
  HTTP 500 if missing — by design).

---

## 12. What must never change (architectural guardrails)

1. Sequential page processing within a document (`PARALLEL_BATCH = 1`).
2. `FOR UPDATE SKIP LOCKED` + `jobs_active_idx` queue design.
3. Tenant isolation via `vendors.user_id` ownership walk — never add a
   shortcut `user_id` to extractions/jobs that bypasses the asserts.
4. Spatial memory stores **geometry, not values**.
5. Postprocess — not LLM — owns the final `done` status transition.
6. Vendor detection never auto-creates a vendor.
7. The finalized llama-server flags (`bottleneck/`) — any new GPU reuses them
   exactly (bbox grounding was tuned against them).

---

*Companion visual document: [architecture.html](architecture.html) — open in a
browser; all diagrams are live Mermaid (system context, container view, ingest
sequence, pipeline state machine, ER model, tenancy, scheduler concurrency,
scaling).*
