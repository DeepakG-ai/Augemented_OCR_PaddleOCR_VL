"""
db.py -- asyncpg pool, schema initialisation, and all SQL queries.

No ORM. Fields are stored as header_fields (JSONB) and line_item_fields (JSONB).
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from .cache import get_cache
from .config import (
    DATABASE_URL,
    DB_POOL_MIN_API,
    DB_POOL_MAX_API,
    CACHE_TTL_AUTH,
    CACHE_TTL_APIKEY_TOUCH,
    CACHE_TTL_VENDOR,
    CACHE_TTL_TEMPLATE,
    CACHE_TTL_MAPPING,
    CACHE_TTL_ALIAS,
)


# -- Pool creation ---------------------------------------------------------

async def create_pool(
    min_size: int = DB_POOL_MIN_API,
    max_size: int = DB_POOL_MAX_API,
) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool.

    Defaults to the API-tier sizing. The pipeline workers pass the smaller
    worker sizing (DB_POOL_MIN_WORKER / DB_POOL_MAX_WORKER) so that the many
    worker processes don't collectively exhaust Postgres connections.
    """
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
    )


# -- Cache helpers ---------------------------------------------------------
# Read-through caching for hot, read-mostly rows shared by the API and the
# pipeline workers. The cache fails soft (see cache.py): with no Redis these
# reduce to a plain DB call. Cached values are returned as shallow copies so a
# caller mutating the result can't corrupt a shared entry — treat them as
# read-only. Invalidation is explicit on the matching mutation below.

async def _cached_read(key: str, ttl: int, loader):
    """get_or_set, returning a deep copy of the result.

    Callers may freely mutate what they get back (including nested lists/dicts
    like template.header_fields) without ever corrupting the shared cache
    entry. The copy costs microseconds versus the cross-region DB round trip it
    avoids.
    """
    return copy.deepcopy(await get_cache().get_or_set(key, ttl, loader))


async def invalidate_user_cache(user_id) -> None:
    uid = _uuid_or_none(user_id)
    if uid is not None:
        await get_cache().delete(f"user:{uid}")


async def invalidate_api_key_cache() -> None:
    # Key cache is keyed by hash; mutations carry only the key id, so clear all
    # cached keys (api-key mutations are rare and the set is tiny).
    await get_cache().delete_pattern("apikey:*")


async def invalidate_vendor_cache(vendor_id: str | None = None) -> None:
    c = get_cache()
    if vendor_id:
        await c.delete(
            f"vendor:{vendor_id}",
            f"template:{vendor_id}",
            f"mapping:{vendor_id}",
            f"aliases:list:{vendor_id}",
        )
    await c.delete_pattern("vendors:list:*")
    await c.delete_pattern("templates:list:*")
    await c.delete_pattern("aliases:detect:*")


async def invalidate_template_cache(vendor_id: str) -> None:
    c = get_cache()
    await c.delete(f"template:{vendor_id}")
    await c.delete_pattern("templates:list:*")


async def invalidate_mapping_cache(vendor_id: str) -> None:
    await get_cache().delete(f"mapping:{vendor_id}")


async def invalidate_alias_cache(vendor_id: str) -> None:
    c = get_cache()
    # "aliases:list:all" is the unfiltered variant — clear it too, in case a
    # caller ever lists all aliases (today only the per-vendor list is used).
    await c.delete(f"aliases:list:{vendor_id}", "aliases:list:all")
    await c.delete_pattern("aliases:detect:*")


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

CREATE TABLE IF NOT EXISTS field_mappings (
    id              SERIAL PRIMARY KEY,
    vendor_id       TEXT REFERENCES vendors(id) ON DELETE CASCADE,
    template_id     INT  REFERENCES templates(id) ON DELETE SET NULL,
    header_map      JSONB NOT NULL DEFAULT '{}'::jsonb,
    line_map        JSONB NOT NULL DEFAULT '{}'::jsonb,
    header_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    line_snapshot   JSONB NOT NULL DEFAULT '[]'::jsonb,
    pending_notices JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id)
);

CREATE TABLE IF NOT EXISTS output_schemas (
    id                      SERIAL PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    slug                    TEXT NOT NULL UNIQUE,
    is_system               BOOLEAN DEFAULT FALSE,
    header_fields           TEXT[] NOT NULL DEFAULT '{}',
    line_fields             TEXT[] NOT NULL DEFAULT '{}',
    header_fields_snapshot  TEXT[] NOT NULL DEFAULT '{}',
    line_fields_snapshot    TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);
"""


async def init(pool: asyncpg.Pool) -> None:
    """Create tables if they don't exist, then run migrations with a retry loop for parallel worker boot."""
    import random
    import asyncio
    import logging
    logger = logging.getLogger("db")
    
    max_retries = 8
    for attempt in range(max_retries):
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT pg_advisory_lock(hashtext('augocr_db_init'))")
                try:
                    await _init_db(pool)
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(hashtext('augocr_db_init'))")
            return
        except Exception as e:
            err_str = str(e).lower()
            is_lock_issue = any(k in err_str for k in ("deadlock", "lock", "unique", "duplicate"))
            if is_lock_issue and attempt < max_retries - 1:
                sleep_time = random.uniform(1.5, 4.0)
                logger.warning(
                    "Database migration lock conflict or deadlock detected: %s. "
                    "Retrying in %.2fs (Attempt %d/%d)...",
                    type(e).__name__, sleep_time, attempt + 1, max_retries
                )
                await asyncio.sleep(sleep_time)
            else:
                logger.error("Database migration failed on attempt %d: %s", attempt + 1, e)
                raise


