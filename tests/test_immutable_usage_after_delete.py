"""
test_immutable_usage_after_delete.py
=====================================
Verify the core billing invariant:

    Deleting an extraction from history MUST NOT reduce the user's
    billable page count or affect quota enforcement.

Why this matters
----------------
Usage/billing counts are sourced from llm_usage, which has no FK to
extractions (the constraint was dropped in a migration).  delete_extraction
removes rows from pages, jobs, and extractions — never from llm_usage.
This means a user cannot regain quota by deleting history.

Test surface
------------
1. DB layer  – delete_extraction does not touch llm_usage
2. DB layer  – get_user_billable_pages reads from llm_usage (not extractions)
3. DB layer  – reserve_quota reads from llm_usage (not extractions)
4. DB layer  – get_usage_stats exposes billable_pages from llm_usage
5. DB layer  – get_usage_by_client exposes billable_pages from llm_usage
6. HTTP      – DELETE /extractions/{id} only deletes from pages/jobs/extractions
7. HTTP      – GET /me/usage returns same count before and after deletion
8. HTTP      – Quota block persists after deleting history
"""
from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod
import backend.main as main
from backend.auth import get_current_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _fake_acquire(conn):
    yield conn


class _FakeTxn:
    """Async context manager that stands in for asyncpg connection.transaction()."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _make_pool_conn():
    """Return (pool, conn) where pool.acquire() yields conn and conn.transaction() works.

    pool.acquire uses side_effect (not return_value) so each call produces a
    fresh async CM — a single generator instance is exhausted after the first use.

    conn.transaction must be a regular MagicMock (not AsyncMock) so that
    `async with conn.transaction():` receives a proper async CM, not a coroutine.
    """
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.side_effect = lambda: _fake_acquire(conn)
    conn.transaction = MagicMock(return_value=_FakeTxn())
    return pool, conn


def _client_user(uid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test.com"}


def _admin_user() -> dict:
    return {"id": "00000000-0000-0000-0000-000000000000", "role": "admin", "email": "admin@test.com"}


def _usage(used: int, limit: int) -> dict:
    return {"billable_pages": used, "subscription_limit": limit, "remaining": limit - used}


def _quota_exceeded(used: int, limit: int) -> dict:
    return {"allowed": False, "reason": "exceeded", "used": used, "limit": limit,
            "remaining": 0, "pending": 0}


# ---------------------------------------------------------------------------
# 1. delete_extraction does not touch llm_usage
# ---------------------------------------------------------------------------

class DeleteExtractionSQLTests(unittest.IsolatedAsyncioTestCase):

    async def test_delete_extraction_sql_never_references_llm_usage(self) -> None:
        """delete_extraction must only delete from pages, jobs, extractions."""
        pool, conn = _make_pool_conn()
        conn.execute.return_value = "DELETE 1"

        await db_mod.delete_extraction(pool, 42)

        all_sqls = " ".join(
            _compact(c.args[0]) for c in conn.execute.call_args_list
        )
        self.assertNotIn("llm_usage", all_sqls)

    async def test_delete_extraction_removes_pages_jobs_extraction(self) -> None:
        """Confirm the three tables that ARE deleted: pages, jobs, extractions."""
        pool, conn = _make_pool_conn()
        conn.execute.return_value = "DELETE 1"

        await db_mod.delete_extraction(pool, 99)

        sqls = " ".join(_compact(c.args[0]) for c in conn.execute.call_args_list)
        self.assertIn("pages", sqls)
        self.assertIn("jobs", sqls)
        self.assertIn("extractions", sqls)

    async def test_delete_extraction_returns_true_on_success(self) -> None:
        pool, conn = _make_pool_conn()
        conn.execute.return_value = "DELETE 1"

        result = await db_mod.delete_extraction(pool, 7)

        self.assertTrue(result)

    async def test_delete_extraction_returns_false_when_row_missing(self) -> None:
        pool, conn = _make_pool_conn()
        conn.execute.return_value = "DELETE 0"

        result = await db_mod.delete_extraction(pool, 999)

        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 2. get_user_billable_pages reads from llm_usage
# ---------------------------------------------------------------------------

class BillablePagesSourceTests(unittest.IsolatedAsyncioTestCase):

    async def test_billable_pages_query_reads_from_llm_usage(self) -> None:
        """get_user_billable_pages must COUNT from llm_usage, not SUM from extractions."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.return_value = {"subscription_limit": 1000, "billable_pages": 30}

        await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )

        sql = _compact(conn.fetchrow.call_args[0][0])
        self.assertIn("llm_usage", sql)
        self.assertIn("COUNT(DISTINCT (lu.extraction_id, lu.page_num))", sql)
        self.assertNotIn("FROM extractions", sql)
        self.assertNotIn("SUM(e.total_pages)", sql)

    async def test_billable_pages_scoped_by_user_id_not_vendor(self) -> None:
        """Billing survives vendor deletion because it filters on lu.user_id."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.return_value = {"subscription_limit": 500, "billable_pages": 30}

        await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )

        sql = _compact(conn.fetchrow.call_args[0][0])
        self.assertIn("lu.user_id = $1", sql)
        self.assertNotIn("JOIN vendors", sql)

    async def test_billable_count_unchanged_after_simulated_deletion(self) -> None:
        """Simulate 30 pages used, delete 3 extractions: billable count stays 30."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.return_value = {"subscription_limit": 100, "billable_pages": 30}
        conn.execute.return_value = "DELETE 1"

        before = await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )
        for eid in [1, 2, 3]:
            await db_mod.delete_extraction(pool, eid)

        after = await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )

        self.assertEqual(before["billable_pages"], 30)
        self.assertEqual(after["billable_pages"], 30)


