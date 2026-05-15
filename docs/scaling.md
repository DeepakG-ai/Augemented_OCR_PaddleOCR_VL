# Scaling Plan — Multi-Customer Deployment

Date: 2026-05-15
Status: **PLAN ONLY — nothing implemented. Do not apply during demo week
without explicit confirmation (queue/worker changes are demo-sensitive).**

Target: 10 customers, each with 10-15 distinct PDF/vendor formats, each
submitting up to ~100 documents.

---

## Part 0 — THE CORE QUESTION: Serving Qwen for the AWS Multi-Customer API

**Question:** Can a single (or 2) GPU serve a Qwen3-VL API for 10-100
customers on AWS? (Mapping / Python / PaddleOCR on CPU is understood and not
in question.)

**Answer: YES — 1-2 GPUs is enough. The whole answer hinges on ONE decision:**

> **Raise llama.cpp from `--parallel 1` to `--parallel 8-16` on a bigger-VRAM
> AWS GPU. Keep the exact GGUF files. Do NOT try vLLM with GGUF.**

### Critical constraint: the model is GGUF

GGUF is a **llama.cpp format**. vLLM / SGLang do **not** consume GGUF for
vision (VL) models — their GGUF support is partial and excludes multimodal.
To use vLLM you would have to abandon GGUF entirely and re-acquire the
original Qwen3-VL-8B **HuggingFace safetensors** (AWQ/GPTQ), then re-validate
accuracy (bbox grounding especially). That is a separate project, not a
config change.

**Therefore the practical path is to stay on llama.cpp.**

### Why llama.cpp `--parallel N` is sufficient

llama.cpp already has **continuous batching**. `--parallel N` = N concurrent
slots, each with its own KV cache, decoded in a batched loop. `--parallel 1`
(the ~240 pages/hr worst case in this doc) was forced only by the 4060's 8GB
VRAM. On a 24GB AWS GPU that limit disappears.

| Server | Concurrent reqs / 1 GPU | Uses your GGUF? |
|--------|-------------------------|-----------------|
| llama.cpp `--parallel 1` | 1 (serial) | yes |
| **llama.cpp `--parallel 8-16`** | **8-16 (continuous batching)** | **yes — no changes** |
| vLLM / SGLang | 8-32 | **NO — requires HF safetensors, not GGUF** |

VRAM math, your exact GGUF files, A10G 24GB:

```
Model Q4_K:        ~4.6 GB
mmproj F16:        ~1.1 GB
Compute buffers:   ~0.7 GB
KV per slot @4096: ~0.576 GB

--parallel 8  → ~11.0 GB total → fits 24GB easily
--parallel 16 → ~15.6 GB total → still fits 24GB
```

Realistic throughput on one A10G with `--parallel 8`: **~5-10× the 240
pages/hr baseline → ~1,200-2,400 pages/hr**, using the *same model file and
the same engine you already tuned*.

### Concurrency ≠ daily volume

"100 customers" ≠ 100 simultaneous requests. Realistic peak concurrency is
5-20 in-flight; llama.cpp `--parallel N` queues + batches these. Size two
things separately: peak concurrency (the `--parallel N` slot count) and
pages/day (sets GPU count).

### Capacity — llama.cpp `--parallel 8`, your GGUF (Qwen3-VL-8B, ~600 img + ~500 out tokens)

| AWS instance | GPU | --parallel | Throughput |
|--------------|-----|-----------|-----------|
| g5.xlarge | 1× A10G 24GB | 8 | ~1,200-2,400 pages/hr |
| g6e.xlarge | 1× L40S 48GB | 16 | ~2,500-4,000 pages/hr |
| g5.12xlarge | 4× A10G | 8 each | ~5,000-9,000 pages/hr |

Worst case 100 customers × 100 docs × 2.5 pages = **25,000 pages**:

| GPUs | Outcome |
|------|---------|
| **1× g5.xlarge** | All 100 as overnight batch (~10-20 h); interactive for 10-40 customers |
| **2× A10G** | ~5-10 h for all 100; solid interactive for 50-100; gives failover |