async def _init_db(pool: asyncpg.Pool) -> None:
    """Actual schema bootstrap and migrations."""
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
            
            CREATE INDEX IF NOT EXISTS llm_usage_doc_ts_idx
            ON llm_usage (doc_id, ts DESC);

            CREATE INDEX IF NOT EXISTS llm_usage_vendor_ts_idx
            ON llm_usage (vendor_id, ts DESC);

            -- Worker job-claim hot path: claim_job filters job_type + status
            -- 'queued' and orders by priority, created_at. Partial index keeps
            -- it tiny (only queued rows) and matches the exact predicate/order.
            CREATE INDEX IF NOT EXISTS jobs_queue_claim_idx
            ON jobs (job_type, priority, created_at)
            WHERE status = 'queued';

            -- Latest-job-for-extraction lookups (SSE progress, status reads).
            CREATE INDEX IF NOT EXISTS jobs_extraction_created_idx
            ON jobs (extraction_id, created_at DESC);

            -- Extraction history listings (per-vendor and global, newest first)
            -- and document joins.
            CREATE INDEX IF NOT EXISTS extractions_vendor_created_idx
            ON extractions (vendor_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS extractions_created_idx
            ON extractions (created_at DESC);

            CREATE INDEX IF NOT EXISTS extractions_document_idx
            ON extractions (document_id);
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
        # Vendor id auto-generation. The primary key stays an opaque, globally
        # unique TEXT (server-issued from a sequence) so it remains safe as a
        # foreign key and as the global spatial_memory/layout key. `client_seq`
        # is a per-owner display number (Vendor #1, #2 ...) — display only,
        # never referenced by any FK. Name is unique per owner (case-insensitive).
        await conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS vendors_global_id_seq;

            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='vendors' AND column_name='client_seq'
                ) THEN
                    ALTER TABLE vendors ADD COLUMN client_seq INT;
                END IF;
            END $$;

            -- Keep the sequence ahead of any existing all-numeric ids so a
            -- freshly issued id can never collide with a legacy one. Idempotent:
            -- server-issued ids are themselves numeric and feed this MAX on the
            -- next startup. is_called=false => next nextval returns max+1 exactly.
            DO $$
            DECLARE maxid bigint;
            BEGIN
                SELECT COALESCE(MAX(id::bigint), 0) INTO maxid
                  FROM vendors WHERE id ~ '^[0-9]+$';
                PERFORM setval('vendors_global_id_seq', maxid + 1, false);
            END $$;

            -- Backfill client_seq for pre-existing vendors, numbered per owner
            -- by creation order.
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id ORDER BY created_at, id
                       ) AS rn
                  FROM vendors
                 WHERE client_seq IS NULL
            )
            UPDATE vendors v
               SET client_seq = r.rn
              FROM ranked r
             WHERE v.id = r.id AND v.client_seq IS NULL;

            -- Name unique per owner (case-insensitive). Partial: legacy
            -- unowned (NULL user_id) rows are not forced globally unique.
            CREATE UNIQUE INDEX IF NOT EXISTS vendors_owner_name_uniq
                ON vendors (user_id, lower(name))
                WHERE user_id IS NOT NULL;
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
        # Fix A4-1 / P1: detach both FKs on llm_usage that used ON DELETE SET NULL so
        # that deleting an extraction or vendor never nullifies historical usage rows.
        # Also add user_id to llm_usage so billing survives vendor deletion.
        await conn.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'llm_usage_extraction_id_fkey'
                      AND table_name = 'llm_usage'
                ) THEN
                    ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_extraction_id_fkey;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'llm_usage_vendor_id_fkey'
                      AND table_name = 'llm_usage'
                ) THEN
                    ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_vendor_id_fkey;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'llm_usage' AND column_name = 'user_id'
                ) THEN
                    ALTER TABLE llm_usage ADD COLUMN user_id UUID;
                    UPDATE llm_usage lu
                       SET user_id = v.user_id
                      FROM vendors v
                     WHERE lu.vendor_id = v.id AND lu.user_id IS NULL;
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
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'extractions' AND column_name = 'universal_agent'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN universal_agent BOOLEAN NOT NULL DEFAULT FALSE;
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
        # Migration: add subscription_limit to users for SaaS page quotas
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name = 'subscription_limit'
                ) THEN
                    ALTER TABLE users ADD COLUMN subscription_limit INT NOT NULL DEFAULT 0;
                ELSE
                    ALTER TABLE users ALTER COLUMN subscription_limit SET DEFAULT 0;
                END IF;
            END $$;
        """)
        # Migration: pending_pages tracks in-flight uploads for atomic quota enforcement
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name = 'pending_pages'
                ) THEN
                    ALTER TABLE users ADD COLUMN pending_pages INT NOT NULL DEFAULT 0;
                END IF;
            END $$;
        """)
        # Migration: API keys table for programmatic access
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id          SERIAL PRIMARY KEY,
                user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                label       TEXT NOT NULL,
                key_hash    VARCHAR(255) UNIQUE NOT NULL,
                prefix      VARCHAR(32) NOT NULL,
                is_active   BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                last_used_at TIMESTAMPTZ
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
        """)
        # Migration: widen prefix column if it was created as VARCHAR(16)
        await conn.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='api_keys' AND column_name='prefix'
                      AND character_maximum_length < 32
                ) THEN
                    ALTER TABLE api_keys ALTER COLUMN prefix TYPE VARCHAR(32);
                END IF;
            END $$;
        """)
        # Migration: add encrypted_key column for admin key recovery
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='api_keys' AND column_name='encrypted_key'
                ) THEN
                    ALTER TABLE api_keys ADD COLUMN encrypted_key TEXT;
                END IF;
            END $$;
        """)
        # Migration: add expires_at column for API key expiry
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='api_keys' AND column_name='expires_at'
                ) THEN
                    ALTER TABLE api_keys ADD COLUMN expires_at TIMESTAMPTZ;
                END IF;
            END $$;
        """)
        # Migration: add api_key_id to llm_usage for per-key usage tracking
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='llm_usage' AND column_name='api_key_id'
                ) THEN
                    ALTER TABLE llm_usage ADD COLUMN api_key_id INT;
                END IF;
            END $$;
        """)
        # ERP field mapping: mapped_result holds the canonical-field JSON sent
        # to API-key clients. Raw `result` stays untouched for review/spatial memory.
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='extractions' AND column_name='mapped_result'
                ) THEN
                    ALTER TABLE extractions ADD COLUMN mapped_result JSONB;
                END IF;
            END $$;
            CREATE INDEX IF NOT EXISTS field_mappings_vendor_idx
                ON field_mappings (vendor_id);
        """)

        # Migration: output_schemas — schema_id FK on field_mappings + seed defaults
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='field_mappings' AND column_name='schema_id'
                ) THEN
                    ALTER TABLE field_mappings ADD COLUMN schema_id INT
                        REFERENCES output_schemas(id) ON DELETE SET NULL;
                END IF;
            END $$;
        """)
        await conn.execute("""
            INSERT INTO output_schemas (name, slug, is_system, header_fields, line_fields,
                                        header_fields_snapshot, line_fields_snapshot)
            VALUES (
                'AP Automation', 'ap_automation', TRUE,
                ARRAY['vendor_name','vendor_address','invoice_number','invoice_date','po_number',
                      'invoice_total','invoice_subtotal','tax_amount','freight_amount','terms'],
                ARRAY['item','line_description','quantity_ordered','quantity_received',
                      'unit_price','line_total','uom'],
                ARRAY['vendor_name','vendor_address','invoice_number','invoice_date','po_number',
                      'invoice_total','invoice_subtotal','tax_amount','freight_amount','terms'],
                ARRAY['item','line_description','quantity_ordered','quantity_received',
                      'unit_price','line_total','uom']
            ) ON CONFLICT (slug) DO NOTHING;

            INSERT INTO output_schemas (name, slug, is_system, header_fields, line_fields,
                                        header_fields_snapshot, line_fields_snapshot)
            VALUES (
                'PO Automation', 'po_automation', TRUE,
                ARRAY['CustNum','Client','CustPo','Zip','Addr1','ShipToaddr','OrderDate'],
                ARRAY['Line','Item','ItemVariant','CustItem','QtyOrdered','Price','UM','DueDate'],
                ARRAY['CustNum','Client','CustPo','Zip','Addr1','ShipToaddr','OrderDate'],
                ARRAY['Line','Item','ItemVariant','CustItem','QtyOrdered','Price','UM','DueDate']
            ) ON CONFLICT (slug) DO NOTHING;
        """)

        # Migration: idempotency claims table for deduplication
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_claims (
                id              SERIAL PRIMARY KEY,
                user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL,
                file_sha256     TEXT NOT NULL,
                extraction_id   INT  REFERENCES extractions(id) ON DELETE SET NULL,
                document_id     INT  REFERENCES documents(id) ON DELETE SET NULL,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_idempotency_claims_key ON idempotency_claims(user_id, idempotency_key);
        """)

        # ── Subscriptions & top-ups ────────────────────────────────────────────
        # Admin grants a subscription period (custom from/to dates) with a base
        # page_limit. Admin can later add top-ups (extra pages) that attach to
        # the *current* active subscription. On period_end everything vanishes —
        # base + topups (use-it-or-lose-it). One active subscription per user is
        # enforced via partial unique index.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id            SERIAL PRIMARY KEY,
                user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                page_limit    INT  NOT NULL CHECK (page_limit >= 0),
                period_start  TIMESTAMPTZ NOT NULL,
                period_end    TIMESTAMPTZ NOT NULL,
                status        TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','expired','cancelled','superseded')),
                note          TEXT,
                created_by    UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (period_end > period_start)
            );
            CREATE INDEX IF NOT EXISTS subscriptions_user_idx
                ON subscriptions (user_id, status, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_active_per_user
                ON subscriptions (user_id) WHERE status = 'active';
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS topups (
                id              SERIAL PRIMARY KEY,
                user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subscription_id INT  NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                pages           INT  NOT NULL CHECK (pages > 0),
                note            TEXT,
                created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS topups_subscription_idx
                ON topups (subscription_id);
            CREATE INDEX IF NOT EXISTS topups_user_idx
                ON topups (user_id, created_at DESC);
        """)
        # One-time backfill: every client with the legacy subscription_limit > 0
        # gets a 1-year subscription starting today, so quota math keeps working.
        # Admins can override later via POST /admin/users/{id}/subscriptions.
        await conn.execute("""
            INSERT INTO subscriptions (user_id, page_limit, period_start, period_end, status, note)
            SELECT u.id, u.subscription_limit, NOW(), NOW() + INTERVAL '365 days', 'active',
                   'Auto-migrated from legacy subscription_limit'
            FROM users u
            WHERE u.subscription_limit > 0
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = u.id AND s.status = 'active'
              );
        """)
        # Migration: user-initiated top-up requests. Users submit these when quota
        # is exhausted; admins see them as pending notifications and approve/reject.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS topup_requests (
                id               SERIAL PRIMARY KEY,
                user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                requested_pages  INT  NOT NULL CHECK (requested_pages > 0),
                requested_period TEXT NOT NULL,
                note             TEXT,
                status           TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','approved','rejected')),
                resolution_note  TEXT,
                resolved_by      UUID REFERENCES users(id) ON DELETE SET NULL,
                resolved_at      TIMESTAMPTZ,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS topup_requests_user_idx
                ON topup_requests (user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS topup_requests_status_idx
                ON topup_requests (status, created_at DESC);
        """)
        # Migration: quota event log — records every grace overage and hard block
        # so admins can see which clients are burning through grace pages.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quota_grace_events (
                id               SERIAL PRIMARY KEY,
                user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                event_type       TEXT NOT NULL
                                 CHECK (event_type IN ('grace_used', 'exceeded')),
                grace_pages_used INT NOT NULL DEFAULT 0,
                incoming_pages   INT NOT NULL,
                used_before      INT NOT NULL,
                limit_at_time    INT NOT NULL,
                filename         TEXT
            );
            CREATE INDEX IF NOT EXISTS quota_grace_events_user_idx
                ON quota_grace_events (user_id, event_ts DESC);
            CREATE INDEX IF NOT EXISTS quota_grace_events_ts_idx
                ON quota_grace_events (event_ts DESC);
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
    billing_user_id: str | None = None,  # override: admin uploads bill to admin, not vendor owner
    api_key_id: int | None = None,       # which API key made this call (for per-key reporting)
) -> dict:
    """Persist one LLM call's usage counters.

    llama.cpp returns usage in the OpenAI-compatible response body. This
    helper stores those reported counters as-is, with a computed total only
    when the server omits total_tokens.

    billing_user_id: when set, overrides the vendor-resolved user_id for this
    usage record. Used so admin uploads are billed to the admin account rather
    than the client who owns the matched vendor.
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
        # Resolve user_id atomically inside the INSERT query.
        # If billing_user_id is explicitly provided, we use it directly (via param $5).
        # Otherwise, we fetch it atomically from the vendors table.
        row = await conn.fetchrow(
            """
            INSERT INTO llm_usage
                (request_id, doc_id, extraction_id, vendor_id, user_id, page_num, total_pages,
                 call_type, model, prompt_tokens, completion_tokens, total_tokens,
                 duration_ms, llm_url, api_key_id)
            VALUES (
                $1, $2, $3, $4, 
                COALESCE($5::uuid, (SELECT user_id FROM vendors WHERE id = $4)), 
                $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
            )
            RETURNING id, ts, request_id, doc_id, extraction_id, vendor_id, user_id,
                      page_num, total_pages, call_type, model, prompt_tokens,
                      completion_tokens, total_tokens, duration_ms, llm_url, api_key_id
            """,
            request_id_str,
            resolved_doc_id_str,
            extraction_id,
            vendor_id,
            _uuid_or_none(billing_user_id),
            page_num,
            total_pages,
            call_type,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            duration_ms,
            llm_url,
            api_key_id,
        )
        return dict(row)


async def get_extraction_token_totals(pool: asyncpg.Pool, extraction_id: int) -> dict:
    """Return summed token counts for all LLM calls belonging to one extraction."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(prompt_tokens), 0)::BIGINT     AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0)::BIGINT AS completion_tokens,
                COALESCE(SUM(total_tokens), 0)::BIGINT      AS total_tokens,
                COUNT(*)::INT                               AS llm_calls
            FROM llm_usage
            WHERE extraction_id = $1
            """,
            extraction_id,
        )
    return dict(row) if row else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0}


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
    uid = _uuid_or_none(user_id)
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
            WHERE ($1::TEXT IS NULL OR lu.vendor_id = $1)
              AND ($2::UUID IS NULL OR lu.user_id = $2)
              AND ($4::TIMESTAMPTZ IS NULL OR lu.ts >= $4)
              AND ($5::TIMESTAMPTZ IS NULL OR lu.ts < $5)
            GROUP BY DATE(lu.ts)
            ORDER BY day DESC
            LIMIT $3
            """,
            vendor_id,
            uid,
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
                    COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) AS user_id,
                    COUNT(e.id) FILTER (WHERE e.status = 'done')::INT AS total_extractions
                FROM vendors v
                LEFT JOIN extractions e ON e.vendor_id = v.id
                LEFT JOIN documents d ON d.id = e.document_id
                GROUP BY COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id)
            ),
            usage_totals AS (
                SELECT
                    lu.user_id,
                    COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
                    COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                    COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS grand_total,
                    COUNT(lu.id)::INT AS total_llm_calls,
                    COUNT(DISTINCT (lu.extraction_id, lu.page_num)) FILTER (
                        WHERE lu.call_type = 'extraction'
                          AND lu.extraction_id IS NOT NULL
                          AND lu.page_num IS NOT NULL
                    )::INT AS billable_pages
                FROM llm_usage lu
                GROUP BY lu.user_id
            )
            SELECT
                u.id::TEXT AS user_id,
                u.email,
                u.role,
                u.is_active,
                COALESCE(et.total_extractions, 0)::INT AS total_extractions,
                COALESCE(ut.billable_pages, 0)::INT AS billable_pages,
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
            JOIN vendors v ON v.id = e.vendor_id
            JOIN documents d ON d.id = e.document_id
            LEFT JOIN llm_usage lu ON lu.extraction_id = e.id
            WHERE COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1::UUID
              AND ($3::TIMESTAMPTZ IS NULL OR e.created_at >= $3)
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
    uid = _uuid_or_none(user_id)
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
            LEFT JOIN documents d ON d.id = e.document_id
            WHERE ($1::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1)
              AND ($2::TIMESTAMPTZ IS NULL OR e.created_at >= $2)
              AND ($3::TIMESTAMPTZ IS NULL OR e.created_at < $3)
            """,
            uid,
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
            WHERE ($1::UUID IS NULL OR lu.user_id = $1)
              AND ($2::TIMESTAMPTZ IS NULL OR lu.ts >= $2)
              AND ($3::TIMESTAMPTZ IS NULL OR lu.ts < $3)
            """,
            uid,
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

_VENDOR_COLS = "id, name, status, user_id, client_seq, created_at"


async def get_vendor(pool: asyncpg.Pool, vendor_id: str) -> dict | None:
    async def _load():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_VENDOR_COLS} FROM vendors WHERE id = $1",
                vendor_id,
            )
            return _stringify_uuid_fields(dict(row), "user_id") if row else None

    return await _cached_read(f"vendor:{vendor_id}", CACHE_TTL_VENDOR, _load)


