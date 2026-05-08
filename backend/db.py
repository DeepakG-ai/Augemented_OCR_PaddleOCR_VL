"""
db.py -- asyncpg pool, schema initialisation, and all SQL queries.

No ORM. Fields are stored as header_fields (JSONB) and line_item_fields (JSONB).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg

from .config import DATABASE_URL


# -- Pool creation ---------------------------------------------------------

async def create_pool() -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )


# -- Schema bootstrap ------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    hashed_pw   TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'client',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vendors (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT DEFAULT 'idle',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS templates (
    id                  SERIAL PRIMARY KEY,
    vendor_id           TEXT REFERENCES vendors(id) ON DELETE CASCADE,
    format_type         TEXT NOT NULL,
    header_fields       JSONB NOT NULL DEFAULT '[]'::jsonb,
    line_item_fields    JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_instructions TEXT,
    extraction_rules    JSONB DEFAULT '[]'::jsonb,
    system_prompt       TEXT,
    prompt_hash         TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id)
);

CREATE TABLE IF NOT EXISTS documents (
    id            SERIAL PRIMARY KEY,
    vendor_id     TEXT REFERENCES vendors(id),
    source_type   TEXT NOT NULL DEFAULT 'ui',
    source_ref    TEXT,
    filename      TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    size_bytes    BIGINT,
    object_key    TEXT,
    metadata      JSONB,
    status        TEXT NOT NULL DEFAULT 'queued',
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS extractions (
    id                  SERIAL PRIMARY KEY,
    document_id         INT REFERENCES documents(id) ON DELETE CASCADE,
    vendor_id           TEXT REFERENCES vendors(id),
    template_id         INT  REFERENCES templates(id),
    filename            TEXT,
    total_pages         INT,
    format_type         TEXT,
    header_fields       JSONB,
    line_item_fields    JSONB,
    result              JSONB,
    page_results        JSONB,
    field_locations     JSONB,
    ocr_data            JSONB,
    corrected_result    JSONB,
    correction_meta     JSONB,
    export_object_key   TEXT,
    progress            JSONB,
    cancel_requested    BOOLEAN NOT NULL DEFAULT FALSE,
    status              TEXT DEFAULT 'pending',
    error               TEXT,
    duration_ms         INT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pages (
    id            SERIAL PRIMARY KEY,
    extraction_id INT REFERENCES extractions(id) ON DELETE CASCADE,
    page_number   INT NOT NULL,
    image_b64     TEXT,
    object_key    TEXT NOT NULL,
    mime_type     TEXT DEFAULT 'image/jpeg',
    width         INT,
    height        INT,
    orig_width    INT,
    orig_height   INT,
    source        TEXT,
    char_count    INT,
    word_geometry JSONB,
    UNIQUE (extraction_id, page_number)
);

CREATE TABLE IF NOT EXISTS gold_examples (
    id                SERIAL PRIMARY KEY,
    vendor_id         TEXT REFERENCES vendors(id) ON DELETE CASCADE,
    extraction_id     INT REFERENCES extractions(id) ON DELETE SET NULL,
    original_result   JSONB NOT NULL,
    corrected_result  JSONB NOT NULL,
    correction_diff   JSONB,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
    id               SERIAL PRIMARY KEY,
    extraction_id    INT REFERENCES extractions(id) ON DELETE CASCADE,
    document_id      INT REFERENCES documents(id) ON DELETE CASCADE,
    job_type         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    payload          JSONB,
    progress         JSONB,
    attempts         INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 3,
    priority         INT NOT NULL DEFAULT 100,
    locked_by        TEXT,
    locked_at        TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    error            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS integration_deliveries (
    id              SERIAL PRIMARY KEY,
    extraction_id   INT REFERENCES extractions(id) ON DELETE CASCADE,
    contract_type   TEXT NOT NULL,
    target_type     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    payload         JSONB,
    object_key      TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                SERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id        TEXT,
    doc_id            TEXT,
    extraction_id     INT REFERENCES extractions(id) ON DELETE SET NULL,
    vendor_id         TEXT REFERENCES vendors(id) ON DELETE SET NULL,
    page_num          INTEGER,
    total_pages       INTEGER,
    call_type         TEXT,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    duration_ms       REAL,
    llm_url           TEXT
);
"""


async def init(pool: asyncpg.Pool) -> None:
    """Create tables if they don't exist, then run migrations."""
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        # Migration: add mime_type to pages if missing (existing DBs)
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'mime_type'
                ) THEN
                    ALTER TABLE pages ADD COLUMN mime_type TEXT DEFAULT 'image/jpeg';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'object_key'
                ) THEN
                    ALTER TABLE pages ADD COLUMN object_key TEXT;
                END IF;
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'image_b64'
                ) THEN
                    ALTER TABLE pages ALTER COLUMN image_b64 DROP NOT NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'orig_width'
                ) THEN
                    ALTER TABLE pages ADD COLUMN orig_width INT;
                    ALTER TABLE pages ADD COLUMN orig_height INT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'source'
                ) THEN
                    ALTER TABLE pages ADD COLUMN source TEXT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'char_count'
                ) THEN
                    ALTER TABLE pages ADD COLUMN char_count INT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pages' AND column_name = 'word_geometry'
                ) THEN
                    ALTER TABLE pages ADD COLUMN word_geometry JSONB;
                END IF;
            END $$;
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS pages_extraction_page_number_idx
            ON pages (extraction_id, page_number);
            
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_idx 
            ON jobs (extraction_id, job_type) 
            WHERE status IN ('queued', 'running') AND extraction_id IS NOT NULL;
            
            CREATE UNIQUE INDEX IF NOT EXISTS integration_deliveries_unique_target_idx
            ON integration_deliveries (extraction_id, contract_type, target_type);

            CREATE INDEX IF NOT EXISTS llm_usage_doc_ts_idx
            ON llm_usage (doc_id, ts DESC);

            CREATE INDEX IF NOT EXISTS llm_usage_vendor_ts_idx
            ON llm_usage (vendor_id, ts DESC);
        """)
        # Auth migration: add user_id column to vendors for per-client isolation
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='vendors' AND column_name='user_id'
                ) THEN
                    ALTER TABLE vendors ADD COLUMN user_id UUID REFERENCES users(id) ON DELETE SET NULL;
                END IF;
            END $$;
            CREATE INDEX IF NOT EXISTS vendors_user_id_idx ON vendors (user_id);
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE table_name='users' AND constraint_name='users_role_check'
                ) THEN
                    ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'client'));
                END IF;
            END $$;
        """)
        # Migration: add extraction_id to llm_usage if the table predates this column.
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'llm_usage' AND column_name = 'extraction_id'
                ) THEN
                    ALTER TABLE llm_usage ADD COLUMN extraction_id INT
                        REFERENCES extractions(id) ON DELETE SET NULL;
                END IF;
            END $$;
        """)
        # Vendor aliases (Phase 2) + spatial memory (Phase 3) — idempotent creation.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vendor_aliases (
                id           SERIAL PRIMARY KEY,
                vendor_id    TEXT REFERENCES vendors(id) ON DELETE CASCADE,
                pattern      TEXT NOT NULL,
                weight       INT NOT NULL DEFAULT 1,
                source       TEXT DEFAULT 'manual',
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(vendor_id, pattern)
            );
            CREATE INDEX IF NOT EXISTS vendor_aliases_pattern_idx
                ON vendor_aliases (pattern);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS spatial_memory (
                id                          SERIAL PRIMARY KEY,
                vendor_id                   TEXT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
                layout_key                  TEXT NOT NULL,
                field_key                   TEXT NOT NULL,
                page_number                 INT  NOT NULL,
                normalized_box              JSONB NOT NULL,
                source_engine               TEXT NOT NULL,
                created_from_extraction_id  INT REFERENCES extractions(id) ON DELETE SET NULL,
                last_verified_at            TIMESTAMPTZ DEFAULT NOW(),
                is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
                UNIQUE(vendor_id, layout_key, field_key, page_number)
            );
            CREATE INDEX IF NOT EXISTS spatial_memory_lookup_idx
                ON spatial_memory (vendor_id, layout_key, is_active);
        """)
        # Qwen-learned label layout boxes — separate from spatial_memory (which
        # is the human-review correction store). One row per
        # (vendor, template, field_key) on page 1.
        await conn.execute("""
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
            CREATE INDEX IF NOT EXISTS qwen_layout_boxes_lookup_idx
                ON qwen_layout_boxes (vendor_id, template_id);
        """)
        # Migration: add field_locations and ocr_data to extractions if missing
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'document_id'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN document_id INT REFERENCES documents(id) ON DELETE CASCADE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'field_locations'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN field_locations JSONB;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'ocr_data'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN ocr_data JSONB;
                END IF;
            END $$;
        """)
        # Migration: add corrected_result and correction_meta to extractions
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'progress'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN progress JSONB;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'cancel_requested'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'export_object_key'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN export_object_key TEXT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'updated_at'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW();
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'corrected_result'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN corrected_result JSONB;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'correction_meta'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN correction_meta JSONB;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'documents' AND column_name = 'object_key'
                ) THEN
                    NULL;
                END IF;
            END $$;
        """)


