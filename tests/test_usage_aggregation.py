from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod


@asynccontextmanager
async def fake_acquire(conn):
    yield conn


def _compact_sql(sql: str) -> str:
    return " ".join(sql.split())


class UsageAggregationQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_breakdown_aggregates_extractions_and_tokens_separately(self) -> None:
        """Avoid multiplying pages/tokens when a client has multiple docs and LLM calls."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_usage_by_client(pool)

        query = conn.fetch.call_args[0][0]
        compact = _compact_sql(query)
        self.assertIn("WITH extraction_totals AS", query)
        self.assertIn("usage_totals AS", query)
        self.assertIn("LEFT JOIN extraction_totals et ON et.user_id = u.id", compact)
        self.assertIn("LEFT JOIN usage_totals ut ON ut.user_id = u.id", compact)
        self.assertNotIn(
            "FROM users u LEFT JOIN vendors v ON v.user_id = u.id "
            "LEFT JOIN extractions e ON e.vendor_id = v.id "
            "LEFT JOIN llm_usage lu ON lu.vendor_id = v.id",
            compact,
        )

    async def test_client_breakdown_returns_database_rows_as_dicts(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = [
            {
                "user_id": "user-123",
                "email": "client@example.com",
                "role": "client",
                "is_active": True,
                "total_extractions": 2,
                "billable_pages": 5,
                "total_input_tokens": 1000,
                "total_output_tokens": 400,
                "grand_total": 1400,
                "total_llm_calls": 5,
            }
        ]

        rows = await db_mod.get_usage_by_client(pool)

        self.assertEqual(rows[0]["total_extractions"], 2)
        self.assertEqual(rows[0]["billable_pages"], 5)
        self.assertEqual(rows[0]["grand_total"], 1400)
        self.assertEqual(rows[0]["total_llm_calls"], 5)

    async def test_client_breakdown_page_count_comes_from_llm_usage_not_extractions(self) -> None:
        """Pages in the client table must be counted from llm_usage so they survive extraction deletion."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_usage_by_client(pool)

        sql = _compact_sql(conn.fetch.call_args[0][0])
        # Page count must be COUNT(DISTINCT ...) from llm_usage
        self.assertIn("COUNT(DISTINCT (lu.extraction_id, lu.page_num))", sql)
        self.assertIn("lu.call_type = 'extraction'", sql)
        # Must NOT use SUM(e.total_pages) — that is deletion-sensitive
        self.assertNotIn("SUM(e.total_pages)", sql)

    async def test_client_breakdown_selects_billable_pages_not_total_pages(self) -> None:
        """Result column is billable_pages (from llm_usage), not total_pages (from extractions)."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_usage_by_client(pool)

        sql = conn.fetch.call_args[0][0]
        self.assertIn("billable_pages", sql)
        # total_pages from the extractions table must not appear in SELECT
        self.assertNotIn("et.total_pages", sql)


if __name__ == "__main__":
    unittest.main()