async def list_vendors(pool: asyncpg.Pool, user_id: str | None = None) -> list[dict]:
    async def _load():
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_VENDOR_COLS} FROM vendors
                WHERE ($1::UUID IS NULL OR user_id = $1)
                ORDER BY created_at DESC
                """,
                user_id,
            )
            return [_stringify_uuid_fields(dict(r), "user_id") for r in rows]

    return await _cached_read(f"vendors:list:{user_id or 'all'}", CACHE_TTL_VENDOR, _load)


async def assign_vendor_owner(pool: asyncpg.Pool, vendor_id: str, user_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE vendors SET user_id = $1::UUID
            WHERE id = $2
            RETURNING {_VENDOR_COLS}
            """,
            user_id, vendor_id,
        )
        if not row:
            return None
    await invalidate_vendor_cache(vendor_id)
    return _stringify_uuid_fields(dict(row), "user_id")


async def _next_client_seq(conn: asyncpg.Connection, user_id) -> int:
    """Next per-owner display number. user_id may be a UUID or None."""
    return await conn.fetchval(
        """
        SELECT COALESCE(MAX(client_seq), 0) + 1 FROM vendors
        WHERE user_id IS NOT DISTINCT FROM $1
        """,
        user_id,
    )


async def create_vendor_by_name(
    pool: asyncpg.Pool,
    name: str,
    user_id: str | None = None,
) -> dict:
    """Create a vendor from a name alone — the id is issued server-side from a
    global sequence. Name is unique per owner (case-insensitive): re-submitting
    an existing name returns that vendor instead of creating a duplicate.
    """
    uid = _uuid_or_none(user_id)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            f"""
            SELECT {_VENDOR_COLS} FROM vendors
            WHERE user_id IS NOT DISTINCT FROM $1 AND lower(name) = lower($2)
            """,
            uid, name,
        )
        if existing:
            return _stringify_uuid_fields(dict(existing), "user_id")
        try:
            new_id = str(await conn.fetchval("SELECT nextval('vendors_global_id_seq')"))
            seq = await _next_client_seq(conn, uid)
            row = await conn.fetchrow(
                f"""
                INSERT INTO vendors (id, name, user_id, client_seq)
                VALUES ($1, $2, $3, $4)
                RETURNING {_VENDOR_COLS}
                """,
                new_id, name, uid, seq,
            )
            created = _stringify_uuid_fields(dict(row), "user_id")
            await invalidate_vendor_cache(created["id"])
            return created
        except asyncpg.exceptions.UniqueViolationError:
            # Concurrent create of the same name lost the race — return the winner.
            row = await conn.fetchrow(
                f"""
                SELECT {_VENDOR_COLS} FROM vendors
                WHERE user_id IS NOT DISTINCT FROM $1 AND lower(name) = lower($2)
                """,
                uid, name,
            )
            return _stringify_uuid_fields(dict(row), "user_id")


async def upsert_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    name: str,
    user_id: str | None = None,
) -> dict:
    """Upsert by explicit id. Used by the template side-door where the vendor
    id already exists (issued earlier by create_vendor_by_name). Backfills
    client_seq if the row is created here.
    """
    async with pool.acquire() as conn:
        uid = _uuid_or_none(user_id)
        seq = await _next_client_seq(conn, uid)
        row = await conn.fetchrow(
            f"""
            INSERT INTO vendors (id, name, user_id, client_seq)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                user_id = COALESCE(EXCLUDED.user_id, vendors.user_id),
                client_seq = COALESCE(vendors.client_seq, EXCLUDED.client_seq)
            RETURNING {_VENDOR_COLS}
            """,
            vendor_id, name, uid, seq,
        )
    result = _stringify_uuid_fields(dict(row), "user_id")
    await invalidate_vendor_cache(result["id"])
    return result


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
    subscription_limit: int | None = None,
) -> dict:
    from backend.config import DEFAULT_SUBSCRIPTION_LIMIT
    limit = subscription_limit if subscription_limit is not None else DEFAULT_SUBSCRIPTION_LIMIT
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, hashed_pw, role, subscription_limit)
            VALUES ($1, $2, $3, $4)
            RETURNING id, email, role, is_active, created_at, subscription_limit
            """,
            email.lower().strip(), hashed_pw, role, limit,
        )
        return _stringify_uuid_fields(dict(row), "id")


async def get_user_by_email(pool: asyncpg.Pool, email: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, hashed_pw, role, is_active, created_at, subscription_limit
            FROM users WHERE email = $1
            """,
            email.lower().strip(),
        )
        return _stringify_uuid_fields(dict(row), "id") if row else None


async def get_user_by_id(pool: asyncpg.Pool, user_id: str) -> dict | None:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return None

    async def _load():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, email, role, is_active, created_at, subscription_limit
                FROM users WHERE id = $1
                """,
                user_uuid,
            )
            return _stringify_uuid_fields(dict(row), "id") if row else None

    return await _cached_read(f"user:{user_uuid}", CACHE_TTL_AUTH, _load)


async def list_users(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                u.id, u.email, u.role, u.is_active, u.created_at, u.subscription_limit,
                COALESCE(u.pending_pages, 0)::INT AS pending_pages,
                s.id AS sub_id,
                s.page_limit AS base_limit,
                s.period_start,
                s.period_end,
                s.status AS sub_status,
                COALESCE(
                    (SELECT SUM(t.pages) FROM topups t WHERE t.subscription_id = s.id),
                    0
                )::INT AS topup_total,
                COALESCE(
                    (
                        SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
                        FROM llm_usage lu
                        WHERE lu.user_id = u.id
                          AND lu.call_type = 'extraction'
                          AND lu.page_num IS NOT NULL
                          AND lu.ts >= s.period_start AND lu.ts < s.period_end
                    ),
                    0
                )::INT AS used
            FROM users u
            LEFT JOIN subscriptions s ON s.user_id = u.id AND s.status = 'active'
            ORDER BY u.created_at DESC
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
    await invalidate_user_cache(user_uuid)
    return result == "UPDATE 1"


async def reactivate_user(pool: asyncpg.Pool, user_id: str) -> bool:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET is_active = TRUE WHERE id = $1", user_uuid,
        )
    await invalidate_user_cache(user_uuid)
    return result == "UPDATE 1"


async def hard_delete_user(pool: asyncpg.Pool, user_id: str) -> bool:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM users WHERE id = $1", user_uuid,
        )
    await invalidate_user_cache(user_uuid)
    return result == "DELETE 1"


async def reset_user_password(pool: asyncpg.Pool, user_id: str, hashed_pw: str) -> bool:
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET hashed_pw = $1 WHERE id = $2", hashed_pw, user_uuid,
        )
    await invalidate_user_cache(user_uuid)
    return result == "UPDATE 1"


async def delete_vendor(pool: asyncpg.Pool, vendor_id: str) -> bool:
    """Delete a vendor and dependent rows even on schemas without FK cascades."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Some deployments still have vendor foreign keys without ON DELETE
            # CASCADE, so delete dependents explicitly before removing the vendor.
            await conn.execute("DELETE FROM gold_examples WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM extractions WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM documents WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM vendor_aliases WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM templates WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM spatial_memory WHERE vendor_id = $1", vendor_id)
            await conn.execute("DELETE FROM qwen_layout_boxes WHERE vendor_id = $1", vendor_id)
            result = await conn.execute("DELETE FROM vendors WHERE id = $1", vendor_id)
            deleted = result == "DELETE 1"
    if deleted:
        await invalidate_vendor_cache(vendor_id)
    return deleted

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
            vendor_id, pattern.strip(), weight, source,
        )
    if not row:
        return None
    await invalidate_alias_cache(vendor_id)
    return dict(row)


async def list_vendor_aliases(pool: asyncpg.Pool, vendor_id: str | None = None) -> list[dict]:
    """List vendor aliases, optionally filtered by vendor_id."""

    async def _load():
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

    return await _cached_read(f"aliases:list:{vendor_id or 'all'}", CACHE_TTL_ALIAS, _load)


async def delete_vendor_alias(pool: asyncpg.Pool, alias_id: int) -> bool:
    """Delete a single vendor alias by id."""
    async with pool.acquire() as conn:
        vendor_id = await conn.fetchval(
            "DELETE FROM vendor_aliases WHERE id = $1 RETURNING vendor_id", alias_id,
        )
    if vendor_id is None:
        return False
    await invalidate_alias_cache(vendor_id)
    return True


async def get_all_aliases_for_detection(
    pool: asyncpg.Pool, user_id: str | None = None
) -> list[dict]:
    """Load vendor aliases for detection, optionally scoped to a single user's vendors."""

    async def _load():
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT va.vendor_id, v.name AS vendor_name, va.pattern, va.weight, va.source
                FROM vendor_aliases va
                JOIN vendors v ON v.id = va.vendor_id
                WHERE va.vendor_id <> '_auto'
                  AND ($1::UUID IS NULL OR v.user_id = $1)
                ORDER BY va.vendor_id, va.weight DESC
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    return await _cached_read(f"aliases:detect:{user_id or 'all'}", CACHE_TTL_ALIAS, _load)


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


async def get_spatial_memory_by_id(
    pool: asyncpg.Pool,
    sm_id: int,
) -> dict | None:
    """Fetch a single spatial memory entry by primary key (active or inactive)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, vendor_id, layout_key, field_key, page_number,
                   normalized_box, source_engine, created_from_extraction_id,
                   last_verified_at, is_active
            FROM spatial_memory
            WHERE id = $1
            """,
            sm_id,
        )
        if row is None:
            return None
        d = dict(row)
        _parse_jsonb(d, "normalized_box")
        return d


async def _delete_gold_correction_field_conn(
    conn: asyncpg.Connection,
    vendor_id: str,
    field_key: str,
) -> int:
    key = str(field_key or "").strip()
    if not vendor_id or not key:
        return 0
    rows = await conn.fetch(
        """
        UPDATE gold_examples
           SET correction_diff = correction_diff - $2::TEXT
         WHERE vendor_id = $1
           AND correction_diff IS NOT NULL
           AND correction_diff ? $2::TEXT
        RETURNING id, correction_diff
        """,
        vendor_id,
        key,
    )
    empty_ids: list[int] = []
    for row in rows:
        diff = row["correction_diff"]
        if isinstance(diff, str):
            diff = json.loads(diff)
        if diff == {}:
            empty_ids.append(int(row["id"]))
    if empty_ids:
        await conn.execute(
            "DELETE FROM gold_examples WHERE id = ANY($1::INT[])",
            empty_ids,
        )
    return len(rows)


async def delete_spatial_memory_by_id(
    pool: asyncpg.Pool,
    sm_id: int,
    *,
    delete_gold_correction: bool = False,
) -> dict | None:
    """Hard-delete a spatial memory entry by ID. Returns the deleted row or None."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                DELETE FROM spatial_memory
                WHERE id = $1
                RETURNING id, vendor_id, layout_key, field_key, page_number
                """,
                sm_id,
            )
            if not row:
                return None
            result = dict(row)
            if delete_gold_correction:
                result["gold_correction_fields_deleted"] = await _delete_gold_correction_field_conn(
                    conn,
                    result["vendor_id"],
                    result["field_key"],
                )
            return result