# -- Helpers ---------------------------------------------------------------

def _parse_jsonb(d: dict, *keys: str) -> None:
    """Parse JSONB columns that asyncpg may return as str."""
    for key in keys:
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])


def _record(row: asyncpg.Record | None, *json_keys: str) -> dict | None:
    if not row:
        return None
    d = dict(row)
    _parse_jsonb(d, *json_keys)
    return d


def _stringify_uuid_fields(row: dict, *keys: str) -> dict:
    for key in keys:
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row


# -- LLM usage queries -----------------------------------------------------

def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def record_llm_usage(
    pool: asyncpg.Pool,
    *,
    doc_id: str | int | None = None,
    document_id: str | int | None = None,
    extraction_id: int | None = None,
    vendor_id: str | None = None,
    page_num: int | None = None,
    total_pages: int | None = None,
    call_type: str = "unknown",
    model: str = "unknown",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: float | None = None,
    llm_url: str = "",
    request_id: str | None = None,
) -> dict:
    """Persist one LLM call's usage counters.

    llama.cpp returns usage in the OpenAI-compatible response body. This
    helper stores those reported counters as-is, with a computed total only
    when the server omits total_tokens.
    """
    prompt_tokens = _int_or_zero(prompt_tokens)
    completion_tokens = _int_or_zero(completion_tokens)
    total_tokens = _int_or_zero(total_tokens) or (prompt_tokens + completion_tokens)
    extraction_id = _int_or_none(extraction_id)
    page_num = _int_or_none(page_num)
    total_pages = _int_or_none(total_pages)
    resolved_doc_id = doc_id if doc_id is not None else document_id
    resolved_doc_id_str = str(resolved_doc_id) if resolved_doc_id is not None else None
    request_id_str = str(request_id) if request_id is not None else None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO llm_usage
                (request_id, doc_id, extraction_id, vendor_id, page_num, total_pages,
                 call_type, model, prompt_tokens, completion_tokens, total_tokens,
                 duration_ms, llm_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING id, ts, request_id, doc_id, extraction_id, vendor_id,
                      page_num, total_pages, call_type, model, prompt_tokens,
                      completion_tokens, total_tokens, duration_ms, llm_url
            """,
            request_id_str,
            resolved_doc_id_str,
            extraction_id,
            vendor_id,
            page_num,
            total_pages,
            call_type,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            duration_ms,
            llm_url,
        )
        return dict(row)


async def get_llm_usage_document_summary(
    pool: asyncpg.Pool,
    *,
    limit: int = 50,
    vendor_id: str | None = None,
) -> list[dict]:
    """Return token totals grouped per document for manager reporting."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(doc_id, extraction_id::TEXT, 'unknown') AS doc_id,
                vendor_id,
                SUM(prompt_tokens)::BIGINT AS total_input_tokens,
                SUM(completion_tokens)::BIGINT AS total_output_tokens,
                SUM(total_tokens)::BIGINT AS grand_total,
                COUNT(*)::INT AS llm_calls,
                ROUND(AVG(duration_ms))::INT AS avg_call_ms,
                MIN(ts) AS first_seen_at,
                MAX(ts) AS last_seen_at
            FROM llm_usage
            WHERE ($1::TEXT IS NULL OR vendor_id = $1)
            GROUP BY COALESCE(doc_id, extraction_id::TEXT, 'unknown'), vendor_id
            ORDER BY MAX(ts) DESC
            LIMIT $2
            """,
            vendor_id,
            max(1, min(limit, 500)),
        )
        return [dict(r) for r in rows]


async def get_llm_usage_daily_summary(
    pool: asyncpg.Pool,
    *,
    limit: int = 30,
    vendor_id: str | None = None,
    user_id: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
) -> list[dict]:
    """Return token totals grouped by day, optionally scoped to one vendor or user."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                DATE(lu.ts) AS day,
                SUM(lu.prompt_tokens)::BIGINT AS input_tokens,
                SUM(lu.completion_tokens)::BIGINT AS output_tokens,
                SUM(lu.total_tokens)::BIGINT AS total_tokens,
                COUNT(DISTINCT COALESCE(lu.doc_id, lu.extraction_id::TEXT, 'unknown'))::INT AS docs_processed,
                COUNT(*)::INT AS llm_calls,
                ROUND(AVG(lu.duration_ms))::INT AS avg_call_ms
            FROM llm_usage lu
            LEFT JOIN vendors v ON v.id = lu.vendor_id
            WHERE ($1::TEXT IS NULL OR lu.vendor_id = $1)
              AND ($2::UUID IS NULL OR v.user_id = $2)
              AND ($4::TIMESTAMPTZ IS NULL OR lu.ts >= $4)
              AND ($5::TIMESTAMPTZ IS NULL OR lu.ts < $5)
            GROUP BY DATE(lu.ts)
            ORDER BY day DESC
            LIMIT $3
            """,
            vendor_id,
            user_id,
            max(1, min(limit, 366)),
            date_from,
            date_to,
        )
        return [dict(r) for r in rows]


async def get_client_daily_summary(
    pool: asyncpg.Pool,
    user_id: str,
    *,
    limit: int = 30,
    date_from: Any = None,
    date_to: Any = None,
) -> list[dict]:
    """Return daily LLM usage for one client user's vendors."""
    return await get_llm_usage_daily_summary(
        pool,
        limit=limit,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
    )


async def get_usage_by_client(pool: asyncpg.Pool) -> list[dict]:
    """Return token/page usage aggregated per user for the admin client-breakdown view."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH extraction_totals AS (
                SELECT
                    v.user_id,
                    COUNT(e.id) FILTER (WHERE e.status = 'done')::INT AS total_extractions,
                    COALESCE(SUM(e.total_pages) FILTER (WHERE e.status = 'done'), 0)::BIGINT AS total_pages
                FROM vendors v
                LEFT JOIN extractions e ON e.vendor_id = v.id
                GROUP BY v.user_id
            ),
            usage_totals AS (
                SELECT
                    v.user_id,
                    COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
                    COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                    COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS grand_total,
                    COUNT(lu.id)::INT AS total_llm_calls
                FROM vendors v
                LEFT JOIN llm_usage lu ON lu.vendor_id = v.id
                GROUP BY v.user_id
            )
            SELECT
                u.id::TEXT AS user_id,
                u.email,
                u.role,
                u.is_active,
                COALESCE(et.total_extractions, 0)::INT AS total_extractions,
                COALESCE(et.total_pages, 0)::BIGINT AS total_pages,
                COALESCE(ut.total_input_tokens, 0)::BIGINT AS total_input_tokens,
                COALESCE(ut.total_output_tokens, 0)::BIGINT AS total_output_tokens,
                COALESCE(ut.grand_total, 0)::BIGINT AS grand_total,
                COALESCE(ut.total_llm_calls, 0)::INT AS total_llm_calls
            FROM users u
            LEFT JOIN extraction_totals et ON et.user_id = u.id
            LEFT JOIN usage_totals ut ON ut.user_id = u.id
            ORDER BY grand_total DESC
            """
        )
        return [dict(r) for r in rows]


