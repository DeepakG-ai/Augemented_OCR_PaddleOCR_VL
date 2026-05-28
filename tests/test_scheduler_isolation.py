"""
test_scheduler_isolation.py — Multi-client isolation and compute_next_run tests.

Now that APScheduler has been removed, the server only stores schedule metadata
in the user_schedules table. The client agent handles all timing and folder scanning.

Covers:
  - DB-level per-client isolation (unchanged)
  - compute_next_run correctness for daily cron expressions
  - Edge cases: invalid cron, boundary times

All DB calls are mocked — no live services.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod
from backend.scheduler import compute_next_run


# ---------------------------------------------------------------------------
# Shared test helpers
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


def _uid(n: int) -> str:
    """Return a deterministic UUID string for client N."""
    return f"00000000-0000-0000-0000-{n:012d}"


def _sched_row(schedule_id: int, user_id: str,
               cron: str = "0 10 * * *", tz: str = "UTC",
               label: str = "", enabled: bool = True) -> dict:
    return {
        "id": schedule_id, "user_id": user_id, "cron_expr": cron,
        "timezone": tz, "label": label, "enabled": enabled, "last_ran_at": None,
    }


# ---------------------------------------------------------------------------
# 1. DB-level per-client isolation
# ---------------------------------------------------------------------------

class MultiClientDBIsolationTests(unittest.IsolatedAsyncioTestCase):
    """
    Verify that DB query functions always scope to a single client's rows,
    that multiple clients can hold independent schedules at the same cron time,
    and that admin can see all clients.
    """

    async def test_client_a_cannot_see_client_b_schedules(self):
        uid_a, uid_b = _uid(1), _uid(2)
        rows_for_a = [_sched_row(1, uid_a, "0 10 * * *")]
        pool, conn = _make_pool(rows_for_a)

        result = await db_mod.get_user_schedules(pool, uid_a)

        self.assertEqual(len(result), 1)
        # DB was queried with uid_a's UUID, never uid_b's
        passed_uid = str(conn.fetch.call_args[0][1]).replace("-", "")
        self.assertIn(uid_a.replace("-", ""), passed_uid)
        self.assertNotIn(uid_b.replace("-", ""), passed_uid)

    async def test_10_clients_each_scoped_to_own_uuid(self):
        """10 independent clients — each fetch call passes only their UUID."""
        for i in range(1, 11):
            uid = _uid(i)
            pool, conn = _make_pool([_sched_row(i, uid, f"0 {i} * * *")])
            result = await db_mod.get_user_schedules(pool, uid)
            self.assertEqual(len(result), 1)
            passed = str(conn.fetch.call_args[0][1]).replace("-", "")
            self.assertIn(uid.replace("-", ""), passed, f"client {i}: wrong UUID passed")

    async def test_clients_can_share_same_cron_time(self):
        """
        Client 1 at 10:10am, Client 2 at 10:12am, Client 3 at 10:00am (same as
        another future client). Each schedule row has a unique ID — no collision.
        """
        uid_1, uid_2, uid_3 = _uid(1), _uid(2), _uid(3)
        rows = [
            {**_sched_row(1, uid_1, "10 10 * * *"), "email": "c1@t.com"},
            {**_sched_row(2, uid_2, "12 10 * * *"), "email": "c2@t.com"},
            {**_sched_row(3, uid_3, "0 10 * * *"),  "email": "c3@t.com"},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_all_schedules(pool)

        self.assertEqual(len(result), 3)
        crons = {r["cron_expr"] for r in result}
        self.assertIn("10 10 * * *", crons)
        self.assertIn("12 10 * * *", crons)
        self.assertIn("0 10 * * *", crons)
        ids = {r["id"] for r in result}
        self.assertEqual(ids, {1, 2, 3})  # each row is unique

    async def test_two_clients_at_same_10am_are_different_rows(self):
        """Client 1 and Client 3 both at 10:00am → two rows, two IDs."""
        uid_1, uid_3 = _uid(1), _uid(3)
        rows = [
            {**_sched_row(1, uid_1, "0 10 * * *"), "email": "c1@t.com"},
            {**_sched_row(3, uid_3, "0 10 * * *"), "email": "c3@t.com"},
        ]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_all_schedules(pool)

        self.assertEqual(len(result), 2)
        self.assertNotEqual(result[0]["id"], result[1]["id"])
        self.assertEqual(result[0]["cron_expr"], result[1]["cron_expr"])

    async def test_admin_sees_all_100_client_schedules(self):
        """get_all_schedules returns every client row — used for admin overview."""
        rows = [{**_sched_row(i, _uid(i), f"0 {i % 24} * * *"),
                 "email": f"client{i}@corp.com"} for i in range(1, 101)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_all_schedules(pool)
        self.assertEqual(len(result), 100)

    async def test_get_all_schedules_enabled_includes_all_active(self):
        """Startup reload: get_all_schedules_enabled returns every enabled row."""
        rows = [_sched_row(i, _uid(i), enabled=True) for i in range(1, 11)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_all_schedules_enabled(pool)
        self.assertEqual(len(result), 10)

    async def test_mark_schedule_ran_only_updates_given_id(self):
        """mark_schedule_ran(7) must NOT update schedule 8 or any other."""
        pool, conn = _make_pool()
        await db_mod.mark_schedule_ran(pool, schedule_id=7)

        conn.execute.assert_called_once()
        args = conn.execute.call_args[0]
        self.assertEqual(args[1], 7)
        self.assertEqual(len(args), 2)  # SQL + exactly one param

    async def test_delete_only_removes_given_schedule(self):
        """delete_user_schedule(3) must pass schedule_id=3, nothing else."""
        pool, conn = _make_pool(execute_val="DELETE 1")
        deleted = await db_mod.delete_user_schedule(pool, schedule_id=3)
        self.assertTrue(deleted)
        args = conn.execute.call_args[0]
        self.assertEqual(args[1], 3)


# ---------------------------------------------------------------------------
# 2. compute_next_run — pure datetime math, no APScheduler
# ---------------------------------------------------------------------------

class ComputeNextRunTests(unittest.TestCase):
    """
    compute_next_run('minute hour * * *') returns the next UTC fire time.
    It replaces APScheduler's entire cron trigger system.
    """

    def test_basic_future_time_today(self):
        """If schedule is later today, return today's time."""
        # Simulate "now" is 08:00 UTC, schedule at 16:30
        now = datetime(2026, 5, 27, 8, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run("30 16 * * *", after=now)
        self.assertEqual(result.hour, 16)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.day, 27)

    def test_past_time_today_returns_tomorrow(self):
        """If schedule time has passed today, return tomorrow's time."""
        now = datetime(2026, 5, 27, 18, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run("30 16 * * *", after=now)
        self.assertEqual(result.hour, 16)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.day, 28)

    def test_exact_current_time_returns_tomorrow(self):
        """If now == schedule time exactly, it's past → return tomorrow."""
        now = datetime(2026, 5, 27, 16, 30, 0, tzinfo=timezone.utc)
        result = compute_next_run("30 16 * * *", after=now)
        self.assertEqual(result.day, 28)

    def test_midnight_schedule(self):
        """Schedule at 00:00 UTC."""
        now = datetime(2026, 5, 27, 0, 1, 0, tzinfo=timezone.utc)
        result = compute_next_run("0 0 * * *", after=now)
        self.assertEqual(result.hour, 0)
        self.assertEqual(result.minute, 0)
        self.assertEqual(result.day, 28)

    def test_end_of_day_schedule(self):
        """Schedule at 23:59 UTC."""
        now = datetime(2026, 5, 27, 8, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run("59 23 * * *", after=now)
        self.assertEqual(result.hour, 23)
        self.assertEqual(result.minute, 59)
        self.assertEqual(result.day, 27)

    def test_invalid_cron_returns_none(self):
        """Malformed expressions return None."""
        self.assertIsNone(compute_next_run(""))
        self.assertIsNone(compute_next_run(None))
        self.assertIsNone(compute_next_run("not-a-cron"))
        self.assertIsNone(compute_next_run("abc def * * *"))

    def test_out_of_range_returns_none(self):
        """Hour > 23 or minute > 59 → None."""
        self.assertIsNone(compute_next_run("0 25 * * *"))
        self.assertIsNone(compute_next_run("61 10 * * *"))

    def test_non_daily_cron_returns_none(self):
        """Only daily schedules are supported."""
        self.assertIsNone(compute_next_run("30 16 1 * *"))
        self.assertIsNone(compute_next_run("30 16 * 1 *"))
        self.assertIsNone(compute_next_run("30 16 * * 1"))

    def test_sequential_calls_produce_consecutive_days(self):
        """Calling with after=previous_result produces the next day each time."""
        t = datetime(2026, 5, 27, 8, 0, 0, tzinfo=timezone.utc)
        days = []
        for _ in range(5):
            t = compute_next_run("30 16 * * *", after=t)
            days.append(t.day)
        self.assertEqual(days, [27, 28, 29, 30, 31])


if __name__ == "__main__":
    unittest.main()
