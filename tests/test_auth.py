"""
Tests for backend/auth.py + auth-protected routes.

Covers:
  - password hashing roundtrip
  - JWT encode/decode roundtrip + tampering + expiry
  - get_current_user accepts Bearer header AND ?token= query param
  - require_admin rejects clients
  - assert_*_access helpers: admin bypass, client owns, client denied, missing
  - /auth/login: wrong password → 401, success → 200 with token
  - cross-client isolation: Client A cannot read Client B's extraction → 403
  - admin POST /vendors without user_id → 400
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Make sure tests can import the backend regardless of how pytest is invoked
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Conftest sets a placeholder SECRET_KEY before importing auth; mirror that here
# so this file can also be run standalone.
os.environ.setdefault("SECRET_KEY", "test-only-secret-do-not-use-in-prod")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

import backend.main as main
from backend import auth as auth_mod
from backend.auth import (
    assert_alias_access,
    assert_extraction_access,
    assert_job_access,
    assert_vendor_access,
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)


def _admin(uid: str = "admin-uuid") -> dict:
    return {"id": uid, "role": "admin", "email": "admin@test"}


def _client(uid: str) -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test"}


# ─────────────────────────────────────────────────────────────────────────
# Password + JWT primitives — no DB, no FastAPI
# ─────────────────────────────────────────────────────────────────────────

class PasswordHashingTests(unittest.TestCase):
    def test_hash_is_not_plaintext_and_verify_roundtrips(self) -> None:
        h = hash_password("hunter2")
        self.assertNotEqual(h, "hunter2")
        self.assertTrue(verify_password("hunter2", h))
        self.assertFalse(verify_password("wrong", h))

    def test_two_hashes_of_same_password_differ_due_to_salt(self) -> None:
        # bcrypt salts every hash; identical passwords must not produce
        # identical digests, otherwise an attacker could spot reuse.
        h1 = hash_password("hunter2")
        h2 = hash_password("hunter2")
        self.assertNotEqual(h1, h2)

    def test_verify_returns_false_on_garbage_hash(self) -> None:
        # passlib raises on malformed hashes; verify_password should swallow.
        self.assertFalse(verify_password("hunter2", "not-a-real-hash"))


class JwtTokenTests(unittest.TestCase):
    def test_create_and_decode_roundtrip(self) -> None:
        token = create_access_token("u-1", "client", "x@y.com")
        payload = decode_token(token)
        self.assertEqual(payload["sub"], "u-1")
        self.assertEqual(payload["role"], "client")
        self.assertEqual(payload["email"], "x@y.com")
        self.assertIn("exp", payload)
        self.assertIn("iat", payload)

    def test_tampered_signature_is_rejected(self) -> None:
        token = create_access_token("u-1", "client", "x@y.com")
        # Flip the last character of the signature segment
        head, payload, sig = token.split(".")
        bad = f"{head}.{payload}.{sig[:-1]}{'A' if sig[-1] != 'A' else 'B'}"
        with self.assertRaises(HTTPException) as ctx:
            decode_token(bad)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_expired_token_is_rejected(self) -> None:
        # Hand-craft an expired token bypassing create_access_token's TTL.
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = {
            "sub": "u-1",
            "role": "client",
            "email": "x@y.com",
            "iat": int((past - timedelta(hours=1)).timestamp()),
            "exp": int(past.timestamp()),
        }
        token = jwt.encode(payload, os.environ["SECRET_KEY"], algorithm="HS256")
        with self.assertRaises(HTTPException) as ctx:
            decode_token(token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_malformed_token_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            decode_token("not.a.jwt")
        self.assertEqual(ctx.exception.status_code, 401)


class CurrentUserAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_user_trusts_db_role_over_jwt_role(self) -> None:
        token = create_access_token("00000000-0000-0000-0000-000000000001", "admin", "old@example.com")

        class AppState:
            pool = object()

        class App:
            state = AppState()

        class Request:
            app = App()

        record = {
            "id": "00000000-0000-0000-0000-000000000001",
            "email": "client@example.com",
            "role": "client",
            "is_active": True,
            "created_at": None,
        }
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with patch.object(auth_mod.db_mod, "get_user_by_id", new=AsyncMock(return_value=record)):
            user = await get_current_user(Request(), credentials=credentials, token=None)

        self.assertEqual(user["role"], "client")
        self.assertEqual(user["email"], "client@example.com")


# ─────────────────────────────────────────────────────────────────────────
# Ownership assertion helpers
# ─────────────────────────────────────────────────────────────────────────

class AssertVendorAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_bypasses_without_db_call(self) -> None:
        with patch.object(auth_mod.db_mod, "get_vendor_owner", new=AsyncMock()) as m:
            await assert_vendor_access(object(), "ANY", _admin())
        m.assert_not_awaited()

    async def test_client_owns_vendor(self) -> None:
        with patch.object(auth_mod.db_mod, "get_vendor_owner", new=AsyncMock(return_value="A")):
            await assert_vendor_access(object(), "ACME", _client("A"))

    async def test_client_does_not_own_returns_403(self) -> None:
        with patch.object(auth_mod.db_mod, "get_vendor_owner", new=AsyncMock(return_value="B")):
            with self.assertRaises(HTTPException) as ctx:
                await assert_vendor_access(object(), "ACME", _client("A"))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_missing_vendor_returns_404(self) -> None:
        with patch.object(auth_mod.db_mod, "get_vendor_owner", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as ctx:
                await assert_vendor_access(object(), "GHOST", _client("A"))
        self.assertEqual(ctx.exception.status_code, 404)


class AssertExtractionAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_bypasses(self) -> None:
        with patch.object(auth_mod.db_mod, "get_extraction", new=AsyncMock()) as m:
            await assert_extraction_access(object(), 99, _admin())
        m.assert_not_awaited()

    async def test_chains_to_vendor_owner(self) -> None:
        with patch.object(auth_mod.db_mod, "get_extraction",
                          new=AsyncMock(return_value={"vendor_id": "ACME"})), \
             patch.object(auth_mod.db_mod, "get_vendor_owner",
                          new=AsyncMock(return_value="A")):
            await assert_extraction_access(object(), 99, _client("A"))

    async def test_other_clients_extraction_returns_403(self) -> None:
        with patch.object(auth_mod.db_mod, "get_extraction",
                          new=AsyncMock(return_value={"vendor_id": "INITECH"})), \
             patch.object(auth_mod.db_mod, "get_vendor_owner",
                          new=AsyncMock(return_value="B")):
            with self.assertRaises(HTTPException) as ctx:
                await assert_extraction_access(object(), 99, _client("A"))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_missing_extraction_returns_404(self) -> None:
        with patch.object(auth_mod.db_mod, "get_extraction", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as ctx:
                await assert_extraction_access(object(), 999, _client("A"))
        self.assertEqual(ctx.exception.status_code, 404)


class AssertJobAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_chains_through_job_to_vendor(self) -> None:
        with patch.object(auth_mod.db_mod, "get_job",
                          new=AsyncMock(return_value={"extraction_id": 7})), \
             patch.object(auth_mod.db_mod, "get_extraction",
                          new=AsyncMock(return_value={"vendor_id": "ACME"})), \
             patch.object(auth_mod.db_mod, "get_vendor_owner",
                          new=AsyncMock(return_value="A")):
            await assert_job_access(object(), 1, _client("A"))

    async def test_orphan_job_without_extraction_id_denies_clients(self) -> None:
        with patch.object(auth_mod.db_mod, "get_job",
                          new=AsyncMock(return_value={"extraction_id": None})):
            with self.assertRaises(HTTPException) as ctx:
                await assert_job_access(object(), 1, _client("A"))
        self.assertEqual(ctx.exception.status_code, 403)


class AssertAliasAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_alias_owned_via_vendor(self) -> None:
        with patch.object(auth_mod.db_mod, "get_alias_vendor_id",
                          new=AsyncMock(return_value="ACME")), \
             patch.object(auth_mod.db_mod, "get_vendor_owner",
                          new=AsyncMock(return_value="A")):
            await assert_alias_access(object(), 5, _client("A"))

    async def test_alias_for_other_clients_vendor_returns_403(self) -> None:
        with patch.object(auth_mod.db_mod, "get_alias_vendor_id",
                          new=AsyncMock(return_value="INITECH")), \
             patch.object(auth_mod.db_mod, "get_vendor_owner",
                          new=AsyncMock(return_value="B")):
            with self.assertRaises(HTTPException) as ctx:
                await assert_alias_access(object(), 5, _client("A"))
        self.assertEqual(ctx.exception.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────
# HTTP integration — login + per-route auth/isolation
# ─────────────────────────────────────────────────────────────────────────

class _ScopedOverrides:
    """Context manager that swaps app dependency_overrides and restores them."""
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


class AuthHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    # -- Login flow --------------------------------------------------------

    def test_login_unknown_email_returns_401(self) -> None:
        with patch.object(main.db_mod, "get_user_by_email", new=AsyncMock(return_value=None)):
            r = self.client.post("/auth/login", json={"email": "nobody@x.com", "password": "x"})
        self.assertEqual(r.status_code, 401)

    def test_login_inactive_user_returns_401(self) -> None:
        record = {"id": "u-1", "email": "a@x", "hashed_pw": hash_password("p"),
                  "role": "client", "is_active": False, "created_at": None}
        with patch.object(main.db_mod, "get_user_by_email", new=AsyncMock(return_value=record)):
            r = self.client.post("/auth/login", json={"email": "a@x", "password": "p"})
        self.assertEqual(r.status_code, 401)

    def test_login_wrong_password_returns_401(self) -> None:
        record = {"id": "u-1", "email": "a@x", "hashed_pw": hash_password("right"),
                  "role": "client", "is_active": True, "created_at": None}
        with patch.object(main.db_mod, "get_user_by_email", new=AsyncMock(return_value=record)):
            r = self.client.post("/auth/login", json={"email": "a@x", "password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_login_success_returns_token_and_user(self) -> None:
        record = {"id": "u-1", "email": "a@x", "hashed_pw": hash_password("p"),
                  "role": "client", "is_active": True, "created_at": None}
        with patch.object(main.db_mod, "get_user_by_email", new=AsyncMock(return_value=record)):
            r = self.client.post("/auth/login", json={"email": "a@x", "password": "p"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["token_type"], "bearer")
        self.assertTrue(body["access_token"])
        self.assertEqual(body["user"]["id"], "u-1")
        self.assertEqual(body["user"]["role"], "client")
        # Token must be signed with SECRET_KEY and decode to the same sub
        decoded = decode_token(body["access_token"])
        self.assertEqual(decoded["sub"], "u-1")

    # -- /auth/me ----------------------------------------------------------

    def test_me_without_token_returns_401(self) -> None:
        # Drop the conftest's blanket override so we exercise the real dep
        with _ScopedOverrides(main.app, {}):
            main.app.dependency_overrides.clear()
            r = self.client.get("/auth/me")
        self.assertEqual(r.status_code, 401)

    def test_me_with_valid_override_returns_user(self) -> None:
        record = {"id": "u-1", "email": "a@x", "role": "client",
                  "is_active": True, "created_at": None}
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client("u-1")}), \
             patch.object(main.db_mod, "get_user_by_id", new=AsyncMock(return_value=record)):
            r = self.client.get("/auth/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], "u-1")

    # -- Per-client isolation ---------------------------------------------

    def test_client_a_cannot_read_extraction_owned_by_client_b(self) -> None:
        # Client A is logged in but extraction 99 belongs to vendor INITECH
        # which is owned by user "B". Should be 403.
        ext = {"id": 99, "vendor_id": "INITECH"}
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client("A")}), \
             patch.object(main.db_mod, "get_extraction", new=AsyncMock(return_value=ext)), \
             patch.object(main.db_mod, "get_vendor_owner", new=AsyncMock(return_value="B")):
            r = self.client.get("/extractions/99")
        self.assertEqual(r.status_code, 403)

    def test_client_a_can_read_their_own_extraction(self) -> None:
        ext = {
            "id": 99, "document_id": None, "vendor_id": "ACME", "vendor_name": "ACME",
            "template_id": None, "filename": "x.pdf", "total_pages": 1,
            "format_type": "single_po_multipage", "header_fields": [],
            "line_item_fields": [], "result": {}, "page_results": [],
            "field_locations": {}, "ocr_data": None, "corrected_result": None,
            "correction_meta": None, "export_object_key": None,
            "progress": None, "cancel_requested": False, "status": "done",
            "error": None, "duration_ms": 1, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client("A")}), \
             patch.object(main.db_mod, "get_extraction", new=AsyncMock(return_value=ext)), \
             patch.object(main.db_mod, "get_vendor_owner", new=AsyncMock(return_value="A")):
            r = self.client.get("/extractions/99")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], 99)

    def test_client_vendor_list_is_filtered_by_user_id(self) -> None:
        captured: dict = {}

        async def fake_list_vendors(pool, user_id=None):
            captured["user_id"] = user_id
            return []

        with _ScopedOverrides(main.app, {get_current_user: lambda: _client("A")}), \
             patch.object(main.db_mod, "list_vendors", new=fake_list_vendors):
            r = self.client.get("/vendors")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["user_id"], "A")  # client filters to themselves

    def test_admin_vendor_list_is_unfiltered(self) -> None:
        captured: dict = {}

        async def fake_list_vendors(pool, user_id=None):
            captured["user_id"] = user_id
            return []

        # Conftest's blanket admin override is already in place.
        with patch.object(main.db_mod, "list_vendors", new=fake_list_vendors):
            r = self.client.get("/vendors")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(captured["user_id"])  # admin sees everything

    # -- Admin-vs-client write paths --------------------------------------

    def test_admin_post_vendor_without_user_id_returns_400(self) -> None:
        with patch.object(main.db_mod, "get_vendor", new=AsyncMock(return_value=None)):
            r = self.client.post("/vendors", json={"id": "NEWCO", "name": "Newco Ltd"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("user_id", r.json()["detail"].lower())

    def test_admin_post_vendor_with_user_id_succeeds(self) -> None:
        row = {"id": "NEWCO", "name": "Newco Ltd", "status": "idle",
               "user_id": "client-A", "created_at": "2026-01-01T00:00:00Z"}
        with patch.object(main.db_mod, "create_vendor_by_name", new=AsyncMock(return_value=row)), \
             patch.object(main.db_mod, "insert_vendor_alias", new=AsyncMock(return_value=None)):
            r = self.client.post(
                "/vendors",
                json={"name": "Newco Ltd", "user_id": "client-A"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], "NEWCO")

    def test_client_post_vendor_ignores_user_id_in_body(self) -> None:
        captured: dict = {}

        async def fake_create(pool, name, user_id=None):
            captured["user_id"] = user_id
            return {"id": "1", "name": name, "status": "idle",
                    "user_id": user_id, "created_at": "2026-01-01T00:00:00Z"}

        with _ScopedOverrides(main.app, {get_current_user: lambda: _client("A")}), \
             patch.object(main.db_mod, "create_vendor_by_name", new=fake_create), \
             patch.object(main.db_mod, "insert_vendor_alias", new=AsyncMock(return_value=None)):
            # Client tries to plant ownership on someone else
            r = self.client.post(
                "/vendors",
                json={"name": "Newco", "user_id": "B"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["user_id"], "A")  # forced to caller, not B

    # -- require_admin gate -----------------------------------------------

    def test_client_cannot_hit_admin_only_route(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client("A")}):
            # require_admin is NOT overridden by conftest's setting because
            # it lives behind get_current_user — clear conftest's override too.
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.get("/admin/users")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
