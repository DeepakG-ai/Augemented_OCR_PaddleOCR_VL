"""
Tests for subscription page-quota enforcement.

Soft-limit model
----------------
- The document that CAUSES the overage is processed (soft).
- Every SUBSEQUENT request while used >= limit is blocked with HTTP 402.
- Admins bypass the quota check entirely.
- Warning is emitted at SUBSCRIPTION_WARNING_THRESHOLD (90 %) but the
  request is still allowed.
- New users default to 0 pages — admin must set the limit explicitly.

Test surface
------------
1. DB layer  – get_user_billable_pages / update_user_subscription_limit
2. Ingest endpoint (POST /ingest/ui) – quota enforcement
3. Self-service endpoint (GET /me/usage)
4. Admin subscription-limit API (PATCH /admin/users/{id}/subscription-limit)
5. Multi-client isolation
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


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _client_user(uid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test.com"}


def _admin_user(uid: str = "00000000-0000-0000-0000-000000000000") -> dict:
    return {"id": uid, "role": "admin", "email": "admin@test.com"}


def _usage(used: int, limit: int) -> dict:
    return {
        "billable_pages": used,
        "subscription_limit": limit,
        "remaining": limit - used,  # negative when over-limit
    }


def _fake_submission() -> dict:
    """Minimal dict that satisfies the ingest endpoint's response builder."""
    return {
        "job": {"id": 1, "status": "queued"},
        "extraction": {"id": 1, "document_id": 1},
    }


# ---------------------------------------------------------------------------
# 1. DB layer
# ---------------------------------------------------------------------------

class PageLimitsQueryTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_user_billable_pages_query_structure(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = _fake_acquire(conn)
        conn.fetchrow.return_value = {"subscription_limit": 5000, "billable_pages": 42}

        result = await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )

        self.assertTrue(conn.fetchrow.called)
        sql = _compact(conn.fetchrow.call_args[0][0])
        self.assertIn("SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))", sql)
        self.assertIn("lu.call_type = 'extraction'", sql)
        # Default fallback is now 0, not 1000
        self.assertIn("COALESCE(u.subscription_limit, 0) AS subscription_limit", sql)
        self.assertEqual(result["subscription_limit"], 5000)
        self.assertEqual(result["billable_pages"], 42)
        self.assertEqual(result["remaining"], 4958)

    async def test_get_user_billable_pages_handles_none_row(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = _fake_acquire(conn)
        conn.fetchrow.return_value = None

        result = await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )
        # Fallback is 0 — admin must set it
        self.assertEqual(result["subscription_limit"], 0)
        self.assertEqual(result["billable_pages"], 0)
        self.assertEqual(result["remaining"], 0)

    async def test_fallback_for_invalid_uuid_returns_zero(self) -> None:
        pool = MagicMock()
        result = await db_mod.get_user_billable_pages(pool, "not-a-uuid")
        self.assertEqual(result["subscription_limit"], 0)
        self.assertEqual(result["billable_pages"], 0)
        self.assertEqual(result["remaining"], 0)

    async def test_remaining_is_negative_when_exceeded(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = _fake_acquire(conn)
        conn.fetchrow.return_value = {"subscription_limit": 500, "billable_pages": 503}

        result = await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )
        # remaining = 500 - 503 = -3 (overage stored as negative)
        self.assertEqual(result["remaining"], -3)

    async def test_remaining_exactly_at_limit_is_zero(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = _fake_acquire(conn)
        conn.fetchrow.return_value = {"subscription_limit": 1000, "billable_pages": 1000}

        result = await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )
        self.assertEqual(result["remaining"], 0)

    async def test_billing_query_uses_user_id_not_vendor_join(self) -> None:
        """Billing query must filter by lu.user_id, not JOIN vendors.
        After vendor deletion vendor_id is detached; user_id persists on llm_usage."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = _fake_acquire(conn)
        conn.fetchrow.return_value = {"subscription_limit": 1000, "billable_pages": 75}

        await db_mod.get_user_billable_pages(
            pool, "12345678-1234-5678-1234-567812345678"
        )

        sql = _compact(conn.fetchrow.call_args[0][0])
        self.assertIn("lu.user_id = $1", sql)
        self.assertNotIn("JOIN vendors", sql)

    async def test_update_user_subscription_limit_query_structure(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = _fake_acquire(conn)
        conn.execute.return_value = "UPDATE 1"

        ok = await db_mod.update_user_subscription_limit(
            pool, "12345678-1234-5678-1234-567812345678", 2500
        )

        self.assertTrue(ok)
        sql = conn.execute.call_args[0][0]
        args = conn.execute.call_args[0][1:]
        self.assertIn("UPDATE users SET subscription_limit = $1 WHERE id = $2", sql)
        self.assertEqual(args[0], 2500)


# ---------------------------------------------------------------------------
# 2. Ingest endpoint quota enforcement
# ---------------------------------------------------------------------------

class SubscriptionQuotaEnforcementTests(unittest.TestCase):
    """POST /ingest/ui — quota check on client users."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self) -> None:
        main.limiter.reset()
        # The /ingest/ui endpoint runs a scheduler guard
        # (db_mod.get_user_is_executing) before the quota check. These tests
        # use a non-DB fake pool, so stub the guard to "not executing" — the
        # quota path under test stays exercised.
        _sched = patch.object(
            main.db_mod, "get_user_is_executing", new=AsyncMock(return_value=False)
        )
        _sched.start()
        self.addCleanup(_sched.stop)

    def _use_client(self, uid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
        main.app.dependency_overrides[get_current_user] = lambda: _client_user(uid)

    def _use_admin(self):
        main.app.dependency_overrides[get_current_user] = _admin_user

    def tearDown(self):
        main.app.dependency_overrides[get_current_user] = _admin_user

    # -- Zero-limit: new user with no plan set → blocked --------------------

    def test_blocked_zero_limit_zero_used(self):
        """New user: limit=0, used=0 → 402. Admin must set a plan first."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(0, 0))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["detail"]["code"], "QUOTA_EXCEEDED")

    # -- Blocked scenarios ---------------------------------------------------

    def test_blocked_when_used_equals_limit(self):
        """used == limit → 402: the previous doc was the soft-limit allowance."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(500, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)
        body = r.json()
        self.assertEqual(body["detail"]["code"], "QUOTA_EXCEEDED")
        self.assertEqual(body["detail"]["total_extracted_pages"], 500)
        self.assertEqual(body["detail"]["subscription_limit"], 500)
        self.assertEqual(body["detail"]["overage"], 0)  # exactly at limit

    def test_402_detail_has_negative_overage_when_over(self):
        """used > limit → overage is negative in the 402 detail."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(1003, 1000))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)
        body = r.json()
        self.assertEqual(body["detail"]["total_extracted_pages"], 1003)
        self.assertEqual(body["detail"]["subscription_limit"], 1000)
        self.assertEqual(body["detail"]["overage"], -3)

    def test_blocked_when_already_over_limit(self):
        """used > limit (previous doc caused overage) → 402."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(503, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)

    def test_error_message_includes_upgrade_hint(self):
        """402 detail message must tell the user to contact admin."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(500, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        msg = r.json()["detail"]["message"].lower()
        self.assertIn("administrator", msg)

    # -- Soft-boundary: the doc that causes the overage is allowed -----------

    def test_allowed_one_page_below_limit(self):
        """used=499, limit=500 → allowed. Next request will be blocked."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(499, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)

    def test_allowed_well_under_limit(self):
        """used=200, limit=5000 → no quota block."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(200, 5000))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)

    def test_admin_raises_limit_unblocks_client(self):
        """Client at 503/500 blocked. Admin raises to 1000. Client at 503/1000 → allowed."""
        self._use_client()
        # First: blocked at 503/500
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(503, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402)

        # After admin raises limit: allowed at 503/1000
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(503, 1000))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)

    # -- Warning threshold (90 %) -------------------------------------------

    def test_warning_included_in_response_at_threshold(self):
        """used/limit >= 90 % → request allowed, usage_warning present."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(450, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)
        body = r.json()
        self.assertIn("usage_warning", body)
        self.assertEqual(body["usage_warning"]["level"], "warning")

    def test_no_warning_well_below_threshold(self):
        """used/limit < 90 % → no usage_warning in response."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(250, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r.status_code, 402)
        body = r.json()
        self.assertIsNone(body.get("usage_warning"))

    # -- Admin bypass --------------------------------------------------------

    def test_admin_bypasses_quota_check(self):
        """Admin users are never subject to quota checks."""
        self._use_admin()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(99999, 100))) as mock_check, \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        mock_check.assert_not_called()
        self.assertNotEqual(r.status_code, 402)

    # -- Quota check failure is now fail-closed (503) -------------------------

    def test_quota_check_db_failure_blocks_upload_with_503(self):
        """If get_user_billable_pages raises, the upload is blocked with 503 (fail-closed)."""
        self._use_client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(side_effect=RuntimeError("db down"))), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertEqual(r.status_code, 503)


# ---------------------------------------------------------------------------
# 3. GET /me/usage
# ---------------------------------------------------------------------------

class MeUsageEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def tearDown(self):
        main.app.dependency_overrides[get_current_user] = _admin_user

    def test_client_can_see_own_usage(self):
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()
        usage = _usage(450, 500)
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=usage)):
            r = self.client.get("/me/usage")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["billable_pages"], 450)
        self.assertEqual(body["subscription_limit"], 500)
        self.assertEqual(body["remaining"], 50)
        self.assertIn("user_id", body)

    def test_admin_can_also_call_me_usage(self):
        main.app.dependency_overrides[get_current_user] = _admin_user
        usage = _usage(0, 0)
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=usage)):
            r = self.client.get("/me/usage")
        self.assertEqual(r.status_code, 200)

    def test_response_includes_email(self):
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(100, 500))):
            r = self.client.get("/me/usage")
        self.assertEqual(r.status_code, 200)
        self.assertIn("email", r.json())

    def test_remaining_negative_when_over_limit(self):
        """When used exceeds limit, remaining is negative (shows actual overage)."""
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(550, 500))):
            r = self.client.get("/me/usage")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["remaining"], -50)

    def test_zero_limit_shows_zero_remaining(self):
        """New user with limit=0 sees 0 remaining."""
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(0, 0))):
            r = self.client.get("/me/usage")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["subscription_limit"], 0)
        self.assertEqual(body["remaining"], 0)


# ---------------------------------------------------------------------------
# 4. Admin subscription-limit API
# ---------------------------------------------------------------------------

class AdminSubscriptionLimitAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self):
        main.limiter.reset()
        main.app.dependency_overrides[get_current_user] = _admin_user

    def tearDown(self):
        main.app.dependency_overrides[get_current_user] = _admin_user

    def test_admin_can_set_limit(self):
        uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        with patch.object(main.db_mod, "update_user_subscription_limit",
                          new=AsyncMock(return_value=True)):
            r = self.client.patch(
                f"/admin/users/{uid}/subscription-limit",
                json={"subscription_limit": 5000},
            )
        self.assertEqual(r.status_code, 200)

    def test_admin_can_set_limit_to_zero(self):
        uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        with patch.object(main.db_mod, "update_user_subscription_limit",
                          new=AsyncMock(return_value=True)):
            r = self.client.patch(
                f"/admin/users/{uid}/subscription-limit",
                json={"subscription_limit": 0},
            )
        self.assertEqual(r.status_code, 200)


# ---------------------------------------------------------------------------
# 5. Multi-client isolation
# ---------------------------------------------------------------------------

class MultiClientIsolationTests(unittest.TestCase):
    """Two clients with different limits are enforced independently."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self):
        main.limiter.reset()
        # See note in SubscriptionQuotaEnforcementTests.setUp — stub the
        # scheduler guard so the quota path is what gets exercised.
        _sched = patch.object(
            main.db_mod, "get_user_is_executing", new=AsyncMock(return_value=False)
        )
        _sched.start()
        self.addCleanup(_sched.stop)

    def tearDown(self):
        main.app.dependency_overrides[get_current_user] = _admin_user

    def test_client_a_blocked_client_b_allowed(self):
        """Client A (limit=100, used=100) blocked. Client B (limit=5000, used=200) allowed."""
        uid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        uid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        # Client A: blocked
        main.app.dependency_overrides[get_current_user] = lambda: _client_user(uid_a)
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(100, 100))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "a@test.com"})):
            r_a = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r_a.status_code, 402)

        # Client B: allowed
        main.app.dependency_overrides[get_current_user] = lambda: _client_user(uid_b)
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(200, 5000))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "b@test.com"})), \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value=_fake_submission())), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r_b = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        self.assertNotEqual(r_b.status_code, 402)


