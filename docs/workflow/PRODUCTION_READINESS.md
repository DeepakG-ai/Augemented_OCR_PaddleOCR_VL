# Production Readiness — Scorecard, Target Structure & Engineering Plan

> Honest assessment of the codebase for go-live, the **target** folder
> structure a staff engineer would refactor toward, and the production tips
> that matter most. Companion to [ARCHITECTURE.md](ARCHITECTURE.md).
>
> Date 2026-05-15. Verdict: **conditional go — ship after the 4 blockers below.**

---

## 1. Scorecard

| Dimension | Score /10 | Verdict |
|---|---|---|
| Architecture & design | 8 | Durable `SKIP LOCKED` queue, clean stage split, coherent isolation — keep as-is |
| Test coverage | 7 | Broad mocked suite; gaps on the exact critical bugs |
| Observability / ops | 6 | Tracing + structured logs good; runtime DDL on boot is the weak point |
| Security | 5 | JWT/bcrypt fine; SECRET_KEY default, fail-open quota, gameable billing |
| Correctness / reliability | 4 | Confirmed money + crash bugs |
| Code structure / maintainability | 4 | Two ~2,900-line god files; flat backend |
| Repo hygiene | 3 | Committed DB binary, loose backups, scratch dirs in root |
| **Overall** | **6** | **Strong bones, MVP body. Not go-live until §2 is closed.** |

**Do not rebuild the architecture.** Every problem below is a localized fix or a
mechanical move. The expensive part (durable pipeline + multi-tenant isolation)
is already correct.

---

## 2. Go-live blockers (must fix — small, well-scoped)

| # | Blocker | Where | Fix | Risk if shipped |
|---|---|---|---|---|
| 1 | Quota **fails open** on DB error | ingest quota check | wrap → on error **reject** (fail closed) | clients exceed paid limits silently |
| 2 | Billing leak via delete | `llm_usage.extraction_id ON DELETE SET NULL` | append-only `billing_ledger`: freeze billable pages at extraction `done`; bill from ledger, not live count | clients delete history to lower bills |
| 3 | `SECRET_KEY` empty default | `auth.py` | hard-fail at **startup** if unset (not just at first sign) | forged tokens if ever deployed unset |
| 4 | LLM parse crash | LLM-response handling | guard missing `choices`/malformed JSON → mark page failed, not crash worker | one bad model response kills a job |

Each is < ~1 day. None touches the queue, the pipeline shape, or the isolation
model. After these: **7.5–8/10, shippable.**

### Should-fix soon (not blockers, but schedule them)
- Cascade or soft-delete on vendor delete (orphan aliases/spatial_memory/boxes).
- `unverified` SSE state must be terminal (UI spinner currently hangs).
- Template save validation (reject empty header/line fields).
- Replace runtime `db.init()` DDL with **Alembic** migrations (see §4).

---

## 3. Target folder structure (the refactor goal)

The current `backend/` is 23 flat modules with two 2,900-line god files. Move
toward a layered package. **This is a mechanical, behavior-preserving refactor —
do it after the demo, one slice at a time, tests green between each move.**

```
Augmented_OCR_PaddleOCR_VL/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI app factory + router include ONLY (~150 LOC)
│   │   ├── api/                    # split from today's 2,917-line main.py
│   │   │   ├── deps.py             # shared Depends (current_user, pool)
│   │   │   ├── auth.py             # /auth/*
│   │   │   ├── vendors.py          # /vendors/*  /templates/*
│   │   │   ├── extractions.py      # /ingest/*  /extractions/*  /jobs/* + SSE
│   │   │   ├── review.py           # /extractions/{id}/review
│   │   │   ├── dashboard.py        # /dashboard  /usage
│   │   │   ├── admin.py            # /admin/*
│   │   │   ├── schedules.py        # /schedules/*
│   │   │   └── health.py           # /health  /ready
│   │   ├── core/
│   │   │   ├── config.py           # env → typed, validated at import; SECRET_KEY required
│   │   │   ├── security.py         # JWT, bcrypt, ownership asserts (today's auth.py)
│   │   │   ├── logging.py          # plog
│   │   │   └── tracing.py          # mlflow spans
│   │   ├── db/
│   │   │   ├── pool.py             # asyncpg pool only
│   │   │   ├── repositories/       # split from today's 2,937-line db.py
│   │   │   │   ├── vendors.py  templates.py  extractions.py
│   │   │   │   ├── jobs.py      usage.py      schedules.py
│   │   │   │   ├── spatial_memory.py  layout_boxes.py  users.py
│   │   │   └── schema.sql          # canonical DDL (no longer run at boot)
│   │   ├── pipeline/
│   │   │   ├── runner.py           # run_worker() poll loop + stale recovery
│   │   │   └── stages/
│   │   │       ├── normalize.py  ocr.py  llm.py  postprocess.py
│   │   ├── services/               # domain logic, no HTTP/SQL
│   │   │   ├── vendor_detector.py  extractor.py  geometry.py
│   │   │   ├── ocr_runner.py  processor.py  pdf_extractor.py
│   │   │   ├── spatial_memory.py   qwen_layout_apply.py
│   │   │   ├── layout_key.py  contracts.py  page_logger.py
│   │   ├── integrations/
│   │   │   ├── object_store.py     # MinIO + local fallback
│   │   │   ├── cache.py            # Redis
│   │   │   ├── llm_qwen.py         # llama.cpp client (today's inline calls)
│   │   │   └── llm_gemini.py       # OPTIONAL adapter — same JSON contract
│   │   └── models/                 # pydantic request/response
│   ├── alembic/                    # versioned migrations (replaces boot DDL)
│   └── tests/                      # mirror app/ layout
├── frontend/                       # modularize the 50KB+ JS files later (low prio)
├── ops/
│   ├── docker/  docker-compose.yml  Dockerfile
│   └── scripts/                    # vendor_renumber.sql etc. live HERE, gitignored data
├── docs/
└── research/                       # bbox_vis_fixing/ gemini/ bottleneck/ outputs/
                                    #   — OUT of repo root, gitignored or own repo
```