async def get_client_document_usage(
    pool: asyncpg.Pool,
    user_id: str,
    limit: int = 50,
    date_from: Any = None,
    date_to: Any = None,
) -> list[dict]:
    """Return per-document token usage for a specific client user (admin only)."""
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                e.id AS extraction_id,
                e.filename,
                v.id AS vendor_id,
                v.name AS vendor_name,
                e.total_pages,
                e.status,
                e.created_at,
                COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
                COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS grand_total,
                COUNT(lu.id)::INT AS llm_calls,
                COUNT(DISTINCT lu.page_num) FILTER (
                    WHERE lu.call_type = 'extraction' AND lu.page_num IS NOT NULL
                )::INT AS billable_pages,
                COALESCE(SUM(lu.duration_ms), 0)::REAL AS total_latency_ms
            FROM extractions e
            JOIN vendors v ON v.id = e.vendor_id AND v.user_id = $1::UUID
            LEFT JOIN llm_usage lu ON lu.extraction_id = e.id
            WHERE ($3::TIMESTAMPTZ IS NULL OR e.created_at >= $3)
              AND ($4::TIMESTAMPTZ IS NULL OR e.created_at < $4)
            GROUP BY e.id, e.filename, v.id, v.name, e.total_pages, e.status, e.created_at
            ORDER BY e.created_at DESC
            LIMIT $2
            """,
            user_uuid,
            max(1, min(limit, 500)),
            date_from,
            date_to,
        )
        return [dict(r) for r in rows]


async def get_extraction_page_usage(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    """Return per-page token breakdown for a single extraction (admin drill-down)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                page_num,
                call_type,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                duration_ms,
                ts
            FROM llm_usage
            WHERE extraction_id = $1
            ORDER BY page_num ASC, ts ASC
            """,
            extraction_id,
        )
        return [dict(r) for r in rows]


async def list_llm_usage_calls(
    pool: asyncpg.Pool,
    *,
    limit: int = 100,
    doc_id: str | None = None,
    vendor_id: str | None = None,
) -> list[dict]:
    """Return recent raw LLM usage rows, one row per model call."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, ts, request_id, doc_id, extraction_id, vendor_id,
                   page_num, total_pages, call_type, model, prompt_tokens,
                   completion_tokens, total_tokens, duration_ms, llm_url
            FROM llm_usage
            WHERE ($1::TEXT IS NULL OR doc_id = $1)
              AND ($2::TEXT IS NULL OR vendor_id = $2)
            ORDER BY ts DESC
            LIMIT $3
            """,
            doc_id,
            vendor_id,
            max(1, min(limit, 1000)),
        )
        return [dict(r) for r in rows]


async def get_usage_stats(
    pool: asyncpg.Pool,
    user_id: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
) -> dict:
    """Return aggregate usage counters for the dashboard, optionally scoped to one user."""
    async with pool.acquire() as conn:
        ext_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::INT AS total_pdfs,
                COUNT(*) FILTER (WHERE e.status = 'done')::INT AS total_extractions,
                COALESCE(SUM(e.total_pages), 0)::BIGINT AS all_pages,
                COALESCE(SUM(e.total_pages) FILTER (WHERE e.status = 'done'), 0)::BIGINT AS total_pages
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            WHERE ($1::UUID IS NULL OR v.user_id = $1)
              AND ($2::TIMESTAMPTZ IS NULL OR e.created_at >= $2)
              AND ($3::TIMESTAMPTZ IS NULL OR e.created_at < $3)
            """,
            user_id,
            date_from,
            date_to,
        )
        llm_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT     AS total_input_tokens,
                COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                COALESCE(SUM(lu.total_tokens), 0)::BIGINT      AS grand_total,
                COUNT(lu.id)::INT                               AS total_llm_calls,
                COUNT(DISTINCT (lu.extraction_id, lu.page_num)) FILTER (
                    WHERE lu.call_type = 'extraction'
                      AND lu.extraction_id IS NOT NULL
                      AND lu.page_num IS NOT NULL
                )::INT AS billable_pages
            FROM llm_usage lu
            LEFT JOIN vendors v ON v.id = lu.vendor_id
            WHERE ($1::UUID IS NULL OR v.user_id = $1)
              AND ($2::TIMESTAMPTZ IS NULL OR lu.ts >= $2)
              AND ($3::TIMESTAMPTZ IS NULL OR lu.ts < $3)
            """,
            user_id,
            date_from,
            date_to,
        )
    stats = {**dict(ext_row), **dict(llm_row)}
    stats["unbilled_pages"] = max(
        int(stats.get("all_pages") or 0) - int(stats.get("billable_pages") or 0),
        0,
    )
    stats["failed_pages"] = 0
    return stats


# -- Vendor queries --------------------------------------------------------

async def get_vendor(pool: asyncpg.Pool, vendor_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, status, user_id, created_at FROM vendors WHERE id = $1",
            vendor_id,
        )
        return _stringify_uuid_fields(dict(row), "user_id") if row else None


async def list_vendors(pool: asyncpg.Pool, user_id: str | None = None) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, status, user_id, created_at FROM vendors
            WHERE ($1::UUID IS NULL OR user_id = $1)
            ORDER BY created_at DESC
            """,
            user_id,
        )
        return [dict(r) for r in rows]


async def upsert_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    name: str,
    user_id: str | None = None,
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO vendors (id, name, user_id) VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                user_id = COALESCE(EXCLUDED.user_id, vendors.user_id)
            RETURNING id, name, status, user_id, created_at
            """,
            vendor_id, name, _uuid_or_none(user_id),
        )
        return _stringify_uuid_fields(dict(row), "user_id")


async def get_vendor_owner(pool: asyncpg.Pool, vendor_id: str) -> str | None:
    """Return the user_id that owns this vendor, or None if vendor missing/unowned."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM vendors WHERE id = $1", vendor_id,
        )
        if not row or row["user_id"] is None:
            return None
        return str(row["user_id"])


async def get_alias_vendor_id(pool: asyncpg.Pool, alias_id: int) -> str | None:
    """Return the vendor_id that owns this alias row, or None if alias missing."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT vendor_id FROM vendor_aliases WHERE id = $1", alias_id,
        )
        return row["vendor_id"] if row else None


# -- User queries ----------------------------------------------------------

async def create_user(
    pool: asyncpg.Pool,
    email: str,
    hashed_pw: str,
    role: str = "client",
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, hashed_pw, role)
            VALUES ($1, $2, $3)
            RETURNING id, email, role, is_active, created_at
            """,
            email.lower().strip(), hashed_pw, role,
        )
        return _stringify_uuid_fields(dict(row), "id")