# ---------------------------------------------------------------------------
# 3. reserve_quota reads from llm_usage
# ---------------------------------------------------------------------------

class ReserveQuotaSourceTests(unittest.IsolatedAsyncioTestCase):

    async def test_reserve_quota_counts_from_llm_usage(self) -> None:
        """reserve_quota must use llm_usage for billable count, not extractions."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.return_value = {
            "subscription_limit": 1000,
            "pending_pages": 0,
            "billable_pages": 30,
        }
        conn.execute.return_value = "UPDATE 1"

        await db_mod.reserve_quota(
            pool, "12345678-1234-5678-1234-567812345678", incoming_pages=5
        )

        sql = _compact(conn.fetchrow.call_args[0][0])
        self.assertIn("llm_usage", sql)
        self.assertIn("COUNT(DISTINCT (lu.extraction_id, lu.page_num))", sql)
        self.assertNotIn("SUM(e.total_pages)", sql)

    async def test_reserve_quota_blocks_user_even_after_deletion(self) -> None:
        """A user at 100/100 cannot upload even if they deleted old extractions."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.return_value = {
            "subscription_limit": 100,
            "pending_pages": 0,
            "billable_pages": 100,
        }
        conn.execute.return_value = "UPDATE 1"

        result = await db_mod.reserve_quota(
            pool, "12345678-1234-5678-1234-567812345678", incoming_pages=1
        )

        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "exceeded")
        self.assertEqual(result["used"], 100)


# ---------------------------------------------------------------------------
# 4. get_usage_stats exposes billable_pages from llm_usage
# ---------------------------------------------------------------------------

class UsageStatsBillablePagesTests(unittest.IsolatedAsyncioTestCase):

    async def test_usage_stats_returns_billable_pages_key(self) -> None:
        """get_usage_stats must include billable_pages (from llm_usage) in response."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.side_effect = [
            {"total_pdfs": 5, "total_extractions": 5, "all_pages": 30, "total_pages": 30},
            {"total_input_tokens": 1000, "total_output_tokens": 400,
             "grand_total": 1400, "total_llm_calls": 5, "billable_pages": 30},
        ]

        stats = await db_mod.get_usage_stats(pool)

        self.assertIn("billable_pages", stats)
        self.assertEqual(stats["billable_pages"], 30)

    async def test_usage_stats_billable_pages_uses_llm_usage_not_extractions(self) -> None:
        """The billable_pages count must come from COUNT(DISTINCT) on llm_usage."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.side_effect = [
            {"total_pdfs": 0, "total_extractions": 0, "all_pages": 0, "total_pages": 0},
            {"total_input_tokens": 0, "total_output_tokens": 0,
             "grand_total": 0, "total_llm_calls": 0, "billable_pages": 0},
        ]

        await db_mod.get_usage_stats(pool)

        llm_sql = _compact(conn.fetchrow.call_args_list[1].args[0])
        self.assertIn("FROM llm_usage", llm_sql)
        self.assertIn("COUNT(DISTINCT (lu.extraction_id, lu.page_num))", llm_sql)
        self.assertIn("lu.call_type = 'extraction'", llm_sql)

    async def test_usage_stats_billable_pages_independent_of_extraction_row_count(self) -> None:
        """Simulate: 5 docs processed, then 3 extractions deleted.
        ext_row now shows 2 docs; llm_usage still shows 30 billable pages."""
        pool, conn = _make_pool_conn()
        conn.fetchrow.side_effect = [
            {"total_pdfs": 2, "total_extractions": 2, "all_pages": 12, "total_pages": 12},
            {"total_input_tokens": 5000, "total_output_tokens": 2000,
             "grand_total": 7000, "total_llm_calls": 30, "billable_pages": 30},
        ]

        stats = await db_mod.get_usage_stats(pool)

        self.assertEqual(stats["total_extractions"], 2)
        self.assertEqual(stats["billable_pages"], 30)


