# Database Schema & Query Layer

> Source file: [backend/db.py](../../backend/db.py)
>
> Why this file matters: there is **no ORM**. Every query is hand-written SQL. If you want to know what data is stored or how it's read/written, this is the only file to consult.

---

## Architecture

- **asyncpg** connection pool, min 2 / max 10 connections, 30-second command timeout.
- Schema declared in a single `_SCHEMA_SQL` string at module top.
- Migrations live in `init()` as idempotent `DO $$ IF NOT EXISTS ... $$` blocks. Safe to run on every startup.
- JSONB everywhere data is shape-flexible (results, page outputs, normalized boxes).
- All datetime columns use `TIMESTAMPTZ` (UTC) — never naive timestamps.

```python
async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
```

---

## Tables (in dependency order)

### `users` — authentication
```sql
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    hashed_pw   TEXT NOT NULL,            -- bcrypt $2b$... string
    role        TEXT NOT NULL DEFAULT 'client',  -- 'admin' | 'client'
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
-- Migration adds: CHECK (role IN ('admin', 'client')) — constraint name users_role_check
```

- **`gen_random_uuid()`** requires the `pgcrypto` extension (Postgres ≥ 13). Container image already has it.
- **`role`** is the only authorization signal; admin bypasses every isolation check (see [auth.md](auth.md)).
- **`is_active = FALSE`** soft-deletes a user — login fails, existing tokens become invalid on next request (because `get_current_user` re-checks `is_active`).
- **`hashed_pw`** never appears in any API response (see `db.create_user` returns).

### `vendors` — the tenant boundary
```sql
CREATE TABLE IF NOT EXISTS vendors (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT DEFAULT 'idle',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
-- Migration adds:
ALTER TABLE vendors ADD COLUMN user_id UUID REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX vendors_user_id_idx ON vendors (user_id);
```

- **`id` is TEXT, not UUID** — clients pick the ID (e.g. `ACME001`), so it's free-form. Keeps URLs human-readable: `/vendors/ACME001/template`.
- **`user_id` ON DELETE SET NULL** — deleting a user does not cascade-delete their vendors. Their vendors become unowned (legacy state) until an admin reassigns or deletes them. This is intentional — accidentally deleting a user shouldn't lose all their extraction history.
- **`vendors_user_id_idx`** — supports the `WHERE user_id = $1` filter on `list_vendors` without a sequential scan.

### `templates` — extraction config per vendor
```sql
CREATE TABLE IF NOT EXISTS templates (
    id                  SERIAL PRIMARY KEY,
    vendor_id           TEXT REFERENCES vendors(id) ON DELETE CASCADE,
    format_type         TEXT NOT NULL,
    header_fields       JSONB NOT NULL DEFAULT '[]'::jsonb,
    line_item_fields    JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_instructions TEXT,
    extraction_rules    JSONB DEFAULT '[]'::jsonb,
    system_prompt       TEXT,             -- legacy cached prompt; no longer authoritative
    prompt_hash         TEXT,             -- sha256 of (fields + rules + format + version)
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id)                     -- one template per vendor
);
```

- **`UNIQUE(vendor_id)`** enforces one template per vendor. `upsert_template` uses `ON CONFLICT (vendor_id) DO UPDATE`.
- **`format_type`** is one of: `single_po_multipage`, `po_per_page`, `single_page`. Determines how `extractor.py` builds prompts and merges results.
- **`header_fields` / `line_item_fields`** are JSONB arrays of strings (snake_case field names). `["po_number", "vendor_name", "ship_to"]`.
- **`system_prompt`** column still exists for legacy reads but per project convention, prompts are **rebuilt fresh from current fields on every extraction** — no Redis cache, no DB cache trust. See [extraction.md](extraction.md) for why.

### `documents` — uploaded files
```sql
CREATE TABLE IF NOT EXISTS documents (
    id            SERIAL PRIMARY KEY,
    vendor_id     TEXT REFERENCES vendors(id),    -- NO cascade — preserves history if vendor deleted
    source_type   TEXT NOT NULL DEFAULT 'ui',
    source_ref    TEXT,
    filename      TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    size_bytes    BIGINT,
    object_key    TEXT,                            -- MinIO key
    metadata      JSONB,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued, processing, normalized, failed
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);
```

- **No CASCADE on `vendor_id`** — a vendor delete doesn't cascade to documents directly; the cascade goes `vendors → extractions (CASCADE) → documents (CASCADE)`. But `documents` itself is referenced from `extractions`, so deleting a doc requires deleting its extractions first. The `delete_vendor_cascade` helper handles ordering.
- **`object_key`** points into MinIO bucket `documents` (or local FS fallback in `.local_object_store/`).