async def list_spatial_memory_for_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
) -> list[dict]:
    """List all active spatial memory entries for a vendor (all layouts)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, vendor_id, layout_key, field_key, page_number,
                   normalized_box, source_engine, created_from_extraction_id,
                   last_verified_at, is_active
            FROM spatial_memory
            WHERE vendor_id = $1 AND is_active = TRUE
            ORDER BY layout_key, field_key, page_number
            """,
            vendor_id,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "normalized_box")
            results.append(d)
        return results


async def list_spatial_memory_all(
    pool: asyncpg.Pool,
    *,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """Admin: list all active spatial memory entries across all vendors."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sm.id, sm.vendor_id, v.name AS vendor_name,
                   sm.layout_key, sm.field_key, sm.page_number,
                   sm.normalized_box, sm.source_engine,
                   sm.created_from_extraction_id,
                   sm.last_verified_at, sm.is_active,
                   u.email AS client_email
            FROM spatial_memory sm
            LEFT JOIN vendors v ON v.id = sm.vendor_id
            LEFT JOIN users u ON u.id = v.user_id
            WHERE sm.is_active = TRUE
            ORDER BY u.email NULLS LAST, v.name, sm.field_key, sm.page_number
            LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "normalized_box")
            results.append(d)
        return results


async def count_spatial_memory_all(pool: asyncpg.Pool) -> int:
    """Admin: total count of active spatial memory entries across all vendors."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM spatial_memory WHERE is_active = TRUE"
        )


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
    async def _load():
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

    return await _cached_read(f"template:{vendor_id}", CACHE_TTL_TEMPLATE, _load)


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
    await invalidate_template_cache(vendor_id)
    return d


async def list_all_templates(pool: asyncpg.Pool, user_id: str | None = None) -> list[dict]:
    """Return all templates joined with vendor name for the saved-templates page."""

    async def _load():
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

    return await _cached_read(f"templates:list:{user_id or 'all'}", CACHE_TTL_TEMPLATE, _load)


# -- Field mapping queries -------------------------------------------------

_FIELD_MAPPING_COLS = """
    id, vendor_id, template_id, schema_id, header_map, line_map,
    header_snapshot, line_snapshot, pending_notices, created_at, updated_at
"""


async def get_field_mapping(pool: asyncpg.Pool, vendor_id: str) -> dict | None:
    """Return the ERP field mapping for a vendor, or None if not configured."""

    async def _load():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_FIELD_MAPPING_COLS} FROM field_mappings WHERE vendor_id = $1",
                vendor_id,
            )
            if not row:
                return None
            d = dict(row)
            _parse_jsonb(d, "header_map", "line_map", "header_snapshot",
                         "line_snapshot", "pending_notices")
            return d

    return await _cached_read(f"mapping:{vendor_id}", CACHE_TTL_MAPPING, _load)


async def upsert_field_mapping(
    pool: asyncpg.Pool,
    vendor_id: str,
    template_id: int | None,
    header_map: dict,
    line_map: dict,
    header_snapshot: list[str],
    line_snapshot: list[str],
    pending_notices: list[dict],
    schema_id: int | None = None,
) -> dict:
    """Insert or update a vendor's ERP field mapping (one per vendor)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO field_mappings
                (vendor_id, template_id, schema_id, header_map, line_map,
                 header_snapshot, line_snapshot, pending_notices, updated_at)
            VALUES ($1, $2, $8, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, NOW())
            ON CONFLICT (vendor_id) DO UPDATE SET
                template_id     = EXCLUDED.template_id,
                schema_id       = EXCLUDED.schema_id,
                header_map      = EXCLUDED.header_map,
                line_map        = EXCLUDED.line_map,
                header_snapshot = EXCLUDED.header_snapshot,
                line_snapshot   = EXCLUDED.line_snapshot,
                pending_notices = EXCLUDED.pending_notices,
                updated_at      = NOW()
            RETURNING {_FIELD_MAPPING_COLS}
            """,
            vendor_id, template_id,
            json.dumps(header_map), json.dumps(line_map),
            json.dumps(header_snapshot), json.dumps(line_snapshot),
            json.dumps(pending_notices), schema_id,
        )
    d = dict(row)
    _parse_jsonb(d, "header_map", "line_map", "header_snapshot",
                 "line_snapshot", "pending_notices")
    await invalidate_mapping_cache(vendor_id)
    return d


async def set_field_mapping_notices(
    pool: asyncpg.Pool, vendor_id: str, pending_notices: list[dict],
) -> None:
    """Overwrite the pending rename notices for a vendor's mapping."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE field_mappings SET pending_notices = $2::jsonb, updated_at = NOW() "
            "WHERE vendor_id = $1",
            vendor_id, json.dumps(pending_notices),
        )
    await invalidate_mapping_cache(vendor_id)


async def update_extraction_mapped_result(
    pool: asyncpg.Pool, extraction_id: int, mapped_result: Any,
) -> None:
    """Store (or clear) the canonical-field mapped result for an extraction."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE extractions SET mapped_result = $2::jsonb, updated_at = NOW() "
            "WHERE id = $1",
            extraction_id,
            json.dumps(mapped_result) if mapped_result is not None else None,
        )


async def get_extraction_mapped_result(pool: asyncpg.Pool, extraction_id: int) -> Any:
    """Return the stored mapped_result for an extraction, or None."""
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT mapped_result FROM extractions WHERE id = $1", extraction_id,
        )
        if isinstance(val, str):
            val = json.loads(val)
        return _normalize_mapped_result(val)


def _normalize_mapped_result(val: Any) -> Any:
    """Rename legacy 'items' key to 'line_items' in stored mapped results."""
    if isinstance(val, list):
        return [_normalize_mapped_result(v) for v in val]
    if isinstance(val, dict) and "items" in val and "line_items" not in val:
        out = dict(val)
        out["line_items"] = out.pop("items")
        return out
    return val


# -- Document queries ------------------------------------------------------

async def create_document(
    pool: asyncpg.Pool,
    vendor_id: str | None,
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


async def set_document_reserved_pages(pool: asyncpg.Pool, document_id: int, pages: int) -> None:
    """Record the currently outstanding quota reservation on the document.

    The worker release paths read metadata.reserved_pages to know how many pages
    to return to the user's quota when the pipeline ends. Resume reserves only
    the missing pages, so this must be updated to that smaller count (it was set
    to the full page count at initial submission).
    """
    if not document_id:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE documents
               SET metadata = COALESCE(metadata, '{}'::jsonb)
                              || jsonb_build_object('reserved_pages', $2::int),
                   updated_at = NOW()
             WHERE id = $1
            """,
            document_id, int(pages),
        )


# -- Extraction queries ----------------------------------------------------

_EXTRACTION_COLS = """
    e.id, e.document_id, e.vendor_id, v.name AS vendor_name, e.template_id,
    e.filename, e.total_pages, e.format_type, e.header_fields, e.line_item_fields,
    e.result, e.page_results, e.field_locations, e.ocr_data,
    e.corrected_result, e.correction_meta, e.export_object_key,
    e.progress, e.cancel_requested, e.status, e.error, e.duration_ms,
    e.universal_agent, e.created_at, e.updated_at
"""


async def create_extraction(
    pool: asyncpg.Pool,
    vendor_id: str | None,
    template_id: int | None,
    filename: str,
    total_pages: int,
    format_type: str | None,
    header_fields: list[str],
    line_item_fields: list[str],
    document_id: int | None = None,
    universal_agent: bool = False,
) -> dict:
    # vendor_id/format_type/fields may be None/empty when the document is
    # submitted for auto-detection — the normalize stage fills them in once it
    # detects the vendor (see update_extraction_vendor). The columns are
    # nullable, so this is a no-op at the schema level.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO extractions
                (document_id, vendor_id, template_id, filename, total_pages,
                 format_type, header_fields, line_item_fields, status, progress,
                 universal_agent)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, 'queued', $9::jsonb, $10)
            RETURNING id
            """,
            document_id, vendor_id, template_id, filename, total_pages,
            format_type, json.dumps(header_fields), json.dumps(line_item_fields),
            json.dumps({"stage": "queued", "message": "Queued for processing"}),
            universal_agent,
        )
        return await get_extraction(pool, row["id"]) or {}


async def update_extraction_vendor(
    pool: asyncpg.Pool,
    extraction_id: int,
    *,
    vendor_id: str,
    template_id: int | None,
    format_type: str | None,
    header_fields: list[str],
    line_item_fields: list[str],
) -> None:
    """Persist a vendor detected during the normalize stage.

    Sets the extraction's vendor/template/format/field lists AND the parent
    document's vendor_id in one transaction so the two never drift apart.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE extractions
                   SET vendor_id        = $2,
                       template_id      = $3,
                       format_type      = $4,
                       header_fields    = $5::jsonb,
                       line_item_fields = $6::jsonb,
                       updated_at       = NOW()
                 WHERE id = $1
                """,
                extraction_id, vendor_id, template_id, format_type,
                json.dumps(header_fields), json.dumps(line_item_fields),
            )
            await conn.execute(
                """
                UPDATE documents d
                   SET vendor_id  = $2,
                       updated_at = NOW()
                  FROM extractions e
                 WHERE e.id = $1 AND d.id = e.document_id
                """,
                extraction_id, vendor_id,
            )


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


async def list_extractions(
    pool: asyncpg.Pool,
    vendor_id: str,
    limit: int = 20,
    user_id: str | None = None,
) -> list[dict]:
    uid = _uuid_or_none(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            LEFT JOIN documents d ON d.id = e.document_id
            WHERE e.vendor_id = $1
              AND ($3::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $3)
            ORDER BY e.created_at DESC LIMIT $2
            """,
            vendor_id, limit, uid,
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
    limit: int = 10,
    offset: int = 0,
    user_id: str | None = None,
) -> list[dict]:
    """Global extraction history with vendor_name joined."""
    uid = _uuid_or_none(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            LEFT JOIN documents d ON d.id = e.document_id
            WHERE ($3::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $3)
            ORDER BY e.created_at DESC LIMIT $1 OFFSET $2
            """,
            limit, offset, uid,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results",
                         "field_locations", "ocr_data", "corrected_result", "correction_meta", "progress")
            results.append(d)
        return results


async def count_all_extractions(pool: asyncpg.Pool, user_id: str | None = None) -> int:
    uid = _uuid_or_none(user_id)
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*) FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            LEFT JOIN documents d ON d.id = e.document_id
            WHERE ($1::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1)
            """,
            uid,
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
    spatial memory reads text from the current OCR/pdfium word boxes. If the
    OCR branch failed, postprocess may still finalize the LLM JSON as partial
    and explicitly disable OCR-backed review features.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH latest_ocr AS (
                SELECT status
                FROM jobs
                WHERE extraction_id = $1
                  AND job_type = 'ocr'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            SELECT (
                e.status = 'processing'
                AND e.error IS NULL
                AND e.result IS NOT NULL
                AND NOT COALESCE((e.result->>'_all_pages_failed') = 'true', FALSE)
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(COALESCE(e.page_results, '[]'::jsonb)) AS pr
                    WHERE pr ? '_error'
                )
                AND (
                    e.ocr_data IS NOT NULL
                    OR COALESCE((SELECT status = 'failed' FROM latest_ocr), FALSE)
                )
            ) AS ready
            FROM extractions e
            WHERE e.id = $1
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


async def delete_extraction(pool: asyncpg.Pool, extraction_id: int) -> bool:
    """Delete exactly one extraction record."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM pages WHERE extraction_id = $1", extraction_id)
            await conn.execute("DELETE FROM jobs WHERE extraction_id = $1", extraction_id)
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
    """Store a human-verified correction as audit history.

    Prompt builders must redact field values before using this data.
    """
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


