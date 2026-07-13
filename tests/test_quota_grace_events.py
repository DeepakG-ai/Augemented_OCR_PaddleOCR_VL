"""
Tests for the quota grace-event feature.

Coverage:
  1. reserve_quota — grace_pages_used math (ok / grace / exceeded)
  2. insert_quota_grace_event — SQL called with right args; bad UUID noop; DB errors swallowed
  3. get_admin_quota_events — returns dicts with email; propagates limit param
  4. page_logger.log_limit_alert — overage sign fix; grace_pages_used in record
  5. POST /ingest/ui — insert_quota_grace_event called on grace and exceeded paths
     — usage_warning includes grace_pages_used; message mentions grace count
  6. GET /admin/quota-events — 200 for admin; 403 for client; correct response shape
"""
from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod
import backend.main as main
import backend.page_logger as page_logger
from backend.auth import get_current_user, require_admin


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_GOOD_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ADMIN_UUID = "00000000-0000-0000-0000-000000000000"


@asynccontextmanager
async def _fake_acquire(conn):
    yield conn


@asynccontextmanager
async def _fake_txn():
    yield


def _make_pool(conn=None):
    pool = MagicMock()
    conn = conn or AsyncMock()
    conn.transaction = MagicMock(side_effect=lambda: _fake_txn())
    pool.acquire.return_value = _fake_acquire(conn)
    return pool, conn


def _sub_row(page_limit: int = 100, used_pages: int = 0, topup: int = 0):
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=180)
    return {"id": 1, "page_limit": page_limit, "period_start": now, "period_end": end}


def _reserve_conn(pending: int = 0, page_limit: int = 100, topup: int = 0, used: int = 0):
    """Return a conn mock wired up for reserve_quota."""
    conn = AsyncMock()
    conn.transaction = MagicMock(side_effect=lambda: _fake_txn())
    conn.fetchrow.side_effect = [
        {"pending": pending},          # user FOR UPDATE
        _sub_row(page_limit),          # active subscription
    ]
    conn.fetchval.side_effect = [topup, used]   # SUM(topups), COUNT(pages)
    return conn


def _client_user(uid: str = _GOOD_UUID) -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test.com"}


def _admin_user() -> dict:
    return {"id": _ADMIN_UUID, "role": "admin", "email": "admin@test.com"}


def _quota_ok(used: int = 50, limit: int = 100) -> dict:
    return {
        "allowed": True, "reason": "ok",
        "used": used, "limit": limit,
        "remaining": max(limit - used, 0), "pending": 0,
        "grace_pages_used": 0,
    }


def _quota_grace(used: int = 99, limit: int = 100, incoming: int = 5) -> dict:
    grace_used = max(0, used + incoming - limit)
    return {
        "allowed": True, "reason": "grace",
        "used": used, "limit": limit,
        "remaining": 0, "pending": 0,
        "grace_pages_used": grace_used,
    }


def _quota_exceeded(used: int = 100, limit: int = 100) -> dict:
    return {
        "allowed": False, "reason": "exceeded",
        "used": used, "limit": limit,
        "remaining": 0, "pending": 0,
        "grace_pages_used": 0,
    }


def _fake_submission() -> dict:
    return {
        "job": {"id": 1, "status": "queued"},
        "extraction": {"id": 1, "document_id": 1},
    }


class _ScopedOverrides:
    def __init__(self, app, overrides):
        self.app = app
        self.overrides = overrides
        self._saved: dict = {}

    def __enter__(self):
        self._saved = dict(self.app.dependency_overrides)
        self.app.dependency_overrides.update(self.overrides)
        return self

    def __exit__(self, *exc):
        self.app.dependency_overrides.clear()
        self.app.dependency_overrides.update(self._saved)


# ---------------------------------------------------------------------------
# 1. reserve_quota — grace_pages_used math
# ---------------------------------------------------------------------------