async def get_user_by_email(pool: asyncpg.Pool, email: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, hashed_pw, role, is_active, created_at
            FROM users WHERE email = $1
            """,
            email.lower().strip(),
        )
        return _stringify_uuid_fields(dict(row), "id") if row else None


async def get_user_by_id(pool: asyncpg.Pool, user_id: str) -> dict | None:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, role, is_active, created_at
            FROM users WHERE id = $1
            """,
            user_uuid,
        )
        return _stringify_uuid_fields(dict(row), "id") if row else None


async def list_users(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, role, is_active, created_at
            FROM users ORDER BY created_at DESC
            """
        )
        return [_stringify_uuid_fields(dict(r), "id") for r in rows]


async def deactivate_user(pool: asyncpg.Pool, user_id: str) -> bool:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET is_active = FALSE WHERE id = $1", user_uuid,
        )
        return result.endswith(" 1")


async def reset_user_password(pool: asyncpg.Pool, user_id: str, hashed_pw: str) -> bool:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET hashed_pw = $1 WHERE id = $2", hashed_pw, user_uuid,
        )
        return result.endswith(" 1")


async def delete_vendor(pool: asyncpg.Pool, vendor_id: str) -> bool:
    """Delete a vendor and dependent rows even on schemas without FK cascades."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Some deployments still have vendor foreign keys without ON DELETE
            # CASCADE, so delete dependents explicitly before removing the vendor.
            await conn.execute("DELETE FROM extractions WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM documents WHERE vendor_id = $1", vendor_id)
            result = await conn.execute("DELETE FROM vendors WHERE id = $1", vendor_id)
            return result == "DELETE 1"

# -- Vendor alias queries --------------------------------------------------

async def insert_vendor_alias(
    pool: asyncpg.Pool,
    vendor_id: str,
    pattern: str,
    weight: int = 1,
    source: str = "manual",
) -> dict | None:
    """Insert a vendor alias pattern. Returns the row or None on conflict."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO vendor_aliases (vendor_id, pattern, weight, source)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (vendor_id, pattern) DO NOTHING
            RETURNING id, vendor_id, pattern, weight, source, created_at
            """,
            vendor_id, pattern.lower().strip(), weight, source,
        )
        return dict(row) if row else None


async def list_vendor_aliases(pool: asyncpg.Pool, vendor_id: str | None = None) -> list[dict]:
    """List vendor aliases, optionally filtered by vendor_id."""
    async with pool.acquire() as conn:
        if vendor_id:
            rows = await conn.fetch(
                """
                SELECT va.id, va.vendor_id, v.name AS vendor_name, va.pattern, va.weight, va.source, va.created_at
                FROM vendor_aliases va
                JOIN vendors v ON v.id = va.vendor_id
                WHERE va.vendor_id = $1
                ORDER BY va.weight DESC, va.created_at ASC
                """,
                vendor_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT va.id, va.vendor_id, v.name AS vendor_name, va.pattern, va.weight, va.source, va.created_at
                FROM vendor_aliases va
                JOIN vendors v ON v.id = va.vendor_id
                ORDER BY va.vendor_id, va.weight DESC, va.created_at ASC
                """
            )
        return [dict(r) for r in rows]


async def delete_vendor_alias(pool: asyncpg.Pool, alias_id: int) -> bool:
    """Delete a single vendor alias by id."""
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM vendor_aliases WHERE id = $1", alias_id)
        return result == "DELETE 1"


async def get_all_aliases_for_detection(
    pool: asyncpg.Pool, user_id: str | None = None
) -> list[dict]:
    """Load vendor aliases for detection, optionally scoped to a single user's vendors."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT va.vendor_id, v.name AS vendor_name, va.pattern, va.weight
            FROM vendor_aliases va
            JOIN vendors v ON v.id = va.vendor_id
            WHERE va.vendor_id <> '_auto'
              AND ($1::UUID IS NULL OR v.user_id = $1)
            ORDER BY va.vendor_id, va.weight DESC
            """,
            user_id,
        )
        return [dict(r) for r in rows]


# -- Spatial memory queries ------------------------------------------------

async def upsert_spatial_memory(
    pool: asyncpg.Pool,
    vendor_id: str,
    layout_key: str,
    field_key: str,
    page_number: int,
    normalized_box: dict,
    source_engine: str,
    created_from_extraction_id: int | None = None,
) -> dict:
    """Insert or update a spatial memory entry. Last write wins on conflict."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO spatial_memory
                (vendor_id, layout_key, field_key, page_number, normalized_box,
                 source_engine, created_from_extraction_id, last_verified_at, is_active)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, NOW(), TRUE)
            ON CONFLICT (vendor_id, layout_key, field_key, page_number) DO UPDATE SET
                normalized_box = EXCLUDED.normalized_box,
                source_engine = EXCLUDED.source_engine,
                created_from_extraction_id = EXCLUDED.created_from_extraction_id,
                last_verified_at = NOW(),
                is_active = TRUE
            RETURNING id, vendor_id, layout_key, field_key, page_number,
                      normalized_box, source_engine, created_from_extraction_id,
                      last_verified_at, is_active
            """,
            vendor_id, layout_key, field_key, page_number,
            json.dumps(normalized_box), source_engine, created_from_extraction_id,
        )
        d = dict(row)
        _parse_jsonb(d, "normalized_box")
        return d


async def get_spatial_memory_for_layout(
    pool: asyncpg.Pool,
    vendor_id: str,
    layout_key: str,
) -> list[dict]:
    """Load all active spatial memory entries for a vendor+layout."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, vendor_id, layout_key, field_key, page_number,
                   normalized_box, source_engine, created_from_extraction_id,
                   last_verified_at, is_active
            FROM spatial_memory
            WHERE vendor_id = $1 AND layout_key = $2 AND is_active = TRUE
            ORDER BY field_key, page_number
            """,
            vendor_id, layout_key,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "normalized_box")
            results.append(d)
        return results


async def deactivate_spatial_memory(
    pool: asyncpg.Pool,
    vendor_id: str,
    layout_key: str,
    field_key: str | None = None,
) -> int:
    """Deactivate spatial memory entries. If field_key is None, deactivate all for layout."""
    async with pool.acquire() as conn:
        if field_key:
            result = await conn.execute(
                """
                UPDATE spatial_memory SET is_active = FALSE
                WHERE vendor_id = $1 AND layout_key = $2 AND field_key = $3
                """,
                vendor_id, layout_key, field_key,
            )
        else:
            result = await conn.execute(
                """
                UPDATE spatial_memory SET is_active = FALSE
                WHERE vendor_id = $1 AND layout_key = $2
                """,
                vendor_id, layout_key,
            )
        # Returns e.g. "UPDATE 3"
        return int(result.split()[-1]) if result else 0


# -- Qwen layout boxes (auto-learned label geometry) -----------------------

async def get_qwen_layout_boxes(
    pool: asyncpg.Pool,
    vendor_id: str,
    template_id: int,
) -> dict[str, dict]:
    """Return all Qwen-learned label boxes for (vendor, template), keyed by field_key."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, vendor_id, template_id, field_key, field_type,
                   normalized_box, page_number, created_from_extraction_id,
                   created_at, updated_at
            FROM qwen_layout_boxes
            WHERE vendor_id = $1 AND template_id = $2
            ORDER BY field_key
            """,
            vendor_id, template_id,
        )
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "normalized_box")
            out[d["field_key"]] = d
        return out


async def upsert_qwen_layout_boxes(
    pool: asyncpg.Pool,
    vendor_id: str,
    template_id: int,
    extraction_id: int | None,
    learned: dict[str, dict],
) -> int:
    """Upsert one row per learned field. `learned` shape:
        {field_key: {"normalized_box": {"x0":..,"y0":..,"x1":..,"y1":..}, "field_type": "header"|"line_item_column"}}
    Returns the number of rows written.
    """
    if not learned:
        return 0
    written = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for field_key, entry in learned.items():
                # Support both old "box" (list) and new "normalized_box" (dict) formats
                # Allow nbox to be None (JSON null) to signify a field that was missing/not found
                # to prevent the agent from repeatedly trying to find it.
                nbox = entry.get("normalized_box") if "normalized_box" in entry else entry.get("box")
                field_type = entry.get("field_type")
                if field_type not in ("header", "line_item_column"):
                    continue
                # Ensure stored as dict format {x0, y0, x1, y1}
                if isinstance(nbox, list) and len(nbox) == 4:
                    nbox = {"x0": nbox[0], "y0": nbox[1], "x1": nbox[2], "y1": nbox[3]}
                await conn.execute(
                    """
                    INSERT INTO qwen_layout_boxes
                        (vendor_id, template_id, field_key, field_type,
                         normalized_box, page_number, created_from_extraction_id,
                         created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5::jsonb, 1, $6, NOW(), NOW())
                    ON CONFLICT (vendor_id, template_id, field_key) DO UPDATE SET
                        field_type = EXCLUDED.field_type,
                        normalized_box = EXCLUDED.normalized_box,
                        created_from_extraction_id = EXCLUDED.created_from_extraction_id,
                        updated_at = NOW()
                    """,
                    vendor_id, template_id, field_key, field_type,
                    json.dumps(nbox), extraction_id,
                )
                written += 1
    return written


# -- Template queries ------------------------------------------------------

_TEMPLATE_COLS = """
    id, vendor_id, format_type, header_fields, line_item_fields,
    prompt_instructions, extraction_rules, system_prompt, prompt_hash,
    created_at, updated_at
