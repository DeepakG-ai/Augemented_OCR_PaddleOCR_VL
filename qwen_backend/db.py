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

CREATE TABLE IF NOT EXISTS extractions (
    id               SERIAL PRIMARY KEY,
    vendor_id        TEXT REFERENCES vendors(id),
    template_id      INT  REFERENCES templates(id),
    filename         TEXT,
    total_pages      INT,
    format_type      TEXT,
    header_fields    JSONB,
    line_item_fields JSONB,
    result           JSONB,
    page_results     JSONB,
    status           TEXT DEFAULT 'pending',
    error            TEXT,
    duration_ms      INT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pages (
    id            SERIAL PRIMARY KEY,
    extraction_id INT REFERENCES extractions(id) ON DELETE CASCADE,
    page_number   INT NOT NULL,
    image_b64     TEXT NOT NULL,
    width         INT,
    height        INT
);
"""


async def init(pool: asyncpg.Pool) -> None:
    """Create tables if they don't exist."""
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)


# -- Helpers ---------------------------------------------------------------

def _parse_jsonb(d: dict, *keys: str) -> None:
    """Parse JSONB columns that asyncpg may return as str."""
    for key in keys:
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])


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


# -- Extraction queries ----------------------------------------------------

_EXTRACTION_COLS = """
    id, vendor_id, template_id, filename, total_pages,
    format_type, header_fields, line_item_fields, result, page_results,
    status, error, duration_ms, created_at
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
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO extractions
                (vendor_id, template_id, filename, total_pages,
                 format_type, header_fields, line_item_fields, status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, 'processing')
            RETURNING {_EXTRACTION_COLS}
            """,
            vendor_id, template_id, filename, total_pages,
            format_type, json.dumps(header_fields), json.dumps(line_item_fields),
        )
        d = dict(row)
        _parse_jsonb(d, "header_fields", "line_item_fields")
        return d


async def update_extraction_result(
    pool: asyncpg.Pool,
    extraction_id: int,
    result: Any,
    page_results: Any,
    status: str,
    duration_ms: int,
    error: str | None = None,
    page_results_partial: list[dict] | None = None,
) -> None:
    async with pool.acquire() as conn:
        if page_results_partial:
            # Incremental append: add page results to existing array
            for pr in page_results_partial:
                await conn.execute(
                    """
                    UPDATE extractions
                    SET page_results = COALESCE(page_results, '[]'::jsonb) || $2::jsonb,
                        status       = $3
                    WHERE id = $1
                    """,
                    extraction_id,
                    json.dumps([pr]),
                    status,
                )
        else:
            await conn.execute(
                """
                UPDATE extractions
                SET result       = $2::jsonb,
                    page_results = $3::jsonb,
                    status       = $4,
                    duration_ms  = $5,
                    error        = $6
                WHERE id = $1
                """,
                extraction_id,
                json.dumps(result) if result is not None else None,
                json.dumps(page_results) if page_results is not None else None,
                status, duration_ms, error,
            )


async def list_extractions(pool: asyncpg.Pool, vendor_id: str, limit: int = 20) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_EXTRACTION_COLS}
            FROM extractions
            WHERE vendor_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            vendor_id, limit,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results")
            results.append(d)
        return results


async def list_all_extractions(pool: asyncpg.Pool, limit: int = 50) -> list[dict]:
    """Global extraction history with vendor_name joined."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.vendor_id, v.name AS vendor_name, e.template_id,
                   e.filename, e.total_pages, e.format_type,
                   e.header_fields, e.line_item_fields, e.result, e.page_results,
                   e.status, e.error, e.duration_ms, e.created_at
            FROM extractions e
            LEFT JOIN vendors v ON v.id = e.vendor_id
            ORDER BY e.created_at DESC LIMIT $1
            """,
            limit,
        )
        results = []
        for r in rows:
            d = dict(r)
            _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results")
            results.append(d)
        return results


async def get_extraction(pool: asyncpg.Pool, extraction_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_EXTRACTION_COLS} FROM extractions WHERE id = $1",
            extraction_id,
        )
        if not row:
            return None
        d = dict(row)
        _parse_jsonb(d, "header_fields", "line_item_fields", "result", "page_results")
        return d


# -- Pages queries ---------------------------------------------------------

async def save_pages(pool: asyncpg.Pool, extraction_id: int, pages: list[dict]) -> None:
    """Bulk-insert rendered page images for an extraction."""
    if not pages:
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO pages (extraction_id, page_number, image_b64, width, height)
            VALUES ($1, $2, $3, $4, $5)
            """,
            [
                (extraction_id, p["page_number"], p["image_b64"], p.get("width", 0), p.get("height", 0))
                for p in pages
            ],
        )


async def get_pages(pool: asyncpg.Pool, extraction_id: int) -> list[dict]:
    """Return all pages for an extraction, ordered by page_number."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT page_number, image_b64, width, height
            FROM pages
            WHERE extraction_id = $1
            ORDER BY page_number ASC
            """,
            extraction_id,
        )
        return [dict(r) for r in rows]