**Rule:** 1 GPU = low end (10-40 interactive / 100 batch). 2 GPUs = 50-100
interactive + redundancy.

### Architecture on AWS

```
customers ─HTTPS→ FastAPI (cheap CPU, autoscale)        ← main.py
                     │  Postgres job queue (RDS, SKIP LOCKED — already built)
                  llm-worker(s) (CPU, just HTTP calls)
                     │  OpenAI-compatible HTTP
                  llama.cpp --parallel 8  (g5.xlarge ×1 or ×2)  ← only GPU cost
                     │  serves YOUR GGUF + mmproj unchanged
mapping / PaddleOCR / postprocess → CPU instances
```

GPU instances run **only llama-server**. API, queue, mapping, OCR are all
cheap CPU and autoscale independently. Two llama-server endpoints are
load-balanced by the existing Postgres `SKIP LOCKED` queue with **zero extra
code** (run one llm-worker per endpoint, each with its own `LLM_URL`).

### Code impact: a flag and an env var

No Python changes. On the GPU host:

```
llama-server ... --parallel 8 --cont-batching     # cont-batching is default in recent builds
```

(plus your already-finalized flags: ctx 4096, image-max-tokens 768, etc. —
see bottleneck/performance_notes.md). On the worker:

```
LLM_URL=http://<gpu-host>:8001/v1/chat/completions
```

Image-base64 message format, temperature, top_p, bbox 0-1000 coords, 32px
alignment — all unchanged because it is the same GGUF and same engine.

### If you ever outgrow llama.cpp batching

vLLM/SGLang give higher concurrency (8-32) but **require dropping GGUF** and
re-acquiring Qwen3-VL-8B HF safetensors (AWQ/GPTQ), plus re-validating bbox
accuracy. Treat that as a future project only if `--parallel 16` on a 48GB
card is no longer enough. For 10-100 customers it will not be needed.

### Recommendation

| Customers | Mode | GPUs | Instance + serving |
|-----------|------|------|--------------------|
| 10-40 | batch + light interactive | 1 | g5.xlarge, llama.cpp `--parallel 8` |
| 40-60 | interactive | 1-2 | g6e.xlarge `--parallel 16`, or g5.xlarge ×2 |
| 60-100 | interactive + HA | 2 | g5.xlarge ×2, `--parallel 8` each (failover) |

**Bottom line:** 1-2 GPUs is sufficient for 10-100 customers. The make-or-break
decision is **llama.cpp `--parallel 8-16` on a 24-48GB AWS GPU** — keeps your
exact GGUF and tuned pipeline, zero Python changes, 5-10× throughput per GPU.
vLLM is not an option here because the model is GGUF and vLLM does not support
GGUF for vision models. Steps 1-5 below remain valid for the pipeline around it.

---

## Part 1 — System Reality (why the plan looks like this)

### Pipeline

```
upload → normalize → ocr → llm → postprocess → done
         (CPU)       (CPU)  (GPU)  (CPU/DB)
```

- Jobs in Postgres `jobs` table. `claim_job` uses `FOR UPDATE SKIP LOCKED`
  (`db.py:2180`) → multiple workers per stage already safe.
- `jobs_active_idx` unique index (`db.py:242`) prevents double-processing
  `(extraction_id, job_type)`.
- Orchestration is **asyncio**, not OS threads.
- Pages within a document are **sequential** (`extractor.py:552`,
  `PARALLEL_BATCH = 1`) — page 1 produces the bbox layout later pages and
  postprocess depend on. This must NOT be parallelized.
- DB pool per worker: `min_size=2, max_size=10` (`db.py:19`).
- Deployment today: 1 replica each of normalize/ocr/llm/postprocess
  (`docker-compose.yml`).

### The hard ceiling

One RTX 4060, llama-server `--parallel 1`, ~15 s/page average
(5 s short → 26 s for a 946-token page).