### `extractions` — the central long-lived record
```sql
CREATE TABLE IF NOT EXISTS extractions (
    id                  SERIAL PRIMARY KEY,
    document_id         INT REFERENCES documents(id) ON DELETE CASCADE,
    vendor_id           TEXT REFERENCES vendors(id),
    template_id         INT  REFERENCES templates(id),
    filename            TEXT,
    total_pages         INT,
    format_type         TEXT,
    header_fields       JSONB,           -- snapshot at extraction time
    line_item_fields    JSONB,
    result              JSONB,           -- final extraction result (after merge)
    page_results        JSONB,           -- per-page raw LLM outputs (with _page tags)
    field_locations     JSONB,           -- bbox mapping {field_name: {page, box, strategy}}
    ocr_data            JSONB,           -- unified per-page geometry (digital + scanned)
    corrected_result    JSONB,           -- after manual review
    correction_meta     JSONB,           -- metadata about the corrections applied
    export_object_key   TEXT,            -- legacy export key, currently unused
    progress            JSONB,           -- live progress {stage, message, page, total_pages}
    cancel_requested    BOOLEAN NOT NULL DEFAULT FALSE,
    status              TEXT DEFAULT 'pending',  -- pending|processing|done|failed|partial|cancelled
    error               TEXT,
    duration_ms         INT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
```

This is the heaviest table — JSONB columns can be megabytes. **Lightweight queries (e.g. `is_postprocess_ready`) deliberately avoid `SELECT *`** and only read `status` / metadata, because pulling `result` + `ocr_data` + `page_results` for a list view would saturate the connection pool.

- **`header_fields` / `line_item_fields` snapshot at extraction time**: even if the template later changes, the extraction record tells you exactly which fields were requested.
- **`progress`** is updated on every page tick. Drives the SSE stream — see [workers.md](workers.md).
- **`cancel_requested`** is a flag that workers poll between pages. Setting it does not abort instantly — the current LLM call must finish (or the cancel-event-race in `extractor.call_llm` aborts mid-flight).
- **`status` lifecycle**: `pending` → `processing` → (`done` | `failed` | `partial` | `cancelled`).

### `pages` — per-page artifacts and geometry
```sql
CREATE TABLE IF NOT EXISTS pages (
    id            SERIAL PRIMARY KEY,
    extraction_id INT REFERENCES extractions(id) ON DELETE CASCADE,
    page_number   INT NOT NULL,
    image_b64     TEXT,                  -- legacy column, no longer required (DROP NOT NULL via migration)
    object_key    TEXT NOT NULL,         -- MinIO key for the page image
    mime_type     TEXT DEFAULT 'image/jpeg',
    width         INT,                   -- rendered (output) dimensions in px
    height        INT,
    orig_width    INT,                   -- original PDF/image dimensions
    orig_height   INT,
    source        TEXT,                  -- 'pypdfium' (digital) or 'paddleocr' (scanned)
    char_count    INT,                   -- digital: chars from pypdfium2; scanned: NULL until OCR
    word_geometry JSONB,                 -- digital: word boxes; scanned: NULL until OCR
    UNIQUE (extraction_id, page_number)
);
```

- **`image_b64` legacy**: pages used to be stored as base64 strings in Postgres. The `ALTER TABLE pages ALTER COLUMN image_b64 DROP NOT NULL` migration moved storage to MinIO. New rows always use `object_key`.
- **`source` column**: critical for the OCR worker. If `source = 'pypdfium'`, the page already has `word_geometry` and **PaddleOCR is skipped**. If `source = 'paddleocr'` (or NULL — legacy), OCR runs.
- **`char_count`** is a quick heuristic during normalization: low char count + high page area suggests scanned, but the actual classifier in `geometry.py` uses both `source` detection and explicit fallback rules.

### `gold_examples` — human-verified corrections
```sql
CREATE TABLE IF NOT EXISTS gold_examples (
    id                SERIAL PRIMARY KEY,
    vendor_id         TEXT REFERENCES vendors(id) ON DELETE CASCADE,
    extraction_id     INT REFERENCES extractions(id) ON DELETE SET NULL,
    original_result   JSONB NOT NULL,
    corrected_result  JSONB NOT NULL,
    correction_diff   JSONB,             -- {"field_name": "corrected_value"} subset
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
```

These are the few-shot examples injected into Qwen's prompt (see `extractor.build_system_prompt`). When a user saves corrections in the review page, this table records the before/after. On subsequent extractions for the same vendor, the latest `correction_diff` becomes a positive example in the system prompt, teaching Qwen the formatting conventions specific to that vendor.

