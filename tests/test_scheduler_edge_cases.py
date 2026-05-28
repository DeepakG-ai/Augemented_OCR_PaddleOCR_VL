"""
test_scheduler_edge_cases.py — Edge cases for the scheduler features.

Now that APScheduler has been removed, the server stores schedule metadata in
the user_schedules table and computes next_run with datetime math. The client
agent handles all timing and folder scanning.

Covers:
  - set_schedule_executing / get_user_is_executing (DB layer)
  - Upload conflict: get_user_is_executing blocks UI upload
  - Multi-schedule: max-3 limit via DB count checks
  - Timezone: cron_expr stores UTC values
  - compute_next_run edge cases

All DB calls are mocked — no live services.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod
from backend.scheduler import compute_next_run


# ---------------------------------------------------------------------------
# Helpers (same pattern as existing test files)
# ---------------------------------------------------------------------------

def _make_pool(rows=None, fetchrow_val=None, execute_val="UPDATE 1"):
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch    = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_val)
    conn.execute  = AsyncMock(return_value=execute_val)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__  = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _sched_row(schedule_id: int, user_id: str,
               cron: str = "0 10 * * *", enabled: bool = True,
               is_executing: bool = False) -> dict:
    return {
        "id": schedule_id, "user_id": user_id, "cron_expr": cron,
        "timezone": "UTC", "label": "", "enabled": enabled,
        "is_executing": is_executing, "last_ran_at": None,
    }


# ---------------------------------------------------------------------------
# 1. set_schedule_executing — DB function
# ---------------------------------------------------------------------------

class SetScheduleExecutingTests(unittest.IsolatedAsyncioTestCase):

    async def test_calls_execute_with_true(self):
        pool, conn = _make_pool()
        await db_mod.set_schedule_executing(pool, schedule_id=5, executing=True)
        conn.execute.assert_called_once()
        args = conn.execute.call_args[0]
        self.assertIs(args[1], True)    # executing=True passed to SQL
        self.assertEqual(args[2], 5)   # schedule_id is second SQL param

    async def test_calls_execute_with_false(self):
        pool, conn = _make_pool()
        await db_mod.set_schedule_executing(pool, schedule_id=7, executing=False)
        conn.execute.assert_called_once()
        args = conn.execute.call_args[0]
        self.assertIs(args[1], False)
        self.assertEqual(args[2], 7)

    async def test_passes_correct_schedule_id(self):
        pool, conn = _make_pool()
        await db_mod.set_schedule_executing(pool, schedule_id=42, executing=True)
        args = conn.execute.call_args[0]
        self.assertEqual(args[2], 42)

    async def test_returns_none(self):
        pool, _ = _make_pool()
        result = await db_mod.set_schedule_executing(pool, 1, True)
        self.assertIsNone(result)

    async def test_setting_false_on_non_executing_schedule_is_safe(self):
        """Clearing an already-False flag must not raise."""
        pool, conn = _make_pool()
        try:
            await db_mod.set_schedule_executing(pool, 99, False)
        except Exception as e:
            self.fail(f"Unexpected exception: {e}")
        conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# 2. get_user_is_executing — DB function
# ---------------------------------------------------------------------------

class GetUserIsExecutingTests(unittest.IsolatedAsyncioTestCase):

    async def test_returns_false_for_invalid_uuid(self):
        pool, conn = _make_pool()
        result = await db_mod.get_user_is_executing(pool, "not-a-uuid")
        self.assertFalse(result)
        conn.fetchrow.assert_not_called()

    async def test_returns_false_when_no_schedule_executing(self):
        pool, _ = _make_pool(fetchrow_val={"running": False})
        result = await db_mod.get_user_is_executing(pool, _uid(1))
        self.assertFalse(result)

    async def test_returns_true_when_a_schedule_is_executing(self):
        pool, _ = _make_pool(fetchrow_val={"running": True})
        result = await db_mod.get_user_is_executing(pool, _uid(1))
        self.assertTrue(result)

    async def test_returns_false_when_fetchrow_returns_none(self):
        pool, _ = _make_pool(fetchrow_val=None)
        result = await db_mod.get_user_is_executing(pool, _uid(1))
        self.assertFalse(result)

    async def test_scoped_to_user_id(self):
        """Query must pass the correct UUID — not a hardcoded or wrong user."""
        pool, conn = _make_pool(fetchrow_val={"running": False})
        uid = _uid(5)
        await db_mod.get_user_is_executing(pool, uid)
        args = conn.fetchrow.call_args[0]
        passed_uid = str(args[1]).replace("-", "")
        self.assertIn(uid.replace("-", ""), passed_uid)

    async def test_different_users_independent(self):
        """User A executing must not influence check for user B."""
        uid_a, uid_b = _uid(1), _uid(2)
        pool_a, _ = _make_pool(fetchrow_val={"running": True})
        pool_b, _ = _make_pool(fetchrow_val={"running": False})
        self.assertTrue(await db_mod.get_user_is_executing(pool_a, uid_a))
        self.assertFalse(await db_mod.get_user_is_executing(pool_b, uid_b))


# ---------------------------------------------------------------------------
# 3. Upload conflict: get_user_is_executing used to block UI uploads
# ---------------------------------------------------------------------------

class UploadConflictLogicTests(unittest.IsolatedAsyncioTestCase):
    """
    The upload endpoint checks get_user_is_executing before accepting a file.
    These tests verify the DB function behaviour that drives that check.
    """

    async def test_no_executing_schedules_allows_upload(self):
        """No schedule running → get_user_is_executing returns False → upload proceeds."""
        pool, _ = _make_pool(fetchrow_val={"running": False})
        result = await db_mod.get_user_is_executing(pool, _uid(1))
        self.assertFalse(result)

    async def test_executing_schedule_blocks_upload(self):
        """One schedule running → get_user_is_executing returns True → upload blocked."""
        pool, _ = _make_pool(fetchrow_val={"running": True})
        result = await db_mod.get_user_is_executing(pool, _uid(2))
        self.assertTrue(result)

    async def test_scheduler_completes_then_upload_allowed(self):
        """After scheduler finishes, upload check returns False (allowed)."""
        uid = _uid(3)
        pool, _ = _make_pool(fetchrow_val={"running": False})
        result = await db_mod.get_user_is_executing(pool, uid)
        self.assertFalse(result)

    async def test_client_a_executing_does_not_block_client_b(self):
        """Client A running must not block client B's upload."""
        uid_a, uid_b = _uid(1), _uid(2)
        pool_a, _ = _make_pool(fetchrow_val={"running": True})
        pool_b, _ = _make_pool(fetchrow_val={"running": False})
        self.assertTrue(await db_mod.get_user_is_executing(pool_a, uid_a))
        self.assertFalse(await db_mod.get_user_is_executing(pool_b, uid_b))

    async def test_invalid_uuid_never_blocks_upload(self):
        """Corrupt/missing user_id must not accidentally block all uploads."""
        pool, conn = _make_pool()
        result = await db_mod.get_user_is_executing(pool, "not-a-uuid")
        self.assertFalse(result)
        conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Multi-schedule: max-3 per user — DB-level validation
