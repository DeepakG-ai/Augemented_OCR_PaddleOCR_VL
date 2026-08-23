//! schema.rs ← db.py `_SCHEMA_SQL` + `init` / `_init_db`.
//!
//! [`SCHEMA_SQL`] is db.py's bootstrap block copied verbatim (every
//! CREATE TABLE IF NOT EXISTS between the "Schema bootstrap" and "Helpers"
//! markers). The migrations that Python's `_init_db` ran as a series of
//! separate `conn.execute(...)` calls are preserved one-for-one in
//! [`MIGRATIONS_SQL`] so each DO $$ block stays an atomic statement.
//!
//! [`init_db`] mirrors Python's `init`: take a session-level advisory lock on
//! a dedicated connection, run schema + migrations on the pool, always unlock,
//! and retry up to 8 times with a random backoff when the failure looks like a
//! lock/deadlock/duplicate conflict (parallel worker boot).

use std::time::Duration;

use sqlx::{raw_sql, PgPool};
use {rand::Rng, crate::error::{AppError, AppResult}};

/// Verbatim copy of db.py `_SCHEMA_SQL` (backend/db.py lines 114–292).
pub const SCHEMA_SQL: &str = r#"
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
"#;

/// One entry per `await conn.execute("""...""")` inside db.py `_init_db`,
/// in the original order. Uniform leading indentation stripped; SQL text
/// otherwise unchanged.
const MIGRATIONS_SQL: &[&str] = &[
    // Migration: add mime_type to pages if missing (existing DBs)
    r#"
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
"#,
    r#"
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
"#,
    // Auth migration: add user_id column to vendors for per-client isolation
    r#"
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
"#,
    // Vendor id auto-generation. The primary key stays an opaque, globally
    // unique TEXT (server-issued from a sequence) so it remains safe as a
    // foreign key and as the global spatial_memory/layout key. `client_seq`
    // is a per-owner display number (Vendor #1, #2 ...) — display only,
    // never referenced by any FK. Name is unique per owner (case-insensitive).
    r#"
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
"#,
    // Migration: add extraction_id to llm_usage if the table predates this column.
    r#"
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'llm_usage' AND column_name = 'extraction_id'
        ) THEN
            ALTER TABLE llm_usage ADD COLUMN extraction_id INT
                REFERENCES extractions(id) ON DELETE SET NULL;
        END IF;
    END $$;
"#,
    // Fix A4-1 / P1: detach both FKs on llm_usage that used ON DELETE SET NULL so
    // that deleting an extraction or vendor never nullifies historical usage rows.
    // Also add user_id to llm_usage so billing survives vendor deletion.
    r#"
    DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name = 'llm_usage_extraction_id_fkey'
              AND table_name = 'llm_usage'
        ) THEN
            ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_extraction_id_fkey;
        END IF;
    END $$;
"#,
    r#"
    DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name = 'llm_usage_vendor_id_fkey'
              AND table_name = 'llm_usage'
        ) THEN
            ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_vendor_id_fkey;
        END IF;
    END $$;
"#,
    r#"
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
"#,
    // Vendor aliases (Phase 2) + spatial memory (Phase 3) — idempotent creation.
    r#"
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
"#,
    r#"
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
"#,
    // Qwen-learned label layout boxes — separate from spatial_memory (which
    // is the human-review correction store). One row per
    // (vendor, template, field_key) on page 1.
    r#"
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
"#,
    // Migration: add field_locations and ocr_data to extractions if missing
    r#"
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
"#,
    // Migration: add corrected_result and correction_meta to extractions
    r#"
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
"#,
    r#"
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'documents' AND column_name = 'object_key'
        ) THEN
            NULL;
        END IF;
    END $$;
"#,
    // Migration: add subscription_limit to users for SaaS page quotas
    r#"
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
"#,
    // Migration: pending_pages tracks in-flight uploads for atomic quota enforcement
    r#"
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'pending_pages'
        ) THEN
            ALTER TABLE users ADD COLUMN pending_pages INT NOT NULL DEFAULT 0;
        END IF;
    END $$;
"#,
    // Migration: API keys table for programmatic access
    r#"
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
"#,
    // Migration: widen prefix column if it was created as VARCHAR(16)
    r#"
    DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='api_keys' AND column_name='prefix'
              AND character_maximum_length < 32
        ) THEN
            ALTER TABLE api_keys ALTER COLUMN prefix TYPE VARCHAR(32);
        END IF;
    END $$;
"#,
    // Migration: add encrypted_key column for admin key recovery
    r#"
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='api_keys' AND column_name='encrypted_key'
        ) THEN
            ALTER TABLE api_keys ADD COLUMN encrypted_key TEXT;
        END IF;
    END $$;
"#,
    // Migration: add expires_at column for API key expiry
    r#"
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='api_keys' AND column_name='expires_at'
        ) THEN
            ALTER TABLE api_keys ADD COLUMN expires_at TIMESTAMPTZ;
        END IF;
    END $$;
