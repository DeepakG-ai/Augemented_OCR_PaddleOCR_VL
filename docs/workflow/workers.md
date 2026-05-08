# Worker Mechanics

> Source file: [backend/worker.py](../../backend/worker.py)
>
> Each pipeline stage runs as a separate `python -m backend.worker --stage <name>` process. They share the database for coordination but never talk to each other directly.

---

## How a worker starts

```bash
.venv/Scripts/python.exe -m backend.worker --stage normalize
.venv/Scripts/python.exe -m backend.worker --stage ocr
.venv/Scripts/python.exe -m backend.worker --stage llm
.venv/Scripts/python.exe -m backend.worker --stage postprocess
.venv/Scripts/python.exe -m backend.worker --stage outbound
```

In Docker, each stage has its own service in `docker-compose.yml`. The `--name` flag (auto-generated as `worker-<8 hex>` if omitted) is recorded as `jobs.locked_by` for debugging "who claimed this job".

---

## The poll loop (lines 1112–1268)

```python
async def run_worker(stage: str, worker_name: str) -> None:
    pool = await db_mod.create_pool()
    await db_mod.init(pool)
    
    # Recover orphaned jobs from a previous crash
    recovered = await db_mod.recover_stale_jobs(pool, stage, stale_minutes=5)
    if recovered:
        logger.info("Startup recovery: reset %d stale '%s' jobs to queued", recovered, stage)
    
    last_recovery = time.monotonic()
    RECOVERY_INTERVAL = 60.0
    
    while True:
        # Periodic stale-job sweep
        if time.monotonic() - last_recovery >= RECOVERY_INTERVAL:
            await db_mod.recover_stale_jobs(pool, stage, stale_minutes=10)
            last_recovery = time.monotonic()
        
        job = await db_mod.claim_job(pool, stage, worker_name)
        if not job:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)  # default 1s
            continue
        
        try:
            await process_job(pool, stage, job)
            await db_mod.complete_job(pool, job["id"], {"stage": stage, "message": "done"})
        except Exception as exc:
            # Mark extraction failed, mark job failed (non-retryable)
            ...
```

Three loops in one:
1. **Periodic recovery** every 60s — resets jobs stuck in `running` for 10+ minutes.
2. **Claim** the next queued job (atomic — see below).
3. **Process** the job and record success/failure.

---

## `claim_job` — atomic claim with row-level lock

The crucial query (in `db.py`):

```sql
UPDATE jobs SET
    status     = 'running',
    locked_by  = $2,
    locked_at  = NOW(),
    started_at = COALESCE(started_at, NOW()),
    attempts   = attempts + 1,
    updated_at = NOW()
WHERE id = (
    SELECT id FROM jobs
    WHERE status = 'queued' AND job_type = $1
    ORDER BY priority ASC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING ...;
```

**`FOR UPDATE SKIP LOCKED`** is the magic. Two workers polling simultaneously won't claim the same row — the second worker skips locked rows and finds the next available one. No external lock manager, no Redis queue — Postgres provides everything.

`ORDER BY priority ASC, created_at ASC` — lower priority number wins (default 100); ties broken by FIFO.

Returns `None` if the queue is empty. The worker sleeps `POLL_INTERVAL_SECONDS` and tries again.

---

## `ensure_job` — idempotent enqueue

Used by the API and by upstream stages to enqueue the next stage:

```python
async def ensure_job(pool, extraction_id, document_id, job_type, payload):
    if await has_active_job(pool, extraction_id, job_type):
        return existing_job
    return await create_job(...)
```

`has_active_job` checks for any job with `status IN ('queued', 'running')` for this `(extraction_id, job_type)`. If found, returns it without creating a new one.

The `jobs_active_idx` partial unique index is the hard guarantee — even if two callers race past `has_active_job`, the second `INSERT` fails with a unique-constraint violation that the helper catches.

This means: **enqueueing the same stage twice for the same extraction is safe and a no-op.**

---

## `_maybe_enqueue_postprocess` — the OCR/LLM rendezvous

```python
async def _maybe_enqueue_postprocess(pool, extraction_id, document_id, trace_context):
    # Lightweight check — avoids loading massive JSONB blobs
    if not await db_mod.is_postprocess_ready(pool, extraction_id):
        return
    payload = {"extraction_id": extraction_id, ...}
    await db_mod.ensure_job(pool, extraction_id, document_id, "postprocess", payload)
```