```
Throughput ≈ 4 pages/min ≈ 240 pages/hour ≈ 5,700 pages/day (24h)

Worked example (avg 2.5 pages/doc):
10 customers × 100 docs × 2.5 = 2,500 pages
2,500 ÷ 240 ≈ 10.4 h of continuous GPU time
```

Acceptable as overnight batch. Not acceptable as interactive.

### Where parallelism helps

| Stage | Bound by | Extra workers help? |
|-------|----------|---------------------|
| normalize | CPU (PDFium, releases GIL) | **Yes** |
| ocr | CPU (PaddleOCR) | **Yes** |
| llm | **GPU, single 4060, serial** | **No — the wall** |
| postprocess | CPU/DB (fast) | Marginal |

More `llm-worker` processes do **nothing** — they queue on the same GPU.

---

## Part 2 — The Plan (5 steps, ordered by ROI and risk)

Each step below is self-contained: **what, exact change, why, how to verify,
how to roll back, risk.**

---

### STEP 1 — CPU-stage replicas (do first; free; low risk)

**Goal:** keep the GPU never-idle. Today a single normalize/ocr worker can
leave the GPU waiting while a PDF renders. The GPU should always have a page
queued.

**Exact change — `docker-compose.yml`.** Use the `--compatibility` flag with
`deploy.replicas`, OR (simpler, no flag) duplicate the service with distinct
`--name`. Recommended explicit form:

```yaml
  normalize-worker-1:
    # identical to current normalize-worker, --name normalize-worker-1
  normalize-worker-2:
    # --name normalize-worker-2
  normalize-worker-3:
    # --name normalize-worker-3

  ocr-worker-1:
    # --name ocr-worker-1
  ocr-worker-2:
    # --name ocr-worker-2
  ocr-worker-3:
    # --name ocr-worker-3
```

Keep `llm-worker` at **1 replica** (more would just queue on the GPU).
Keep `postprocess-worker` at 1 (raise to 2 only if postprocess lag appears
in logs).

**Sizing rationale:** normalize+ocr together must out-pace 240 pages/hour so
the GPU never starves. 3+3 on an i9 (multi-core, PDFium releases GIL,
PaddleOCR is 3-thread CPU) comfortably exceeds that.

**Verify:**
1. `docker compose up -d` then watch llm-worker log — `Claimed job` lines
   for the `llm` stage should be near-continuous (gaps < a few seconds).
2. In the pipeline log, normalize/ocr durations should no longer appear on
   the critical path between consecutive LLM calls.
3. Confirm no duplicate processing: each `extraction_id` appears once per
   stage (guaranteed by `jobs_active_idx`, but eyeball the first run).

**Rollback:** scale services back to 1 / remove the extra service blocks and
`docker compose up -d`. Stateless — no data migration, instant.

**Risk:** Low. Pure process count change. Connection math: see Step 3.

---

### STEP 2 — Per-customer fair queueing (do second; medium risk; needs test)

**Problem:** `claim_job` orders strictly `priority ASC, created_at ASC`
(`db.py:2189`). Customer A uploads 500 pages, Customer B uploads 5 → B waits
behind all 500 (~30 min). Classic multi-tenant head-of-line blocking.

**Design (no schema change required):** set the `priority` column at
enqueue time so the queue interleaves customers instead of draining one.

Two viable algorithms — pick one:

**Option 2a — Round-robin priority stamp (simplest, recommended):**
At `ensure_job` time, compute priority = count of currently queued+running
jobs already belonging to that customer. A customer's 1st pending page gets
priority 0, 2nd gets 1, … Customers interleave naturally; bulk uploads sink
behind every other customer's first item.

```sql
-- conceptual, computed in ensure_job before INSERT
priority := (
  SELECT COUNT(*) FROM jobs j
  JOIN extractions e ON e.id = j.extraction_id
  JOIN vendors v ON v.id = e.vendor_id
  WHERE v.user_id = :this_customer
    AND j.status IN ('queued','running')
)
```

**Option 2b — Deficit/weighted fair queueing:** per-customer token budget
refilled over time. More correct under sustained load, more code. Defer
unless 2a proves insufficient.