"""


async def get_template(pool: asyncpg.Pool, vendor_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_TEMPLATE_COLS} FROM templates WHERE vendor_id = $1",
            vendor_id,
        )
        if not row:
            return None
        d = dict(row)
        _parse_jsonb(d, "header_fields", "line_item_fields", "extraction_rules")
        return d


async def upsert_template(
    pool: asyncpg.Pool,
    vendor_id: str,
    format_type: str,
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    system_prompt: str,
    prompt_hash: str,
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO templates
                (vendor_id, format_type, header_fields, line_item_fields,
                 prompt_instructions, extraction_rules, system_prompt, prompt_hash, updated_at)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6::jsonb, $7, $8, NOW())
            ON CONFLICT (vendor_id) DO UPDATE SET
                format_type         = EXCLUDED.format_type,
                header_fields       = EXCLUDED.header_fields,
                line_item_fields    = EXCLUDED.line_item_fields,
                prompt_instructions = EXCLUDED.prompt_instructions,
                extraction_rules    = EXCLUDED.extraction_rules,
                system_prompt       = EXCLUDED.system_prompt,
                prompt_hash         = EXCLUDED.prompt_hash,
                updated_at          = NOW()
            RETURNING {_TEMPLATE_COLS}
            """,
            vendor_id, format_type,
            json.dumps(header_fields), json.dumps(line_item_fields),
            instructions, json.dumps(rules),
            system_prompt, prompt_hash,
        )
        d = dict(row)
        _parse_jsonb(d, "header_fields", "line_item_fields", "extraction_rules")
        return d


async def list_all_templates(pool: asyncpg.Pool, user_id: str | None = None) -> list[dict]:
    """Return all templates joined with vendor name for the saved-templates page."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT t.id, t.vendor_id, v.name AS vendor_name, t.format_type,
                   t.header_fields, t.line_item_fields, t.prompt_instructions,
                   t.extraction_rules, t.prompt_hash, t.created_at, t.updated_at
            FROM templates t
            JOIN vendors v ON v.id = t.vendor_id
            WHERE ($1::UUID IS NULL OR v.user_id = $1)
            ORDER BY t.updated_at DESC
            """,
            user_id,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "extraction_rules")
            results.append(d)
        return results


# -- Document queries ------------------------------------------------------

async def create_document(
    pool: asyncpg.Pool,
    vendor_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    object_key: str,
    source_type: str = "ui",
    source_ref: str | None = None,
    metadata: dict | None = None,
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO documents
                (vendor_id, source_type, source_ref, filename, mime_type, size_bytes, object_key, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            RETURNING id, vendor_id, source_type, source_ref, filename, mime_type, size_bytes,
                      object_key, metadata, status, created_at, updated_at
            """,
            vendor_id, source_type, source_ref, filename, mime_type, size_bytes,
            object_key, json.dumps(metadata or {}),
        )
        return _record(row, "metadata") or {}


async def get_document(pool: asyncpg.Pool, document_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, vendor_id, source_type, source_ref, filename, mime_type, size_bytes,
                   object_key, metadata, status, created_at, updated_at
            FROM documents
            WHERE id = $1
            """,
            document_id,
        )
        return _record(row, "metadata")


async def update_document_status(pool: asyncpg.Pool, document_id: int, status: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE documents
            SET status = $2, updated_at = NOW()
            WHERE id = $1
            """,
            document_id, status,
        )


# -- Extraction queries ----------------------------------------------------

_EXTRACTION_COLS = """
    e.id, e.document_id, e.vendor_id, v.name AS vendor_name, e.template_id,
    e.filename, e.total_pages, e.format_type, e.header_fields, e.line_item_fields,
    e.result, e.page_results, e.field_locations, e.ocr_data,
    e.corrected_result, e.correction_meta, e.export_object_key,
    e.progress, e.cancel_requested, e.status, e.error, e.duration_ms,
    e.created_at, e.updated_at
"""


async def create_extraction(
    pool: asyncpg.Pool,
    vendor_id: str,
    template_id: int | None,
    filename: str,
    total_pages: int,
    format_type: str,
    header_fields: list[str],
    line_item_fields: list[str],
    document_id: int | None = None,
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO extractions
                (document_id, vendor_id, template_id, filename, total_pages,
                 format_type, header_fields, line_item_fields, status, progress)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, 'queued', $9::jsonb)
            RETURNING id
            """,
            document_id, vendor_id, template_id, filename, total_pages,
            format_type, json.dumps(header_fields), json.dumps(line_item_fields),
            json.dumps({"stage": "queued", "message": "Queued for processing"}),
        )
        return await get_extraction(pool, row["id"]) or {}


async def update_extraction_result(
    pool: asyncpg.Pool,
    extraction_id: int,
    result: Any,
    page_results: Any,
    status: str,
    duration_ms: int | None = None,
    error: str | None = None,
    page_results_partial: list[dict] | None = None,
    progress: dict | None = None,
) -> None:
    async with pool.acquire() as conn:
        if page_results_partial:
            # Incremental append: add page results to existing array
            for pr in page_results_partial:
                await conn.execute(
                    """
                    UPDATE extractions
                    SET page_results = COALESCE(page_results, '[]'::jsonb) || $2::jsonb,
                        status       = $3,
                        progress     = COALESCE($4::jsonb, progress),
                        updated_at   = NOW()
                    WHERE id = $1
                    """,
                    extraction_id,
                    json.dumps([pr]),
                    status,
                    json.dumps(progress) if progress is not None else None,
                )
        else:
            await conn.execute(
                """
                UPDATE extractions
                SET result       = $2::jsonb,
                    page_results = $3::jsonb,
                    status       = $4,
                    duration_ms  = COALESCE($5, duration_ms),
                    error        = $6,
                    progress     = COALESCE($7::jsonb, progress),
                    updated_at   = NOW()
                WHERE id = $1
                """,
                extraction_id,
                json.dumps(result) if result is not None else None,
                json.dumps(page_results) if page_results is not None else None,
                status, duration_ms, error,
                json.dumps(progress) if progress is not None else None,
            )


async def list_extractions(pool: asyncpg.Pool, vendor_id: str, limit: int = 20) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            WHERE e.vendor_id = $1
            ORDER BY e.created_at DESC LIMIT $2
            """,
            vendor_id, limit,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results",
                         "field_locations", "ocr_data", "corrected_result", "correction_meta", "progress")
            results.append(d)
        return results


async def list_all_extractions(
    pool: asyncpg.Pool,
    limit: int = 50,
    user_id: str | None = None,
) -> list[dict]:
    """Global extraction history with vendor_name joined."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            WHERE ($2::UUID IS NULL OR v.user_id = $2)
            ORDER BY e.created_at DESC LIMIT $1
            """,
            limit, user_id,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results",
                         "field_locations", "ocr_data", "corrected_result", "correction_meta", "progress")
            results.append(d)
        return results


async def count_all_extractions(pool: asyncpg.Pool, user_id: str | None = None) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*) FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            WHERE ($1::UUID IS NULL OR v.user_id = $1)
            """,
            user_id,
        )