async def delete_gold_correction_field(
    pool: asyncpg.Pool,
    vendor_id: str,
    field_key: str,
) -> int:
    """Remove a field from all prompt correction examples for a vendor.

    Removing every occurrence prevents get_gold_examples() from falling back to
    an older correction for the same field after the latest one is deleted.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _delete_gold_correction_field_conn(conn, vendor_id, field_key)


async def get_gold_examples(
    pool: asyncpg.Pool,
    vendor_id: str,
    limit: int | None = None,
) -> list[dict]:
    """Retrieve latest correction per field for value-redacted prompt hints.

    gold_examples remains append-only for audit history, but the prompt should
    not receive stale conflicting hints for the same field. This returns a
    single consolidated correction_diff object where each field uses its latest
    saved correction. Callers must not expose the raw values to the model.
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
        return int((result or "DELETE 0").split()[-1])


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
        return int((result or "DELETE 0").split()[-1])


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


async def cancel_job(pool: asyncpg.Pool, job_id: int, progress: dict | None = None, error: str | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled',
                progress = COALESCE($2::jsonb, progress),
                finished_at = NOW(),
                updated_at = NOW(),
                error = COALESCE($3, error, 'Cancelled')
            WHERE id = $1
            """,
            job_id,
            json.dumps(progress) if progress is not None else None,
            error,
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
    left in 'running' state by a crashed or killed worker process. Also purges expired idempotency claims.
    Returns the number of jobs recovered.
    """
    async with pool.acquire() as conn:
        # Purge expired idempotency claims older than 24 hours
        try:
            await conn.execute("DELETE FROM idempotency_claims WHERE created_at < NOW() - INTERVAL '24 hours'")
        except Exception as exc:
            logging.getLogger("db").warning("recover_stale_jobs: idempotency prune failed: %s", exc)

        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE jobs
                SET status     = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
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
                RETURNING id, extraction_id, status
                """,
                stage, stale_minutes,
            )
            failed_ext_ids = [
                r["extraction_id"] for r in rows
                if r["status"] == "failed" and r["extraction_id"] is not None
            ]
            if failed_ext_ids:
                if stage == "ocr":
                    await conn.execute(
                        """
                        UPDATE extractions
                        SET progress = jsonb_build_object(
                                'stage', 'ocr',
                                'message', 'JSON extraction may still complete, but OCR-backed review is unavailable.',
                                'review_available', false,
                                'ocr_error', 'Worker crash - max attempts reached'
                            ),
                            updated_at = NOW()
                        WHERE id = ANY($1::int[])
                          AND status NOT IN ('done', 'failed', 'partial', 'cancelled')
                        """,
                        failed_ext_ids,
                    )
                    await conn.execute(
                        """
                        INSERT INTO jobs (extraction_id, document_id, job_type, payload)
                        SELECT e.id, e.document_id, 'postprocess', jsonb_build_object('extraction_id', e.id)
                        FROM extractions e
                        WHERE e.id = ANY($1::int[])
                          AND e.status = 'processing'
                          AND e.result IS NOT NULL
                        ON CONFLICT (extraction_id, job_type)
                        WHERE status IN ('queued', 'running') AND extraction_id IS NOT NULL
                        DO NOTHING
                        """,
                        failed_ext_ids,
                    )
                    return len(rows)
                await conn.execute(
                    """
                    UPDATE extractions
                    SET status     = 'failed',
                        error      = 'Worker crash — max attempts reached',
                        progress   = '{"stage":"failed","message":"Worker crash — max attempts reached"}'::jsonb,
                        updated_at = NOW()
                    WHERE id = ANY($1::int[])
                      AND status NOT IN ('done', 'failed', 'partial', 'cancelled')
                    """,
                    failed_ext_ids,
                )
            return len(rows)  # job count — matches what callers log



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


async def get_latest_job_for_extraction_type(
    pool: asyncpg.Pool,
    extraction_id: int,
    job_type: str,
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, extraction_id, document_id, job_type, status, payload, progress,
                   attempts, max_attempts, priority, locked_by, locked_at,
                   started_at, finished_at, error, created_at, updated_at
            FROM jobs
            WHERE extraction_id = $1
              AND job_type = $2
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            extraction_id,
            job_type,
        )
        return _record(row, "payload", "progress")


async def latest_job_failed(pool: asyncpg.Pool, extraction_id: int, job_type: str) -> bool:
    job = await get_latest_job_for_extraction_type(pool, extraction_id, job_type)
    return bool(job and job.get("status") == "failed")


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
    end_to_end: bool = False,
) -> None:
    # `end_to_end=True` recomputes duration_ms as the full pipeline wall-clock
    # (created_at → now), overriding the LLM-stage-only value the llm worker
    # stored. This is what the history page shows as "End-to-end latency", and it
    # matches the live timer on the pipeline page (normalize → ocr → llm →
    # postprocess), not just the llama-server call.
    args = [
        extraction_id,
        status,
        json.dumps(progress) if progress is not None else None,
        error,
    ]
    if end_to_end:
        # No $5: recompute from created_at in SQL. Passing an unused parameter
        # would make asyncpg reject the query (arg-count mismatch).
        duration_expr = "ROUND(EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000)::INT"
    else:
        duration_expr = "COALESCE($5, duration_ms)"
        args.append(duration_ms)
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE extractions
            SET status = $2,
                progress = COALESCE($3::jsonb, progress),
                error = $4,
                duration_ms = {duration_expr},
                updated_at = NOW()
            WHERE id = $1
            """,
            *args,
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

# -- Subscription / page-limit queries ------------------------------------

async def get_user_billable_pages(
    pool: asyncpg.Pool,
    user_id: str,
) -> dict:
    """Return current billable pages, effective subscription limit, and
    remaining pages for a user.

    Uses the v2 quota model:
      * billable_pages = COUNT(DISTINCT (extraction_id, page_num)) within the
        active subscription's window. Falls back to all-time when there is no
        active subscription so admins can still see total usage history.
      * subscription_limit = active subscription base + topups, or 0 when none.
      * Extra fields (period_start/end, topup_total) included so callers (UI,
        warning banners) don't need a second round-trip.
    """
    uid = _uuid_or_none(user_id)
    blank = {
        "billable_pages": 0, "subscription_limit": 0, "remaining": 0,
        "base_limit": 0, "topup_total": 0,
        "period_start": None, "period_end": None,
        "has_active_subscription": False,
    }
    if uid is None:
        return blank
    quota = await get_user_quota_v2(pool, user_id)
    if quota["has_active_subscription"]:
        return {
            "billable_pages": quota["used"],
            "subscription_limit": quota["effective_limit"],
            "remaining": quota["effective_limit"] - quota["used"],
            "base_limit": quota["base_limit"],
            "topup_total": quota["topup_total"],
            "period_start": quota["period_start"],
            "period_end": quota["period_end"],
            "has_active_subscription": True,
        }
    # No active subscription — report lifetime usage so the admin UI is still useful.
    async with pool.acquire() as conn:
        used = int(await conn.fetchval(
            """
            SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
            FROM llm_usage lu
            WHERE lu.user_id = $1
              AND lu.call_type = 'extraction'
              AND lu.page_num IS NOT NULL
            """,
            uid,
        ) or 0)
    return {**blank, "billable_pages": used, "remaining": -used}


async def reserve_quota(
    pool: asyncpg.Pool,
    user_id: str,
    incoming_pages: int,
    grace_pages: int = 10,
) -> dict:
    """Atomically check quota and reserve pages for an in-flight upload.

    Uses SELECT … FOR UPDATE to serialize concurrent uploads from the same user
    so two simultaneous requests cannot both see the same usage snapshot.
    If allowed, increments pending_pages by incoming_pages — this acts as a
    reservation that future concurrent checks will see immediately.

    Quota source is the v2 model:
        effective_limit = active_subscription.page_limit + SUM(topups)
        used            = pages billed within [period_start, period_end]

    Behavior matrix:
        active subscription, fits          → allowed, reason='ok'
        active subscription, near overage  → allowed (if ≤ grace_pages),
                                              reason='grace'
        active subscription, over          → blocked, reason='exceeded'
        no active subscription (expired,
            cancelled, never set)          → blocked, reason='no_subscription'

    Returns: allowed, reason, used, limit, remaining, pending.
    """
    uid = _uuid_or_none(user_id)
    blocked = {"allowed": False, "reason": "exceeded", "used": 0, "limit": 0,
               "remaining": 0, "pending": 0}
    if uid is None:
        return blocked
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lazy expiry inside the txn so concurrent uploads see the same view.
            await conn.execute(
                """
                WITH expired AS (
                    UPDATE subscriptions
                       SET status = 'expired'
                     WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                     RETURNING user_id
                )
                UPDATE users
                   SET subscription_limit = 0
                 WHERE id IN (SELECT user_id FROM expired)
                """,
                uid,
            )
            # Lock the user row so concurrent reserves serialise on the
            # pending_pages counter.
            user_row = await conn.fetchrow(
                "SELECT COALESCE(pending_pages, 0) AS pending FROM users WHERE id = $1 FOR UPDATE",
                uid,
            )
            if not user_row:
                return blocked
            pending = int(user_row["pending"])

            sub = await conn.fetchrow(
                """
                SELECT id, page_limit, period_start, period_end
                FROM subscriptions
                WHERE user_id = $1 AND status = 'active'
                LIMIT 1
                """,
                uid,
            )
            if not sub:
                return {**blocked, "reason": "no_subscription", "pending": pending}

            sub_id = int(sub["id"])
            base_limit = int(sub["page_limit"])
            period_start = sub["period_start"]
            period_end = sub["period_end"]
            topup_total = int(await conn.fetchval(
                "SELECT COALESCE(SUM(pages), 0) FROM topups WHERE subscription_id = $1",
                sub_id,
            ) or 0)
            effective_limit = base_limit + topup_total

            used = int(await conn.fetchval(
                """
                SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
                FROM llm_usage lu
                WHERE lu.user_id = $1
                  AND lu.call_type = 'extraction'
                  AND lu.page_num IS NOT NULL
                  AND lu.ts >= $2 AND lu.ts < $3
                """,
                uid, period_start, period_end,
            ) or 0)

            committed = used + pending
            remaining = max(effective_limit - committed, 0)
            would_exceed = (committed + incoming_pages) > effective_limit

            if not would_exceed:
                reason, allowed = "ok", True
            elif committed < effective_limit and incoming_pages <= grace_pages:
                # Grace applies only when the user still has headroom (committed
                # strictly under the limit). At exactly limit==committed there is
                # no room left, so block — no grace.
                reason, allowed = "grace", True
            else:
                reason, allowed = "exceeded", False

            grace_pages_used = 0
            if reason == "grace":
                grace_pages_used = max(0, committed + incoming_pages - effective_limit)

            if allowed:
                await conn.execute(
                    "UPDATE users SET pending_pages = pending_pages + $1 WHERE id = $2",
                    incoming_pages, uid,
                )
            return {
                "allowed": allowed,
                "reason": reason,
                "used": used,
                "limit": effective_limit,
                "remaining": remaining,
                "pending": pending,
                "grace_pages_used": grace_pages_used,
            }


async def release_quota_reservation(
    pool: asyncpg.Pool,
    user_id: str,
    pages: int,
) -> None:
    """Decrement pending_pages after the normalize worker finishes (success or failure).

    Uses GREATEST(0, …) so a double-release or stale reservation never drives
    pending_pages negative.
    """
    uid = _uuid_or_none(user_id)
    if uid is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET pending_pages = GREATEST(0, pending_pages - $1) WHERE id = $2",
            pages, uid,
        )


