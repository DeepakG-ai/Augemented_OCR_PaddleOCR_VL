"""
Tests for the new subscription/top-up model and quota integration.

Covers:
  - db.create_subscription / cancel_subscription / add_topup input validation
  - db.expire_due_subscriptions return parsing
  - db.get_user_quota_v2 / get_user_history short-circuit on bad UUID
  - reserve_quota: no active subscription → blocked with reason='no_subscription'
  - Admin endpoints:
      POST   /admin/users/{id}/subscriptions
      POST   /admin/users/{id}/topups
      PATCH  /admin/users/{id}/subscriptions/{sub_id}/cancel
      GET    /admin/users/{id}/history
      GET    /admin/users/{id}/subscription
    + permission checks
"""
from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod
import backend.main as main
from backend.auth import get_current_user, require_admin


_GOOD_UUID = "11111111-1111-1111-1111-111111111111"
_ADMIN_UUID = "00000000-0000-0000-0000-000000000000"


@asynccontextmanager
async def _fake_acquire(conn):
    yield conn


@asynccontextmanager
async def _fake_txn():
    yield


def _make_pool_with_conn(conn=None):
    pool = MagicMock()
    conn = conn or AsyncMock()
    conn.transaction = MagicMock(side_effect=lambda: _fake_txn())
    pool.acquire.return_value = _fake_acquire(conn)
    return pool, conn


# ─────────────────────────────────────────────────────────────────────────
# DB layer: input validation + short-circuits
# ─────────────────────────────────────────────────────────────────────────


class CreateSubscriptionInputValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_negative_page_limit_raises_value_error(self) -> None:
        pool, _ = _make_pool_with_conn()
        with self.assertRaises(ValueError):
            await db_mod.create_subscription(
                pool, _GOOD_UUID,
                page_limit=-1,
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc) + timedelta(days=30),
            )

    async def test_period_end_not_after_start_raises(self) -> None:
        pool, _ = _make_pool_with_conn()
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            await db_mod.create_subscription(
                pool, _GOOD_UUID,
                page_limit=100,
                period_start=now,
                period_end=now,  # not strictly after
            )

    async def test_malformed_user_id_returns_none(self) -> None:
        pool, _ = _make_pool_with_conn()
        now = datetime.now(timezone.utc)
        result = await db_mod.create_subscription(
            pool, "not-a-uuid",
            page_limit=100,
            period_start=now,
            period_end=now + timedelta(days=30),
        )
        self.assertIsNone(result)

    async def test_supersedes_prior_active_in_same_txn(self) -> None:
        """create_subscription must UPDATE prior active rows to 'superseded'
        before inserting the new one (single transaction)."""
        pool, conn = _make_pool_with_conn()
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=365)
        conn.fetchrow.return_value = {
            "id": 42, "user_id": _GOOD_UUID, "page_limit": 1000,
            "period_start": now, "period_end": end, "status": "active",
            "note": None, "created_by": None, "created_at": now,
        }
        await db_mod.create_subscription(
            pool, _GOOD_UUID,
            page_limit=1000, period_start=now, period_end=end,
        )
        executes = [c.args[0] for c in conn.execute.call_args_list]
        # First execute should be the supersede UPDATE
        self.assertTrue(any("status = 'superseded'" in q for q in executes),
                        f"expected supersede UPDATE, got: {executes}")
        # And the users.subscription_limit sync UPDATE
        self.assertTrue(any("UPDATE users SET subscription_limit" in q for q in executes),
                        f"expected legacy sync UPDATE, got: {executes}")


class AddTopupInputValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_pages_raises(self) -> None:
        pool, _ = _make_pool_with_conn()
        with self.assertRaises(ValueError):
            await db_mod.add_topup(pool, _GOOD_UUID, pages=0)

    async def test_negative_pages_raises(self) -> None:
        pool, _ = _make_pool_with_conn()
        with self.assertRaises(ValueError):
            await db_mod.add_topup(pool, _GOOD_UUID, pages=-5)

    async def test_malformed_user_id_returns_none(self) -> None:
        pool, _ = _make_pool_with_conn()
        self.assertIsNone(await db_mod.add_topup(pool, "bad", pages=10))

    async def test_no_active_subscription_returns_none(self) -> None:
        pool, conn = _make_pool_with_conn()
        conn.fetchrow.return_value = None  # no active sub
        result = await db_mod.add_topup(pool, _GOOD_UUID, pages=50)
        self.assertIsNone(result)

    async def test_attaches_to_active_subscription(self) -> None:
        pool, conn = _make_pool_with_conn()
        # First fetchrow returns the active sub, second returns the new topup row
        conn.fetchrow.side_effect = [
            {"id": 7},  # active subscription lookup
            {  # topup INSERT RETURNING
                "id": 99, "user_id": _GOOD_UUID, "subscription_id": 7,
                "pages": 50, "note": None, "created_by": None,
                "created_at": datetime.now(timezone.utc),
            },
        ]
        topup = await db_mod.add_topup(pool, _GOOD_UUID, pages=50)
        self.assertIsNotNone(topup)
        self.assertEqual(topup["subscription_id"], 7)
        self.assertEqual(topup["pages"], 50)


