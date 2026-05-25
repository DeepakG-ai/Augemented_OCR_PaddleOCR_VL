"""Tests for spatial memory management: list, get, and delete endpoints."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sm_entry(
    sm_id: int = 1,
    vendor_id: str = "vendor-abc",
    layout_key: str = "vendor-abc:1",
    field_key: str = "invoice_number",
    page_number: int = 1,
    source_engine: str = "pypdfium",
    is_active: bool = True,
) -> dict:
    return {
        "id": sm_id,
        "vendor_id": vendor_id,
        "layout_key": layout_key,
        "field_key": field_key,
        "page_number": page_number,
        "normalized_box": {"x0": 0.1, "y0": 0.1, "x1": 0.4, "y1": 0.15},
        "source_engine": source_engine,
        "created_from_extraction_id": 42,
        "last_verified_at": "2026-05-01T10:00:00+00:00",
        "is_active": is_active,
    }


# ---------------------------------------------------------------------------
# DB function tests
# ---------------------------------------------------------------------------

class DbSpatialMemoryTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the new db.py functions (pool is fully mocked)."""

    def _make_pool(self, fetchrow=None, fetch=None, fetchval=None, execute=None):
        """Return a mock pool whose acquire() context manager yields a conn mock."""
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow)
        conn.fetch = AsyncMock(return_value=fetch or [])
        conn.fetchval = AsyncMock(return_value=fetchval)
        conn.execute = AsyncMock(return_value=execute or "DELETE 0")
        conn.transaction = MagicMock(return_value=_AsyncCtxMgr(None))
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))
        return pool, conn

    async def test_get_spatial_memory_by_id_returns_entry(self):
        import backend.db as db_mod
        entry = _sm_entry()
        pool, _ = self._make_pool(fetchrow=_FakeRecord(entry))
        result = await db_mod.get_spatial_memory_by_id(pool, sm_id=1)
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["field_key"], "invoice_number")

    async def test_get_spatial_memory_by_id_returns_none_when_missing(self):
        import backend.db as db_mod
        pool, _ = self._make_pool(fetchrow=None)
        result = await db_mod.get_spatial_memory_by_id(pool, sm_id=999)
        self.assertIsNone(result)

    async def test_delete_spatial_memory_by_id_returns_deleted_row(self):
        import backend.db as db_mod
        deleted = {"id": 1, "vendor_id": "vendor-abc", "layout_key": "vendor-abc:1",
                   "field_key": "invoice_number", "page_number": 1}
        pool, _ = self._make_pool(fetchrow=_FakeRecord(deleted))
        result = await db_mod.delete_spatial_memory_by_id(pool, sm_id=1)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 1)

    async def test_delete_spatial_memory_by_id_returns_none_when_not_found(self):
        import backend.db as db_mod
        pool, _ = self._make_pool(fetchrow=None)
        result = await db_mod.delete_spatial_memory_by_id(pool, sm_id=999)
        self.assertIsNone(result)

    async def test_delete_spatial_memory_by_id_can_remove_prompt_correction(self):
        import backend.db as db_mod
        deleted = {"id": 1, "vendor_id": "vendor-abc", "layout_key": "vendor-abc:1",
                   "field_key": "invoice_number", "page_number": 1}
        changed = [
            _FakeRecord({"id": 11, "correction_diff": {"vendor_name": {"original": "A", "corrected": "B"}}}),
            _FakeRecord({"id": 12, "correction_diff": {}}),
        ]
        pool, conn = self._make_pool(fetchrow=_FakeRecord(deleted), fetch=changed)
        result = await db_mod.delete_spatial_memory_by_id(
            pool, sm_id=1, delete_gold_correction=True,
        )
        self.assertEqual(result["gold_correction_fields_deleted"], 2)
        sql = " ".join(conn.fetch.call_args[0][0].split())
        self.assertIn("UPDATE gold_examples", sql)
        self.assertIn("correction_diff = correction_diff - $2::TEXT", sql)
        self.assertEqual(conn.fetch.call_args[0][1:], ("vendor-abc", "invoice_number"))
        conn.execute.assert_awaited_once_with(
            "DELETE FROM gold_examples WHERE id = ANY($1::INT[])",
            [12],
        )

    async def test_delete_gold_correction_field_removes_all_versions(self):
        import backend.db as db_mod
        changed = [
            _FakeRecord({"id": 21, "correction_diff": {"po_number": {"original": "1", "corrected": "2"}}}),
            _FakeRecord({"id": 22, "correction_diff": {}}),
            _FakeRecord({"id": 23, "correction_diff": {"invoice_date": {"original": "x", "corrected": "y"}}}),
        ]
        pool, conn = self._make_pool(fetch=changed)
        deleted = await db_mod.delete_gold_correction_field(
            pool, "vendor-abc", "vendor_name",
        )
        self.assertEqual(deleted, 3)
        sql = " ".join(conn.fetch.call_args[0][0].split())
        self.assertIn("correction_diff ? $2::TEXT", sql)
        conn.execute.assert_awaited_once_with(
            "DELETE FROM gold_examples WHERE id = ANY($1::INT[])",
            [22],
        )

    async def test_list_spatial_memory_for_vendor_returns_all_entries(self):
        import backend.db as db_mod
        rows = [_FakeRecord(_sm_entry(sm_id=i, field_key=f"field_{i}")) for i in range(1, 4)]
        pool, _ = self._make_pool(fetch=rows)
        result = await db_mod.list_spatial_memory_for_vendor(pool, vendor_id="vendor-abc")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["field_key"], "field_1")

    async def test_list_spatial_memory_for_vendor_empty(self):
        import backend.db as db_mod
        pool, _ = self._make_pool(fetch=[])
        result = await db_mod.list_spatial_memory_for_vendor(pool, vendor_id="no-vendor")
        self.assertEqual(result, [])

    async def test_list_spatial_memory_all_includes_vendor_name(self):
        import backend.db as db_mod
        entry = {**_sm_entry(), "vendor_name": "Acme Corp"}
        rows = [_FakeRecord(entry)]
        pool, _ = self._make_pool(fetch=rows)
        result = await db_mod.list_spatial_memory_all(pool, limit=100, offset=0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["vendor_name"], "Acme Corp")

    async def test_count_spatial_memory_all_returns_integer(self):
        import backend.db as db_mod
        pool, _ = self._make_pool(fetchval=7)
        result = await db_mod.count_spatial_memory_all(pool)
        self.assertEqual(result, 7)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class SpatialMemoryApiTests(unittest.IsolatedAsyncioTestCase):
    """Integration-style tests for the new FastAPI routes (all I/O mocked)."""

    def setUp(self):
        import os
        os.environ.setdefault("DATABASE_URL", "postgresql://augocr:augocr@localhost:5432/augocr")
        os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("LLM_URL", "http://localhost:8001/v1/chat/completions")
        os.environ.setdefault("MLFLOW_ENABLED", "false")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        import backend.main as main_mod
        self._saved_overrides = dict(main_mod.app.dependency_overrides)

    def _make_app(self, pool_mock, user: dict):
        """Import app, attach a mock pool, and override auth dependencies."""
        import backend.main as main_mod
        from backend.auth import get_current_user, require_admin, get_current_user_or_api_key

        app = main_mod.app
        app.state.pool = pool_mock

        # Override auth so no real JWT is needed
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_user_or_api_key] = lambda: user
        app.dependency_overrides[require_admin] = lambda: user
        return app

    async def _call(self, app, method: str, path: str, *, json_body=None):
        """Minimal ASGI call using httpx AsyncClient."""
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            func = getattr(client, method.lower())
            kwargs = {}
            if json_body is not None:
                kwargs["json"] = json_body
            resp = await func(path, **kwargs)
        return resp

    def tearDown(self):
        """Restore dependency overrides after each test."""
        import backend.main as main_mod
        main_mod.app.dependency_overrides.clear()
        main_mod.app.dependency_overrides.update(self._saved_overrides)

    # -- List vendor spatial memory ------------------------------------------

    async def test_list_vendor_spatial_memory_returns_entries(self):
        entries = [_sm_entry(sm_id=1), _sm_entry(sm_id=2, field_key="po_number")]
        user = {"sub": "user-1", "role": "client", "id": "user-1"}
        pool = _MockPool()

        with patch("backend.main.assert_vendor_access", new=AsyncMock()), \
             patch("backend.main.db_mod.list_spatial_memory_for_vendor", new=AsyncMock(return_value=entries)):
            app = self._make_app(pool, user)
            resp = await self._call(app, "GET", "/vendors/vendor-abc/spatial-memory")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["entries"]), 2)

    async def test_list_vendor_spatial_memory_empty(self):
        user = {"sub": "user-1", "role": "client", "id": "user-1"}
        pool = _MockPool()

        with patch("backend.main.assert_vendor_access", new=AsyncMock()), \
             patch("backend.main.db_mod.list_spatial_memory_for_vendor", new=AsyncMock(return_value=[])):
            app = self._make_app(pool, user)
            resp = await self._call(app, "GET", "/vendors/vendor-abc/spatial-memory")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)

    # -- Delete spatial memory entry -----------------------------------------

    async def test_delete_spatial_memory_entry_success(self):
        entry = _sm_entry(sm_id=5)
        user = {"sub": "user-1", "role": "client", "id": "user-1"}
        pool = _MockPool()

        with patch("backend.main.assert_vendor_access", new=AsyncMock()), \
             patch("backend.main.db_mod.get_spatial_memory_by_id", new=AsyncMock(return_value=entry)), \
             patch("backend.main.db_mod.delete_spatial_memory_by_id", new=AsyncMock(return_value={
                 "id": 5, "vendor_id": "vendor-abc", "layout_key": "vendor-abc:1",
                 "field_key": "invoice_number", "page_number": 1,
                 "gold_correction_fields_deleted": 1})) as mock_delete:
            app = self._make_app(pool, user)
            resp = await self._call(app, "DELETE", "/spatial-memory/5")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "deleted")
        self.assertEqual(data["sm_id"], 5)
        self.assertEqual(data["field_key"], "invoice_number")
        self.assertEqual(data["gold_correction_fields_deleted"], 1)
        mock_delete.assert_awaited_once_with(pool, 5, delete_gold_correction=True)

    async def test_delete_spatial_memory_entry_not_found_returns_404(self):
        user = {"sub": "user-1", "role": "client", "id": "user-1"}
        pool = _MockPool()

        with patch("backend.main.db_mod.get_spatial_memory_by_id", new=AsyncMock(return_value=None)):
            app = self._make_app(pool, user)
            resp = await self._call(app, "DELETE", "/spatial-memory/999")

        self.assertEqual(resp.status_code, 404)

    async def test_delete_spatial_memory_admin_can_delete_any_vendor(self):
        """Admin should bypass assert_vendor_access."""
        entry = _sm_entry(sm_id=7, vendor_id="other-vendor")
        admin = {"sub": "admin-1", "role": "admin", "id": "admin-1"}
        pool = _MockPool()

        assert_access_called = []

        async def _mock_assert(*args, **kwargs):
            assert_access_called.append(True)

        with patch("backend.main.assert_vendor_access", new=AsyncMock(side_effect=_mock_assert)), \
             patch("backend.main.db_mod.get_spatial_memory_by_id", new=AsyncMock(return_value=entry)), \
             patch("backend.main.db_mod.delete_spatial_memory_by_id", new=AsyncMock(return_value={
                 "id": 7, "vendor_id": "other-vendor", "layout_key": "other-vendor:1",
                 "field_key": "total_amount", "page_number": 1,
                 "gold_correction_fields_deleted": 1})):
            app = self._make_app(pool, admin)
            resp = await self._call(app, "DELETE", "/spatial-memory/7")

        self.assertEqual(resp.status_code, 200)
        # assert_vendor_access should NOT have been called for admin
        self.assertEqual(len(assert_access_called), 0)

    async def test_delete_vendor_gold_correction_field_success(self):
        user = {"sub": "user-1", "role": "client", "id": "user-1"}
        pool = _MockPool()

        with patch("backend.main.assert_vendor_access", new=AsyncMock()) as mock_access, \
             patch("backend.main.db_mod.delete_gold_correction_field", new=AsyncMock(return_value=2)) as mock_delete:
            app = self._make_app(pool, user)
            resp = await self._call(app, "DELETE", "/vendors/vendor-abc/gold-corrections/vendor_name")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "deleted")
        self.assertEqual(data["field_key"], "vendor_name")
        self.assertEqual(data["gold_correction_fields_deleted"], 2)
        mock_access.assert_awaited_once()
        mock_delete.assert_awaited_once_with(pool, "vendor-abc", "vendor_name")

    # -- Admin list all spatial memory ---------------------------------------

    async def test_admin_list_spatial_memory_returns_all(self):
        entries = [
            {**_sm_entry(sm_id=1), "vendor_name": "Vendor A", "client_email": "clienta@example.com"},
            {**_sm_entry(sm_id=2, vendor_id="v2", field_key="total"), "vendor_name": "Vendor B", "client_email": "clientb@example.com"},
        ]
        admin = {"sub": "admin-1", "role": "admin", "id": "admin-1"}
        pool = _MockPool()

        with patch("backend.main.db_mod.list_spatial_memory_all", new=AsyncMock(return_value=entries)), \
             patch("backend.main.db_mod.count_spatial_memory_all", new=AsyncMock(return_value=2)):
            app = self._make_app(pool, admin)
            resp = await self._call(app, "GET", "/admin/spatial-memory")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total"], 2)

    async def test_admin_list_spatial_memory_pagination(self):
        admin = {"sub": "admin-1", "role": "admin", "id": "admin-1"}
        pool = _MockPool()
        entries = [_sm_entry(sm_id=i) for i in range(1, 4)]

        with patch("backend.main.db_mod.list_spatial_memory_all", new=AsyncMock(return_value=entries)), \
             patch("backend.main.db_mod.count_spatial_memory_all", new=AsyncMock(return_value=5)):
            app = self._make_app(pool, admin)
            resp = await self._call(app, "GET", "/admin/spatial-memory?limit=3&offset=0")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["limit"], 3)
        self.assertEqual(data["offset"], 0)


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------

class _FakeRecord(dict):
    """asyncpg Record stand-in that behaves like a dict."""
    pass


class _AsyncCtxMgr:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *args):
        pass


class _MockPool:
    """Minimal pool mock — acquire() raises so tests must patch db functions."""
    def acquire(self):
        raise RuntimeError("Raw pool.acquire should not be called in API tests — patch db functions instead")


if __name__ == "__main__":
    unittest.main()