# ---------------------------------------------------------------------------
# 5. get_usage_by_client exposes billable_pages from llm_usage
# ---------------------------------------------------------------------------

class ClientBreakdownBillablePagesTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_usage_by_client_returns_billable_pages(self) -> None:
        """Admin client table must show billable_pages from llm_usage."""
        pool, conn = _make_pool_conn()
        conn.fetch.return_value = [
            {"user_id": "u1", "email": "c@test.com", "role": "client",
             "is_active": True, "total_extractions": 3, "billable_pages": 30,
             "total_input_tokens": 5000, "total_output_tokens": 2000,
             "grand_total": 7000, "total_llm_calls": 30}
        ]

        rows = await db_mod.get_usage_by_client(pool)

        self.assertEqual(rows[0]["billable_pages"], 30)
        self.assertNotIn("total_pages", rows[0])

    async def test_get_usage_by_client_sql_uses_llm_usage_for_pages(self) -> None:
        """SQL must COUNT from llm_usage, not SUM from extractions."""
        pool, conn = _make_pool_conn()
        conn.fetch.return_value = []

        await db_mod.get_usage_by_client(pool)

        sql = _compact(conn.fetch.call_args[0][0])
        self.assertIn("COUNT(DISTINCT (lu.extraction_id, lu.page_num))", sql)
        self.assertNotIn("SUM(e.total_pages)", sql)

    async def test_history_count_reflects_current_extractions_not_billing(self) -> None:
        """HISTORY column (total_extractions) can decrease on deletion.
        It reflects visible records; billable_pages stays at original count."""
        pool, conn = _make_pool_conn()
        conn.fetch.return_value = [
            {"user_id": "u1", "email": "c@test.com", "role": "client",
             "is_active": True, "total_extractions": 2, "billable_pages": 30,
             "total_input_tokens": 0, "total_output_tokens": 0,
             "grand_total": 0, "total_llm_calls": 0}
        ]

        rows = await db_mod.get_usage_by_client(pool)

        # 3 of the original 5 extractions were deleted, so history shows 2
        self.assertEqual(rows[0]["total_extractions"], 2)
        # billing is unchanged
        self.assertEqual(rows[0]["billable_pages"], 30)


# ---------------------------------------------------------------------------
# 6 & 7. HTTP: DELETE then GET /me/usage
# ---------------------------------------------------------------------------

class DeleteThenUsageHTTPTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self) -> None:
        main.limiter.reset()
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()

    def tearDown(self) -> None:
        main.app.dependency_overrides[get_current_user] = _admin_user

    def _done_extraction(self, eid: int = 1) -> dict:
        return {
            "id": eid, "status": "done", "vendor_id": "acme",
            "filename": "doc.pdf", "document_id": 10,
            "cancel_requested": False,
        }

    def _delete_patches(self, eid: int = 1):
        """All mocks needed for DELETE /extractions/{id} to succeed."""
        return (
            patch.object(main, "assert_extraction_access", new=AsyncMock()),
            patch.object(main.db_mod, "get_extraction",
                         new=AsyncMock(return_value=self._done_extraction(eid))),
            patch.object(main.db_mod, "get_document",
                         new=AsyncMock(return_value={"id": 10, "object_key": None})),
            patch.object(main.db_mod, "get_page_object_keys",
                         new=AsyncMock(return_value=[])),
            patch.object(main.db_mod, "delete_extraction",
                         new=AsyncMock(return_value=True)),
            patch.object(main.db_mod, "count_extractions_for_document",
                         new=AsyncMock(return_value=0)),
            patch.object(main.db_mod, "delete_document",
                         new=AsyncMock(return_value=True)),
        )

    def test_delete_endpoint_only_removes_extraction_not_llm_usage(self) -> None:
        """DELETE /extractions/{id} calls delete_extraction (pages/jobs/extractions).
        There must be no separate db_mod function that deletes llm_usage rows."""
        with patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=self._done_extraction())), \
             patch.object(main.db_mod, "get_document",
                          new=AsyncMock(return_value={"id": 10, "object_key": None})), \
             patch.object(main.db_mod, "get_page_object_keys",
                          new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "delete_extraction",
                          new=AsyncMock(return_value=True)) as mock_del_ext, \
             patch.object(main.db_mod, "count_extractions_for_document",
                          new=AsyncMock(return_value=0)), \
             patch.object(main.db_mod, "delete_document",
                          new=AsyncMock(return_value=True)):
            r = self.client.delete("/extractions/1")

        self.assertEqual(r.status_code, 200)
        # The extraction delete is called exactly once
        mock_del_ext.assert_called_once_with(main.app.state.pool, 1)
        # db_mod has no delete_llm_usage function — this is the architectural guarantee
        self.assertFalse(hasattr(db_mod, "delete_llm_usage"))

    def test_me_usage_returns_same_count_after_deletion(self) -> None:
        """GET /me/usage must return the same billable count before and after DELETE."""
        usage_30 = _usage(30, 100)

        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=usage_30)):
            before = self.client.get("/me/usage").json()

        ctx_managers = self._delete_patches()
        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], \
             ctx_managers[4], ctx_managers[5], ctx_managers[6]:
            self.client.delete("/extractions/1")

        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=usage_30)):
            after = self.client.get("/me/usage").json()

        self.assertEqual(before["billable_pages"], 30)
        self.assertEqual(after["billable_pages"], 30)

    def test_active_extraction_cannot_be_deleted(self) -> None:
        """Active (processing) extractions return 409 — no partial delete."""
        active = {**self._done_extraction(), "status": "processing"}
        with patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=active)):
            r = self.client.delete("/extractions/1")
        self.assertEqual(r.status_code, 409)

    def test_delete_response_body_contains_status_deleted(self) -> None:
        ctx_managers = self._delete_patches()
        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], \
             ctx_managers[4], ctx_managers[5], ctx_managers[6]:
            r = self.client.delete("/extractions/1")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "deleted")