class GetUserQuotaV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_user_id_returns_blank_quota(self) -> None:
        pool, _ = _make_pool_with_conn()
        q = await db_mod.get_user_quota_v2(pool, "not-a-uuid")
        self.assertEqual(q["effective_limit"], 0)
        self.assertEqual(q["used"], 0)
        self.assertFalse(q["has_active_subscription"])
        self.assertEqual(q["status"], "none")

    async def test_no_active_subscription_keeps_pending_count(self) -> None:
        pool, conn = _make_pool_with_conn()
        # subs flip UPDATE → returns nothing meaningful via execute
        # active-sub fetchrow → None, pending fetchrow → {pending: 3}
        conn.fetchrow.side_effect = [None, {"pending": 3}]
        q = await db_mod.get_user_quota_v2(pool, _GOOD_UUID)
        self.assertFalse(q["has_active_subscription"])
        self.assertEqual(q["pending"], 3)
        self.assertEqual(q["effective_limit"], 0)

    async def test_active_subscription_combines_base_and_topups(self) -> None:
        pool, conn = _make_pool_with_conn()
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=180)
        # Order matches the function: active-sub fetchrow first, then pending
        conn.fetchrow.side_effect = [
            {"id": 1, "page_limit": 1000, "period_start": now, "period_end": end},
            {"pending": 0},
        ]
        # fetchval is called twice: SUM(topups), then COUNT used pages
        conn.fetchval.side_effect = [200, 350]
        q = await db_mod.get_user_quota_v2(pool, _GOOD_UUID)
        self.assertTrue(q["has_active_subscription"])
        self.assertEqual(q["base_limit"], 1000)
        self.assertEqual(q["topup_total"], 200)
        self.assertEqual(q["effective_limit"], 1200)
        self.assertEqual(q["used"], 350)
        self.assertEqual(q["remaining"], 850)


class ExpireDueSubscriptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_postgres_update_count(self) -> None:
        pool, conn = _make_pool_with_conn()
        conn.execute.return_value = "UPDATE 4"
        count = await db_mod.expire_due_subscriptions(pool)
        self.assertEqual(count, 4)

    async def test_zero_updates(self) -> None:
        pool, conn = _make_pool_with_conn()
        conn.execute.return_value = "UPDATE 0"
        count = await db_mod.expire_due_subscriptions(pool)
        self.assertEqual(count, 0)


class GetUserHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_uuid_returns_empty_lists(self) -> None:
        pool, _ = _make_pool_with_conn()
        hist = await db_mod.get_user_history(pool, "bad")
        self.assertEqual(hist, {"subscriptions": [], "topups": []})

    async def test_query_joins_admin_email(self) -> None:
        pool, conn = _make_pool_with_conn()
        conn.fetch.return_value = []
        await db_mod.get_user_history(pool, _GOOD_UUID)
        queries = [c.args[0] for c in conn.fetch.call_args_list]
        # Subscriptions query should join users for created_by_email
        self.assertTrue(any("created_by_email" in q for q in queries),
                        f"expected admin email join, got queries: {queries}")


class ReserveQuotaSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_active_subscription_blocks_with_reason(self) -> None:
        pool, conn = _make_pool_with_conn()
        # users lookup returns pending=0, sub lookup returns None
        conn.fetchrow.side_effect = [{"pending": 0}, None]
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=5)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "no_subscription")

    async def test_uses_effective_limit_base_plus_topups(self) -> None:
        pool, conn = _make_pool_with_conn()
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=180)
        conn.fetchrow.side_effect = [
            {"pending": 0},
            {"id": 1, "page_limit": 100, "period_start": now, "period_end": end},
        ]
        # fetchval: first call → SUM(topups)=50, second → used pages=120
        conn.fetchval.side_effect = [50, 120]
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=1)
        # effective=150, used=120, incoming=1 → 121 <= 150 → allowed
        self.assertTrue(result["allowed"])
        self.assertEqual(result["limit"], 150)
        self.assertEqual(result["used"], 120)
        self.assertEqual(result["remaining"], 30)

    async def test_exceeded_when_over_effective_limit_and_above_grace(self) -> None:
        pool, conn = _make_pool_with_conn()
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=180)
        conn.fetchrow.side_effect = [
            {"pending": 0},
            {"id": 1, "page_limit": 100, "period_start": now, "period_end": end},
        ]
        conn.fetchval.side_effect = [0, 95]  # topups=0, used=95
        result = await db_mod.reserve_quota(pool, _GOOD_UUID, incoming_pages=50, grace_pages=10)
        # used=95, incoming=50 → 145 > 100, and 50 > grace → exceeded
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "exceeded")


# ─────────────────────────────────────────────────────────────────────────
# Admin REST endpoints
# ─────────────────────────────────────────────────────────────────────────


def _user_row(uid: str = _GOOD_UUID, email: str = "client@x.com",
              role: str = "client", is_active: bool = True) -> dict:
    return {
        "id": uid, "email": email, "role": role, "is_active": is_active,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "subscription_limit": 0,
    }


def _sub_row() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 1, "user_id": _GOOD_UUID, "page_limit": 1000,
        "period_start": now, "period_end": now + timedelta(days=365),
        "status": "active", "note": None, "created_by": _ADMIN_UUID,
        "created_at": now,
    }


def _client_user(uid: str = "client-uuid") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@x.com"}


class _ScopedOverrides:
    def __init__(self, app, overrides: dict):
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


class AdminCreateSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_creates_subscription_201(self) -> None:
        end = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=_user_row())), \
             patch.object(main.db_mod, "create_subscription",
                          new=AsyncMock(return_value=_sub_row())):
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/subscriptions",
                json={"page_limit": 1000, "period_end": end},
            )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["page_limit"], 1000)
        self.assertEqual(body["status"], "active")

    def test_nonexistent_user_returns_404(self) -> None:
        end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=None)):
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/subscriptions",
                json={"page_limit": 100, "period_end": end},
            )
        self.assertEqual(r.status_code, 404)

    def test_period_end_before_start_returns_400(self) -> None:
        start = datetime.now(timezone.utc).isoformat()
        end = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=_user_row())):
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/subscriptions",
                json={"page_limit": 100, "period_start": start, "period_end": end},
            )
        self.assertEqual(r.status_code, 400)

    def test_negative_page_limit_returns_422(self) -> None:
        end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        r = self.client.post(
            f"/admin/users/{_GOOD_UUID}/subscriptions",
            json={"page_limit": -100, "period_end": end},
        )
        self.assertEqual(r.status_code, 422)

    def test_client_cannot_create_subscription(self) -> None:
        end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/subscriptions",
                json={"page_limit": 100, "period_end": end},
            )
        self.assertEqual(r.status_code, 403)


class AdminAddTopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_adds_topup_201(self) -> None:
        topup_row = {
            "id": 5, "user_id": _GOOD_UUID, "subscription_id": 1,
            "pages": 200, "note": "Paid", "created_by": _ADMIN_UUID,
            "created_at": datetime.now(timezone.utc),
        }
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=_user_row())), \
             patch.object(main.db_mod, "add_topup",
                          new=AsyncMock(return_value=topup_row)):
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/topups",
                json={"pages": 200, "note": "Paid"},
            )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["pages"], 200)

    def test_no_active_subscription_returns_409(self) -> None:
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=_user_row())), \
             patch.object(main.db_mod, "add_topup",
                          new=AsyncMock(return_value=None)):
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/topups",
                json={"pages": 50},
            )
        self.assertEqual(r.status_code, 409)
        self.assertIn("no active subscription", r.json()["error"]["message"].lower())

    def test_missing_user_returns_404(self) -> None:
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=None)):
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/topups",
                json={"pages": 50},
            )
        self.assertEqual(r.status_code, 404)

    def test_zero_pages_returns_422(self) -> None:
        r = self.client.post(
            f"/admin/users/{_GOOD_UUID}/topups",
            json={"pages": 0},
        )
        self.assertEqual(r.status_code, 422)

    def test_client_cannot_add_topup(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.post(
                f"/admin/users/{_GOOD_UUID}/topups",
                json={"pages": 100},
            )
        self.assertEqual(r.status_code, 403)


class AdminCancelSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_cancels_active_subscription(self) -> None:
        with patch.object(main.db_mod, "cancel_subscription",
                          new=AsyncMock(return_value=True)):
            r = self.client.patch(
                f"/admin/users/{_GOOD_UUID}/subscriptions/1/cancel",
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "cancelled")

    def test_unknown_subscription_returns_404(self) -> None:
        with patch.object(main.db_mod, "cancel_subscription",
                          new=AsyncMock(return_value=False)):
            r = self.client.patch(
                f"/admin/users/{_GOOD_UUID}/subscriptions/999/cancel",
            )
        self.assertEqual(r.status_code, 404)


class AdminGetHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_returns_subscriptions_and_topups(self) -> None:
        now = datetime.now(timezone.utc)
        history = {
            "subscriptions": [
                {"id": 1, "page_limit": 1000, "period_start": now,
                 "period_end": now + timedelta(days=365), "status": "active",
                 "note": None, "created_at": now, "created_by_email": "admin@x",
                 "topup_total": 200, "pages_used": 350},
            ],
            "topups": [
                {"id": 5, "subscription_id": 1, "pages": 200, "note": "Paid",
                 "created_at": now, "created_by_email": "admin@x",
                 "sub_period_start": now,
                 "sub_period_end": now + timedelta(days=365),
                 "sub_status": "active"},
            ],
        }
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=_user_row())), \
             patch.object(main.db_mod, "get_user_history",
                          new=AsyncMock(return_value=history)):
            r = self.client.get(f"/admin/users/{_GOOD_UUID}/history")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["subscriptions"]), 1)
        self.assertEqual(len(body["topups"]), 1)
        self.assertEqual(body["topups"][0]["pages"], 200)

    def test_missing_user_returns_404(self) -> None:
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=None)):
            r = self.client.get(f"/admin/users/{_GOOD_UUID}/history")
        self.assertEqual(r.status_code, 404)

    def test_client_cannot_view_history(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.get(f"/admin/users/{_GOOD_UUID}/history")
        self.assertEqual(r.status_code, 403)


class AdminGetSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_returns_quota_v2_shape(self) -> None:
        quota = {
            "has_active_subscription": True,
            "subscription_id": 1,
            "period_start": datetime.now(timezone.utc),
            "period_end": datetime.now(timezone.utc) + timedelta(days=180),
            "base_limit": 1000,
            "topup_total": 200,
            "effective_limit": 1200,
            "used": 350,
            "pending": 0,
            "remaining": 850,
            "status": "active",
        }
        with patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value=_user_row())), \
             patch.object(main.db_mod, "get_user_quota_v2",
                          new=AsyncMock(return_value=quota)):
            r = self.client.get(f"/admin/users/{_GOOD_UUID}/subscription")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["effective_limit"], 1200)
        self.assertEqual(body["used"], 350)
        self.assertEqual(body["remaining"], 850)
        self.assertEqual(body["email"], "client@x.com")


class AdminListUsersEmbedsSubscriptionTests(unittest.TestCase):
    """GET /admin/users should embed per-user period summary so the UI table
    renders without N+1 round-trips."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_list_users_includes_period_fields(self) -> None:
        now = datetime.now(timezone.utc)
        row = _user_row("u-1", "a@x.com", "client")
        row.update({
            "pending_pages": 0,
            "sub_id": 1,
            "period_start": now,
            "period_end": now + timedelta(days=180),
            "base_limit": 1000,
            "topup_total": 200,
            "used": 350,
        })
        with patch.object(main.db_mod, "list_users",
                          new=AsyncMock(return_value=[row])), \
             patch.object(main.db_mod, "expire_due_subscriptions",
                          new=AsyncMock(return_value=0)) as mock_expire:
            r = self.client.get("/admin/users")
        self.assertEqual(r.status_code, 200)
        mock_expire.assert_awaited_once()
        body = r.json()
        self.assertEqual(len(body), 1)
        u = body[0]
        self.assertEqual(u["base_limit"], 1000)
        self.assertEqual(u["topup_total"], 200)
        self.assertEqual(u["effective_limit"], 1200)
        self.assertEqual(u["pages_used"], 350)
        self.assertEqual(u["pages_remaining"], 850)
        self.assertEqual(u["period_status"], "active")


if __name__ == "__main__":
    unittest.main()
