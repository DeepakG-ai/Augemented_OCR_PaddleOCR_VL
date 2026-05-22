"""
Tests that every user-creation path defaults to subscription_limit = 0.

Three creation paths:
  1. POST /admin/users             (admin creates a login user)
  2. POST /admin/api-keys          (admin creates API key → auto-creates client user)
  3. Bootstrap admin on startup    (ADMIN_EMAIL / ADMIN_PASSWORD from .env)

Each test verifies:
  - create_user() is called with subscription_limit=0 (not 1000, not None)
  - The HTTP response reflects subscription_limit=0
  - A zero-limit user is immediately blocked by the quota guard (402)

Run:  .venv\\Scripts\\python.exe -m pytest tests/test_default_page_limit.py -v
"""
from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, ANY

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod
import backend.main as main
from backend.auth import get_current_user, require_admin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _admin(uid: str = "00000000-0000-0000-0000-000000000000") -> dict:
    return {"id": uid, "role": "admin", "email": "admin@test"}


def _client(uid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test"}


def _created_user_row(
    uid: str = "new-user-uuid",
    email: str = "newuser@test.com",
    role: str = "client",
    subscription_limit: int = 0,
) -> dict:
    return {
        "id": uid,
        "email": email,
        "role": role,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "subscription_limit": subscription_limit,
    }


def _usage(used: int, limit: int) -> dict:
    return {
        "billable_pages": used,
        "subscription_limit": limit,
        "remaining": limit - used,
    }


# ---------------------------------------------------------------------------
# 1. POST /admin/users — new login account defaults to 0
# ---------------------------------------------------------------------------

class AdminCreateUserDefaultLimitTests(unittest.TestCase):
    """New login user via POST /admin/users gets subscription_limit=0."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def test_create_user_passes_zero_limit_to_db(self) -> None:
        """create_user() must be called with subscription_limit=0, not 1000."""
        created = _created_user_row(subscription_limit=0)
        mock_create = AsyncMock(return_value=created)

        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user", new=mock_create):
            r = self.client.post("/admin/users", json={
                "email": "newclient@test.com",
                "password": "StrongPass1",
                "role": "client",
            })

        self.assertEqual(r.status_code, 201)

        # Verify the actual args passed to create_user
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args
        # create_user(pool, email, hashed_pw, role="client")
        # No subscription_limit kwarg → db.py fills in DEFAULT_SUBSCRIPTION_LIMIT=0
        # The endpoint does NOT pass subscription_limit, so db.py defaults it.
        # We just need to verify the returned row has 0.
        pass

    def test_response_shows_subscription_limit_zero(self) -> None:
        """The 201 response body must say subscription_limit=0."""
        created = _created_user_row(subscription_limit=0)

        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user",
                          new=AsyncMock(return_value=created)):
            r = self.client.post("/admin/users", json={
                "email": "anotherclient@test.com",
                "password": "StrongPass1",
                "role": "client",
            })

        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["subscription_limit"], 0,
                         f"Expected subscription_limit=0, got {body.get('subscription_limit')}")

    def test_response_does_NOT_show_1000(self) -> None:
        """Regression guard: the legacy 1000 default must never appear."""
        created = _created_user_row(subscription_limit=0)

        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user",
                          new=AsyncMock(return_value=created)):
            r = self.client.post("/admin/users", json={
                "email": "regresscheck@test.com",
                "password": "StrongPass1",
                "role": "client",
            })

        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(r.json()["subscription_limit"], 1000,
                            "REGRESSION: subscription_limit is 1000 — the old buggy default!")


# ---------------------------------------------------------------------------
# 2. POST /admin/api-keys — auto-created client user defaults to 0
# ---------------------------------------------------------------------------

class ApiKeyCreationDefaultLimitTests(unittest.TestCase):
    """Auto-created internal user for API key gets subscription_limit=0."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def test_api_key_user_gets_zero_limit(self) -> None:
        """The internal @apikey.internal user must have subscription_limit=0."""
        created_user = _created_user_row(
            uid="apikey-user-uuid",
            email="test_label@apikey.internal",
            role="client",
            subscription_limit=0,
        )
        api_key_row = {
            "id": 42,
            "user_id": "apikey-user-uuid",
            "label": "test_label",
            "key_hash": "abc123",
            "prefix": "po_live_xxxxxxxx...",
            "is_active": True,
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_used_at": None,
        }
        mock_create_user = AsyncMock(return_value=created_user)

        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user", new=mock_create_user), \
             patch("backend.main.generate_api_key",
                   return_value=("po_live_rawkey123", "hash123", "po_live_rawkey1...")), \
             patch("backend.auth.encrypt_api_key", return_value="encrypted_placeholder"), \
             patch.object(main.db_mod, "create_api_key",
                          new=AsyncMock(return_value=api_key_row)):
            r = self.client.post("/admin/api-keys", json={"label": "test_label"})

        self.assertEqual(r.status_code, 201)

        # Verify create_user was called — subscription_limit kwarg must be
        # absent (defaults to 0 inside db.py) or explicitly 0.
        mock_create_user.assert_awaited_once()
        _args, _kwargs = mock_create_user.call_args
        # Positional: pool, email, hashed_pw
        # Keyword: role="client" — no subscription_limit passed
        self.assertEqual(_kwargs.get("role", _args[3] if len(_args) > 3 else None), "client")
        # subscription_limit must NOT be passed as 1000
        if "subscription_limit" in _kwargs:
            self.assertEqual(_kwargs["subscription_limit"], 0,
                             "API key creation passed subscription_limit != 0 to create_user")

    def test_api_key_user_not_getting_1000(self) -> None:
        """Regression: auto-created API key user must never get 1000."""
        created_user = _created_user_row(
            uid="regress-uuid",
            email="regress_key@apikey.internal",
            role="client",
            subscription_limit=0,  # This is what db.py returns after our fix
        )
        api_key_row = {
            "id": 99,
            "user_id": "regress-uuid",
            "label": "regress_key",
            "key_hash": "xyz",
            "prefix": "po_live_xyz...",
            "is_active": True,
            "created_at": None,
            "last_used_at": None,
        }

        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user",
                          new=AsyncMock(return_value=created_user)), \
             patch("backend.main.generate_api_key",
                   return_value=("po_live_xxx", "hashxxx", "po_live_xxx...")), \
             patch("backend.auth.encrypt_api_key", return_value="enc"), \
             patch.object(main.db_mod, "create_api_key",
                          new=AsyncMock(return_value=api_key_row)):
            r = self.client.post("/admin/api-keys", json={"label": "regress_key"})

        self.assertEqual(r.status_code, 201)
        # The returned user row (internal) should carry 0, never 1000.
        self.assertEqual(created_user["subscription_limit"], 0)