async def insert_quota_grace_event(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    event_type: str,
    grace_pages_used: int,
    incoming_pages: int,
    used_before: int,
    limit_at_time: int,
    filename: str | None = None,
) -> None:
    """Record one grace overage or hard-block event for admin visibility.

    event_type: 'grace_used' | 'exceeded'
    Never raises — event logging must not crash the upload path.
    """
    uid = _uuid_or_none(user_id)
    if uid is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO quota_grace_events
                    (user_id, event_type, grace_pages_used, incoming_pages,
                     used_before, limit_at_time, filename)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                uid, event_type, grace_pages_used, incoming_pages,
                used_before, limit_at_time, filename,
            )
    except Exception:
        pass  # best-effort — never crash the upload path


async def get_admin_quota_events(
    pool: asyncpg.Pool,
    limit: int = 100,
) -> list[dict]:
    """Return recent quota grace/exceeded events with the user's email."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT qge.id, qge.event_ts, qge.event_type, qge.grace_pages_used,
                   qge.incoming_pages, qge.used_before, qge.limit_at_time,
                   qge.filename, u.email
            FROM quota_grace_events qge
            JOIN users u ON u.id = qge.user_id
            ORDER BY qge.event_ts DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def release_quota_once(pool: asyncpg.Pool, document_id: int | None, user_id: str | None) -> int:
    """Idempotently release a document's page reservation.

    Reads metadata.reserved_pages and decrements the billing user's pending_pages
    by that amount, then clears reserved_pages in the SAME transaction. A second
    call (e.g. a job re-run after stale-job recovery picks up a job that released
    but crashed before complete_job) finds reserved_pages already gone and is a
    no-op — so a double-release can't steal pending quota from another in-flight
    upload by the same user. Returns the pages actually released.
    """
    if document_id is None:
        return 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT metadata FROM documents WHERE id = $1 FOR UPDATE",
                document_id,
            )
            if not row:
                return 0
            meta = row["metadata"]
            if isinstance(meta, str):
                import json as _json
                meta = _json.loads(meta) if meta else {}
            meta = meta or {}
            raw_pages = meta.get("reserved_pages")
            try:
                pages = int(raw_pages) if raw_pages is not None else 0
            except (TypeError, ValueError):
                pages = 0
            if pages <= 0:
                return 0
            uid = _uuid_or_none(user_id) or _uuid_or_none(meta.get("billing_user_id"))
            if uid is not None:
                await conn.execute(
                    "UPDATE users SET pending_pages = GREATEST(0, pending_pages - $1) WHERE id = $2",
                    pages, uid,
                )
            await conn.execute(
                """
                UPDATE documents
                   SET metadata = COALESCE(metadata, '{}'::jsonb) - 'reserved_pages',
                       updated_at = NOW()
                 WHERE id = $1
                """,
                document_id,
            )
    return pages


async def update_user_subscription_limit(
    pool: asyncpg.Pool,
    user_id: str,
    new_limit: int,
) -> bool:
    """Update the subscription_limit for a user. Admin-only at runtime."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET subscription_limit = $1 WHERE id = $2",
            new_limit,
            uid,
        )
    await invalidate_user_cache(uid)  # subscription_limit is in the cached user row
    return result == "UPDATE 1"


# -- Subscriptions + top-ups (v2 quota model) ------------------------------
#
# Quota model:
#   effective_limit  = active_subscription.page_limit + SUM(topups in same sub)
#   used             = pages billed within [period_start, period_end]
#   remaining        = max(effective_limit - used, 0)
#
# On period_end pass: status flips active → expired (lazy on read + endpoint).
# Topups are bound to their subscription via FK, so they vanish with it.

def _row_subscription(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    if d.get("user_id") is not None:
        d["user_id"] = str(d["user_id"])
    if d.get("created_by") is not None:
        d["created_by"] = str(d["created_by"])
    return d


async def expire_due_subscriptions(pool: asyncpg.Pool) -> int:
    """Flip any active subscription past its period_end to status='expired'.
    Returns number of rows flipped. Safe to call repeatedly."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            WITH expired AS (
                UPDATE subscriptions
                   SET status = 'expired'
                 WHERE status = 'active' AND period_end < NOW()
                 RETURNING user_id
            )
            UPDATE users
               SET subscription_limit = 0
             WHERE id IN (SELECT user_id FROM expired)
            """
        )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def create_subscription(
    pool: asyncpg.Pool,
    user_id: str,
    page_limit: int,
    period_start,
    period_end,
    note: str | None = None,
    created_by: str | None = None,
) -> dict | None:
    """Create a new active subscription. Any prior active subscription for the
    same user is marked 'superseded' in the same transaction so the partial
    unique index (one active per user) is respected."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return None
    if page_limit < 0:
        raise ValueError("page_limit must be non-negative")
    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")
    creator_uuid = _uuid_or_none(created_by) if created_by else None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE subscriptions
                   SET status = 'superseded'
                 WHERE user_id = $1 AND status = 'active'
                """,
                uid,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO subscriptions
                    (user_id, page_limit, period_start, period_end, status, note, created_by)
                VALUES ($1, $2, $3, $4, 'active', $5, $6)
                RETURNING id, user_id, page_limit, period_start, period_end,
                          status, note, created_by, created_at
                """,
                uid, int(page_limit), period_start, period_end, note, creator_uuid,
            )
            # Keep the legacy users.subscription_limit in sync so any code path
            # that still reads it sees the new base.
            await conn.execute(
                "UPDATE users SET subscription_limit = $1 WHERE id = $2",
                int(page_limit), uid,
            )
    await invalidate_user_cache(uid)  # legacy subscription_limit mirror changed
    return _row_subscription(row)


async def cancel_subscription(
    pool: asyncpg.Pool,
    user_id: str,
    subscription_id: int,
) -> bool:
    """Mark a subscription as cancelled (admin action). Only affects rows
    belonging to this user; idempotent if already cancelled."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return False
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE subscriptions
                   SET status = 'cancelled'
                 WHERE id = $1 AND user_id = $2 AND status = 'active'
                """,
                int(subscription_id), uid,
            )
            is_cancelled = result == "UPDATE 1"
            if is_cancelled:
                await conn.execute(
                    "UPDATE users SET subscription_limit = 0 WHERE id = $1",
                    uid,
                )
    if is_cancelled:
        await invalidate_user_cache(uid)  # legacy subscription_limit mirror changed
    return is_cancelled


async def get_active_subscription(
    pool: asyncpg.Pool,
    user_id: str,
) -> dict | None:
    """Return the user's currently active subscription, or None.
    Auto-expires stale rows before reading."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return None
    async with pool.acquire() as conn:
        # Lazy expiry — keeps reads correct even if a background job hasn't run.
        await conn.execute(
            """
            WITH expired AS (
                UPDATE subscriptions
                   SET status = 'expired'
                 WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                 RETURNING user_id
            )
            UPDATE users
               SET subscription_limit = 0
             WHERE id IN (SELECT user_id FROM expired)
            """,
            uid,
        )
        row = await conn.fetchrow(
            """
            SELECT id, user_id, page_limit, period_start, period_end,
                   status, note, created_by, created_at
            FROM subscriptions
            WHERE user_id = $1 AND status = 'active'
            LIMIT 1
            """,
            uid,
        )
    return _row_subscription(row)


async def list_user_subscriptions(
    pool: asyncpg.Pool,
    user_id: str,
) -> list[dict]:
    """All subscriptions for this user, newest first."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, page_limit, period_start, period_end,
                   status, note, created_by, created_at
            FROM subscriptions
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            uid,
        )
    return [_row_subscription(r) for r in rows]


async def add_topup(
    pool: asyncpg.Pool,
    user_id: str,
    pages: int,
    note: str | None = None,
    created_by: str | None = None,
) -> dict | None:
    """Attach pages to the user's current active subscription.
    Returns None if there is no active subscription (admin must create
    a subscription period first)."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return None
    if pages <= 0:
        raise ValueError("topup pages must be positive")
    creator_uuid = _uuid_or_none(created_by) if created_by else None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                WITH expired AS (
                    UPDATE subscriptions
                       SET status = 'expired'
                     WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                     RETURNING user_id
                )
                UPDATE users
                   SET subscription_limit = 0
                 WHERE id IN (SELECT user_id FROM expired)
                """,
                uid,
            )
            sub = await conn.fetchrow(
                "SELECT id FROM subscriptions WHERE user_id = $1 AND status = 'active' LIMIT 1",
                uid,
            )
            if not sub:
                return None
            row = await conn.fetchrow(
                """
                INSERT INTO topups (user_id, subscription_id, pages, note, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, user_id, subscription_id, pages, note, created_by, created_at
                """,
                uid, int(sub["id"]), int(pages), note, creator_uuid,
            )
    return _row_subscription(row)


async def list_topups_for_subscription(
    pool: asyncpg.Pool,
    subscription_id: int,
) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, subscription_id, pages, note, created_by, created_at
            FROM topups
            WHERE subscription_id = $1
            ORDER BY created_at DESC
            """,
            int(subscription_id),
        )
    return [_row_subscription(r) for r in rows]


async def list_topups_for_user(
    pool: asyncpg.Pool,
    user_id: str,
) -> list[dict]:
    uid = _uuid_or_none(user_id)
    if uid is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, subscription_id, pages, note, created_by, created_at
            FROM topups
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            uid,
        )
    return [_row_subscription(r) for r in rows]


async def get_user_quota_v2(
    pool: asyncpg.Pool,
    user_id: str,
) -> dict:
    """Compute the current page quota for a user using the new model.

    Returns:
        {
          has_active_subscription: bool,
          subscription_id: int | None,
          period_start: datetime | None,
          period_end: datetime | None,
          base_limit: int,        # subscription.page_limit
          topup_total: int,       # sum of topups for this subscription
          effective_limit: int,   # base + topups
          used: int,              # pages billed within window
          pending: int,           # pages reserved by in-flight uploads (legacy)
          remaining: int,         # max(effective_limit - used - pending, 0)
          status: str,            # 'active' | 'expired' | 'none'
        }

    For users with no active subscription we still return a shape so callers
    don't need null-checks; effective_limit/remaining are 0.
    """
    uid = _uuid_or_none(user_id)
    blank = {
        "has_active_subscription": False,
        "subscription_id": None,
        "period_start": None,
        "period_end": None,
        "base_limit": 0,
        "topup_total": 0,
        "effective_limit": 0,
        "used": 0,
        "pending": 0,
        "remaining": 0,
        "status": "none",
    }
    if uid is None:
        return blank
    async with pool.acquire() as conn:
        await conn.execute(
            """
            WITH expired AS (
                UPDATE subscriptions
                   SET status = 'expired'
                 WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                 RETURNING user_id
            )
            UPDATE users
               SET subscription_limit = 0
             WHERE id IN (SELECT user_id FROM expired)
            """,
            uid,
        )
        sub = await conn.fetchrow(
            """
            SELECT id, page_limit, period_start, period_end
            FROM subscriptions
            WHERE user_id = $1 AND status = 'active'
            LIMIT 1
            """,
            uid,
        )
        pending_row = await conn.fetchrow(
            "SELECT COALESCE(pending_pages, 0) AS pending FROM users WHERE id = $1",
            uid,
        )
        pending = int(pending_row["pending"]) if pending_row else 0
        if not sub:
            return {**blank, "pending": pending}
        sub_id = int(sub["id"])
        base_limit = int(sub["page_limit"])
        period_start = sub["period_start"]
        period_end = sub["period_end"]
        topup_total = int(await conn.fetchval(
            "SELECT COALESCE(SUM(pages), 0) FROM topups WHERE subscription_id = $1",
            sub_id,
        ) or 0)
        used = int(await conn.fetchval(
            """
            SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
            FROM llm_usage lu
            WHERE lu.user_id = $1
              AND lu.call_type = 'extraction'
              AND lu.page_num IS NOT NULL
              AND lu.ts >= $2 AND lu.ts < $3
            """,
            uid, period_start, period_end,
        ) or 0)
    effective_limit = base_limit + topup_total
    remaining = max(effective_limit - used - pending, 0)
    return {
        "has_active_subscription": True,
        "subscription_id": sub_id,
        "period_start": period_start,
        "period_end": period_end,
        "base_limit": base_limit,
        "topup_total": topup_total,
        "effective_limit": effective_limit,
        "used": used,
        "pending": pending,
        "remaining": remaining,
        "status": "active",
    }