# ---------------------------------------------------------------------------
# 6. Resume endpoint quota enforcement
# ---------------------------------------------------------------------------

class ResumeQuotaEnforcementTests(unittest.TestCase):
    """POST /jobs/extractions/{id}/resume must honour the same quota as /ingest/ui."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self):
        main.limiter.reset()

    def tearDown(self):
        main.app.dependency_overrides[get_current_user] = _admin_user

    def _use_client(self, uid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
        main.app.dependency_overrides[get_current_user] = lambda: _client_user(uid)

    def _partial_extraction(self):
        return {
            "id": 1, "status": "partial", "vendor_id": "acme",
            "filename": "doc.pdf", "document_id": 1,
        }

    def test_resume_blocked_when_already_over_limit(self):
        """Client already at negative balance cannot resume a partial extraction."""
        self._use_client()
        with patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=self._partial_extraction())), \
             patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(1003, 1000))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post("/jobs/extractions/1/resume")
        self.assertEqual(r.status_code, 402)
        body = r.json()
        self.assertEqual(body["detail"]["code"], "QUOTA_EXCEEDED")
        self.assertEqual(body["detail"]["overage"], -3)

    def test_resume_blocked_at_exact_limit(self):
        """Client exactly at limit (overage=0) cannot resume — quota exhausted."""
        self._use_client()
        with patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=self._partial_extraction())), \
             patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(500, 500))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "c@test.com"})):
            r = self.client.post("/jobs/extractions/1/resume")
        self.assertEqual(r.status_code, 402)

    def test_admin_can_resume_regardless_of_quota(self):
        """Admin bypasses quota on resume."""
        main.app.dependency_overrides[get_current_user] = _admin_user
        with patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=self._partial_extraction())), \
             patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(99999, 100))) as mock_check, \
             patch.object(main.db_mod, "list_jobs_for_extraction",
                          new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "get_pages",
                          new=AsyncMock(return_value=[{"page_number": 1}])), \
             patch.object(main.db_mod, "set_cancel_requested", new=AsyncMock()), \
             patch.object(main.db_mod, "enqueue_job",
                          new=AsyncMock(return_value={"id": 99, "status": "queued"})), \
             patch.object(main.db_mod, "set_extraction_status", new=AsyncMock()):
            r = self.client.post("/jobs/extractions/1/resume")
        mock_check.assert_not_called()
        self.assertNotEqual(r.status_code, 402)


if __name__ == "__main__":
    unittest.main()