**Where to change (plan, not applied):**
- `db.ensure_job` (`db.py:2166`): accept/compute `priority` from the
  customer's current backlog instead of the fixed default `100`.
- `claim_job` (`db.py:2180`): no change — it already honors `priority ASC`.
- Need `user_id` reachable from a job: join `jobs → extractions → vendors`
  (`vendors.user_id` exists from the auth migration). Confirm every
  enqueued extraction has a resolvable `vendor_id` before relying on this.

**Verify (must test before prod):**
1. Seed test: Customer A uploads 50-page batch; 5 s later Customer B uploads
   2 pages. Expected: B's 2 pages complete within ~2-3 LLM cycles, not after
   A's 50.
2. Confirm no starvation of A: A still drains fully, just interleaved.
3. Confirm single-customer case is unchanged (priority degrades to FIFO when
   only one customer is active).
4. Regression: existing tests in `tests/` still green
   (`.venv/Scripts/python.exe -m pytest tests/`).

**Rollback:** revert `ensure_job` to fixed `priority=100`. In-flight jobs
already stamped will still drain correctly (priority is only an ordering
hint). No data cleanup needed.

**Risk:** Medium. Changes queue ordering globally. Do NOT deploy during demo
week. Requires the seed test above to pass first.

---

### STEP 3 — Database connection ceiling (do before >8 total workers)

**Math:**
```
Today:        4 workers × max_size 10 = 40 conns  (Postgres default 100) OK
After Step 1: (3+3+1+1) × 10           = 80 conns  near limit
+ api server pool + headroom           → exceeds 100 under load
```

**Plan — two parts:**

1. **Immediate (no new infra):** lower per-worker `max_size` for CPU stages.
   normalize/ocr workers do little concurrent DB work (mostly object store +
   one LLM/OCR call). `max_size=4` for those is ample.
   - Change site: `db.create_pool` (`db.py:19`) — make `min_size/max_size`
     parameters, pass smaller values for CPU-stage workers via env.
   - Recomputed: (3+3)×4 + 1×10 + 1×10 + api×10 ≈ 54 conns. Safe.

2. **Before scaling past ~8 workers or adding GPU #2:** introduce
   **pgbouncer** (transaction pooling mode) as a sidecar in
   docker-compose; point all `DATABASE_URL`s at pgbouncer:6432. App code
   unchanged. Also raise Postgres `max_connections` to 200 as belt-and-braces.

**Verify:** under a full 2,500-page batch, monitor
`SELECT count(*) FROM pg_stat_activity;` — must stay well under
`max_connections`. No `too many clients` errors in any worker log.

**Rollback:** restore default pool sizes / remove pgbouncer and point
`DATABASE_URL` back at postgres:5432. Stateless.

**Risk:** Low for part 1, Medium for pgbouncer (new infra component — test
transaction-mode compatibility with asyncpg; prepared statements need
`statement_cache_size=0` on asyncpg under pgbouncer transaction mode —
note this explicitly when implementing).

---

### STEP 4 — Add a second GPU (the only true throughput multiplier)

Everything above keeps the GPU fed and fair. **Only this raises the 240
pages/hour ceiling.**

**Plan:**
1. Second GPU in the host (or a second host). Run a second llama-server
   instance on port 8002 with the **same flags already finalized**
   (ctx 4096, image-max-tokens 768, etc. — see `bottleneck/performance_notes.md`).
2. Run a second `llm-worker` with `LLM_URL=http://host:8002/...`.
3. Distribution: the Postgres queue already load-balances naturally — two
   llm-workers each `claim_job` independently via `SKIP LOCKED`. **No load
   balancer needed.** Worker A pulls a job → GPU A; Worker B pulls the next
   → GPU B.
4. Throughput → ~480 pages/hour. 2,500-page batch ≈ 5.2 h.

**Alternative:** single RTX 4090 (24GB) running `--parallel 2`/`4` — true
concurrent decode on one card (model + mmproj + N KV contexts fit in 24GB).
2-4× on one device, simpler than multi-GPU.

