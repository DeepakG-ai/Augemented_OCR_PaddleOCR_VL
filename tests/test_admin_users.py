"""
Tests for admin user-management endpoints.

Covers:
  - GET  /admin/users  → returns list, client gets 403
  - POST /admin/users  → creates user, duplicate email → 409, short password → 422
  - DELETE /admin/users/{id} → deactivates, self-deactivation → 400, missing → 404
  - client cannot reach any admin/users route (403)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.main as main
from backend.auth import get_current_user, require_admin


def _admin(uid: str = "admin-uuid") -> dict:
    return {"id": uid, "role": "admin", "email": "admin@test"}


def _client_user(uid: str = "client-uuid") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test"}


def _user_row(uid: str = "u-1", email: str = "a@x.com",
              role: str = "client", is_active: bool = True) -> dict:
    return {
        "id": uid,
        "email": email,
        "role": role,
        "is_active": is_active,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


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


class AdminListUsersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_returns_list_of_users(self) -> None:
        rows = [_user_row("u-1", "a@x.com", "client"),
                _user_row("u-2", "b@x.com", "admin")]
        with patch.object(main.db_mod, "expire_due_subscriptions",
                          new=AsyncMock(return_value=0)) as mock_expire, \
             patch.object(main.db_mod, "list_users", new=AsyncMock(return_value=rows)):
            r = self.client.get("/admin/users")
        self.assertEqual(r.status_code, 200)
        mock_expire.assert_awaited_once()
        body = r.json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["email"], "a@x.com")
        self.assertEqual(body[1]["role"], "admin")

    def test_returns_empty_list_when_no_users(self) -> None:
        with patch.object(main.db_mod, "expire_due_subscriptions",
                          new=AsyncMock(return_value=0)) as mock_expire, \
             patch.object(main.db_mod, "list_users", new=AsyncMock(return_value=[])):
            r = self.client.get("/admin/users")
        self.assertEqual(r.status_code, 200)
        mock_expire.assert_awaited_once()
        self.assertEqual(r.json(), [])

    def test_client_cannot_list_users(self) -> None:
        client_overrides = {
            get_current_user: lambda: _client_user(),
            require_admin: main.require_admin,  # use real dep so it rejects the client
        }
        with _ScopedOverrides(main.app, client_overrides):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.get("/admin/users")
        self.assertEqual(r.status_code, 403)


class AdminCreateUserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_creates_user_and_returns_201(self) -> None:
        created = _user_row("new-uuid", "newuser@x.com", "client")
        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user",
                          new=AsyncMock(return_value=created)):
            r = self.client.post("/admin/users", json={
                "email": "newuser@x.com",
                "password": "strongpass1",
                "role": "client",
            })
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["email"], "newuser@x.com")
        self.assertEqual(body["role"], "client")
        self.assertTrue(body["is_active"])

    def test_creates_admin_role_user(self) -> None:
        created = _user_row("adm-uuid", "newadmin@x.com", "admin")
        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_user",
                          new=AsyncMock(return_value=created)):
            r = self.client.post("/admin/users", json={
                "email": "newadmin@x.com",
                "password": "strongpass1",
                "role": "admin",
            })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["role"], "admin")

    def test_duplicate_email_returns_409(self) -> None:
        existing = _user_row("u-1", "taken@x.com")
        with patch.object(main.db_mod, "get_user_by_email",
                          new=AsyncMock(return_value=existing)):
            r = self.client.post("/admin/users", json={
                "email": "taken@x.com",
                "password": "strongpass1",
                "role": "client",
            })
        self.assertEqual(r.status_code, 409)
        self.assertIn("already registered", r.json()["error"]["message"].lower())

    def test_password_shorter_than_8_chars_returns_422(self) -> None:
        r = self.client.post("/admin/users", json={
            "email": "x@x.com",
            "password": "short",
            "role": "client",
        })
        self.assertEqual(r.status_code, 422)

    def test_invalid_role_returns_422(self) -> None:
        r = self.client.post("/admin/users", json={
            "email": "x@x.com",
            "password": "strongpass1",
            "role": "superuser",
        })
        self.assertEqual(r.status_code, 422)

    def test_missing_email_returns_422(self) -> None:
        r = self.client.post("/admin/users", json={
            "password": "strongpass1",
            "role": "client",
        })
        self.assertEqual(r.status_code, 422)

    def test_client_cannot_create_user(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.post("/admin/users", json={
                "email": "x@x.com",
                "password": "strongpass1",
                "role": "client",
            })
        self.assertEqual(r.status_code, 403)


class AdminDeactivateUserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_deactivates_existing_user(self) -> None:
        with patch.object(main.db_mod, "deactivate_user",
                          new=AsyncMock(return_value=True)):
            r = self.client.delete("/admin/users/other-uuid")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "deactivated")
        self.assertEqual(body["user_id"], "other-uuid")

    def test_self_deactivation_returns_400(self) -> None:
        # conftest sets admin id to "00000000-0000-0000-0000-000000000000"
        admin_id = "00000000-0000-0000-0000-000000000000"
        r = self.client.delete(f"/admin/users/{admin_id}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("yourself", r.json()["error"]["message"].lower())

    def test_nonexistent_user_returns_404(self) -> None:
        with patch.object(main.db_mod, "deactivate_user",
                          new=AsyncMock(return_value=False)):
            r = self.client.delete("/admin/users/ghost-uuid")
        self.assertEqual(r.status_code, 404)

    def test_client_cannot_deactivate_user(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.delete("/admin/users/some-uuid")
        self.assertEqual(r.status_code, 403)


class AdminResetPasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_resets_password_for_existing_user(self) -> None:
        with patch.object(main.db_mod, "reset_user_password",
                          new=AsyncMock(return_value=True)) as mock_reset:
            r = self.client.patch("/admin/users/other-uuid/password",
                                  json={"new_password": "NewPass123"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "password_reset")
        self.assertEqual(body["user_id"], "other-uuid")
        mock_reset.assert_awaited_once()
        # Verify the stored password is a bcrypt hash, not plaintext
        stored_hash = mock_reset.call_args[0][2]
        self.assertTrue(stored_hash.startswith("$2b$"))

    def test_nonexistent_user_returns_404(self) -> None:
        with patch.object(main.db_mod, "reset_user_password",
                          new=AsyncMock(return_value=False)):
            r = self.client.patch("/admin/users/ghost-uuid/password",
                                  json={"new_password": "NewPass123"})
        self.assertEqual(r.status_code, 404)

    def test_self_reset_returns_400(self) -> None:
        admin_id = "00000000-0000-0000-0000-000000000000"
        r = self.client.patch(f"/admin/users/{admin_id}/password",
                              json={"new_password": "NewPass123"})
        self.assertEqual(r.status_code, 400)

    def test_password_shorter_than_8_returns_422(self) -> None:
        r = self.client.patch("/admin/users/other-uuid/password",
                              json={"new_password": "short"})
        self.assertEqual(r.status_code, 422)

    def test_client_cannot_reset_password(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.patch("/admin/users/some-uuid/password",
                                  json={"new_password": "NewPass123"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()


class AdminUserUUIDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_invalid_uuid_returns_404(self) -> None:
        r = self.client.delete("/admin/users/not-a-uuid")
        self.assertEqual(r.status_code, 404)

    def test_invalid_uuid_for_password_reset_returns_404(self) -> None:
        r = self.client.patch("/admin/users/not-a-uuid/password", json={"new_password": "NewPass123"})
        self.assertEqual(r.status_code, 404)