async def get_extraction(pool: asyncpg.Pool, extraction_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            WHERE e.id = $1
            """,
            extraction_id,
        )
        if not row:
            return None
        d = dict(row)
        _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results",
                     "field_locations", "ocr_data", "corrected_result", "correction_meta", "progress")
        return d


# -- Pages queries ---------------------------------------------------------

async def save_pages(pool: asyncpg.Pool, extraction_id: int, pages: list[dict]) -> None:
    """Bulk-insert page artifact metadata for an extraction."""
    if not pages:
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO pages (extraction_id, page_number, object_key, mime_type, width, height, orig_width, orig_height,
                               source, char_count, word_geometry)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (extraction_id, page_number) DO UPDATE SET
                object_key = EXCLUDED.object_key,
                mime_type = EXCLUDED.mime_type,
                width = EXCLUDED.width,
                height = EXCLUDED.height,
                orig_width = EXCLUDED.orig_width,
                orig_height = EXCLUDED.orig_height,
                source = EXCLUDED.source,
                char_count = EXCLUDED.char_count,
                word_geometry = EXCLUDED.word_geometry
            """,
            [
                (
                    extraction_id,
                    p["page_number"],
                    p["object_key"],
                    p.get("mime_type", "image/jpeg"),
                    p.get("width", 0),
                    p.get("height", 0),
                    p.get("orig_width", p.get("width", 0)),
                    p.get("orig_height", p.get("height", 0)),
                    p.get("source"),
                    p.get("char_count"),
                    json.dumps(p["word_geometry"]) if p.get("word_geometry") is not None else None,
                )
                for p in pages
            ],
        )


async def get_pages(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    """Return all pages for an extraction, ordered by page_number."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT page_number, object_key, mime_type, width, height, orig_width, orig_height,
                   source, char_count, word_geometry
            FROM pages
            WHERE extraction_id = $1
            ORDER BY page_number ASC
            """,
            extraction_id,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "word_geometry")
            results.append(d)
        return results


async def get_page_object_keys(pool: asyncpg.Pool, extraction_id: int) -> list[str]:
    """Return artifact object keys for all rendered pages of an extraction."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT object_key
            FROM pages
            WHERE extraction_id = $1
            ORDER BY page_number ASC
            """,
            extraction_id,
        )
        return [r["object_key"] for r in rows if r.get("object_key")]


async def list_delivery_object_keys(pool: asyncpg.Pool, extraction_id: int) -> list[str]:
    """Return object keys created for integration deliveries/exports."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT object_key
            FROM integration_deliveries
            WHERE extraction_id = $1 AND object_key IS NOT NULL
            ORDER BY created_at DESC
            """,
            extraction_id,
        )
        return [r["object_key"] for r in rows if r.get("object_key")]


async def get_vendor_object_keys(pool: asyncpg.Pool, vendor_id: str) -> dict[str, list[str]]:
    """Collect object-store keys owned by a vendor for cleanup on delete."""
    async with pool.acquire() as conn:
        document_rows = await conn.fetch(
            """
            SELECT object_key
            FROM documents
            WHERE vendor_id = $1 AND object_key IS NOT NULL
            ORDER BY created_at ASC
            """,
            vendor_id,
        )
        page_rows = await conn.fetch(
            """
            SELECT p.object_key
            FROM pages p
            JOIN extractions e ON e.id = p.extraction_id
            WHERE e.vendor_id = $1 AND p.object_key IS NOT NULL
            ORDER BY p.extraction_id ASC, p.page_number ASC
            """,
            vendor_id,
        )
        delivery_rows = await conn.fetch(
            """
            SELECT d.object_key
            FROM integration_deliveries d
            JOIN extractions e ON e.id = d.extraction_id
            WHERE e.vendor_id = $1 AND d.object_key IS NOT NULL
            ORDER BY d.created_at ASC
            """,
            vendor_id,
        )
        export_rows = await conn.fetch(
            """
            SELECT export_object_key
            FROM extractions
            WHERE vendor_id = $1 AND export_object_key IS NOT NULL
            ORDER BY created_at ASC
            """,
            vendor_id,
        )
        return {
            "documents": [r["object_key"] for r in document_rows if r.get("object_key")],
            "pages": [r["object_key"] for r in page_rows if r.get("object_key")],
            "deliveries": [r["object_key"] for r in delivery_rows if r.get("object_key")],
            "exports": [r["export_object_key"] for r in export_rows if r.get("export_object_key")],
        }


# -- Field locations + OCR data --------------------------------------------

async def save_field_locations(
    pool: asyncpg.Pool,
    extraction_id: int,
    field_locations: dict,
) -> None:
    """Save field_locations to the extraction record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET field_locations = $2::jsonb,
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            json.dumps(field_locations),
        )


async def save_ocr_data(
    pool: asyncpg.Pool,
    extraction_id: int,
    ocr_data: list[dict],
) -> None:
    """Save PaddleOCR results (words + boxes per page) to the extraction record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET ocr_data = $2::jsonb,
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            json.dumps(ocr_data),
        )

async def is_postprocess_ready(pool: asyncpg.Pool, extraction_id: int) -> bool:
    """Lightweight check for postprocess prerequisites.

    Hybrid extraction needs current-document geometry before postprocess because
    spatial memory reads text from the current OCR/pdfium word boxes.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT (result IS NOT NULL AND ocr_data IS NOT NULL) AS ready
            FROM extractions
            WHERE id = $1
            """,
            extraction_id,
        )
        return bool(row and row["ready"])


async def get_ocr_data(pool: asyncpg.Pool, extraction_id: int) -> list[dict] | None:
    """Return PaddleOCR results (words + boxes per page) for click-to-select."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ocr_data FROM extractions WHERE id = $1",
            extraction_id,
        )
        if not row or row["ocr_data"] is None:
            return None
        data = row["ocr_data"]
        if isinstance(data, str):
            return json.loads(data)
        return data


async def save_corrections(
    pool: asyncpg.Pool,
    extraction_id: int,
    corrected_result: Any,
    field_locations: dict,
    correction_meta: dict | None = None,
) -> bool:
    """Persist user corrections to corrected_result (original result stays immutable).

    Returns True if the extraction was found and updated, False otherwise.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE extractions
            SET corrected_result = $2::jsonb,
                field_locations  = $3::jsonb,
                correction_meta  = $4::jsonb,
                updated_at       = NOW()
            WHERE id = $1
            """,
            extraction_id,
            json.dumps(corrected_result) if corrected_result is not None else None,
            json.dumps(field_locations),
            json.dumps(correction_meta) if correction_meta else None,
        )
        # asyncpg returns e.g. "UPDATE 1" or "UPDATE 0"
        return result == "UPDATE 1"


async def save_export_artifact(pool: asyncpg.Pool, extraction_id: int, object_key: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET export_object_key = $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            object_key,
        )


async def delete_extraction(pool: asyncpg.Pool, extraction_id: int) -> bool:
    """Delete exactly one extraction record."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM extractions WHERE id = $1",
            extraction_id,
        )
        return result == "DELETE 1"