### `vendor_aliases` — alias patterns for vendor detection
```sql
CREATE TABLE IF NOT EXISTS vendor_aliases (
    id           SERIAL PRIMARY KEY,
    vendor_id    TEXT REFERENCES vendors(id) ON DELETE CASCADE,
    pattern      TEXT NOT NULL,
    weight       INT NOT NULL DEFAULT 1,
    source       TEXT DEFAULT 'manual',   -- 'manual' (UI) or 'auto' (rare/legacy)
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id, pattern)
);
CREATE INDEX vendor_aliases_pattern_idx ON vendor_aliases (pattern);
```

- **`UNIQUE(vendor_id, pattern)`** prevents duplicate aliases for the same vendor.
- The detector treats vendor names themselves as implicit aliases (see [vendor_detection.md](vendor_detection.md)) — `vendor_aliases` is purely additive.

### `spatial_memory` — durable geometry for manual corrections
```sql
CREATE TABLE IF NOT EXISTS spatial_memory (
    id                          SERIAL PRIMARY KEY,
    vendor_id                   TEXT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    layout_key                  TEXT NOT NULL,    -- "vendor_id:template_id"
    field_key                   TEXT NOT NULL,
    page_number                 INT  NOT NULL,
    normalized_box              JSONB NOT NULL,    -- {x0,y0,x1,y1} in [0..1]
    source_engine               TEXT NOT NULL,    -- 'pypdfium' or 'paddleocr'
    created_from_extraction_id  INT REFERENCES extractions(id) ON DELETE SET NULL,
    last_verified_at            TIMESTAMPTZ DEFAULT NOW(),
    is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(vendor_id, layout_key, field_key, page_number)
);
CREATE INDEX spatial_memory_lookup_idx ON spatial_memory (vendor_id, layout_key, is_active);
```

See [spatial_memory.md](spatial_memory.md) for full semantics. Critical rule: the table stores **WHERE** a value lives (geometry), never **WHAT** the value is. Reuse always reads the current document's text in the saved region.

### `qwen_layout_boxes` — BBox Agent learnings
```sql
CREATE TABLE IF NOT EXISTS qwen_layout_boxes (
    id                          SERIAL PRIMARY KEY,
    vendor_id                   TEXT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    template_id                 INT NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    field_key                   TEXT NOT NULL,
    field_type                  TEXT NOT NULL CHECK (field_type IN ('header', 'line_item_column')),
    normalized_box              JSONB NOT NULL,
    page_number                 INT NOT NULL DEFAULT 1,
    created_from_extraction_id  INT REFERENCES extractions(id) ON DELETE SET NULL,
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id, template_id, field_key)
);
```

Distinct from `spatial_memory` — this is what the **BBox Agent** (one-shot label detector) learned from page 1. It's per-template, populated by Qwen3-VL itself (no human in the loop). See [bbox_agent.md](bbox_agent.md).

### `jobs` — the durable pipeline queue
```sql
CREATE TABLE IF NOT EXISTS jobs (
    id               SERIAL PRIMARY KEY,
    extraction_id    INT REFERENCES extractions(id) ON DELETE CASCADE,
    document_id      INT REFERENCES documents(id) ON DELETE CASCADE,
    job_type         TEXT NOT NULL,    -- normalize|ocr|llm|postprocess
    status           TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    payload          JSONB,             -- arbitrary stage-specific data
    progress         JSONB,             -- live progress for SSE
    attempts         INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 3,
    priority         INT NOT NULL DEFAULT 100,
    locked_by        TEXT,              -- worker name (e.g. "worker-a3b9c1f2")
    locked_at        TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    error            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Critical: prevents duplicate active jobs for the same extraction+stage
CREATE UNIQUE INDEX jobs_active_idx
    ON jobs (extraction_id, job_type)
    WHERE status IN ('queued', 'running') AND extraction_id IS NOT NULL;
```

The `jobs_active_idx` partial unique index is what makes `ensure_job` idempotent — if the same `(extraction_id, job_type)` is already queued or running, a second insert fails with a unique-constraint violation that the helper catches. Done jobs don't block re-runs (the partial WHERE clause excludes them).

### `review_events` — audit log for corrections
```sql
CREATE TABLE IF NOT EXISTS review_events (
    id               SERIAL PRIMARY KEY,
    extraction_id    INT REFERENCES extractions(id) ON DELETE CASCADE,
    actor            TEXT NOT NULL DEFAULT 'ui',
    reason_code      TEXT NOT NULL DEFAULT 'manual_review',
    note             TEXT,
    before_result    JSONB NOT NULL,
    after_result     JSONB NOT NULL,
    before_locations JSONB,
    after_locations  JSONB,
    diff             JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
```

Append-only log. Every save in the review page produces one row. Useful for retrospective debugging ("why was this field changed?") and for training data export.