Why the lightweight check? `is_postprocess_ready` queries:
```sql
SELECT (result IS NOT NULL) AS has_result,
       (ocr_data IS NOT NULL) AS has_ocr
FROM extractions WHERE id = $1
```

A bare `SELECT * FROM extractions` would pull megabytes (the `result` and `ocr_data` JSONB columns can be huge). The lightweight check is essentially free.

OCR and LLM stages both call this when they finish. The first one to finish sees `has_result=False` (or `has_ocr=False`) and bails. The second one sees both are True and enqueues postprocess. Idempotent — running it three times is safe (second and third calls are no-ops via `has_active_job`).

---

## Stale-job recovery (lines 1117–1130)

Two layers:

```python
# 1. Startup recovery — runs once on worker boot
recovered = await db_mod.recover_stale_jobs(pool, stage, stale_minutes=5)

# 2. Periodic sweep — every 60s
if time.monotonic() - last_recovery >= RECOVERY_INTERVAL:
    await db_mod.recover_stale_jobs(pool, stage, stale_minutes=10)
```

The query:
```sql
UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL
WHERE status = 'running'
  AND job_type = $1
  AND locked_at < NOW() - INTERVAL '$2 minutes'
RETURNING id;
```

**Two thresholds**:
- 5 minutes on startup — covers a worker that crashed seconds before this one came up.
- 10 minutes during steady-state — only resets jobs that are *really* stuck (longer to avoid stealing from a slow but live worker).

This is what makes the system durable. A `kill -9` on a worker mid-job leaves the job in `running` status with the dead worker's name. Within 10 minutes, the next live worker resets it and tries again.

`attempts` increments each time `claim_job` runs. If a job has been failing repeatedly (poison message), `max_attempts` (default 3) caps retries and the job moves to a permanent failed state.

---

## Per-stage flow inside `process_job` (lines 1079–1109)

```python
async def process_job(pool, stage, job):
    trace_context = _job_trace_context(job)
    with use_trace_context(trace_context):
        extraction = await db_mod.get_extraction(pool, job["extraction_id"])
        document = await db_mod.get_document(pool, job["document_id"])
        with trace_pipeline_stage(stage, ...) as stage_trace:
            if stage == "normalize":
                await _process_normalize(pool, job)
            elif stage == "ocr":
                await _process_ocr(pool, job)
            elif stage == "llm":
                await _process_llm(pool, job)
            elif stage == "postprocess":
                await _process_postprocess(pool, job)
            elif stage == "outbound":
                await _process_outbound(pool, job)
            stage_trace["output"] = {"status": "completed", "stage": stage}
```

The `trace_pipeline_stage` context manager opens a Phoenix span for the stage; child spans (LLM calls, OCR runs, etc.) inherit the trace context and show up nested in the Phoenix UI at `localhost:6006`.

---

## SSE streaming — how progress reaches the browser

The browser opens `GET /jobs/{job_id}/stream`. The endpoint (in `main.py`) opens a long-lived response and polls the DB:

```python
async def event_stream():
    while True:
        job = await db_mod.get_job(pool, job_id)
        extraction = await db_mod.get_extraction(pool, job.get("extraction_id"))
        
        event = {
            "event": "progress",
            "job": job,
            "extraction": extraction,
        }
        
        # Determine terminal state
        if extraction["status"] in ("done", "failed", "partial", "cancelled"):
            event["event"] = extraction["status"]
            yield f"data: {json.dumps(event, default=str)}\n\n"
            break
        
        yield f"data: {json.dumps(event, default=str)}\n\n"
        await asyncio.sleep(0.5)
```

(Simplified — actual code includes auth, ownership check via `assert_job_access`, and a heartbeat to keep the connection alive.)