async def count_extractions_for_document(pool: asyncpg.Pool, document_id: int) -> int:
    """How many extractions still reference a document."""
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM extractions WHERE document_id = $1",
                document_id,
            )
            or 0
        )


async def delete_document(pool: asyncpg.Pool, document_id: int) -> bool:
    """Delete exactly one document record."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM documents WHERE id = $1",
            document_id,
        )
        return result == "DELETE 1"


def get_effective_result(extraction: dict) -> dict:
    """Return corrected_result if available, else the original result."""
    return extraction.get("corrected_result") or extraction.get("result") or {}


async def create_review_event(
    pool: asyncpg.Pool,
    extraction_id: int,
    actor: str,
    reason_code: str,
    note: str | None,
    before_result: dict,
    after_result: dict,
    before_locations: dict | None,
    after_locations: dict | None,
    diff: dict,
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO review_events
                (extraction_id, actor, reason_code, note, before_result, after_result,
                 before_locations, after_locations, diff)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb)
            RETURNING id
            """,
            extraction_id,
            actor,
            reason_code,
            note,
            json.dumps(before_result),
            json.dumps(after_result),
            json.dumps(before_locations) if before_locations is not None else None,
            json.dumps(after_locations) if after_locations is not None else None,
            json.dumps(diff),
        )
        return row["id"]


async def list_review_events(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, extraction_id, actor, reason_code, note, before_result, after_result,
                   before_locations, after_locations, diff, created_at
            FROM review_events
            WHERE extraction_id = $1
            ORDER BY created_at DESC
            """,
            extraction_id,
        )
        results = []
        for row in rows:
            d = dict(row)
            _parse_jsonb(d, "before_result", "after_result", "before_locations", "after_locations", "diff")
            results.append(d)
        return results


# -- Gold examples (learning from corrections) -----------------------------

async def save_gold_example(
    pool: asyncpg.Pool,
    vendor_id: str,
    extraction_id: int,
    original_result: dict,
    corrected_result: dict,
    correction_diff: dict | None = None,
) -> int:
    """Store a human-verified correction as a gold example for future prompts."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO gold_examples (vendor_id, extraction_id, original_result, corrected_result, correction_diff)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)
            RETURNING id
            """,
            vendor_id, extraction_id,
            json.dumps(original_result),
            json.dumps(corrected_result),
            json.dumps(correction_diff) if correction_diff else None,
        )
        return row["id"]


async def get_gold_examples(
    pool: asyncpg.Pool,
    vendor_id: str,
    limit: int | None = None,
) -> list[dict]:
    """Retrieve latest correction per field for prompt injection.

    gold_examples remains append-only for audit history, but the prompt should
    not receive stale conflicting examples for the same field. This returns a
    single consolidated correction_diff object where each field uses its latest
    saved correction.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (field.key)
                   field.key AS field_key,
                   field.value AS correction,
                   ge.id,
                   ge.extraction_id,
                   ge.created_at
            FROM gold_examples ge
            CROSS JOIN LATERAL jsonb_each(ge.correction_diff) AS field(key, value)
            WHERE ge.vendor_id = $1 AND ge.correction_diff IS NOT NULL
            ORDER BY field.key, ge.created_at DESC, ge.id DESC
            """,
            vendor_id,
        )

        latest: dict[str, Any] = {}
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "correction")
            latest[d["field_key"]] = d["correction"]

        if limit is not None and limit > 0:
            latest = dict(list(latest.items())[:limit])

        return [{"correction_diff": latest}] if latest else []


async def delete_stale_qwen_layout_boxes(
    pool: asyncpg.Pool,
    vendor_id: str,
    template_id: int,
    valid_field_keys: list[str],
) -> int:
    """Delete qwen_layout_boxes rows for fields no longer in the template."""
    async with pool.acquire() as conn:
        if valid_field_keys:
            result = await conn.execute(
                """
                DELETE FROM qwen_layout_boxes
                WHERE vendor_id = $1 AND template_id = $2
                  AND field_key != ALL($3::text[])
                """,
                vendor_id, template_id, valid_field_keys,
            )
        else:
            result = await conn.execute(
                "DELETE FROM qwen_layout_boxes WHERE vendor_id = $1 AND template_id = $2",
                vendor_id, template_id,
            )
        return int(result.split()[-1])


async def delete_stale_spatial_memory(
    pool: asyncpg.Pool,
    vendor_id: str,
    valid_field_keys: list[str],
) -> int:
    """Delete spatial_memory rows for fields no longer in the template."""
    async with pool.acquire() as conn:
        if valid_field_keys:
            result = await conn.execute(
                """
                DELETE FROM spatial_memory
                WHERE vendor_id = $1
                  AND field_key != ALL($2::text[])
                """,
                vendor_id, valid_field_keys,
            )
        else:
            result = await conn.execute(
                "DELETE FROM spatial_memory WHERE vendor_id = $1",
                vendor_id,
            )
        return int(result.split()[-1])


async def get_latest_gold_correction_fields(
    pool: asyncpg.Pool,
    vendor_id: str,
) -> dict[str, Any]:
    """Return latest saved gold correction by field for UI warnings."""
    examples = await get_gold_examples(pool, vendor_id)
    if not examples:
        return {}
    correction_diff = examples[0].get("correction_diff")
    return correction_diff if isinstance(correction_diff, dict) else {}


# -- Job queue -------------------------------------------------------------

async def enqueue_job(
    pool: asyncpg.Pool,
    extraction_id: int | None,
    document_id: int | None,
    job_type: str,
    payload: dict | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (extraction_id, document_id, job_type, payload, priority, max_attempts)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6)
            ON CONFLICT (extraction_id, job_type) WHERE status IN ('queued', 'running') AND extraction_id IS NOT NULL
            DO NOTHING
            RETURNING id, extraction_id, document_id, job_type, status, payload, progress,
                      attempts, max_attempts, priority, locked_by, locked_at,
                      started_at, finished_at, error, created_at, updated_at
            """,
            extraction_id,
            document_id,
            job_type,
            json.dumps(payload or {}),
            priority,
            max_attempts,
        )
        return _record(row, "payload", "progress") if row else None


async def has_active_job(pool: asyncpg.Pool, extraction_id: int, job_type: str) -> bool:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1
                FROM jobs
                WHERE extraction_id = $1
                  AND job_type = $2
                  AND status IN ('queued', 'running')
            )
            """,
            extraction_id,
            job_type,
        )
        return bool(value)


async def ensure_job(
    pool: asyncpg.Pool,
    extraction_id: int | None,
    document_id: int | None,
    job_type: str,
    payload: dict | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> dict | None:
    if extraction_id is not None and await has_active_job(pool, extraction_id, job_type):
        return None
    return await enqueue_job(pool, extraction_id, document_id, job_type, payload, priority, max_attempts)


async def claim_job(pool: asyncpg.Pool, job_type: str, worker_name: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                FROM jobs
                WHERE job_type = $1
                  AND status = 'queued'
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs j
            SET status = 'running',
                locked_by = $2,
                locked_at = NOW(),
                started_at = COALESCE(started_at, NOW()),
                attempts = attempts + 1,
                updated_at = NOW()
            FROM candidate
            WHERE j.id = candidate.id
            RETURNING j.id, j.extraction_id, j.document_id, j.job_type, j.status, j.payload, j.progress,
                      j.attempts, j.max_attempts, j.priority, j.locked_by, j.locked_at,
                      j.started_at, j.finished_at, j.error, j.created_at, j.updated_at
            """,
            job_type,
            worker_name,
        )
        return _record(row, "payload", "progress")


