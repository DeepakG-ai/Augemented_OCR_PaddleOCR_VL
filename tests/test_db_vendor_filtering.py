"""
Tests for vendor isolation in DB queries.

Correct security behavior:
- Clients only see their own vendors (user_id = their id)
- Admin (user_id=None) sees all vendors
- Vendors with user_id IS NULL are NOT leaked to clients
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


class DBVendorFilteringTests(unittest.IsolatedAsyncioTestCase):

    async def test_list_vendors_client_scoped_to_own_vendors(self) -> None:
        """Client user_id filters to only their owned vendors — no NULL leakage."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.list_vendors(pool, user_id="user-123")

        query = conn.fetch.call_args[0][0]
        self.assertIn("user_id = $1", query, "Client-scoped filter must reference $1")
        self.assertNotIn("user_id IS NULL", query, "Null-owner vendors must not leak to clients")

    async def test_list_vendors_admin_sees_all(self) -> None:
        """Admin (user_id=None) passes NULL, so the IS NULL guard makes all rows match."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.list_vendors(pool, user_id=None)

        query = conn.fetch.call_args[0][0]
        # Admin path uses the same query; NULL param makes $1::UUID IS NULL → TRUE
        self.assertIn("$1::UUID IS NULL", query, "Admin bypass must use $1::UUID IS NULL")

    async def test_get_all_aliases_client_scoped_to_own_vendors(self) -> None:
        """Alias detection for a client only loads aliases from their own vendors."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_all_aliases_for_detection(pool, user_id="user-123")

        query = conn.fetch.call_args[0][0]
        self.assertIn("v.user_id = $1", query, "Client alias filter must reference v.user_id = $1")
        self.assertNotIn("v.user_id IS NULL", query, "Null-owner vendor aliases must not leak to clients")

    async def test_get_all_aliases_admin_sees_all(self) -> None:
        """Admin alias detection uses NULL param so all vendors are included."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetch.return_value = []

        await db_mod.get_all_aliases_for_detection(pool, user_id=None)

        query = conn.fetch.call_args[0][0]
        self.assertIn("$1::UUID IS NULL", query, "Admin bypass must use $1::UUID IS NULL")

    async def test_get_vendor_returns_user_id_for_owner_aware_paths(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetchrow.return_value = {
            "id": "ACME",
            "name": "Acme",
            "status": "idle",
            "user_id": None,
            "created_at": None,
        }

        await db_mod.get_vendor(pool, "ACME")

        query = conn.fetchrow.call_args[0][0]
        self.assertIn("user_id", query)

    async def test_upsert_vendor_can_assign_legacy_owner_on_conflict(self) -> None:
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value = fake_acquire(conn)
        conn.fetchrow.return_value = {
            "id": "ACME",
            "name": "Acme",
            "status": "idle",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "created_at": None,
        }

        await db_mod.upsert_vendor(
            pool,
            "ACME",
            "Acme",
            user_id="00000000-0000-0000-0000-000000000001",
        )

        query = conn.fetchrow.call_args[0][0]
        self.assertIn("user_id = COALESCE(EXCLUDED.user_id, vendors.user_id)", query)

    async def test_get_user_by_id_malformed_uuid_returns_none_without_sql(self) -> None:
        pool = MagicMock()

        row = await db_mod.get_user_by_id(pool, "not-a-uuid")

        self.assertIsNone(row)
        pool.acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