### Why this shape
- **`api/` split:** a 2,917-line `main.py` is unreviewable and a merge-conflict
  magnet. One router per resource = parallel work, smaller diffs.
- **`db/repositories/`:** SQL stays out of HTTP and worker code (today's rule)
  but a single 2,937-line module hides the data model. One file per aggregate.
- **`services/` has no HTTP or SQL:** makes the domain unit-testable without
  FastAPI or a DB — you already mock these, this makes it natural.
- **`integrations/llm_*`:** the Qwen-vs-Gemini decision becomes a one-line
  factory swap behind a shared interface, not a code archaeology project.
- **`research/` out of root:** `bbox_vis_fixing/`, `gemini/`, `bottleneck/`,
  `outputs/`, `audit/` are scratch — they inflate the repo, confuse onboarding,
  and (`mlflow.db`) bloat git. They are not the product.

### Refactor order (each step independently shippable, tests green)
1. `research/` move + `.gitignore` the scratch dirs and `mlflow.db`. *(1 hr, zero risk)*
2. Split `main.py` → `api/` routers (no logic change, just move + `include_router`).
3. Split `db.py` → `db/repositories/` (move functions, keep signatures).
4. Introduce **Alembic**; freeze current schema as the baseline migration; delete boot-time DDL.
5. `pipeline/stages/` split of `worker.py`.
6. `integrations/llm_qwen.py` + `llm_gemini.py` behind one interface.

Steps 1–2 are pure win, near-zero risk. Step 4 is the highest-value
correctness/ops improvement (see §4). **None of this during demo week.**

---

## 4. Production engineering tips (highest leverage first)

1. **Stop running DDL on every startup.** `db.init()` doing
   `CREATE TABLE / ALTER` idempotently on each boot is convenient in dev and a
   liability in prod: no version history, no rollback, a race if two instances
   boot together, and schema drift you can't audit. Adopt **Alembic**. Baseline
   = current schema. From then on, every change is a reviewed, versioned,
   reversible migration run as a deliberate deploy step — not a side effect of
   `uvicorn` starting.

2. **Fail closed on anything touching money or auth.** Quota check, billing
   count, token verification: on error, **deny**. Failing open is invisible in
   testing and expensive in production.

3. **Make billing append-only.** Derived-from-live-rows billing is gameable by
   delete (your confirmed bug). Write an immutable `billing_ledger` row when an
   extraction reaches `done`; bill from the ledger; deletes never touch it.

4. **Config validated at import, secrets required.** One `core/config.py` that
   parses and validates all env on startup and refuses to boot if `SECRET_KEY`,
   `DATABASE_URL`, `LLM_URL` are missing/empty. Fail at deploy, not at the first
   user request.

5. **Add `/ready` vs `/health`.** `/health` = process alive. `/ready` = pool
   acquired + Redis reachable + (optionally) LLM endpoint reachable. Load
   balancers and `docker-compose healthcheck` should gate on `/ready`.

6. **CI gate before merge.** `pytest tests/` + a lint (ruff) + `alembic upgrade
   head` on a throwaway DB. No green, no merge. You already have the test
   suite — wire it to the branch.

7. **Get binaries and scratch out of git.** `mlflow.db` (currently committed
   and dirty), `outputs/`, `*.sql` backups, `.pip_tmp/`, the `=` file. They
   bloat clones and leak run data. `.gitignore` + `git rm --cached`.

8. **One LLM interface, two backends.** `extract(pages, prompt) -> JSON` with a
   `QwenClient` and a `GeminiClient` implementation. Model choice = config, not
   a code change. Keeps the Qwen3-VL-primary / Gemini-3-Flash-fallback strategy
   a deploy decision (see ARCHITECTURE.md §5.3a).

9. **Per-customer fair queueing before multi-tenant load** (`scaling.md`
   Step 2). Not a blocker for go-live with few clients; becomes one the day two
   clients submit large batches simultaneously.

10. **Backups & retention.** Postgres PITR (or daily dump), object-store
    lifecycle policy for page images, and a defined retention for `llm_usage`
    (it's your billing audit trail — never auto-purge it).

---

## 5. The one-paragraph answer to "is it good to go?"

The architecture is production-grade and should not be rebuilt. The code
*structure* and a handful of *correctness/security* defects are not yet
production-grade. Close the four §2 blockers (each < 1 day, none architectural)
and you can ship to a small, controlled set of clients at a defensible 7.5–8/10.
Do the §3 refactor and §4 tips over the following weeks — after the demo, never
during it — to reach a sustainable 8.5+ that a growing team can safely build on.