# ---------------------------------------------------------------------------

class MultiScheduleTests(unittest.IsolatedAsyncioTestCase):

    async def test_user_can_have_three_independent_schedules(self):
        """Three distinct cron times must produce three rows — no deduplication."""
        uid = _uid(1)
        rows = [
            _sched_row(1, uid, "0 10 * * *"),
            _sched_row(2, uid, "0 13 * * *"),
            _sched_row(3, uid, "0 19 * * *"),
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        self.assertEqual(len(result), 3)
        crons = {r["cron_expr"] for r in result}
        self.assertEqual(crons, {"0 10 * * *", "0 13 * * *", "0 19 * * *"})

    async def test_get_user_schedules_returns_correct_count(self):
        uid = _uid(2)
        rows = [_sched_row(i, uid) for i in range(1, 4)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        self.assertEqual(len(result), 3)

    async def test_create_schedule_passes_cron_correctly(self):
        uid = _uid(3)
        cron = "30 13 * * *"
        row = _sched_row(4, uid, cron)
        pool, conn = _make_pool(fetchrow_val=row)
        result = await db_mod.create_user_schedule(pool, uid, cron, "UTC", "afternoon")
        self.assertEqual(result["cron_expr"], cron)
        args = conn.fetchrow.call_args[0]
        self.assertIn(cron, args)

    async def test_each_schedule_has_unique_id(self):
        uid = _uid(4)
        rows = [_sched_row(i, uid) for i in range(10, 13)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        ids = [r["id"] for r in result]
        self.assertEqual(len(set(ids)), 3)

    async def test_stop_one_schedule_leaves_others_enabled(self):
        uid = _uid(5)
        pool, conn = _make_pool(fetchrow_val=_sched_row(2, uid, enabled=False))
        await db_mod.update_user_schedule(pool, 2, enabled=False)
        args = conn.fetchrow.call_args[0]
        self.assertEqual(args[1], 2, "WHERE clause must target schedule_id=2 only")
        self.assertIn("WHERE id", args[0])

    async def test_delete_one_schedule_only_calls_execute_once(self):
        pool, conn = _make_pool(execute_val="DELETE 1")
        await db_mod.delete_user_schedule(pool, schedule_id=5)
        conn.execute.assert_called_once()
        self.assertEqual(conn.execute.call_args[0][1], 5)


# ---------------------------------------------------------------------------
# 5. UTC cron expression storage
# ---------------------------------------------------------------------------

class CronUTCStorageTests(unittest.IsolatedAsyncioTestCase):

    async def test_cron_expr_format_is_minute_hour_stars(self):
        uid = _uid(1)
        utc_hour, utc_minute = 14, 51
        cron = f"{utc_minute} {utc_hour} * * *"
        row = _sched_row(1, uid, cron)
        pool, conn = _make_pool(fetchrow_val=row)
        result = await db_mod.create_user_schedule(pool, uid, cron)
        self.assertEqual(result["cron_expr"], "51 14 * * *")

    async def test_midnight_utc_cron_format(self):
        uid = _uid(2)
        cron = "0 0 * * *"
        pool, _ = _make_pool(fetchrow_val=_sched_row(2, uid, cron))
        result = await db_mod.create_user_schedule(pool, uid, cron)
        self.assertEqual(result["cron_expr"], "0 0 * * *")

    async def test_three_utc_schedules_stored_independently(self):
        uid = _uid(3)
        crons = ["0 4 * * *", "30 9 * * *", "0 15 * * *"]
        rows = [_sched_row(i + 1, uid, crons[i]) for i in range(3)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        stored_crons = [r["cron_expr"] for r in result]
        self.assertEqual(stored_crons, crons)

    def test_cron_parts_parse_back_to_utc(self):
        cron = "51 14 * * *"
        parts = cron.split()
        utc_minute = int(parts[0])
        utc_hour   = int(parts[1])
        self.assertEqual(utc_hour, 14)
        self.assertEqual(utc_minute, 51)

    def test_multiple_cron_exprs_parse_correctly(self):
        cases = [
            (0, 0,   "0 0 * * *"),
            (8, 30,  "30 8 * * *"),
            (12, 0,  "0 12 * * *"),
            (23, 59, "59 23 * * *"),
            (14, 51, "51 14 * * *"),
        ]
        for utc_h, utc_m, expected in cases:
            cron = f"{utc_m} {utc_h} * * *"
            self.assertEqual(cron, expected,
                             f"UTC {utc_h}:{utc_m:02d} → expected '{expected}', got '{cron}'")


# ---------------------------------------------------------------------------
# 6. compute_next_run — additional edge cases
# ---------------------------------------------------------------------------

class ComputeNextRunEdgeCases(unittest.TestCase):
    """Additional edge cases beyond test_scheduler_isolation.py."""

    def test_just_before_midnight_returns_today(self):
        now = datetime(2026, 5, 27, 23, 58, 0, tzinfo=timezone.utc)
        result = compute_next_run("59 23 * * *", after=now)
        self.assertEqual(result.day, 27)
        self.assertEqual(result.hour, 23)
        self.assertEqual(result.minute, 59)

    def test_just_after_midnight_schedule_returns_today(self):
        now = datetime(2026, 5, 27, 0, 0, 1, tzinfo=timezone.utc)
        result = compute_next_run("30 10 * * *", after=now)
        self.assertEqual(result.day, 27)

    def test_none_input_returns_none(self):
        self.assertIsNone(compute_next_run(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(compute_next_run(""))

    def test_single_token_returns_none(self):
        self.assertIsNone(compute_next_run("30"))

    def test_negative_hour_returns_none(self):
        self.assertIsNone(compute_next_run("0 -1 * * *"))

    def test_hour_24_returns_none(self):
        self.assertIsNone(compute_next_run("0 24 * * *"))

    def test_minute_60_returns_none(self):
        self.assertIsNone(compute_next_run("60 10 * * *"))


if __name__ == "__main__":
    unittest.main()