**Verify:** both llm-workers show interleaved `Claimed job` lines; total
completed pages/hour ≈ 2× single-GPU baseline; no VRAM OOM in either
llama-server log.

**Rollback:** stop GPU #2 llama-server + its llm-worker. Queue continues on
GPU #1 with zero data impact.

**Risk:** Hardware/cost. Zero software risk — the queue design already
supports this with no code change.

---

### STEP 5 — Shorten LLM output (code-only, ~15-25% per page)

Generation tokens dominate per-page time (946-token pages = 26 s). Independent
of GPU count; multiplies every step above.

**Plan (code change, not applied — separate from this doc's scope):**
- Switch the user-message JSON template to compact `json.dumps(...)` (drop
  `indent=2`) — ~20-40 fewer prompt tokens, zero accuracy impact.
  **Keep the `<header_fields>`/`<line_item_columns>` lists** — they aid
  accuracy (confirmed with user; do not remove).
- Instruct the model to omit null fields in line items where the schema
  allows, reducing completion tokens on sparse rows.
- Cap `LLM_MAX_TOKENS_FIELDS` to the observed P99 (~1000) instead of 6000 so
  a runaway generation can't stall a worker for the full timeout.

**Verify:** re-run a representative document set; compare avg completion
tokens and total ms vs `bottleneck/performance_notes.md` baseline. Confirm
field accuracy unchanged on a labeled sample.

**Risk:** Low-medium. Output-format changes can affect parsing — validate
the JSON-repair fallback path still covers the new shape.

---

## Part 3 — Sequencing & Decision Guide

```
Now (software-only, no hardware):
  Step 1  → keeps GPU fed              (low risk, do first)
  Step 3a → shrink CPU-worker pools    (low risk, pairs with Step 1)
  Step 2  → fair queueing              (medium risk, test first, NOT demo week)
  Step 5  → shorter output             (separate code task, ~20% gain)

When interactive latency is required at this volume:
  Step 4  → second GPU / 4090          (hardware; the only real ceiling lift)
  Step 3b → pgbouncer                  (required before/with Step 4 scale-out)
```

**Decision rule:**
- If 2,500 pages as an **overnight batch** is acceptable → Steps 1, 3a, 5
  only. Single GPU is enough (~10 h, or ~8 h with Step 5).
- If customers expect **same-hour** turnaround → Step 4 is mandatory; no
  software-only path removes single-GPU serialization.

---

## Part 4 — What must NOT change

- Sequential page processing within a document (`extractor.py:552`). Page 1
  bbox output is a hard dependency; 2 concurrent pages also won't fit 8GB.
- The `FOR UPDATE SKIP LOCKED` + `jobs_active_idx` queue design — it is
  already correct and scale-ready.
- Per-client isolation via `vendors.user_id` — already present; Step 2
  builds on it, does not replace it.
- The finalized llama-server flags (`bottleneck/performance_notes.md`) — any
  GPU added must reuse them exactly.

---

## Appendix — Verification Commands

```bash
# Queue depth by stage
psql "$DATABASE_URL" -c \
 "SELECT job_type,status,count(*) FROM jobs GROUP BY 1,2 ORDER BY 1,2;"

# Live DB connection count vs limit
psql "$DATABASE_URL" -c \
 "SELECT count(*) FROM pg_stat_activity; SHOW max_connections;"

# Per-customer backlog (validates Step 2 fairness)
psql "$DATABASE_URL" -c \
 "SELECT v.user_id, count(*) FROM jobs j
  JOIN extractions e ON e.id=j.extraction_id
  JOIN vendors v ON v.id=e.vendor_id
  WHERE j.status IN ('queued','running') GROUP BY 1 ORDER BY 2 DESC;"

# Throughput sample (pages completed per hour)
psql "$DATABASE_URL" -c \
 "SELECT date_trunc('hour',updated_at) h, count(*)
  FROM jobs WHERE job_type='llm' AND status='done'
  GROUP BY 1 ORDER BY 1 DESC LIMIT 12;"

# Regression suite before deploying Step 2
.venv/Scripts/python.exe -m pytest tests/
```
