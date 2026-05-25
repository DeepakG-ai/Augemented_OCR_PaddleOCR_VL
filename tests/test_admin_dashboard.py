from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod
import backend.main as main


@asynccontextmanager
async def fake_acquire(conn):
    yield conn


class ClientDocumentUsageQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_document_usage_includes_billable_pages_and_latency(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_client_document_usage(pool, "00000000-0000-0000-0000-000000000001")

        query = conn.fetch.call_args[0][0]
        self.assertIn("COUNT(DISTINCT lu.page_num)", query)
        self.assertIn("lu.call_type = 'extraction'", query)
        self.assertIn("SUM(lu.duration_ms)", query)

    async def test_client_document_usage_date_filter_params_are_passed(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []
        start = datetime(2026, 5, 8, tzinfo=UTC)
        end = datetime(2026, 5, 9, tzinfo=UTC)

        await db_mod.get_client_document_usage(
            pool,
            "00000000-0000-0000-0000-000000000001",
            limit=25,
            date_from=start,
            date_to=end,
        )

        args = conn.fetch.call_args[0]
        self.assertIn("e.created_at >= $3", args[0])
        self.assertIn("e.created_at < $4", args[0])
        self.assertEqual(str(args[1]), "00000000-0000-0000-0000-000000000001")
        self.assertEqual(args[2], 25)
        self.assertEqual(args[3], start)
        self.assertEqual(args[4], end)

    async def test_billable_pages_is_distinct_page_count_not_total_calls(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_client_document_usage(pool, "00000000-0000-0000-0000-000000000001")

        query = " ".join(conn.fetch.call_args[0][0].split())
        self.assertIn("COUNT(DISTINCT lu.page_num)", query)
        self.assertNotIn("COUNT(lu.id)::INT AS billable_pages", query)

    async def test_client_document_usage_rejects_malformed_user_id_before_sql(self) -> None:
        pool = MagicMock()

        rows = await db_mod.get_client_document_usage(pool, "not-a-uuid")

        self.assertEqual(rows, [])
        pool.acquire.assert_not_called()


class ClientDashboardEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_client_dashboard_endpoint_returns_expected_shape(self) -> None:
        user_id = "00000000-0000-0000-0000-000000000001"
        client_row = {"id": user_id, "email": "client@example.com", "role": "client", "is_active": True}
        stats = {
            "total_pdfs": 2,
            "total_extractions": 1,
            "all_pages": 5,
            "billable_pages": 3,
            "unbilled_pages": 2,
            "failed_pages": 0,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "grand_total": 1500,
            "total_llm_calls": 3,
        }
        days = [{"day": "2026-05-08", "input_tokens": 1000, "output_tokens": 500}]
        documents = [{"extraction_id": 10, "filename": "po.pdf", "billable_pages": 3}]

        with patch.object(main.db_mod, "get_user_by_id", new=AsyncMock(return_value=client_row)), \
             patch.object(main.db_mod, "get_usage_stats", new=AsyncMock(return_value=stats)), \
             patch.object(main.db_mod, "get_client_daily_summary", new=AsyncMock(return_value=days)), \
             patch.object(main.db_mod, "get_client_document_usage", new=AsyncMock(return_value=documents)):
            response = self.client.get(
                f"/admin/usage/clients/{user_id}/dashboard?date_from=2026-05-08&date_to=2026-05-08"
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["client"]["email"], "client@example.com")
        self.assertEqual(body["stats"]["todays_pdfs"], 2)
        self.assertEqual(body["stats"]["billable_pages"], 3)
        self.assertEqual(body["stats"]["unbilled_pages"], 2)
        self.assertEqual(body["stats"]["failed_pages"], 0)
        self.assertEqual(body["days"], days)
        self.assertEqual(body["documents"], documents)

    def test_client_dashboard_cost_estimate(self) -> None:
        with patch.object(main, "USAGE_INPUT_USD_PER_1K", 0.006), \
             patch.object(main, "USAGE_OUTPUT_USD_PER_1K", 0.018):
            self.assertEqual(main._usage_cost_estimate(1000, 1000), 0.024)

    def test_date_filter_defaults_to_today(self) -> None:
        user_id = "00000000-0000-0000-0000-000000000001"

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 5, 8)

        with patch.object(main, "date", FrozenDate), \
             patch.object(main.db_mod, "get_user_by_id", new=AsyncMock(return_value={
                 "id": user_id, "email": "client@example.com", "role": "client", "is_active": True,
             })), \
             patch.object(main.db_mod, "get_usage_stats", new=AsyncMock(return_value={})), \
             patch.object(main.db_mod, "get_client_daily_summary", new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "get_client_document_usage", new=AsyncMock(return_value=[])) as docs:
            response = self.client.get(f"/admin/usage/clients/{user_id}/dashboard")

        self.assertEqual(response.status_code, 200)
        kwargs = docs.await_args.kwargs
        self.assertEqual(kwargs["date_from"], datetime(2026, 5, 8, tzinfo=UTC))
        self.assertEqual(kwargs["date_to"], datetime(2026, 5, 9, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
