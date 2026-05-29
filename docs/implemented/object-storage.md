# Object Storage

## What it is

The object store holds every binary file the pipeline produces or consumes: original uploaded PDFs and the rendered page images made from them. It has two backends — MinIO when available, and a plain folder on disk when it is not. **No code change is needed to switch between them.** The backend is picked once at startup and stays fixed for the lifetime of the process. Everything else in the system (workers, API routes) calls the same three methods (`put_bytes`, `get_bytes`, `delete_object`) regardless of which backend is running.

---

## Key States

| State | When it happens | What the system uses |
|---|---|---|
| **MinIO mode** | MinIO is reachable when `get_store()` is first called | Real MinIO client; objects stored in MinIO buckets |
| **Local fallback mode** | MinIO is unreachable at startup, or the `minio` package is not installed | `.local_object_store/` on disk; identical API surface |

The decision is made exactly once. If MinIO goes down after startup, calls will throw `StorageUnavailableError` — the system does not silently fall back mid-run.

---

## How it works

### Startup

```
Process starts
  → get_store() called (lru_cache — runs only once per process)
      → try Minio(endpoint, access_key, secret_key, secure)
          success → self.client = Minio instance
          failure → self.client = None  (local fallback)
      → ensure_buckets()
          MinIO mode  → create "augocr-documents" and "augocr-artifacts" if missing
          Local mode  → create .local_object_store/augocr-documents/ and .../augocr-artifacts/ on disk
```

### Two buckets

| Bucket | Default name | What goes in it |
|---|---|---|
| **documents** | `augocr-documents` | Original uploaded PDFs — one file per uploaded document |
| **artifacts** | `augocr-artifacts` | Rendered page images (JPEG) — one per page per extraction |

Bucket names are configurable via `MINIO_DOCUMENTS_BUCKET` and `MINIO_ARTIFACTS_BUCKET` env vars.

### Object key naming

Every object has a deterministic, human-readable key:

```
documents/{vendor_id}/{uuid}_{original_filename}
    e.g.  documents/42/a3f8c1d2e5b6_invoice_march.pdf

extractions/{extraction_id}/pages/page_{page_number}.jpg
    e.g.  extractions/17/pages/page_1.jpg
              extractions/17/pages/page_2.jpg
```

The key is stored in the `documents.object_key` and `pages.object_key` columns in Postgres. The object store holds the bytes; Postgres holds the pointer.

### Who reads and writes what

```
Upload (POST /ingest/ui  or  /ingest/rest)
  → put_bytes(DOCUMENTS, "documents/{vendor_id}/{uuid}_{name}", pdf_bytes)
  → object_key saved to documents table

Normalize worker
  → get_bytes(DOCUMENTS, document.object_key)          ← download original PDF
  → render all pages to JPEG
  → put_bytes(ARTIFACTS, "extractions/{id}/pages/page_N.jpg", jpeg_bytes)
  → object_key saved to pages table

LLM worker
  → get_bytes(ARTIFACTS, page.object_key)              ← read page image for Qwen3-VL

API  GET /extractions/{id}/pages
  → get_bytes(ARTIFACTS, page.object_key)              ← serve thumbnail to browser UI

DELETE /extractions/{id}
  → delete_object(ARTIFACTS, page.object_key)          ← remove each page image
  → delete_object(DOCUMENTS, document.object_key)      ← remove original PDF
```

### Security — path traversal guard

Every `put_bytes`, `get_bytes`, and `delete_object` call runs `_validate_object_key` before touching anything:

- Empty key → rejected
- Key contains `..` → rejected
- Key starts with `/` → rejected
- Key contains `\` → rejected

In local fallback mode a second check resolves the absolute path and verifies it is still inside `_local_root` — defence in depth against any edge case the first check missed.

---

## Rules

- `get_store()` is an `@lru_cache(maxsize=1)` singleton. The same `ObjectStore` instance is reused by every worker and API call in the process.
- MinIO mode vs local mode is determined at first call to `get_store()`. It never changes mid-run.
- `delete_object` is idempotent. A missing key is silently ignored — no error raised.
- `ensure_buckets()` is called on startup. Workers and routes never call it manually.
- The system stores only the key in Postgres, never the bytes. Bytes live exclusively in the store.
- Object keys must use forward slashes. Backslashes are rejected by `_validate_object_key`.
- `MINIO_SECURE` must be `true` in production. Default is `false` (HTTP) for local dev only.

---

## All Scenarios

**Scenario 1 — Normal MinIO upload**
User posts a PDF. `_submit_ingestion_job` calls `put_bytes(DOCUMENTS, key, data)`. Key is saved to `documents.object_key`. Normalize worker later reads it with `get_bytes`. Everything in MinIO.

**Scenario 2 — MinIO not running (local fallback)**
Server starts with no MinIO available. `ObjectStore.__init__` catches the connection error and sets `self.client = None`. `ensure_buckets` creates `.local_object_store/augocr-documents/` and `.local_object_store/augocr-artifacts/` on disk. All put/get/delete calls transparently read/write files there. No config change. No code change.

**Scenario 3 — MinIO goes down after startup**
`get_store()` already returned a MinIO-mode instance. A later `put_bytes` call fails inside `self.client.put_object(...)`. The exception is wrapped in `StorageUnavailableError` and propagates. The upload fails with HTTP 500. The system does not silently switch to local fallback.

**Scenario 4 — Object not found**
Normalize worker crashed mid-run. Page image was never written. LLM worker calls `get_bytes(ARTIFACTS, key)`. MinIO returns `NoSuchKey` → raised as `ObjectNotFoundError`. LLM stage marks the job failed and logs the error.

**Scenario 5 — Path traversal attempt**
A crafted upload with filename `../../etc/passwd` produces key `documents/42/uuid_../../etc/passwd`. `_validate_object_key` detects `..` and raises `ValueError` before any storage call is made. Upload returns HTTP 500 (internal guard).

**Scenario 6 — Normalize writes page images**
Normalize renders a 3-page PDF into 3 JPEG images. It calls `put_bytes(ARTIFACTS, "extractions/17/pages/page_1.jpg", ...)`, then `page_2.jpg`, then `page_3.jpg`. The object keys are saved to the `pages` table. LLM worker later reads them by querying those rows.

**Scenario 7 — Deleting an extraction**
Admin deletes extraction 17. Code collects all `pages.object_key` values, loops over them calling `delete_object(ARTIFACTS, key)` for each. Then calls `delete_object(DOCUMENTS, document.object_key)`. If any page key is already missing (e.g. a previous partial delete), `delete_object` returns silently — no error.

**Scenario 8 — Two workers reading the same page image**
LLM worker and a concurrent API thumbnail request both call `get_bytes(ARTIFACTS, "extractions/17/pages/page_1.jpg")` at the same time. Object stores are read-safe under concurrency — both calls succeed independently. No locking needed for reads.

**Scenario 9 — Switch from local fallback to MinIO**
Developer sets `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` and restarts the server. On next `get_store()` call MinIO connects successfully. All new objects go to MinIO. Old objects that were in `.local_object_store/` are now unreachable by the running server. Clean start (or manual migration) is needed if old extractions must be preserved.

**Scenario 10 — Upload rejected before storage write**
Template check in `_submit_ingestion_job` fails (no template found). `put_bytes` is never called. `create_document` is never called. No objects written. No DB rows created. The failure is fully clean.

---

## Error Responses

| Situation | Exception | HTTP result |
|---|---|---|
| MinIO unreachable at startup | logs warning, falls back to local | No HTTP error — transparent |
| MinIO unavailable mid-run | `StorageUnavailableError` | 500 Internal Server Error |
| Object key not found | `ObjectNotFoundError` | 500 (pipeline fails, job marked failed) |
| Object store access denied | `StoragePermissionError` | 500 Internal Server Error |
| Path traversal in key | `ValueError` | 500 Internal Server Error |
| Upload before template exists | `HTTPException(400)` | `{"error": {"code": "...", "message": "..."}}` |

---

## Test Coverage

| Test | File | What it proves |
|---|---|---|
| `test_submit_ingestion_job_blocks_no_template_without_writing_to_db` | `test_ingest_hardening.py` | `put_bytes` is NOT called when the template check fails — no orphan objects |
| `test_submit_ingestion_job_blocks_no_fields_without_writing_to_db` | `test_ingest_hardening.py` | `put_bytes` is NOT called when the template has no fields — no orphan objects |
| `FakeObjectStore` integration tests | `test_pipeline_integration_flow.py` | Full normalize → ocr → llm → postprocess flow uses an in-memory store; proves object keys flow correctly through all stages |
| `store = SimpleNamespace(get_bytes=..., put_bytes=...)` | `test_pipeline_hardening.py` | Normalize stage mock verifies `get_bytes` called for the PDF and `put_bytes` called for each rendered page |

No tests for `_validate_object_key` directly — this is a gap. Path traversal guard should be added to `test_infrastructure_safety.py`.

---

## Quick Reference

| Question | Answer |
|---|---|
| Where are uploaded PDFs stored? | `augocr-documents` bucket, key = `documents/{vendor_id}/{uuid}_{filename}` |
| Where are page images stored? | `augocr-artifacts` bucket, key = `extractions/{id}/pages/page_N.jpg` |
| How do I run without MinIO? | Just don't start MinIO. Server falls back to `.local_object_store/` automatically |
| How do I change the fallback folder? | Set `LOCAL_OBJECT_STORE_DIR` env var |
| How do I change bucket names? | Set `MINIO_DOCUMENTS_BUCKET` and `MINIO_ARTIFACTS_BUCKET` env vars |
| Does switching backends lose existing data? | Yes — objects in local fallback are not visible to MinIO and vice versa |
| Is `delete_object` safe to call twice? | Yes — missing key is silently ignored |
| Can two workers read the same file simultaneously? | Yes — reads are safe under concurrency, no locking |
| How is the singleton shared across workers? | Each worker is a separate process; `get_store()` creates one `ObjectStore` per process |
| Is HTTPS used? | Only if `MINIO_SECURE=true`. Default is HTTP for local dev. Always set true in production. |