### `llm_usage` — token accounting
```sql
CREATE TABLE IF NOT EXISTS llm_usage (
    id                SERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id        TEXT,
    doc_id            TEXT,
    extraction_id     INT REFERENCES extractions(id) ON DELETE SET NULL,
    vendor_id         TEXT REFERENCES vendors(id) ON DELETE SET NULL,
    page_num          INTEGER,
    total_pages       INTEGER,
    call_type         TEXT,                  -- 'extraction' | 'bbox_agent'
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    duration_ms       REAL,
    llm_url           TEXT
);
CREATE INDEX llm_usage_doc_ts_idx ON llm_usage (doc_id, ts DESC);
CREATE INDEX llm_usage_vendor_ts_idx ON llm_usage (vendor_id, ts DESC);
```

Recorded by both `extractor.call_llm` and `bbox_agent.learn_layout_for_vendor`. Drives the admin `/admin/usage` dashboard.

---

## Migration philosophy

Every migration is wrapped in:

```sql
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='X' AND column_name='Y'
    ) THEN
        ALTER TABLE X ADD COLUMN Y ...;
    END IF;
END $$;
```

- **Idempotent**: runs on every API and worker startup. Safe to call repeatedly.
- **No external migration tool**: Alembic / Flyway are absent by design. The deployment is a single `docker compose up` and migrations are part of `init()`.
- **Forward-only**: there are no rollback scripts. To revert a migration, write a new one that drops the column.

---

## Auth-related queries (the relevant 6 functions)

These are the functions called by the auth helpers in [auth.md](auth.md):

```python
async def get_user_by_email(pool, email) -> dict | None:
    """SELECT * FROM users WHERE email=$1"""

async def get_user_by_id(pool, user_id) -> dict | None:
    """SELECT * FROM users WHERE id=$1::UUID"""

async def get_vendor_owner(pool, vendor_id) -> str | None:
    """SELECT user_id::text FROM vendors WHERE id=$1"""

async def get_alias_vendor_id(pool, alias_id) -> str | None:
    """SELECT vendor_id FROM vendor_aliases WHERE id=$1"""

async def list_users(pool) -> list[dict]:
    """SELECT id,email,role,is_active,created_at FROM users ORDER BY created_at DESC"""

async def create_user(pool, email, hashed_pw, role) -> dict:
    """INSERT ... RETURNING id,email,role,is_active,created_at"""
```

`get_user_by_id` is invoked on **every protected request** (via `get_current_user`'s `is_active` check). One indexed PK lookup per request — cheap.

---

## Tenant-scoped list queries

Every list query that surfaces user-visible data accepts an optional `user_id`:

```python
async def list_vendors(pool, user_id: str | None = None) -> list[dict]:
    """
    SELECT id, name, status, created_at FROM vendors
    WHERE ($1::UUID IS NULL OR user_id = $1)
    ORDER BY created_at DESC
    """
```

The `($1 IS NULL OR user_id = $1)` idiom is the universal admin-bypass pattern. Same shape used in:
- `list_vendors`
- `get_all_aliases_for_detection` (joins to `vendors` and filters by `v.user_id`)
- The list-extraction queries built inside `main.py`

---

## Common pitfalls

1. **`SELECT *` on `extractions`**: pulls megabytes of JSONB. Avoid in hot paths. Use the lightweight helpers like `is_postprocess_ready` that only read `status`.
2. **JSONB returned as string**: asyncpg sometimes returns JSONB columns as `str` instead of parsed `dict`. The `_parse_jsonb()` helper normalizes — always use `_record(row, "result", "page_results", ...)` when reading rows that contain JSON columns.
3. **UUID casting on text params**: Postgres requires `$1::UUID` to compare against UUID columns. Forgetting the cast results in `operator does not exist: uuid = text`.
4. **Missing `ON DELETE` clause**: All FKs need a deliberate `ON DELETE` policy. Default is `NO ACTION` which throws on parent delete. Common choices in this schema: `CASCADE` (cleanup), `SET NULL` (preserve history).
5. **Forgetting partial unique indexes**: `jobs_active_idx` is a *partial* index. A query without `status IN (...)` can't use it for uniqueness reasoning — use `has_active_job()` helper instead.

---

## What's NOT in this database

- **No password reset tokens table.** Closed-system; admin handles resets.
- **No audit table for logins.** Could be added; currently login attempts are only in stdout logs.
- **No multi-org concept.** A user owns vendors directly; there's no `organizations` layer between `users` and `vendors`. Adding one would be an additive migration if needed.
- **No soft-delete for vendors / extractions.** Deletes are permanent. The closest thing to soft-delete is `users.is_active=FALSE`.
- **No row-level security (RLS).** All isolation is enforced at the application layer via `assert_*_access`. RLS would be the defence-in-depth layer if exposure changes.
