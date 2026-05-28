"""
test_scheduler.py — Unit tests for per-user schedule DB queries.

All DB calls are mocked; no live services required.
Pattern mirrors test_config_api.py exactly.
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
from backend import scheduler as sched_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(rows=None, fetchrow_val=None, execute_val="DELETE 0"):
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_val)
    conn.execute = AsyncMock(return_value=execute_val)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


_UID = "00000000-0000-0000-0000-000000000001"
_UID2 = "00000000-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# get_user_schedules
# ---------------------------------------------------------------------------

class GetUserSchedulesTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_empty_for_invalid_uuid(self):
        pool, _ = _make_pool()
        result = await db_mod.get_user_schedules(pool, "not-a-uuid")
        self.assertEqual(result, [])

    async def test_returns_list_for_valid_uuid(self):
        rows = [
            {"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
             "timezone": "UTC", "label": "Morning", "enabled": True, "last_ran_at": None},
        ]
        pool, conn = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, _UID)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["cron_expr"], "0 10 * * *")

    async def test_query_passes_uuid_as_first_arg(self):
        pool, conn = _make_pool()
        await db_mod.get_user_schedules(pool, _UID)
        conn.fetch.assert_called_once()
        passed_uid = str(conn.fetch.call_args[0][1])
        self.assertIn(_UID.replace("-", ""), passed_uid.replace("-", ""))


# ---------------------------------------------------------------------------
# create_user_schedule
# ---------------------------------------------------------------------------

class CreateUserScheduleTests(unittest.IsolatedAsyncioTestCase):

    async def test_raises_value_error_for_invalid_uuid(self):
        pool, _ = _make_pool()
        with self.assertRaises(ValueError):
            await db_mod.create_user_schedule(pool, "bad-uuid", "0 10 * * *")

    async def test_returns_dict_on_success(self):
        row = {"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
               "timezone": "UTC", "label": "Run", "enabled": True, "last_ran_at": None}
        pool, _ = _make_pool(fetchrow_val=row)
        result = await db_mod.create_user_schedule(pool, _UID, "0 10 * * *", "UTC", "Run")
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["cron_expr"], "0 10 * * *")

    async def test_calls_fetchrow_with_correct_cron(self):
        row = {"id": 2, "user_id": _UID, "cron_expr": "0 22 * * *",
               "timezone": "America/New_York", "label": "", "enabled": True, "last_ran_at": None}
        pool, conn = _make_pool(fetchrow_val=row)
        await db_mod.create_user_schedule(pool, _UID, "0 22 * * *", "America/New_York", "")
        conn.fetchrow.assert_called_once()
        args = conn.fetchrow.call_args[0]
        self.assertEqual(args[2], "0 22 * * *")   # $2 is cron_expr
        self.assertEqual(args[3], "America/New_York")  # $3 is timezone


# ---------------------------------------------------------------------------
# get_schedule
# ---------------------------------------------------------------------------

class GetScheduleTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_none_when_not_found(self):
        pool, _ = _make_pool(fetchrow_val=None)
        result = await db_mod.get_schedule(pool, 999)
        self.assertIsNone(result)

    async def test_returns_dict_when_found(self):
        row = {"id": 5, "user_id": _UID, "cron_expr": "0 6 * * *",
               "timezone": "UTC", "label": "Early", "enabled": True, "last_ran_at": None}
        pool, _ = _make_pool(fetchrow_val=row)
        result = await db_mod.get_schedule(pool, 5)
        self.assertEqual(result["id"], 5)
        self.assertEqual(result["label"], "Early")


# ---------------------------------------------------------------------------
# update_user_schedule
# ---------------------------------------------------------------------------

class UpdateUserScheduleTests(unittest.IsolatedAsyncioTestCase):

    async def test_no_valid_kwargs_returns_existing(self):
        row = {"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
               "timezone": "UTC", "label": "A", "enabled": True, "last_ran_at": None}
        pool, conn = _make_pool(fetchrow_val=row)
        result = await db_mod.update_user_schedule(pool, 1, unknown_key="x")
        # Should call fetchrow (via get_schedule), not execute
        conn.execute.assert_not_called()

    async def test_valid_kwargs_call_update(self):
        row = {"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
               "timezone": "UTC", "label": "B", "enabled": False, "last_ran_at": None}
        pool, conn = _make_pool(fetchrow_val=row)
        await db_mod.update_user_schedule(pool, 1, enabled=False)
        conn.fetchrow.assert_called_once()


# ---------------------------------------------------------------------------
# delete_user_schedule
# ---------------------------------------------------------------------------

class DeleteUserScheduleTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_true_when_deleted(self):
        pool, _ = _make_pool(execute_val="DELETE 1")
        result = await db_mod.delete_user_schedule(pool, 1)
        self.assertTrue(result)

    async def test_returns_false_when_not_found(self):
        pool, _ = _make_pool(execute_val="DELETE 0")
        result = await db_mod.delete_user_schedule(pool, 999)
        self.assertFalse(result)

    async def test_calls_execute_with_schedule_id(self):
        pool, conn = _make_pool(execute_val="DELETE 1")
        await db_mod.delete_user_schedule(pool, 42)
        conn.execute.assert_called_once()
        self.assertEqual(conn.execute.call_args[0][1], 42)


# ---------------------------------------------------------------------------
# get_all_schedules_enabled
# ---------------------------------------------------------------------------

class GetAllSchedulesEnabledTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_only_enabled_rows(self):
        rows = [
            {"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
             "timezone": "UTC", "label": "A", "enabled": True, "last_ran_at": None},
            {"id": 2, "user_id": _UID2, "cron_expr": "0 22 * * *",
             "timezone": "UTC", "label": "B", "enabled": True, "last_ran_at": None},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_all_schedules_enabled(pool)
        self.assertEqual(len(result), 2)

    async def test_returns_empty_when_none_enabled(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_all_schedules_enabled(pool)
        self.assertEqual(result, [])


class RuntimeSchedulerRemovedTests(unittest.TestCase):

    def test_server_scheduler_module_has_no_runtime_scheduler(self):
        self.assertFalse(hasattr(sched_mod, "_scheduler"))
        self.assertFalse(hasattr(sched_mod, "reload_all_schedules"))
        self.assertFalse(hasattr(sched_mod, "sync_job"))

    def test_server_scheduler_only_computes_display_next_run(self):
        self.assertTrue(callable(sched_mod.compute_next_run))


# ---------------------------------------------------------------------------
# get_all_schedules (admin)
# ---------------------------------------------------------------------------

class GetAllSchedulesTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_rows_with_email(self):
        rows = [
            {"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
             "timezone": "UTC", "label": "A", "enabled": True,
             "last_ran_at": None, "email": "a@test.com"},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_all_schedules(pool)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["email"], "a@test.com")

    async def test_returns_empty_when_no_schedules(self):
        pool, _ = _make_pool([])
        result = await db_mod.get_all_schedules(pool)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# mark_schedule_ran
# ---------------------------------------------------------------------------

class MarkScheduleRanTests(unittest.IsolatedAsyncioTestCase):

    async def test_calls_execute_once(self):
        pool, conn = _make_pool()
        await db_mod.mark_schedule_ran(pool, 7)
        conn.execute.assert_called_once()

    async def test_passes_schedule_id_as_arg(self):
        pool, conn = _make_pool()
        await db_mod.mark_schedule_ran(pool, 99)
        call_args = conn.execute.call_args[0]
        self.assertEqual(call_args[1], 99)


# ---------------------------------------------------------------------------
# Isolation: user A cannot see user B schedules
# ---------------------------------------------------------------------------

class ScheduleIsolationTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_schedules_scoped_to_user(self):
        rows_a = [{"id": 1, "user_id": _UID, "cron_expr": "0 10 * * *",
                   "timezone": "UTC", "label": "A", "enabled": True, "last_ran_at": None}]
        pool, conn = _make_pool(rows_a)
        result = await db_mod.get_user_schedules(pool, _UID)
        called_uid = str(conn.fetch.call_args[0][1])
        self.assertIn(_UID.replace("-", ""), called_uid.replace("-", ""))
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