class ReserveQuotaGracePagesUsedTests(unittest.IsolatedAsyncioTestCase):
    """grace_pages_used = max(0, committed + incoming - effective_limit) on grace path;
    0 on all other paths."""

    async def test_ok_path_returns_zero_grace_pages(self) -> None:
        """Well under limit → reason='ok', grace_pages_used=0."""
        conn = _reserve_conn(pending=0, page_limit=100, topup=0, used=50)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=5)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["grace_pages_used"], 0)

    async def test_grace_path_computes_pages_over_limit(self) -> None:
        """used=99, limit=100, incoming=5 → committed=99, grace_used=4."""
        conn = _reserve_conn(pending=0, page_limit=100, topup=0, used=99)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=5)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "grace")
        self.assertEqual(result["grace_pages_used"], 4)   # 99+5-100=4

    async def test_grace_path_single_page_over(self) -> None:
        """used=99, limit=100, incoming=2 → grace_used=1."""
        conn = _reserve_conn(pending=0, page_limit=100, topup=0, used=99)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=2)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "grace")
        self.assertEqual(result["grace_pages_used"], 1)   # 99+2-100=1

    async def test_grace_path_nine_pages_over(self) -> None:
        """used=95, limit=100, incoming=14 → grace would give grace_used=9.
        But 14 > grace_pages(10) → exceeded; grace_pages_used stays 0."""
        conn = _reserve_conn(pending=0, page_limit=100, topup=0, used=95)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=14, grace_pages=10)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "exceeded")
        self.assertEqual(result["grace_pages_used"], 0)

    async def test_grace_path_exactly_at_grace_boundary(self) -> None:
        """used=91, limit=100, incoming=10 → committed=91 < 100, incoming ≤ 10 → grace.
        grace_used = 91+10-100 = 1."""
        conn = _reserve_conn(pending=0, page_limit=100, topup=0, used=91)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=10, grace_pages=10)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "grace")
        self.assertEqual(result["grace_pages_used"], 1)

    async def test_exceeded_path_returns_zero_grace_pages(self) -> None:
        """used=100, limit=100 (committed==limit) → no grace, grace_pages_used=0."""
        conn = _reserve_conn(pending=0, page_limit=100, topup=0, used=100)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=1)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "exceeded")
        self.assertEqual(result["grace_pages_used"], 0)

    async def test_grace_with_pending_counts_toward_committed(self) -> None:
        """used=90, pending=9, limit=100, incoming=5 → committed=99, grace_used=4."""
        conn = _reserve_conn(pending=9, page_limit=100, topup=0, used=90)
        pool, _ = _make_pool(conn)
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=5)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "grace")
        self.assertEqual(result["grace_pages_used"], 4)   # 99+5-100=4


# ---------------------------------------------------------------------------
# 2. insert_quota_grace_event
# ---------------------------------------------------------------------------

class InsertQuotaGraceEventTests(unittest.IsolatedAsyncioTestCase):

    async def test_inserts_grace_event_with_correct_params(self) -> None:
        conn = AsyncMock()
        pool, _ = _make_pool(conn)
        await db_mod.insert_quota_grace_event(
            pool,
            user_id=_GOOD_UUID,
            event_type="grace_used",
            grace_pages_used=4,
            incoming_pages=5,
            used_before=99,
            limit_at_time=100,
            filename="invoice.pdf",
        )
        conn.execute.assert_awaited_once()
        sql, *args = conn.execute.call_args.args
        self.assertIn("INSERT INTO quota_grace_events", sql)
        # Positional params: uid, event_type, grace_pages_used, incoming, used, limit, filename
        self.assertEqual(args[1], "grace_used")
        self.assertEqual(args[2], 4)
        self.assertEqual(args[3], 5)
        self.assertEqual(args[4], 99)
        self.assertEqual(args[5], 100)
        self.assertEqual(args[6], "invoice.pdf")

    async def test_inserts_exceeded_event(self) -> None:
        conn = AsyncMock()
        pool, _ = _make_pool(conn)
        await db_mod.insert_quota_grace_event(
            pool,
            user_id=_GOOD_UUID,
            event_type="exceeded",
            grace_pages_used=0,
            incoming_pages=11,
            used_before=100,
            limit_at_time=100,
        )
        conn.execute.assert_awaited_once()
        sql, *args = conn.execute.call_args.args
        self.assertIn("INSERT INTO quota_grace_events", sql)
        self.assertEqual(args[1], "exceeded")
        self.assertEqual(args[2], 0)

    async def test_bad_uuid_returns_silently(self) -> None:
        """Malformed user_id → no DB calls, no exception."""
        conn = AsyncMock()
        pool, _ = _make_pool(conn)
        await db_mod.insert_quota_grace_event(
            pool,
            user_id="not-a-uuid",
            event_type="grace_used",
            grace_pages_used=4,
            incoming_pages=5,
            used_before=99,
            limit_at_time=100,
        )
        conn.execute.assert_not_awaited()

    async def test_db_error_silently_swallowed(self) -> None:
        """A DB error must not propagate — upload path must not be crashed."""
        conn = AsyncMock()
        conn.execute.side_effect = RuntimeError("connection lost")
        pool, _ = _make_pool(conn)
        # Should not raise
        await db_mod.insert_quota_grace_event(
            pool,
            user_id=_GOOD_UUID,
            event_type="grace_used",
            grace_pages_used=4,
            incoming_pages=5,
            used_before=99,
            limit_at_time=100,
        )

    async def test_none_filename_stored_as_null(self) -> None:
        conn = AsyncMock()
        pool, _ = _make_pool(conn)
        await db_mod.insert_quota_grace_event(
            pool,
            user_id=_GOOD_UUID,
            event_type="grace_used",
            grace_pages_used=1,
            incoming_pages=2,
            used_before=99,
            limit_at_time=100,
            filename=None,
        )
        conn.execute.assert_awaited_once()
        *_, filename_arg = conn.execute.call_args.args
        self.assertIsNone(filename_arg)


