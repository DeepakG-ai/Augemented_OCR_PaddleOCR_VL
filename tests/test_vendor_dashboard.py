"""
test_vendor_dashboard.py — Tests for vendor dashboard DB queries.

All DB calls are mocked; no live services required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(rows: list[dict] | None = None):
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


# ---------------------------------------------------------------------------
# get_client_vendor_summary (admin view)
# ---------------------------------------------------------------------------

class ClientVendorSummaryTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_per_client_row(self):
        rows = [
            {
                "user_id": "uid-a", "email": "a@x.com", "role": "client",
                "is_active": True, "vendor_count": 5,
                "extraction_count": 42, "completed_extractions": 38,
            },
            {
                "user_id": "uid-b", "email": "b@x.com", "role": "client",
                "is_active": True, "vendor_count": 10,
                "extraction_count": 100, "completed_extractions": 99,
            },
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_client_vendor_summary(pool)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["vendor_count"], 5)
        self.assertEqual(result[1]["vendor_count"], 10)

    async def test_empty_returns_empty_list(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_client_vendor_summary(pool)
        self.assertEqual(result, [])

    async def test_client_a_has_5_vendors_client_b_has_10(self):
        """Explicit scenario: Client A → 5 vendors, Client B → 10 vendors."""
        rows = [
            {"user_id": "a", "email": "a@co.com", "role": "client",
             "is_active": True, "vendor_count": 5,
             "extraction_count": 20, "completed_extractions": 18},
            {"user_id": "b", "email": "b@co.com", "role": "client",
             "is_active": True, "vendor_count": 10,
             "extraction_count": 80, "completed_extractions": 75},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_client_vendor_summary(pool)
        by_email = {r["email"]: r for r in result}
        self.assertEqual(by_email["a@co.com"]["vendor_count"], 5)
        self.assertEqual(by_email["b@co.com"]["vendor_count"], 10)


# ---------------------------------------------------------------------------
# get_client_vendors_with_stats (per-client vendor list)
# ---------------------------------------------------------------------------

class ClientVendorsWithStatsTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_vendor_list_for_user(self):
        rows = [
            {"vendor_id": "v1", "vendor_name": "ACME Corp", "status": "idle",
             "extraction_count": 10, "total_pages_processed": 35,
             "last_extraction_at": None, "completed": 9, "failed": 1},
        ]
        pool, _ = _make_pool(rows)
        uid = "00000000-0000-0000-0000-000000000001"
        result = await db_mod.get_client_vendors_with_stats(pool, uid)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["vendor_name"], "ACME Corp")
        self.assertEqual(result[0]["extraction_count"], 10)

    async def test_invalid_uuid_returns_empty(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_client_vendors_with_stats(pool, "not-a-uuid")
        self.assertEqual(result, [])

    async def test_client_isolation_query_uses_user_id(self):
        """The SQL must filter by the given user_id, not return all vendors."""
        pool, conn = _make_pool([])
        uid = "00000000-0000-0000-0000-000000000099"
        await db_mod.get_client_vendors_with_stats(pool, uid)
        # Confirm fetch was called and the UUID arg is uid's value
        conn.fetch.assert_called_once()
        call_args = conn.fetch.call_args[0]
        # Second positional arg is the $1 UUID
        self.assertIn(uid.replace("-", ""), str(call_args[1]).replace("-", ""))


# ---------------------------------------------------------------------------
# get_vendor_extraction_daily
# ---------------------------------------------------------------------------

class VendorExtractionDailyTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_per_day_rows(self):
        rows = [
            {"day": "2025-05-14", "extractions": 3, "completed": 3, "failed": 0, "total_pages": 9},
            {"day": "2025-05-13", "extractions": 1, "completed": 0, "failed": 1, "total_pages": 2},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_vendor_extraction_daily(pool, "vendor-1", limit=30)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["day"], "2025-05-14")
        self.assertEqual(result[0]["extractions"], 3)

    async def test_failed_extractions_counted_separately(self):
        rows = [
            {"day": "2025-05-14", "extractions": 5, "completed": 3, "failed": 2, "total_pages": 10},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_vendor_extraction_daily(pool, "vendor-x")
        self.assertEqual(result[0]["failed"], 2)
        self.assertEqual(result[0]["completed"], 3)

    async def test_empty_when_no_extractions(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_vendor_extraction_daily(pool, "new-vendor")
        self.assertEqual(result, [])

    async def test_limit_is_passed_to_query(self):
        pool, conn = _make_pool([])
        await db_mod.get_vendor_extraction_daily(pool, "v1", limit=7)
        conn.fetch.assert_called_once()
        call_args = conn.fetch.call_args[0]
        # $2 is the limit value
        self.assertEqual(call_args[2], 7)


# ---------------------------------------------------------------------------
# get_vendor_page_stats
# ---------------------------------------------------------------------------

class VendorPageStatsTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_per_page_rows(self):
        rows = [
            {"page_number": 1, "times_processed": 15, "avg_tokens": 3200, "avg_latency_ms": 4500},
            {"page_number": 2, "times_processed": 12, "avg_tokens": 2800, "avg_latency_ms": 3900},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_vendor_page_stats(pool, "vendor-1")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["page_number"], 1)
        self.assertEqual(result[0]["times_processed"], 15)

    async def test_page_1_most_processed_scenario(self):
        """Page 1 typically has the highest hit count (vendor detection + extraction)."""
        rows = [
            {"page_number": 1, "times_processed": 100, "avg_tokens": 4000, "avg_latency_ms": 5000},
            {"page_number": 2, "times_processed": 80,  "avg_tokens": 3500, "avg_latency_ms": 4500},
            {"page_number": 3, "times_processed": 60,  "avg_tokens": 3000, "avg_latency_ms": 4000},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_vendor_page_stats(pool, "big-vendor")
        self.assertGreaterEqual(result[0]["times_processed"], result[-1]["times_processed"])

    async def test_avg_tokens_can_be_none_for_no_llm_data(self):
        rows = [
            {"page_number": 1, "times_processed": 5, "avg_tokens": None, "avg_latency_ms": None},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_vendor_page_stats(pool, "vendor-y")
        self.assertIsNone(result[0]["avg_tokens"])

    async def test_empty_when_no_pages(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_vendor_page_stats(pool, "new-vendor")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
