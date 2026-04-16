"""
db.py -- asyncpg pool, schema initialisation, and all SQL queries.

No ORM. Fields are stored as header_fields (JSONB) and line_item_fields (JSONB).
"""
from __future__ import annotations

import json
import os
from typing import Any

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://augocr:augocr@localhost:5432/augocr")


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
            END $$;
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS pages_extraction_page_number_idx
            ON pages (extraction_id, page_number);
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


# -- Vendor queries --------------------------------------------------------

async def get_vendor(pool: asyncpg.Pool, vendor_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, status, created_at FROM vendors WHERE id = $1", vendor_id
        )
        return dict(row) if row else None


async def list_vendors(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, status, created_at FROM vendors ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]


async def upsert_vendor(pool: asyncpg.Pool, vendor_id: str, name: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO vendors (id, name) VALUES ($1, $2)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            RETURNING id, name, status, created_at
            """,
            vendor_id, name,
        )
        return dict(row)


async def delete_vendor(pool: asyncpg.Pool, vendor_id: str) -> bool:
    """Delete a vendor. CASCADE handles templates/extractions."""
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM vendors WHERE id = $1", vendor_id)
        return result == "DELETE 1"


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


async def list_all_templates(pool: asyncpg.Pool) -> list[dict]:
    """Return all templates joined with vendor name for the saved-templates page."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT t.id, t.vendor_id, v.name AS vendor_name, t.format_type,
                   t.header_fields, t.line_item_fields, t.prompt_instructions,
                   t.extraction_rules, t.prompt_hash, t.created_at, t.updated_at
            FROM templates t
            JOIN vendors v ON v.id = t.vendor_id
            ORDER BY t.updated_at DESC
            """
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
    duration_ms: int,
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
                    duration_ms  = $5,
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


async def list_all_extractions(pool: asyncpg.Pool, limit: int = 50) -> list[dict]:
    """Global extraction history with vendor_name joined."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            ORDER BY e.created_at DESC LIMIT $1
            """,
            limit,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results",
                         "field_locations", "ocr_data", "corrected_result", "correction_meta", "progress")
            results.append(d)
        return results


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
            INSERT INTO pages (extraction_id, page_number, object_key, mime_type, width, height)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (extraction_id, page_number) DO UPDATE SET
                object_key = EXCLUDED.object_key,
                mime_type = EXCLUDED.mime_type,
                width = EXCLUDED.width,
                height = EXCLUDED.height
            """,
            [
                (
                    extraction_id,
                    p["page_number"],
                    p["object_key"],
                    p.get("mime_type", "image/jpeg"),
                    p.get("width", 0),
                    p.get("height", 0),
                )
                for p in pages
            ],
        )


async def get_pages(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    """Return all pages for an extraction, ordered by page_number."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT page_number, object_key, mime_type, width, height
            FROM pages
            WHERE extraction_id = $1
            ORDER BY page_number ASC
            """,
            extraction_id,
        )
        return [dict(r) for r in rows]


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


# -- Field locations + OCR data --------------------------------------------

async def save_field_locations(
    pool: asyncpg.Pool,
    extraction_id: int,
    field_locations: dict,
) -> None:
    """Save text_matcher field_locations to the extraction record."""
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
    """Lightweight check: are both result and ocr_data present?
    
    Avoids loading the massive JSONB blobs — just checks for IS NOT NULL.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT (result IS NOT NULL AND ocr_data IS NOT NULL) AS ready FROM extractions WHERE id = $1",
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
    limit: int = 2,
) -> list[dict]:
    """Retrieve the most recent gold examples for a vendor (for prompt injection)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT correction_diff
            FROM gold_examples
            WHERE vendor_id = $1 AND correction_diff IS NOT NULL
            ORDER BY created_at DESC
            LIMIT $2
            """,
            vendor_id, limit,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "correction_diff")
            results.append(d)
        return results


# -- Job queue -------------------------------------------------------------

async def enqueue_job(
    pool: asyncpg.Pool,
    extraction_id: int | None,
    document_id: int | None,
    job_type: str,
    payload: dict | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (extraction_id, document_id, job_type, payload, priority, max_attempts)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6)
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
        return _record(row, "payload", "progress") or {}


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


async def cancel_jobs_for_extraction(pool: asyncpg.Pool, extraction_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
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
            """,
            extraction_id,
        )


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
