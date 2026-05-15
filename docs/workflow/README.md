# Augmented OCR — Workflow Documentation

This folder is a line-by-line study guide for every major workflow in the Augmented OCR system. Each file walks the actual logic in code, including:
- What every function does
- Why it exists
- How data flows in and out
- How multi-tenant **isolation** is enforced (vendor → user_id chain)

---

## Start here (production architecture)

- **[architecture.html](architecture.html)** — visual architect deliverable.
  Open in a browser; live Mermaid: system context, container view, ingest
  sequence, pipeline state machine, ER model, tenancy, scheduler concurrency,
  scaling.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the single written reference: repo
  structure, invariants, end-to-end workflow, metering/billing, scheduler
  concurrency ("two clients at 10 PM"), edge cases, test map, scaling roadmap,
  guardrails.

The files below are the deeper per-workflow study guides.

---

## Reading order

If you're new to the codebase, follow this order. Each file builds on the previous.

1. **[isolation.md](isolation.md)** — The single most important concept. Every client owns vendors; every other resource chains back to a vendor. Read this first.
2. **[db.md](db.md)** — Schema. Which tables exist, what columns, which migrations.
3. **[auth.md](auth.md)** — JWT, password hashing, `get_current_user`, `assert_*_access` helpers.
4. **[login.md](login.md)** — Frontend login page + `/auth/login` endpoint, full request flow.
5. **[frontend.md](frontend.md)** — SPA router, `apiFetch`, auth guard, state model.
6. **[vendor_detection.md](vendor_detection.md)** — How a document becomes a vendor (alias + fuzzy matching), and how the user_id filter prevents leakage.
7. **[extraction.md](extraction.md)** — The 4-stage pipeline (`/ingest/ui` → normalize → ocr → llm → postprocess).
8. **[workers.md](workers.md)** — Worker mechanics: claim, retry, stale recovery, SSE streaming.
9. **[bbox_agent.md](bbox_agent.md)** — The two-agent split (BBox Agent + Fields Agent) and why it exists.
10. **[spatial_memory.md](spatial_memory.md)** — How manual corrections become durable geometry memory.
11. **[review.md](review.md)** — Review page, drag-box corrections, save flow.

---

## Codebase map (cross-reference)

### Backend (Python, FastAPI + asyncpg)

| File | Workflow doc |
|---|---|
| `backend/main.py` | [auth.md](auth.md), [extraction.md](extraction.md), [login.md](login.md) |
| `backend/auth.py` | [auth.md](auth.md) |
| `backend/db.py` | [db.md](db.md) |
| `backend/worker.py` | [workers.md](workers.md), [extraction.md](extraction.md) |
| `backend/extractor.py` | [extraction.md](extraction.md) (LLM call + prompt building) |
| `backend/bbox_agent.py` | [bbox_agent.md](bbox_agent.md) |
| `backend/qwen_layout_apply.py` | [bbox_agent.md](bbox_agent.md) (apply-side) |
| `backend/spatial_memory.py` | [spatial_memory.md](spatial_memory.md) |
| `backend/vendor_detector.py` | [vendor_detection.md](vendor_detection.md) |
| `backend/geometry.py` | [extraction.md](extraction.md) (digital vs scanned classification) |
| `backend/ocr_runner.py` | [extraction.md](extraction.md) (PaddleOCR) |
| `backend/processor.py` | [extraction.md](extraction.md) (PDF → images) |

### Frontend (vanilla JS SPA)

| File | Workflow doc |
|---|---|
| `frontend/login.js` | [login.md](login.md) |
| `frontend/core.js` | [frontend.md](frontend.md) (router, apiFetch, auth guard) |
| `frontend/vendors.js` | [frontend.md](frontend.md) (vendor + template pages) |
| `frontend/extract.js` | [extraction.md](extraction.md) (UI side: SSE, pipeline viz) |
| `frontend/review.js` | [review.md](review.md) |

---

## The one-line model

> **Users own vendors. Everything else chains to a vendor. Admin sees everything.**

```
users.id  ──owns──>  vendors.user_id
                       │
                       ├── templates.vendor_id
                       ├── vendor_aliases.vendor_id
                       ├── spatial_memory.vendor_id
                       ├── gold_examples.vendor_id
                       └── extractions.vendor_id ─── pages
                                                  ├── jobs
                                                  ├── reviews
                                                  ├── llm_usage
                                                  └── deliveries
```

Every API route either:
1. **Filters by user_id** (list routes: `/vendors`, `/extractions`, `/templates`)
2. **Asserts ownership** via `assert_vendor_access` / `assert_extraction_access` / `assert_job_access` / `assert_alias_access`
3. **Is admin-only** (`require_admin`: `/admin/users`, `/admin/stats`, `/admin/usage`)
4. **Is public** (`/health`)

That is the entire isolation contract.