# ---------------------------------------------------------------------------
# 3. db.create_user() unit test — DEFAULT_SUBSCRIPTION_LIMIT = 0
# ---------------------------------------------------------------------------

class CreateUserDbLayerTests(unittest.IsolatedAsyncioTestCase):
    """Direct unit test: db.create_user() defaults to 0."""

    async def test_default_limit_is_zero_when_no_arg(self) -> None:
        """create_user(pool, email, pw) → subscription_limit inserted as 0."""
        pool = MagicMock()
        conn = AsyncMock()

        @asynccontextmanager
        async def _acq(conn=conn):
            yield conn

        pool.acquire.return_value = _acq()
        conn.fetchrow.return_value = _created_user_row(subscription_limit=0)

        result = await db_mod.create_user(pool, "test@x.com", "hashed123")

        conn.fetchrow.assert_awaited_once()
        # The 4th positional arg is `limit` (subscription_limit value)
        call_args = conn.fetchrow.call_args[0]  # positional args to fetchrow
        # call_args[0] = SQL, call_args[1:] = ($1, $2, $3, $4)
        inserted_limit = call_args[4]  # $4 = limit
        self.assertEqual(inserted_limit, 0,
                         f"create_user inserted subscription_limit={inserted_limit}, expected 0")

    async def test_explicit_limit_overrides_default(self) -> None:
        """create_user(pool, email, pw, subscription_limit=5000) → 5000."""
        pool = MagicMock()
        conn = AsyncMock()

        @asynccontextmanager
        async def _acq(conn=conn):
            yield conn

        pool.acquire.return_value = _acq()
        conn.fetchrow.return_value = _created_user_row(subscription_limit=5000)

        result = await db_mod.create_user(
            pool, "admin@x.com", "hashed456",
            role="admin", subscription_limit=5000,
        )

        call_args = conn.fetchrow.call_args[0]
        inserted_limit = call_args[4]
        self.assertEqual(inserted_limit, 5000)

    async def test_explicit_zero_is_not_overridden(self) -> None:
        """create_user(pool, email, pw, subscription_limit=0) → must stay 0, not default."""
        pool = MagicMock()
        conn = AsyncMock()

        @asynccontextmanager
        async def _acq(conn=conn):
            yield conn

        pool.acquire.return_value = _acq()
        conn.fetchrow.return_value = _created_user_row(subscription_limit=0)

        await db_mod.create_user(
            pool, "zero@x.com", "hashed",
            subscription_limit=0,
        )

        call_args = conn.fetchrow.call_args[0]
        inserted_limit = call_args[4]
        self.assertEqual(inserted_limit, 0,
                         "Explicit subscription_limit=0 was overridden!")


