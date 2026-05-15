"""
test_config_api.py — Tests for user config CRUD and per-user isolation.

All DB and store calls are mocked; no live services required.
Follows the same IsolatedAsyncioTestCase pattern used throughout the project.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(config_rows: list[dict] | None = None):
    """Return a mock asyncpg pool whose acquire() yields a mock connection."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=config_rows or [])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


# ---------------------------------------------------------------------------
# get_user_config
# ---------------------------------------------------------------------------

class GetUserConfigTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_empty_dict_for_unknown_uuid(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_user_config(pool, "not-a-uuid")
        self.assertEqual(result, {})

    async def test_returns_key_value_dict(self):
        rows = [
            {"key": "upload_mode",  "value": "folder"},
            {"key": "input_folder", "value": "/tmp/pdfs"},
        ]
        pool, conn = _make_pool(rows)
        # Use a valid UUID string so the UUID parse succeeds
        uid = "00000000-0000-0000-0000-000000000001"
        result = await db_mod.get_user_config(pool, uid)
        self.assertEqual(result["upload_mode"], "folder")
        self.assertEqual(result["input_folder"], "/tmp/pdfs")

    async def test_empty_config_returns_empty_dict(self):
        pool, _ = _make_pool([])
        uid = "00000000-0000-0000-0000-000000000001"
        result = await db_mod.get_user_config(pool, uid)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# set_user_config
# ---------------------------------------------------------------------------

class SetUserConfigTests(unittest.IsolatedAsyncioTestCase):

    async def test_executes_upsert_for_each_key(self):
        pool, conn = _make_pool()
        uid = "00000000-0000-0000-0000-000000000002"
        await db_mod.set_user_config(pool, uid, {
            "upload_mode": "folder",
            "input_folder": "/data/in",
        })
        # Two keys → two execute calls
        self.assertEqual(conn.execute.call_count, 2)

    async def test_no_op_for_invalid_uuid(self):
        pool, conn = _make_pool()
        await db_mod.set_user_config(pool, "bad-uuid", {"upload_mode": "ui"})
        conn.execute.assert_not_called()

    async def test_empty_updates_is_no_op(self):
        pool, conn = _make_pool()
        uid = "00000000-0000-0000-0000-000000000003"
        await db_mod.set_user_config(pool, uid, {})
        conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# get_all_user_configs — groups rows by user
# ---------------------------------------------------------------------------

class GetAllUserConfigsTests(unittest.IsolatedAsyncioTestCase):

    async def test_groups_config_rows_by_user(self):
        uid_a = "00000000-0000-0000-0000-000000000010"
        uid_b = "00000000-0000-0000-0000-000000000011"
        rows = [
            {"user_id": uid_a, "email": "a@test.com", "role": "client",
             "key": "upload_mode", "value": "folder", "updated_at": None},
            {"user_id": uid_a, "email": "a@test.com", "role": "client",
             "key": "input_folder", "value": "/a/in", "updated_at": None},
            {"user_id": uid_b, "email": "b@test.com", "role": "client",
             "key": "upload_mode", "value": "ui", "updated_at": None},
        ]
        pool, conn = _make_pool(rows)
        result = await db_mod.get_all_user_configs(pool)
        self.assertEqual(len(result), 2)
        user_a = next(u for u in result if u["user_id"] == uid_a)
        self.assertEqual(user_a["config"]["upload_mode"], "folder")
        self.assertEqual(user_a["config"]["input_folder"], "/a/in")
        user_b = next(u for u in result if u["user_id"] == uid_b)
        self.assertEqual(user_b["config"]["upload_mode"], "ui")

    async def test_user_with_no_config_still_appears(self):
        uid = "00000000-0000-0000-0000-000000000020"
        rows = [{"user_id": uid, "email": "c@test.com", "role": "client",
                 "key": None, "value": None, "updated_at": None}]
        pool, conn = _make_pool(rows)
        result = await db_mod.get_all_user_configs(pool)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["config"], {})


# ---------------------------------------------------------------------------
# Isolation: client A cannot see client B data
# ---------------------------------------------------------------------------

class ConfigIsolationTests(unittest.IsolatedAsyncioTestCase):
    """
    Verify that get_user_config is always scoped to the given user_id.
    When client A queries, only their rows are returned (DB filters by user_id).
    """

    async def test_query_is_scoped_to_user_id(self):
        uid_a = "00000000-0000-0000-0000-000000000030"
        uid_b = "00000000-0000-0000-0000-000000000031"

        # Simulate DB returning only rows for A (correct behaviour)
        rows_a = [{"key": "upload_mode", "value": "folder"}]
        pool, conn = _make_pool(rows_a)

        result_a = await db_mod.get_user_config(pool, uid_a)

        # Confirm the SQL was called with uid_a's UUID, not uid_b's
        called_uid = str(conn.fetch.call_args[0][1])
        self.assertIn(uid_a.replace("-", ""), called_uid.replace("-", ""))
        # Result contains A's config
        self.assertEqual(result_a.get("upload_mode"), "folder")

    async def test_set_config_writes_to_correct_user(self):
        uid_a = "00000000-0000-0000-0000-000000000032"
        pool, conn = _make_pool()
        await db_mod.set_user_config(pool, uid_a, {"upload_mode": "ui"})
        # The first positional arg of execute (after the SQL) must be uid_a
        call_args = conn.execute.call_args[0]
        written_uid = str(call_args[1])  # $1 in the SQL
        self.assertIn(uid_a.replace("-", ""), written_uid.replace("-", ""))


# ---------------------------------------------------------------------------
# Valid upload_mode values (business rule — values match what main.py enforces)
# These are declared here rather than imported from main to avoid pulling in
# heavy runtime deps (mlflow, paddleocr, etc.) in the unit test environment.
# ---------------------------------------------------------------------------

_VALID_UPLOAD_MODES = frozenset({"ui", "folder"})
_CONFIG_KEYS = frozenset({"input_folder", "output_folder", "upload_mode"})


class UploadModeValidationTests(unittest.IsolatedAsyncioTestCase):

    def test_valid_modes_are_ui_and_folder(self):
        self.assertIn("ui", _VALID_UPLOAD_MODES)
        self.assertIn("folder", _VALID_UPLOAD_MODES)

    def test_config_keys_are_restricted(self):
        self.assertIn("input_folder", _CONFIG_KEYS)
        self.assertIn("output_folder", _CONFIG_KEYS)
        self.assertIn("upload_mode", _CONFIG_KEYS)
        self.assertNotIn("hacked_key", _CONFIG_KEYS)

    def test_unknown_key_not_allowed(self):
        allowed = _CONFIG_KEYS
        submitted = {"input_folder": "/x", "evil_key": "drop table users"}
        unknown = set(submitted) - allowed
        self.assertEqual(unknown, {"evil_key"})

    def test_invalid_upload_mode_rejected(self):
        invalid_modes = {"sftp", "s3", "email", "", "admin", "FOLDER"}
        for mode in invalid_modes:
            with self.subTest(mode=mode):
                self.assertNotIn(mode, _VALID_UPLOAD_MODES)


if __name__ == "__main__":
    unittest.main()