**Why SSE instead of polling?**
- One HTTP request per extraction instead of 50+ per minute.
- Server pushes; client doesn't need to know the polling cadence.
- Standard browser API (`EventSource`), no WebSocket complexity. (Actually the frontend uses raw `fetch` + `ReadableStream` instead of `EventSource` because EventSource can't send `Authorization` headers — but the wire format is the same.)

The frontend's `streamJob` (in `extract.js`) parses each `data: {...}` chunk and calls `applyJobStatus(event)` which updates the pipeline visualization, page progress, and result display.

---

## Worker error handling (lines 1159–1266)

```python
except Exception as exc:
    logger.exception("Job failed id=%s stage=%s", job["id"], stage)
    plog.event("job_failed", stage=stage, ..., error=str(exc))
    
    # Stage-specific failure logging to page_logger.append_log(...)
    if stage in ("normalize", "ocr", "llm", "postprocess") and job.get("extraction_id"):
        # ... write detailed failure log entry ...
    
    # Mark extraction failed
    if job.get("extraction_id") and stage != "outbound":
        await db_mod.set_extraction_status(pool, ext_id, "failed", error=str(exc))
    elif job.get("extraction_id") and stage == "outbound":
        # Outbound failures don't fail the extraction (result is already done)
        await db_mod.upsert_delivery(pool, ext_id, ..., status="failed", error=str(exc))
    
    await db_mod.fail_job(pool, job["id"], str(exc), retryable=False)
```

**Outbound asymmetry**: a failed outbound stage doesn't mark the extraction as failed because the user-facing result is correct — only the export delivery is broken. The `integration_deliveries` row records the error; the user can re-export.

`retryable=False` means the job goes straight to `failed` — no automatic retry. Stale-job recovery doesn't reset failed jobs.

---

## What runs in parallel vs. sequential

```
Document upload
       │
       ▼
   [normalize]              ←  one job, one worker, ~5–30s
       │
       ├──> [ocr] ─────────┐
       │                   │   parallel
       └──> [llm] ─────────┤
                           ▼
                    [postprocess]    ←  rendezvous via _maybe_enqueue_postprocess
                           │
                           ▼
                      [outbound]     ←  fire-and-forget for the user
```

Each box is a separate worker process. If you scale `--stage llm` to 2 workers, two extractions can run their LLM stages concurrently (each on its own GPU stream — though llama.cpp typically batches at the model level).

---

## How retry / resume works

**Stage-level retry** (system-driven): if a worker crashes, the stale-job sweep resets the job. The next worker picks it up. This is automatic; the user sees a brief pause in the SSE stream.

**Page-level resume** (user-driven): only the LLM stage supports resume. The frontend's "Resume from page N" button posts to `/jobs/extractions/{id}/resume`, which:
1. Reads the existing `page_results` (page 1 ✓, page 2 ✓, page 3 ✗).
2. Re-enqueues the LLM stage with `payload = {start_from_page: 3, existing_page_results: [page1, page2]}`.
3. The LLM worker re-runs `extract_document` skipping done pages and starting fresh from page 3.

This is implemented in `extractor.extract_document` lines 537–554:

```python
already_done = {pr["_page"] for pr in page_results if "_error" not in pr}
page_results = [pr for pr in page_results if "_error" not in pr]
pending_pages = [p for p in pages if p["page_number"] >= start_from_page and p["page_number"] not in already_done]
```

---

## Common operational tips

- **Log line `Claimed job id=X stage=Y extraction=Z`**: every poll-loop pickup. Use to confirm a worker is alive.
- **Log line `Periodic recovery: reset N stale jobs`**: if you see this often, a worker is dying mid-job; investigate stack traces.
- **`docker compose logs -f api`**: watch ingest + SSE traffic.
- **`docker compose logs -f normalize-worker llm-worker`**: watch the slow stages.
- **Phoenix at localhost:6006**: timeline of stages and LLM calls per extraction.
- **`SELECT id, job_type, status, attempts, locked_by, locked_at, error FROM jobs ORDER BY id DESC LIMIT 20`**: quick health check on the queue.

---

## What this design does NOT support

- **Cross-stage transactions.** Each stage commits its own work before enqueuing the next. There is no rollback if postprocess fails after LLM succeeded — the LLM result stays in the DB.
- **Job priorities for paying customers.** All priorities are 100 by default. The schema supports it but no code uses it.
- **Worker scaling per tenant.** All clients share the same worker pool. No per-tenant queue.
- **Dead-letter queue.** Failed jobs stay as `status='failed'` rows. No auto-archive or alerting.
- **Job dependencies in the DB schema.** The chain (`normalize → ocr/llm → postprocess → outbound`) is encoded in the worker code, not as DB foreign keys between job rows.