async def get_user_history(
    pool: asyncpg.Pool,
    user_id: str,
) -> dict:
    """Return all subscriptions + all topups for a user (admin history view).
    Topups carry their subscription's period dates so the UI can show them
    on the same timeline."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return {"subscriptions": [], "topups": []}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            WITH expired AS (
                UPDATE subscriptions
                   SET status = 'expired'
                 WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                 RETURNING user_id
            )
            UPDATE users
               SET subscription_limit = 0
             WHERE id IN (SELECT user_id FROM expired)
            """,
            uid,
        )
        subs = await conn.fetch(
            """
            SELECT s.id, s.page_limit, s.period_start, s.period_end,
                   s.status, s.note, s.created_at,
                   admin.email AS created_by_email,
                   COALESCE((SELECT SUM(pages) FROM topups WHERE subscription_id = s.id), 0)::INT
                       AS topup_total,
                   (
                       SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
                       FROM llm_usage lu
                       WHERE lu.user_id = s.user_id
                         AND lu.call_type = 'extraction'
                         AND lu.page_num IS NOT NULL
                         AND lu.ts >= s.period_start AND lu.ts < s.period_end
                   )::INT AS pages_used
            FROM subscriptions s
            LEFT JOIN users admin ON admin.id = s.created_by
            WHERE s.user_id = $1
            ORDER BY s.created_at DESC
            """,
            uid,
        )
        tops = await conn.fetch(
            """
            SELECT t.id, t.subscription_id, t.pages, t.note, t.created_at,
                   admin.email AS created_by_email,
                   s.period_start AS sub_period_start,
                   s.period_end   AS sub_period_end,
                   s.status       AS sub_status
            FROM topups t
            JOIN subscriptions s ON s.id = t.subscription_id
            LEFT JOIN users admin ON admin.id = t.created_by
            WHERE t.user_id = $1
            ORDER BY t.created_at DESC
            """,
            uid,
        )
    return {
        "subscriptions": [dict(r) for r in subs],
        "topups": [dict(r) for r in tops],
    }


# -- Vendor dashboard stats ------------------------------------------------

async def get_client_vendor_summary(pool: asyncpg.Pool) -> list[dict]:
    """Admin: per-client row with vendor_count and extraction_count."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.id::TEXT AS user_id, u.email, u.role, u.is_active,
                   COUNT(DISTINCT v.id)::INT         AS vendor_count,
                   COUNT(DISTINCT e.id)::INT         AS extraction_count,
                   SUM(CASE WHEN e.status = 'done'   THEN 1 ELSE 0 END)::INT AS completed_extractions
            FROM users u
            LEFT JOIN vendors    v ON v.user_id = u.id
            LEFT JOIN extractions e ON e.vendor_id = v.id
            WHERE u.role = 'client'
            GROUP BY u.id, u.email, u.role, u.is_active
            ORDER BY u.email
            """
        )
    return [dict(r) for r in rows]


async def get_client_vendors_with_stats(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    """Vendor list for one client with extraction count, page total, and last run."""
    uid = _uuid_or_none(user_id)
    if uid is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT v.id AS vendor_id, v.name AS vendor_name, v.status,
                   COUNT(DISTINCT e.id)::INT              AS extraction_count,
                   COALESCE(SUM(e.total_pages), 0)::INT  AS total_pages_processed,
                   MAX(e.created_at)                      AS last_extraction_at,
                   SUM(CASE WHEN e.status = 'done'   THEN 1 ELSE 0 END)::INT AS completed,
                   SUM(CASE WHEN e.status = 'failed' THEN 1 ELSE 0 END)::INT AS failed
            FROM vendors v
            LEFT JOIN extractions e ON e.vendor_id = v.id
            WHERE v.user_id = $1
            GROUP BY v.id, v.name, v.status
            ORDER BY v.name
            """,
            uid,
        )
    return [dict(r) for r in rows]


async def get_vendor_extraction_daily(
    pool: asyncpg.Pool,
    vendor_id: str,
    limit: int = 30,
) -> list[dict]:
    """Per-day extraction counts for one vendor (most recent N days)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('day', e.created_at)::DATE::TEXT AS day,
                   COUNT(*)::INT                               AS extractions,
                   SUM(CASE WHEN e.status = 'done'   THEN 1 ELSE 0 END)::INT AS completed,
                   SUM(CASE WHEN e.status = 'failed' THEN 1 ELSE 0 END)::INT AS failed,
                   COALESCE(SUM(e.total_pages), 0)::INT        AS total_pages
            FROM extractions e
            WHERE e.vendor_id = $1
              AND e.created_at >= NOW() - ($2 * INTERVAL '1 day')
            GROUP BY 1
            ORDER BY 1 DESC
            """,
            vendor_id,
            limit,
        )
    return [dict(r) for r in rows]


async def get_vendor_page_stats(pool: asyncpg.Pool, vendor_id: str) -> list[dict]:
    """Per page_number aggregates across all done extractions for a vendor."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.page_number,
                   COUNT(*)::INT                                   AS times_processed,
                   AVG(NULLIF(lu.total_tokens,  0))::INT          AS avg_tokens,
                   AVG(NULLIF(lu.duration_ms,   0))::INT          AS avg_latency_ms
            FROM pages p
            JOIN extractions e
                 ON e.id = p.extraction_id
                AND e.vendor_id = $1
                AND e.status = 'done'
            LEFT JOIN llm_usage lu
                 ON lu.extraction_id = e.id
                AND lu.page_num = p.page_number
                AND lu.call_type = 'extraction'
            GROUP BY p.page_number
            ORDER BY p.page_number
            """,
            vendor_id,
        )
    return [dict(r) for r in rows]


# -- API Key CRUD ----------------------------------------------------------

async def get_api_key_encrypted(pool: asyncpg.Pool, key_id: int) -> dict | None:
    """Fetch the encrypted raw key for admin reveal."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, label, encrypted_key FROM api_keys WHERE id = $1",
            key_id,
        )
    return dict(row) if row else None


async def create_api_key(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    label: str,
    key_hash: str,
    prefix: str,
    encrypted_key: str | None = None,
    expires_at=None,
) -> dict:
    """Create a new API key row. Returns the created record."""
    from uuid import UUID as _UUID
    uid = _UUID(user_id) if isinstance(user_id, str) else user_id
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO api_keys (user_id, label, key_hash, prefix, encrypted_key, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, user_id, label, key_hash, prefix, is_active, created_at, last_used_at, expires_at
            """,
            uid, label, key_hash, prefix, encrypted_key, expires_at,
        )
    return _record(row) if row else {}


async def list_api_keys(pool: asyncpg.Pool) -> list[dict]:
    """List all API keys with owner email and usage stats."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ak.id, ak.user_id, ak.label, ak.prefix, ak.is_active,
                   ak.created_at, ak.last_used_at, ak.expires_at,
                   u.email AS owner_email,
                   COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
                   COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                   COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS total_tokens,
                   COALESCE(COUNT(DISTINCT (lu.extraction_id, lu.page_num)) FILTER (WHERE lu.call_type = 'extraction' AND lu.page_num IS NOT NULL), 0)::BIGINT AS total_pages,
                   COUNT(DISTINCT lu.doc_id)::INT AS total_documents
            FROM api_keys ak
            JOIN users u ON u.id = ak.user_id
            LEFT JOIN llm_usage lu ON lu.api_key_id = ak.id
            GROUP BY ak.id, ak.user_id, ak.label, ak.prefix, ak.is_active,
                     ak.created_at, ak.last_used_at, ak.expires_at, u.email
            ORDER BY ak.created_at DESC
            """
        )
    return [dict(r) for r in rows]


async def verify_api_key_hash(pool: asyncpg.Pool, key_hash: str) -> dict | None:
    """Look up an active API key by its SHA-256 hash, or None if missing/expired.

    Cached positive-only, with the TTL capped at the key's own expiry so a
    cached entry can never outlive the key it represents. Invalidated on any
    api-key mutation (see invalidate_api_key_cache).
    """
    cache = get_cache()
    ckey = f"apikey:{key_hash}"
    cached = await cache.get(ckey)
    if cached is not None:
        return dict(cached)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, is_active, expires_at
            FROM api_keys
            WHERE key_hash = $1
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            key_hash,
        )
    if not row:
        return None

    result = dict(row)
    ttl = CACHE_TTL_AUTH
    expires_at = result.get("expires_at")
    if expires_at is not None:
        # Column is TIMESTAMPTZ (aware), but guard against a naive value ever
        # reaching here — a TypeError must never break the auth path.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        secs = int((expires_at - datetime.now(timezone.utc)).total_seconds())
        if secs <= 0:
            return result  # expired in the race window — return but don't cache
        ttl = max(1, min(CACHE_TTL_AUTH, secs))
    await cache.set(ckey, result, ttl)
    return result


async def touch_api_key(pool: asyncpg.Pool, key_hash: str) -> None:
    """Update last_used_at, throttled to at most once per key per window.

    A marker key in the cache suppresses repeat writes; with no Redis the
    marker is never seen, so this writes on every call exactly as before.
    """
    cache = get_cache()
    tkey = f"apikey_touch:{key_hash}"
    if await cache.get(tkey) is not None:
        return
    await cache.set(tkey, 1, ttl=CACHE_TTL_APIKEY_TOUCH)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = $1",
            key_hash,
        )


async def deactivate_api_key(pool: asyncpg.Pool, key_id: int) -> bool:
    """Deactivate an API key (soft disable)."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE api_keys SET is_active = FALSE WHERE id = $1",
            key_id,
        )
    await invalidate_api_key_cache()
    return result == "UPDATE 1"


async def activate_api_key(pool: asyncpg.Pool, key_id: int) -> bool:
    """Reactivate a previously deactivated API key."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE api_keys SET is_active = TRUE WHERE id = $1",
            key_id,
        )
    await invalidate_api_key_cache()
    return result == "UPDATE 1"


async def delete_api_key(pool: asyncpg.Pool, key_id: int) -> bool:
    """Hard-delete an API key row."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM api_keys WHERE id = $1",
            key_id,
        )
    await invalidate_api_key_cache()
    return result == "DELETE 1"