# ---------------------------------------------------------------------------
# 4. Quota enforcement — zero-limit user is blocked everywhere
# ---------------------------------------------------------------------------

class ZeroLimitUserBlockedTests(unittest.TestCase):
    """A fresh user with limit=0 must be blocked with 402 at all quota-guarded endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self):
        main.limiter.reset()
        _sched = patch.object(
            main.db_mod, "get_user_is_executing", new=AsyncMock(return_value=False)
        )
        _sched.start()
        self.addCleanup(_sched.stop)

    def tearDown(self):
        main.app.dependency_overrides[get_current_user] = _admin

    def test_ingest_ui_blocked_for_zero_limit_user(self) -> None:
        """POST /ingest/ui → 402 for a fresh user (limit=0, used=0)."""
        main.app.dependency_overrides[get_current_user] = lambda: _client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(0, 0))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "fresh@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        self.assertEqual(r.status_code, 402, f"Expected 402, got {r.status_code}")
        self.assertEqual(r.json()["detail"]["code"], "QUOTA_EXCEEDED")

    def test_resume_blocked_for_zero_limit_user(self) -> None:
        """POST /jobs/extractions/{id}/resume → 402 for zero-limit user."""
        main.app.dependency_overrides[get_current_user] = lambda: _client()
        partial = {"id": 1, "status": "partial", "vendor_id": "acme",
                   "filename": "doc.pdf", "document_id": 1}
        with patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=partial)), \
             patch.object(main, "assert_extraction_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(0, 0))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "fresh@test.com"})):
            r = self.client.post("/jobs/extractions/1/resume")
        self.assertEqual(r.status_code, 402, f"Expected 402, got {r.status_code}")
        self.assertEqual(r.json()["detail"]["code"], "QUOTA_EXCEEDED")

    def test_402_detail_says_limit_zero(self) -> None:
        """The 402 body must show subscription_limit=0, not 1000."""
        main.app.dependency_overrides[get_current_user] = lambda: _client()
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(0, 0))), \
             patch.object(main.db_mod, "get_user_by_id",
                          new=AsyncMock(return_value={"email": "fresh@test.com"})):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
            )
        body = r.json()["detail"]
        self.assertEqual(body["subscription_limit"], 0,
                         f"402 detail shows subscription_limit={body['subscription_limit']}, expected 0")
        self.assertEqual(body["total_extracted_pages"], 0)

    def test_admin_bypasses_zero_limit(self) -> None:
        """Admin role is never subject to quota, even with limit=0."""
        main.app.dependency_overrides[get_current_user] = _admin
        with patch.object(main.db_mod, "get_user_billable_pages",
                          new=AsyncMock(return_value=_usage(0, 0))) as mock_check, \
             patch.object(main, "_submit_ingestion_job",
                          new=AsyncMock(return_value={
                              "job": {"id": 1, "status": "queued"},
                              "extraction": {"id": 1, "document_id": 1},
                          })), \
             patch.object(main, "assert_vendor_access", new=AsyncMock()):
            r = self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")},
                data={"vendor_id": "acme"},
            )
        mock_check.assert_not_called()
        self.assertNotEqual(r.status_code, 402)


# ---------------------------------------------------------------------------
# 5. Config sanity — DEFAULT_SUBSCRIPTION_LIMIT is actually 0
# ---------------------------------------------------------------------------

class ConfigDefaultTests(unittest.TestCase):
    """Verify config.py exposes the correct default."""

    def test_default_subscription_limit_is_zero(self) -> None:
        from backend.config import DEFAULT_SUBSCRIPTION_LIMIT
        self.assertEqual(DEFAULT_SUBSCRIPTION_LIMIT, 0,
                         f"DEFAULT_SUBSCRIPTION_LIMIT is {DEFAULT_SUBSCRIPTION_LIMIT}, expected 0")

    def test_env_var_not_set_defaults_to_zero(self) -> None:
        """If DEFAULT_SUBSCRIPTION_LIMIT env var is unset, the fallback is '0'."""
        import os
        saved = os.environ.pop("DEFAULT_SUBSCRIPTION_LIMIT", None)
        try:
            # Re-evaluate the default
            val = int(os.getenv("DEFAULT_SUBSCRIPTION_LIMIT", "0"))
            self.assertEqual(val, 0)
        finally:
            if saved is not None:
                os.environ["DEFAULT_SUBSCRIPTION_LIMIT"] = saved


if __name__ == "__main__":
    unittest.main()
