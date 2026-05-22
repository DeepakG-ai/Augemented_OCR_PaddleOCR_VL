"""
test_admin_billing_reporting_isolation.py
=========================================
Tests to verify the aligned billing and reporting query modifications in db.py.
Specifically, verifies that when an admin uploads a document for a client's vendor
(passing billing_user_id = admin_id in metadata):
  1. Extraction metrics and document stats filter and group by COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id)
  2. LLM usage stats filter and group by lu.user_id
  3. This ensures the admin's testing uploads are billed to the admin and isolated from the client's reports.
"""
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


class AdminBillingReportingIsolationTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_usage_stats_isolates_extraction_and_llm_metrics(self) -> None:
        """
        Verify get_usage_stats uses COALESCE for extractions and lu.user_id for LLM usage.
        """
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))
        conn.fetchrow.side_effect = [
            # First fetchrow for extractions
            {
                "total_pdfs": 1,
                "total_extractions": 1,
                "all_pages": 3,
                "total_pages": 3,
            },
            # Second fetchrow for LLM usage
            {
                "total_input_tokens": 100,
                "total_output_tokens": 50,
                "grand_total": 150,
                "total_llm_calls": 2,
                "billable_pages": 3,
            }
        ]

        user_id = "00000000-0000-0000-0000-000000000001"
        stats = await db_mod.get_usage_stats(pool, user_id=user_id)

        self.assertEqual(conn.fetchrow.call_count, 2)
        
        # 1. First fetchrow query assertions (extractions)
        ext_query_args = conn.fetchrow.call_args_list[0]
        ext_query = _compact_sql(ext_query_args[0][0])
        ext_params = ext_query_args[0][1:]
        self.assertIn("COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1", ext_query)
        self.assertEqual(str(ext_params[0]), user_id)

        # 2. Second fetchrow query assertions (LLM usage)
        llm_query_args = conn.fetchrow.call_args_list[1]
        llm_query = _compact_sql(llm_query_args[0][0])
        llm_params = llm_query_args[0][1:]
        self.assertIn("lu.user_id = $1", llm_query)
        self.assertEqual(str(llm_params[0]), user_id)

        # 3. Check stats merging and calculations
        self.assertEqual(stats["total_pdfs"], 1)
        self.assertEqual(stats["grand_total"], 150)
        self.assertEqual(stats["billable_pages"], 3)
        self.assertEqual(stats["unbilled_pages"], 0)

    async def test_get_llm_usage_daily_summary_filters_by_billed_user(self) -> None:
        """
        Verify get_llm_usage_daily_summary filters by lu.user_id = $2.
        """
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))
        conn.fetch.return_value = []

        user_id = "00000000-0000-0000-0000-000000000001"
        await db_mod.get_llm_usage_daily_summary(pool, user_id=user_id)

        query_args = conn.fetch.call_args
        query = _compact_sql(query_args[0][0])
        params = query_args[0][1:]

        self.assertIn("lu.user_id = $2", query)
        self.assertEqual(str(params[1]), user_id)

    async def test_get_usage_by_client_aggregates_properly(self) -> None:
        """
        Verify get_usage_by_client groups extraction_totals by COALESCE billing user
        and usage_totals by lu.user_id.
        """
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))
        conn.fetch.return_value = []

        await db_mod.get_usage_by_client(pool)

        query_args = conn.fetch.call_args
        query = _compact_sql(query_args[0][0])

        # Verify extraction totals CTE uses COALESCE
        self.assertIn("COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) AS user_id", query)
        self.assertIn("GROUP BY COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id)", query)

        # Verify usage totals CTE groups by lu.user_id
        self.assertIn("lu.user_id,", query)
        self.assertIn("GROUP BY lu.user_id", query)

    async def test_get_client_document_usage_filters_by_billing_user(self) -> None:
        """
        Verify get_client_document_usage filters by COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1::UUID.
        """
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))
        conn.fetch.return_value = []

        user_id = "00000000-0000-0000-0000-000000000001"
        await db_mod.get_client_document_usage(pool, user_id=user_id)

        query_args = conn.fetch.call_args
        query = _compact_sql(query_args[0][0])
        params = query_args[0][1:]

        self.assertIn("COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1::UUID", query)
        self.assertEqual(str(params[0]), user_id)

    async def test_list_and_count_all_extractions_filter_by_billing_user(self) -> None:
        """
        Verify list_all_extractions and count_all_extractions filter by COALESCE billing user.
        """
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))
        conn.fetch.return_value = []
        conn.fetchval.return_value = 0

        user_id = "00000000-0000-0000-0000-000000000001"

        # Check list_all_extractions
        await db_mod.list_all_extractions(pool, user_id=user_id)
        list_query_args = conn.fetch.call_args
        list_query = _compact_sql(list_query_args[0][0])
        list_params = list_query_args[0][1:]
        self.assertIn("COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $2", list_query)
        self.assertEqual(str(list_params[1]), user_id)

        # Check count_all_extractions
        await db_mod.count_all_extractions(pool, user_id=user_id)
        count_query_args = conn.fetchval.call_args
        count_query = _compact_sql(count_query_args[0][0])
        count_params = count_query_args[0][1:]
        self.assertIn("COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1", count_query)
        self.assertEqual(str(count_params[0]), user_id)

    async def test_list_extractions_filters_by_billing_user(self) -> None:
        """
        Verify list_extractions filters by COALESCE billing user if user_id is provided.
        """
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))
        conn.fetch.return_value = []

        vendor_id = "vendor-123"
        user_id = "00000000-0000-0000-0000-000000000001"

        await db_mod.list_extractions(pool, vendor_id, limit=20, user_id=user_id)
        query_args = conn.fetch.call_args
        query = _compact_sql(query_args[0][0])
        params = query_args[0][1:]

        self.assertIn("COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $3", query)
        self.assertEqual(vendor_id, params[0])
        self.assertEqual(20, params[1])
        self.assertEqual(str(params[2]), user_id)

    async def test_assert_extraction_access_respects_billing_user(self) -> None:
        """
        Verify assert_extraction_access blocks access to non-admin client if they are not the billing user.
        """
        from fastapi import HTTPException
        import backend.auth as auth_mod
        from unittest.mock import patch

        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire = MagicMock(side_effect=lambda: fake_acquire(conn))

        client_user = {"id": "client-123", "role": "client", "email": "client@test.com"}
        admin_user = {"id": "admin-123", "role": "admin", "email": "admin@test.com"}

        # Case 1: Admin bypasses
        with patch.object(auth_mod.db_mod, "get_extraction", new=AsyncMock(return_value={"vendor_id": "v1"})):
            # Should not raise exception
            await auth_mod.assert_extraction_access(pool, 123, admin_user)

        # Case 2: Client gets extraction with no billing user but owns vendor
        with patch.object(auth_mod.db_mod, "get_extraction", new=AsyncMock(return_value={"vendor_id": "v1", "document_id": 456})), \
             patch.object(auth_mod.db_mod, "get_document", new=AsyncMock(return_value={"metadata": {}})), \
             patch.object(auth_mod.db_mod, "get_vendor_owner", new=AsyncMock(return_value="client-123")):
            await auth_mod.assert_extraction_access(pool, 123, client_user)

        # Case 3: Client gets extraction but is NOT the billing user (Admin uploaded "acted as Client")
        with patch.object(auth_mod.db_mod, "get_extraction", new=AsyncMock(return_value={"vendor_id": "v1", "document_id": 456})), \
             patch.object(auth_mod.db_mod, "get_document", new=AsyncMock(return_value={"metadata": {"billing_user_id": "admin-123"}})):
            with self.assertRaises(HTTPException) as ctx:
                await auth_mod.assert_extraction_access(pool, 123, client_user)
            self.assertEqual(ctx.exception.status_code, 403)
            self.assertEqual(ctx.exception.detail, "Access denied")

        # Case 4: Client gets extraction and IS the billing user
        with patch.object(auth_mod.db_mod, "get_extraction", new=AsyncMock(return_value={"vendor_id": "v1", "document_id": 456})), \
             patch.object(auth_mod.db_mod, "get_document", new=AsyncMock(return_value={"metadata": {"billing_user_id": "client-123"}})):
            await auth_mod.assert_extraction_access(pool, 123, client_user)


if __name__ == "__main__":
    unittest.main()