"#,
    // Migration: add api_key_id to llm_usage for per-key usage tracking
    r#"
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='llm_usage' AND column_name='api_key_id'
        ) THEN
            ALTER TABLE llm_usage ADD COLUMN api_key_id INT;
        END IF;
    END $$;
"#,
    // ERP field mapping: mapped_result holds the canonical-field JSON sent
    // to API-key clients. Raw `result` stays untouched for review/spatial memory.
    r#"
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
"#,
    // Migration: output_schemas — schema_id FK on field_mappings + seed defaults
    r#"
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='field_mappings' AND column_name='schema_id'
        ) THEN
            ALTER TABLE field_mappings ADD COLUMN schema_id INT
                REFERENCES output_schemas(id) ON DELETE SET NULL;
        END IF;
    END $$;
"#,
    r#"
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
"#,
    // Migration: idempotency claims table for deduplication
    r#"
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
"#,
    // ── Subscriptions & top-ups ────────────────────────────────────────────
    // Admin grants a subscription period (custom from/to dates) with a base
    // page_limit. Admin can later add top-ups (extra pages) that attach to
    // the *current* active subscription. On period_end everything vanishes —
    // base + topups (use-it-or-lose-it). One active subscription per user is
    // enforced via partial unique index.
    r#"
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
"#,
    r#"
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
"#,
    // One-time backfill: every client with the legacy subscription_limit > 0
    // gets a 1-year subscription starting today, so quota math keeps working.
    // Admins can override later via POST /admin/users/{id}/subscriptions.
    r#"
    INSERT INTO subscriptions (user_id, page_limit, period_start, period_end, status, note)
    SELECT u.id, u.subscription_limit, NOW(), NOW() + INTERVAL '365 days', 'active',
           'Auto-migrated from legacy subscription_limit'
    FROM users u
    WHERE u.subscription_limit > 0
      AND NOT EXISTS (
          SELECT 1 FROM subscriptions s
          WHERE s.user_id = u.id AND s.status = 'active'
      );
"#,
    // Migration: user-initiated top-up requests. Users submit these when quota
    // is exhausted; admins see them as pending notifications and approve/reject.
    r#"
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
"#,
    // Migration: quota event log — records every grace overage and hard block
    // so admins can see which clients are burning through grace pages.
    r#"
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
"#,
];

async fn bootstrap_schema(pool: &PgPool) -> AppResult<()> {
    raw_sql(SCHEMA_SQL).execute(pool).await.map_err(AppError::from)?;
    for block in MIGRATIONS_SQL {
        raw_sql(block).execute(pool).await.map_err(AppError::from)?;
    }
    Ok(())
}

/// Run schema + migrations under the session advisory lock, unlocking even on
/// failure (Python's try/finally around `_init_db`).
async fn bootstrap_locked(pool: &PgPool) -> AppResult<()> {
    let mut conn = pool.acquire().await.map_err(AppError::from)?;
    sqlx::query("SELECT pg_advisory_lock(hashtext('augocr_db_init'))")
        .execute(&mut *conn)
        .await
        .map_err(AppError::from)?;
    let result = bootstrap_schema(pool).await;
    let unlock = sqlx::query("SELECT pg_advisory_unlock(hashtext('augocr_db_init'))")
        .execute(&mut *conn)
        .await;
    match result {
        Ok(()) => {
            unlock.map_err(AppError::from)?;
            Ok(())
        }
        Err(e) => {
            if let Err(unlock_err) = unlock {
                tracing::warn!(error = %unlock_err, "advisory unlock failed after init error");
            }
            Err(e)
        }
    }
}

/// Create tables if they don't exist, then run migrations with a retry loop
/// for parallel worker boot (port of db.py `init`).
pub async fn init_db(pool: &PgPool) -> AppResult<()> {
    const MAX_RETRIES: usize = 8;
    for attempt in 0..MAX_RETRIES {
        match bootstrap_locked(pool).await {
            Ok(()) => return Ok(()),
            Err(e) => {
                let err_str = e.to_string().to_lowercase();
                let is_lock_issue = ["deadlock", "lock", "unique", "duplicate"]
                    .iter()
                    .any(|k| err_str.contains(k));
                if is_lock_issue && attempt < MAX_RETRIES - 1 {
                    let sleep_secs = rand::thread_rng().gen_range(1.5..4.0);
                    tracing::warn!(
                        error = %e,
                        attempt = attempt + 1,
                        max_retries = MAX_RETRIES,
                        sleep_secs,
                        "database migration lock conflict or deadlock detected; retrying"
                    );
                    tokio::time::sleep(Duration::from_secs_f64(sleep_secs)).await;
                } else {
                    tracing::error!(attempt = attempt + 1, error = %e, "database migration failed");
                    return Err(e);
                }
            }
        }
    }
    Ok(())
}