async def update_job_progress(pool: asyncpg.Pool, job_id: int, progress: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET progress = $2::jsonb,
                updated_at = NOW()
            WHERE id = $1
            """,
            job_id,
            json.dumps(progress),
        )


async def complete_job(pool: asyncpg.Pool, job_id: int, progress: dict | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'done',
                progress = COALESCE($2::jsonb, progress),
                finished_at = NOW(),
                updated_at = NOW(),
                error = NULL
            WHERE id = $1
            """,
            job_id,
            json.dumps(progress) if progress is not None else None,
        )


async def fail_job(pool: asyncpg.Pool, job_id: int, error: str, retryable: bool = True) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT attempts, max_attempts FROM jobs WHERE id = $1", job_id)
        if not row:
            return
        next_status = "queued" if retryable and row["attempts"] < row["max_attempts"] else "failed"
        await conn.execute(
            """
            UPDATE jobs
            SET status = $2,
                error = $3,
                locked_by = NULL,
                locked_at = NULL,
                finished_at = CASE WHEN $2 = 'failed' THEN NOW() ELSE finished_at END,
                updated_at = NOW()
            WHERE id = $1
            """,
            job_id,
            next_status,
            error,
        )


async def recover_stale_jobs(pool: asyncpg.Pool, stage: str, stale_minutes: int = 10) -> int:
    """Reset running jobs stuck longer than stale_minutes back to queued (or failed if exhausted).

    Called on worker startup and periodically in the poll loop to reclaim orphaned jobs
    left in 'running' state by a crashed or killed worker process.
    Returns the number of jobs recovered.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE jobs
            SET status     = CASE
                                 WHEN attempts >= max_attempts THEN 'failed'
                                 ELSE 'queued'
                             END,
                locked_by  = NULL,
                locked_at  = NULL,
                error      = CASE
                                 WHEN attempts >= max_attempts
                                 THEN COALESCE(error, 'Worker crash — max attempts reached')
                                 ELSE 'Worker crash — requeued'
                             END,
                updated_at = NOW()
            WHERE job_type = $1
              AND status   = 'running'
              AND updated_at < NOW() - ($2 * INTERVAL '1 minute')
            """,
            stage,
            stale_minutes,
        )
        return int(result.split()[-1]) if result else 0


async def cancel_jobs_for_extraction(pool: asyncpg.Pool, extraction_id: int) -> dict:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE jobs
            SET status = CASE
                    WHEN status = 'queued' THEN 'cancelled'
                    WHEN status = 'running' THEN 'cancelling'
                    ELSE status
                END,
                error = CASE
                    WHEN status = 'queued' THEN 'Cancelled before execution'
                    WHEN status = 'running' THEN 'Cancellation requested during execution'
                    ELSE error
                END,
                finished_at = CASE WHEN status = 'queued' THEN NOW() ELSE finished_at END,
                updated_at = NOW()
            WHERE extraction_id = $1
              AND status IN ('queued', 'running')
            RETURNING status
            """,
            extraction_id,
        )
        cancelled = sum(1 for row in rows if row["status"] == "cancelled")
        cancelling = sum(1 for row in rows if row["status"] == "cancelling")
        return {
            "cancelled": cancelled,
            "cancelling": cancelling,
            "updated": len(rows),
        }


async def get_job(pool: asyncpg.Pool, job_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, extraction_id, document_id, job_type, status, payload, progress,
                   attempts, max_attempts, priority, locked_by, locked_at,
                   started_at, finished_at, error, created_at, updated_at
            FROM jobs
            WHERE id = $1
            """,
            job_id,
        )
        return _record(row, "payload", "progress")


async def get_latest_job_for_extraction(pool: asyncpg.Pool, extraction_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, extraction_id, document_id, job_type, status, payload, progress,
                   attempts, max_attempts, priority, locked_by, locked_at,
                   started_at, finished_at, error, created_at, updated_at
            FROM jobs
            WHERE extraction_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            extraction_id,
        )
        return _record(row, "payload", "progress")


async def list_jobs_for_extraction(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, extraction_id, document_id, job_type, status, payload, progress,
                   attempts, max_attempts, priority, locked_by, locked_at,
                   started_at, finished_at, error, created_at, updated_at
            FROM jobs
            WHERE extraction_id = $1
            ORDER BY created_at ASC
            """,
            extraction_id,
        )
        results = []
        for row in rows:
            d = dict(row)
            _parse_jsonb(d, "payload", "progress")
            results.append(d)
        return results


async def set_extraction_status(
    pool: asyncpg.Pool,
    extraction_id: int,
    status: str,
    progress: dict | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET status = $2,
                progress = COALESCE($3::jsonb, progress),
                error = $4,
                duration_ms = COALESCE($5, duration_ms),
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            status,
            json.dumps(progress) if progress is not None else None,
            error,
            duration_ms,
        )


async def update_extraction_progress(
    pool: asyncpg.Pool,
    extraction_id: int,
    progress: dict,
    status: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET progress = $2::jsonb,
                status = COALESCE($3, status),
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            json.dumps(progress),
            status,
        )


async def set_total_pages(pool: asyncpg.Pool, extraction_id: int, total_pages: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET total_pages = $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            total_pages,
        )


async def set_cancel_requested(pool: asyncpg.Pool, extraction_id: int, cancel_requested: bool = True) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE extractions
            SET cancel_requested = $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            extraction_id,
            cancel_requested,
        )
        return result == "UPDATE 1"


async def is_cancel_requested(pool: asyncpg.Pool, extraction_id: int) -> bool:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT cancel_requested FROM extractions WHERE id = $1",
            extraction_id,
        )
        return bool(value)


# -- Outbound deliveries ---------------------------------------------------

async def upsert_delivery(
    pool: asyncpg.Pool,
    extraction_id: int,
    contract_type: str,
    target_type: str,
    status: str,
    payload: dict | None = None,
    object_key: str | None = None,
    error: str | None = None,
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE integration_deliveries
            SET status = $4,
                payload = $5::jsonb,
                object_key = $6,
                error = $7,
                updated_at = NOW()
            WHERE extraction_id = $1
              AND contract_type = $2
              AND target_type = $3
            RETURNING id, extraction_id, contract_type, target_type, status, payload, object_key, error, created_at, updated_at
            """,
            extraction_id,
            contract_type,
            target_type,
            status,
            json.dumps(payload) if payload is not None else None,
            object_key,
            error,
        )
        if row:
            return _record(row, "payload") or {}

        row = await conn.fetchrow(
            """
            INSERT INTO integration_deliveries (extraction_id, contract_type, target_type, status, payload, object_key, error)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING id, extraction_id, contract_type, target_type, status, payload, object_key, error, created_at, updated_at
            """,
            extraction_id,
            contract_type,
            target_type,
            status,
            json.dumps(payload) if payload is not None else None,
            object_key,
            error,
        )
        return _record(row, "payload") or {}


async def list_deliveries(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, extraction_id, contract_type, target_type, status, payload, object_key, error, created_at, updated_at
            FROM integration_deliveries
            WHERE extraction_id = $1
            ORDER BY updated_at DESC
            """,
            extraction_id,
        )
        results = []
        for row in rows:
            d = dict(row)
            _parse_jsonb(d, "payload")
            results.append(d)
        return results