async def claim_idempotency(pool: asyncpg.Pool, user_id: str, idempotency_key: str, file_sha256: str) -> dict:
    """Returns {"status": "claimed", "claim_id": int}
              {"status": "duplicate", "extraction_id": int, "extraction_status": str}
              {"status": "conflict"}
    """
    uid = _uuid_or_none(user_id)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO idempotency_claims (user_id, idempotency_key, file_sha256) "
                "VALUES ($1, $2, $3) RETURNING id",
                uid, idempotency_key, file_sha256,
            )
        return {"status": "claimed", "claim_id": row["id"]}
    except asyncpg.UniqueViolationError:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """SELECT ic.id, ic.file_sha256, ic.extraction_id, e.status
                   FROM idempotency_claims ic
                   LEFT JOIN extractions e ON e.id = ic.extraction_id
                   WHERE ic.user_id = $1 AND ic.idempotency_key = $2
                     AND ic.created_at > NOW() - INTERVAL '24 hours'""",
                uid, idempotency_key,
            )
        if not existing:
            # Expired claim: delete old and insert new inside connection/transaction
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM idempotency_claims WHERE user_id = $1 AND idempotency_key = $2",
                        uid, idempotency_key,
                    )
                    row = await conn.fetchrow(
                        "INSERT INTO idempotency_claims (user_id, idempotency_key, file_sha256) "
                        "VALUES ($1, $2, $3) RETURNING id",
                        uid, idempotency_key, file_sha256,
                    )
            return {"status": "claimed", "claim_id": row["id"]}

        if existing["file_sha256"] != file_sha256:
            return {"status": "conflict"}
        return {
            "status": "duplicate",
            "extraction_id": existing["extraction_id"],
            "extraction_status": existing["status"],
        }


async def bind_idempotency_claim(pool: asyncpg.Pool, claim_id: int, extraction_id: int, document_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE idempotency_claims SET extraction_id=$2, document_id=$3 WHERE id=$1",
            claim_id, extraction_id, document_id,
        )


async def delete_idempotency_claim(pool: asyncpg.Pool, user_id: str, idempotency_key: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM idempotency_claims WHERE user_id=$1 AND idempotency_key=$2",
            _uuid_or_none(user_id), idempotency_key,
        )


# -- Output schema helpers -------------------------------------------------

def _schema_row(row: asyncpg.Record) -> dict:
    d = dict(row)
    for k in ("header_fields", "line_fields", "header_fields_snapshot", "line_fields_snapshot"):
        if d.get(k) is None:
            d[k] = []
        elif isinstance(d[k], str):
            import json as _json
            d[k] = _json.loads(d[k])
    return d


async def get_all_schemas(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM output_schemas ORDER BY is_system DESC, id ASC"
        )
    return [_schema_row(r) for r in rows]


async def get_schema_by_id(pool: asyncpg.Pool, schema_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM output_schemas WHERE id=$1", schema_id)
    return _schema_row(row) if row else None


async def get_schema_by_slug(pool: asyncpg.Pool, slug: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM output_schemas WHERE slug=$1", slug)
    return _schema_row(row) if row else None


async def create_schema(
    pool: asyncpg.Pool,
    *,
    name: str,
    header_fields: list[str],
    line_fields: list[str],
) -> dict:
    async with pool.acquire() as conn:
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "schema"
        # ensure slug uniqueness by appending sequence if needed
        base_slug = slug
        for suffix in [""] + [f"_{i}" for i in range(2, 100)]:
            candidate = base_slug + suffix
            exists = await conn.fetchval(
                "SELECT 1 FROM output_schemas WHERE slug=$1", candidate
            )
            if not exists:
                slug = candidate
                break
        row = await conn.fetchrow(
            """INSERT INTO output_schemas
                   (name, slug, is_system, header_fields, line_fields,
                    header_fields_snapshot, line_fields_snapshot)
               VALUES ($1,$2,FALSE,$3::text[],$4::text[],$3::text[],$4::text[])
               RETURNING *""",
            name, slug, header_fields, line_fields,
        )
    return _schema_row(row)


async def update_schema(
    pool: asyncpg.Pool,
    schema_id: int,
    *,
    name: str,
    header_fields: list[str],
    line_fields: list[str],
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE output_schemas
               SET name=$2,
                   header_fields=$3::text[], line_fields=$4::text[],
                   header_fields_snapshot=$3::text[], line_fields_snapshot=$4::text[],
                   updated_at=NOW()
               WHERE id=$1
               RETURNING *""",
            schema_id, name, header_fields, line_fields,
        )
    if not row:
        raise ValueError(f"Schema {schema_id} not found")
    return _schema_row(row)


async def delete_schema(pool: asyncpg.Pool, schema_id: int) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_system FROM output_schemas WHERE id=$1", schema_id
        )
        if not row:
            raise ValueError(f"Schema {schema_id} not found")
        if row["is_system"]:
            raise ValueError("System schemas cannot be deleted")
        await conn.execute("DELETE FROM output_schemas WHERE id=$1", schema_id)


async def reset_schema(pool: asyncpg.Pool, schema_id: int) -> dict:
    """Restore header_fields / line_fields from their last-saved snapshot."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE output_schemas
               SET header_fields = header_fields_snapshot,
                   line_fields   = line_fields_snapshot,
                   updated_at    = NOW()
               WHERE id=$1
               RETURNING *""",
            schema_id,
        )
    if not row:
        raise ValueError(f"Schema {schema_id} not found")
    return _schema_row(row)


async def get_schema_for_vendor(pool: asyncpg.Pool, vendor_id: str) -> dict | None:
    """Return the output schema assigned to a vendor's field mapping, or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.* FROM output_schemas s
               JOIN field_mappings fm ON fm.schema_id = s.id
               WHERE fm.vendor_id = $1""",
            vendor_id,
        )
    return _schema_row(row) if row else None


# -- Top-up requests -------------------------------------------------------

def _row_topup_request(row: asyncpg.Record) -> dict:
    d = dict(row)
    for k in ("user_id", "resolved_by"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


async def create_topup_request(
    pool: asyncpg.Pool,
    user_id: str,
    requested_pages: int,
    requested_period: str,
    note: str | None = None,
) -> dict:
    uid = _uuid_or_none(user_id)
    if uid is None:
        raise ValueError("Invalid user_id")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO topup_requests (user_id, requested_pages, requested_period, note)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            uid, requested_pages, requested_period, note,
        )
    return _row_topup_request(row)


async def list_topup_requests(
    pool: asyncpg.Pool,
    status: str | None = None,
) -> list[dict]:
    """List all top-up requests, optionally filtered by status. Joins user email."""
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                """
                SELECT tr.*, u.email AS user_email,
                       ru.email AS resolved_by_email
                FROM topup_requests tr
                JOIN users u ON u.id = tr.user_id
                LEFT JOIN users ru ON ru.id = tr.resolved_by
                WHERE tr.status = $1
                ORDER BY tr.created_at DESC
                """,
                status,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT tr.*, u.email AS user_email,
                       ru.email AS resolved_by_email
                FROM topup_requests tr
                JOIN users u ON u.id = tr.user_id
                LEFT JOIN users ru ON ru.id = tr.resolved_by
                ORDER BY tr.created_at DESC
                """,
            )
    return [_row_topup_request(r) for r in rows]


async def list_topup_requests_for_user(
    pool: asyncpg.Pool,
    user_id: str,
) -> list[dict]:
    uid = _uuid_or_none(user_id)
    if uid is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tr.*, ru.email AS resolved_by_email
            FROM topup_requests tr
            LEFT JOIN users ru ON ru.id = tr.resolved_by
            WHERE tr.user_id = $1
            ORDER BY tr.created_at DESC
            """,
            uid,
        )
    return [_row_topup_request(r) for r in rows]


async def get_topup_request(
    pool: asyncpg.Pool,
    request_id: int,
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tr.*, u.email AS user_email,
                   ru.email AS resolved_by_email
            FROM topup_requests tr
            JOIN users u ON u.id = tr.user_id
            LEFT JOIN users ru ON ru.id = tr.resolved_by
            WHERE tr.id = $1
            """,
            request_id,
        )
    return _row_topup_request(row) if row else None


async def resolve_topup_request(
    pool: asyncpg.Pool,
    request_id: int,
    resolved_by: str,
    status: str,  # 'approved' | 'rejected'
    resolution_note: str | None = None,
) -> dict | None:
    admin_uid = _uuid_or_none(resolved_by)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE topup_requests
               SET status = $1,
                   resolution_note = $2,
                   resolved_by = $3,
                   resolved_at = NOW()
             WHERE id = $4 AND status = 'pending'
             RETURNING *
            """,
            status, resolution_note, admin_uid, request_id,
        )
    return _row_topup_request(row) if row else None


async def approve_topup_atomically(
    pool: asyncpg.Pool,
    request_id: int,
    resolved_by: str,
    resolution_note: str | None = None,
) -> dict:
    """Approve a pending top-up request and apply its pages in one transaction.

    SELECT … FOR UPDATE on the request row serialises concurrent admin approvals
    so a request can produce at most one top-up. All-or-nothing: either the
    status flip AND the topup insert both commit, or neither does.

    Returns: {"request": {...}, "topup": {...}}
    Raises ValueError("not_found" | "already_<status>" | "no_active_subscription").
    """
    admin_uid = _uuid_or_none(resolved_by)
    async with pool.acquire() as conn:
        async with conn.transaction():
            req = await conn.fetchrow(
                "SELECT * FROM topup_requests WHERE id = $1 FOR UPDATE",
                request_id,
            )
            if not req:
                raise ValueError("not_found")
            if req["status"] != "pending":
                raise ValueError(f"already_{req['status']}")

            user_id = req["user_id"]
            pages = int(req["requested_pages"])

            # Expire any stale subscriptions for this user (same rule as reserve_quota).
            await conn.execute(
                """
                WITH expired AS (
                    UPDATE subscriptions
                       SET status = 'expired'
                     WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                     RETURNING user_id
                )
                UPDATE users
                   SET subscription_limit = 0
                 WHERE id IN (SELECT user_id FROM expired)
                """,
                user_id,
            )

            sub = await conn.fetchrow(
                "SELECT id FROM subscriptions WHERE user_id = $1 AND status = 'active' LIMIT 1",
                user_id,
            )
            if not sub:
                raise ValueError("no_active_subscription")

            topup_row = await conn.fetchrow(
                """
                INSERT INTO topups (user_id, subscription_id, pages, note, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, user_id, subscription_id, pages, note, created_by, created_at
                """,
                user_id, int(sub["id"]), pages,
                f"Approved top-up request #{request_id}"
                + (f": {resolution_note}" if resolution_note else ""),
                admin_uid,
            )

            resolved_row = await conn.fetchrow(
                """
                UPDATE topup_requests
                   SET status = 'approved',
                       resolution_note = $1,
                       resolved_by = $2,
                       resolved_at = NOW()
                 WHERE id = $3
                 RETURNING *
                """,
                resolution_note, admin_uid, request_id,
            )

    return {
        "request": _row_topup_request(resolved_row),
        "topup": _row_subscription(topup_row),
    }


async def get_pending_topup_request_count(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT COUNT(*) FROM topup_requests WHERE status = 'pending'"
        )
    return int(val or 0)