# ---------------------------------------------------------------------------
# 8. Quota block persists after deleting history
# ---------------------------------------------------------------------------

class QuotaPersistsAfterDeleteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self) -> None:
        main.limiter.reset()
        _sched = patch.object(
            main.db_mod, "get_user_is_executing", new=AsyncMock(return_value=False)
        )
        _sched.start()
        self.addCleanup(_sched.stop)
        _pages = patch.object(main.processor, "count_pdf_pages", return_value=1)
        _pages.start()
        self.addCleanup(_pages.stop)
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()

    def tearDown(self) -> None:
        main.app.dependency_overrides[get_current_user] = _admin_user

    def _done_extraction(self, eid: int = 1) -> dict:
        return {
            "id": eid, "status": "done", "vendor_id": "acme",
            "filename": "doc.pdf", "document_id": 10,
            "cancel_requested": False,
        }

    def _do_delete(self, eid: int = 1) -> None:
        with patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=self._done_extraction(eid))), \
             patch.object(main.db_mod, "get_document",
                          new=AsyncMock(return_value={"id": 10, "object_key": None})), \
             patch.object(main.db_mod, "get_page_object_keys",
                          new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "delete_extraction",
                          new=AsyncMock(return_value=True)), \
             patch.object(main.db_mod, "count_extractions_for_document",
                          new=AsyncMock(return_value=0)), \
             patch.object(main.db_mod, "delete_document",
                          new=AsyncMock(return_value=True)):
            r = self.client.delete(f"/extractions/{eid}")
        self.assertEqual(r.status_code, 200)

    def test_zero_limit_user_blocked_even_after_deleting_all_history(self) -> None:
        """limit=0 user is blocked even with an empty history.
        Deleting extractions doesn't reset their quota (quota uses llm_usage)."""
        self._do_delete(1)

        with patch.object(main.db_mod, "reserve_quota",
                          new=AsyncMock(return_value=_quota_exceeded(0, 0))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("new.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["code"], "QUOTA_EXCEEDED")

    def test_fully_used_quota_still_blocked_after_partial_history_delete(self) -> None:
        """User at 30/30: deletes 3 extractions. llm_usage still shows 30 → still blocked."""
        for eid in [1, 2, 3]:
            self._do_delete(eid)

        with patch.object(main.db_mod, "reserve_quota",
                          new=AsyncMock(return_value=_quota_exceeded(30, 30))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("new.pdf", b"%PDF-fake", "application/pdf")},
            )

        self.assertEqual(r.status_code, 402)
        detail = r.json()["error"]
        self.assertEqual(detail["total_extracted_pages"], 30)
        self.assertEqual(detail["subscription_limit"], 30)

    def test_deleting_history_items_reduces_visible_count_not_billing(self) -> None:
        """HISTORY ITEMS can go down on delete; PAGES USED must stay the same.
        Tests that both GET /me/usage and the quota check still return 30."""
        self._do_delete(1)

        usage = _usage(30, 100)
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=usage)):
            r = self.client.get("/me/usage")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["billable_pages"], 30)
        self.assertEqual(r.json()["subscription_limit"], 100)


if __name__ == "__main__":
    unittest.main()