# ---------------------------------------------------------------------------
# 3. get_admin_quota_events
# ---------------------------------------------------------------------------

class GetAdminQuotaEventsTests(unittest.IsolatedAsyncioTestCase):

    def _make_db_row(self, **kwargs):
        defaults = {
            "id": 1,
            "event_ts": datetime.now(timezone.utc),
            "event_type": "grace_used",
            "grace_pages_used": 4,
            "incoming_pages": 5,
            "used_before": 99,
            "limit_at_time": 100,
            "filename": "test.pdf",
            "email": "client@test.com",
        }
        defaults.update(kwargs)
        # asyncpg Record-like: just use a dict (dict(r) works on MagicMock if __iter__ works)
        return MagicMock(**defaults, **{"__iter__": lambda s: iter(defaults.items()),
                                        "keys": lambda s: defaults.keys(),
                                        "items": lambda s: defaults.items()})

    async def test_returns_list_of_dicts_with_email(self) -> None:
        row = {
            "id": 1, "event_ts": datetime.now(timezone.utc),
            "event_type": "grace_used", "grace_pages_used": 4,
            "incoming_pages": 5, "used_before": 99,
            "limit_at_time": 100, "filename": "inv.pdf",
            "email": "deepak@test.com",
        }
        conn = AsyncMock()
        conn.fetch.return_value = [row]
        pool, _ = _make_pool(conn)
        pool.acquire.return_value = _fake_acquire(conn)

        results = await db_mod.get_admin_quota_events(pool, limit=50)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["email"], "deepak@test.com")
        self.assertEqual(results[0]["grace_pages_used"], 4)
        self.assertEqual(results[0]["event_type"], "grace_used")

    async def test_empty_result_returns_empty_list(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool, _ = _make_pool(conn)
        pool.acquire.return_value = _fake_acquire(conn)

        results = await db_mod.get_admin_quota_events(pool, limit=10)
        self.assertEqual(results, [])

    async def test_limit_is_passed_to_query(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool, _ = _make_pool(conn)
        pool.acquire.return_value = _fake_acquire(conn)

        await db_mod.get_admin_quota_events(pool, limit=25)

        fetch_args = conn.fetch.call_args.args
        # Last positional arg is the LIMIT $1 value
        self.assertEqual(fetch_args[-1], 25)

    async def test_joins_email_from_users_table(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool, _ = _make_pool(conn)
        pool.acquire.return_value = _fake_acquire(conn)

        await db_mod.get_admin_quota_events(pool)

        sql = conn.fetch.call_args.args[0]
        self.assertIn("JOIN users", sql)
        self.assertIn("u.email", sql)

    async def test_orders_by_event_ts_desc(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool, _ = _make_pool(conn)
        pool.acquire.return_value = _fake_acquire(conn)

        await db_mod.get_admin_quota_events(pool)

        sql = conn.fetch.call_args.args[0]
        self.assertIn("ORDER BY qge.event_ts DESC", sql)


# ---------------------------------------------------------------------------
# 4. page_logger.log_limit_alert — overage sign + grace_pages_used
# ---------------------------------------------------------------------------

class PageLoggerLimitAlertTests(unittest.TestCase):

    def setUp(self) -> None:
        self._buf = StringIO()
        patcher = patch.object(page_logger, "_get_alerts_file", return_value=self._buf)
        self._patch = patcher.start()
        self.addCleanup(patcher.stop)

    def _written(self) -> str:
        return self._buf.getvalue()

    def test_overage_is_positive_when_total_exceeds_limit(self) -> None:
        """total=105, limit=100 → overage=5 (was -5 before the fix)."""
        page_logger.log_limit_alert(
            user_id=_GOOD_UUID,
            email="a@test.com",
            total_extracted_pages=105,
            subscription_limit=100,
            alert_type="exceeded",
        )
        written = self._written()
        self.assertIn("overage=5", written)
        self.assertNotIn("overage=-5", written)

    def test_overage_is_zero_when_total_equals_limit(self) -> None:
        """total=100, limit=100 → overage=0."""
        page_logger.log_limit_alert(
            user_id=_GOOD_UUID,
            email="a@test.com",
            total_extracted_pages=100,
            subscription_limit=100,
            alert_type="warning",
        )
        self.assertIn("overage=0", self._written())

    def test_overage_is_zero_when_total_below_limit(self) -> None:
        """total=90, limit=100 → overage=0 (warning, not over yet)."""
        page_logger.log_limit_alert(
            user_id=_GOOD_UUID,
            email="a@test.com",
            total_extracted_pages=90,
            subscription_limit=100,
            alert_type="warning",
        )
        self.assertIn("overage=0", self._written())

    def test_grace_pages_used_written_when_nonzero(self) -> None:
        page_logger.log_limit_alert(
            user_id=_GOOD_UUID,
            email="a@test.com",
            total_extracted_pages=99,
            subscription_limit=100,
            alert_type="small_overage",
            grace_pages_used=4,
        )
        self.assertIn("grace_pages_used=4", self._written())

    def test_grace_pages_used_absent_when_zero(self) -> None:
        """grace_pages_used=0 must not clutter the log line."""
        page_logger.log_limit_alert(
            user_id=_GOOD_UUID,
            email="a@test.com",
            total_extracted_pages=90,
            subscription_limit=100,
            alert_type="warning",
            grace_pages_used=0,
        )
        self.assertNotIn("grace_pages_used", self._written())

    def test_alert_type_recorded_in_log(self) -> None:
        page_logger.log_limit_alert(
            user_id=_GOOD_UUID,
            email="test@x.com",
            total_extracted_pages=99,
            subscription_limit=100,
            alert_type="small_overage",
        )
        self.assertIn("small_overage", self._written())

    def test_never_raises_on_write_error(self) -> None:
        """log_limit_alert must not propagate exceptions — billing alerts must not crash the pipeline."""
        broken = MagicMock()
        broken.write.side_effect = OSError("disk full")
        with patch.object(page_logger, "_get_alerts_file", return_value=broken):
            page_logger.log_limit_alert(  # must not raise
                user_id=_GOOD_UUID,
                email="x@x.com",
                total_extracted_pages=100,
                subscription_limit=100,
                alert_type="exceeded",
            )


# ---------------------------------------------------------------------------
# 5. POST /ingest/ui — grace and exceeded event persistence
# ---------------------------------------------------------------------------

class IngestGraceEventPersistenceTests(unittest.TestCase):
    """Verify that insert_quota_grace_event is called on both the grace and
    exceeded code paths in the ingest handler, with correct arguments."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self) -> None:
        main.limiter.reset()
        _pages = patch.object(main.processor, "count_pdf_pages", return_value=5)
        _pages.start()
        self.addCleanup(_pages.stop)

    def tearDown(self) -> None:
        main.app.dependency_overrides[get_current_user] = _admin_user

    def _use_client(self, uid: str = _GOOD_UUID) -> None:
        main.app.dependency_overrides[get_current_user] = lambda: _client_user(uid)

    # -- grace path ----------------------------------------------------------

    def test_grace_path_calls_insert_quota_grace_event(self) -> None:
        """When quota reason is 'grace', insert_quota_grace_event must be called
        with event_type='grace_used' and the correct grace_pages_used count."""
        self._use_client()
        quota = _quota_grace(used=99, limit=100, incoming=5)  # grace_pages_used=4
        with patch.object(main.db_mod, "reserve_quota", new=AsyncMock(return_value=quota)), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "deepak@test.com"})), \
             patch.object(main.db_mod, "insert_quota_grace_event",
                          new=AsyncMock()) as mock_insert, \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("inv.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        mock_insert.assert_awaited_once()
        kw = mock_insert.call_args.kwargs
        self.assertEqual(kw["event_type"], "grace_used")
        self.assertEqual(kw["grace_pages_used"], 4)
        self.assertEqual(kw["incoming_pages"], 5)
        self.assertEqual(kw["used_before"], 99)
        self.assertEqual(kw["limit_at_time"], 100)

    def test_grace_path_usage_warning_includes_grace_pages_used(self) -> None:
        """The usage_warning returned in the response must include grace_pages_used."""
        self._use_client()
        quota = _quota_grace(used=99, limit=100, incoming=5)
        with patch.object(main.db_mod, "reserve_quota", new=AsyncMock(return_value=quota)), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "deepak@test.com"})), \
             patch.object(main.db_mod, "insert_quota_grace_event", new=AsyncMock()), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("inv.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)
        body = r.json()
        self.assertIn("usage_warning", body)
        self.assertEqual(body["usage_warning"]["grace_pages_used"], 4)

    def test_grace_path_warning_message_mentions_grace_count(self) -> None:
        """The human-readable warning message must mention how many grace pages were used."""
        self._use_client()
        quota = _quota_grace(used=99, limit=100, incoming=5)
        with patch.object(main.db_mod, "reserve_quota", new=AsyncMock(return_value=quota)), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "deepak@test.com"})), \
             patch.object(main.db_mod, "insert_quota_grace_event", new=AsyncMock()), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("inv.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        msg = r.json()["usage_warning"]["message"]
        self.assertIn("4 grace page", msg)

    def test_grace_path_single_grace_page_uses_singular(self) -> None:
        """1 grace page → 'page' (not 'pages')."""
        self._use_client()
        quota = _quota_grace(used=99, limit=100, incoming=2)  # grace_pages_used=1
        with patch.object(main.db_mod, "reserve_quota", new=AsyncMock(return_value=quota)), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "x@test.com"})), \
             patch.object(main.db_mod, "insert_quota_grace_event", new=AsyncMock()), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("inv.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        msg = r.json()["usage_warning"]["message"]
        self.assertIn("1 grace page", msg)
        self.assertNotIn("1 grace pages", msg)

    # -- exceeded path -------------------------------------------------------

    def test_exceeded_path_calls_insert_quota_grace_event(self) -> None:
        """When quota is exceeded, insert_quota_grace_event must be called
        with event_type='exceeded' and grace_pages_used=0."""
        self._use_client()
        quota = _quota_exceeded(used=100, limit=100)
        with patch.object(main.db_mod, "reserve_quota", new=AsyncMock(return_value=quota)), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "deepak@test.com"})), \
             patch.object(main.db_mod, "insert_quota_grace_event",
                          new=AsyncMock()) as mock_insert:
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("inv.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)
        mock_insert.assert_awaited_once()
        kw = mock_insert.call_args.kwargs
        self.assertEqual(kw["event_type"], "exceeded")
        self.assertEqual(kw["grace_pages_used"], 0)
        self.assertEqual(kw["incoming_pages"], 5)
        self.assertEqual(kw["used_before"], 100)
        self.assertEqual(kw["limit_at_time"], 100)

    def test_ok_path_does_not_call_insert_quota_grace_event(self) -> None:
        """Normal upload well under the limit must NOT record any grace event."""
        self._use_client()
        quota = _quota_ok(used=50, limit=100)
        with patch.object(main.db_mod, "reserve_quota", new=AsyncMock(return_value=quota)), \
             patch.object(main.db_mod, "insert_quota_grace_event",
                          new=AsyncMock()) as mock_insert, \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("inv.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)
        mock_insert.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. GET /admin/quota-events endpoint
# ---------------------------------------------------------------------------

class AdminQuotaEventsEndpointTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self) -> None:
        main.limiter.reset()
        main.app.dependency_overrides[get_current_user] = _admin_user

    def tearDown(self) -> None:
        main.app.dependency_overrides[get_current_user] = _admin_user

    def _event_row(self, event_type: str = "grace_used", grace_pages_used: int = 4,
                   email: str = "deepak@test.com") -> dict:
        return {
            "id": 1,
            "event_ts": datetime.now(timezone.utc),
            "event_type": event_type,
            "grace_pages_used": grace_pages_used,
            "incoming_pages": 5,
            "used_before": 99,
            "limit_at_time": 100,
            "filename": "invoice.pdf",
            "email": email,
        }

    def test_admin_gets_200_with_list(self) -> None:
        rows = [self._event_row("grace_used", 4, "deepak@test.com"),
                self._event_row("exceeded", 0, "alice@test.com")]
        with patch.object(main.db_mod, "get_admin_quota_events",
                          new=AsyncMock(return_value=rows)):
            r = self.client.get("/admin/quota-events")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 2)

    def test_response_shape_includes_required_fields(self) -> None:
        rows = [self._event_row()]
        with patch.object(main.db_mod, "get_admin_quota_events",
                          new=AsyncMock(return_value=rows)):
            r = self.client.get("/admin/quota-events")
        self.assertEqual(r.status_code, 200)
        event = r.json()[0]
        for key in ("id", "event_ts", "event_type", "email",
                    "grace_pages_used", "incoming_pages",
                    "used_before", "limit_at_time", "filename"):
            self.assertIn(key, event, f"missing key: {key}")

    def test_grace_event_has_correct_values(self) -> None:
        rows = [self._event_row("grace_used", 4, "deepak@test.com")]
        with patch.object(main.db_mod, "get_admin_quota_events",
                          new=AsyncMock(return_value=rows)):
            r = self.client.get("/admin/quota-events")
        event = r.json()[0]
        self.assertEqual(event["event_type"], "grace_used")
        self.assertEqual(event["grace_pages_used"], 4)
        self.assertEqual(event["email"], "deepak@test.com")
        self.assertEqual(event["incoming_pages"], 5)
        self.assertEqual(event["used_before"], 99)
        self.assertEqual(event["limit_at_time"], 100)

    def test_empty_returns_empty_list(self) -> None:
        with patch.object(main.db_mod, "get_admin_quota_events",
                          new=AsyncMock(return_value=[])):
            r = self.client.get("/admin/quota-events")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_limit_param_capped_at_500(self) -> None:
        """?limit= values above 500 must be capped to 500 by the endpoint."""
        with patch.object(main.db_mod, "get_admin_quota_events",
                          new=AsyncMock(return_value=[])) as mock_fn:
            self.client.get("/admin/quota-events?limit=9999")
        actual_limit = mock_fn.call_args.kwargs.get("limit") or mock_fn.call_args.args[1]
        self.assertLessEqual(actual_limit, 500)

    def test_client_cannot_access_quota_events(self) -> None:
        """Non-admin users must get 403."""
        client_overrides = {get_current_user: lambda: _client_user()}
        with _ScopedOverrides(main.app, client_overrides):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.get("/admin/quota-events")
        self.assertEqual(r.status_code, 403)

    def test_datetime_serialised_as_iso_string(self) -> None:
        """event_ts must come back as an ISO 8601 string, not a raw datetime object."""
        rows = [self._event_row()]
        with patch.object(main.db_mod, "get_admin_quota_events",
                          new=AsyncMock(return_value=rows)):
            r = self.client.get("/admin/quota-events")
        ts = r.json()[0]["event_ts"]
        self.assertIsInstance(ts, str)
        datetime.fromisoformat(ts)  # must parse without error


if __name__ == "__main__":
    unittest.main()
