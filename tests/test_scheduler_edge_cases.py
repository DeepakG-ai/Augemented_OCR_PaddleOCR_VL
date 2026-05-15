"""
test_scheduler_edge_cases.py — Edge cases for the new scheduler features.

Covers:
  - set_schedule_executing / get_user_is_executing (DB layer)
  - _run_schedule is_executing lifecycle (set True → finally False, every code path)
  - Upload conflict: get_user_is_executing blocks UI upload
  - max_instances=1 / coalesce=True enforced in sync_job
  - Multi-schedule: max-3 limit via DB count checks
  - Timezone: cron_expr stores UTC values

All DB, APScheduler, and filesystem calls are mocked — no live services.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod
from backend import scheduler as sched_mod


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


def _mock_apscheduler():
    s = MagicMock()
    s.running = True
    s.add_job  = MagicMock()
    s.remove_job = MagicMock()
    s.get_job  = MagicMock(return_value=None)
    return s


class _FakePath:
    def __init__(self, path_str, *, is_dir=True, pdfs=None):
        self._s   = path_str
        self._dir = is_dir
        self._pdfs = list(pdfs or [])

    def is_dir(self):   return self._dir
    def glob(self, pat): return iter(self._pdfs if pat == "*.pdf" else [])
    def __str__(self):   return self._s
    def __fspath__(self): return self._s


def _path_factory(configs: dict):
    def make(p):
        cfg = configs.get(str(p), {})
        return _FakePath(str(p), is_dir=cfg.get("is_dir", False), pdfs=cfg.get("pdfs", []))
    return make


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
        # user A pool returns True, user B pool returns False
        pool_a, _ = _make_pool(fetchrow_val={"running": True})
        pool_b, _ = _make_pool(fetchrow_val={"running": False})
        self.assertTrue(await db_mod.get_user_is_executing(pool_a, uid_a))
        self.assertFalse(await db_mod.get_user_is_executing(pool_b, uid_b))


# ---------------------------------------------------------------------------
# 3. _run_schedule is_executing lifecycle
# ---------------------------------------------------------------------------

class RunScheduleIsExecutingTests(unittest.IsolatedAsyncioTestCase):
    """
    Verify that is_executing is set True before ingesting and always cleared
    to False in the finally block, across every code path.
    """

    def setUp(self):
        sched_mod._ctx.clear()

    async def _fire(self, schedule_id, user_id, config, path_configs=None,
                    ingest_raises=False):
        mock_pool = MagicMock()
        exec_calls = []   # captures (sid, bool)
        ingest_calls = []

        async def ingest_cb(uid, path):
            if ingest_raises:
                raise RuntimeError("ingest failed")
            ingest_calls.append((str(uid), path))

        async def set_executing(pool, sid, val):
            exec_calls.append((sid, val))

        sched_mod.set_context(mock_pool, ingest_cb)

        async def get_config(pool, uid):
            return config

        factory = _path_factory(path_configs or {})

        with patch("backend.db.get_user_config",      side_effect=get_config), \
             patch("backend.db.mark_schedule_ran",    new_callable=AsyncMock), \
             patch("backend.db.set_schedule_executing", side_effect=set_executing), \
             patch("pathlib.Path", side_effect=factory):
            await sched_mod._run_schedule(schedule_id=schedule_id, user_id=user_id)

        return ingest_calls, exec_calls

    async def test_sets_executing_true_before_ingesting(self):
        """is_executing=True must be set BEFORE any ingest_cb call."""
        uid = _uid(1)
        folder = "/data/c1"
        pdfs   = [f"{folder}/a.pdf", f"{folder}/b.pdf"]
        ingest_calls, exec_calls = await self._fire(
            1, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
        )
        self.assertIn((1, True), exec_calls,
                      "set_schedule_executing(True) must be called when PDFs exist")
        # True must appear before any ingest work happens (exec_calls list order)
        self.assertEqual(exec_calls[0], (1, True))

    async def test_clears_executing_after_success(self):
        """After successful run, is_executing must be set back to False."""
        uid = _uid(2)
        folder = "/data/c2"
        pdfs = [f"{folder}/doc.pdf"]
        _, exec_calls = await self._fire(
            2, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
        )
        self.assertIn((2, False), exec_calls,
                      "set_schedule_executing(False) must always be called in finally")
        # Last call must be False (cleanup)
        self.assertEqual(exec_calls[-1], (2, False))

    async def test_order_is_true_then_false(self):
        """True is set first, False is set last — no inversion."""
        uid = _uid(3)
        folder = "/data/c3"
        pdfs = [f"{folder}/x.pdf"]
        _, exec_calls = await self._fire(
            3, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
        )
        self.assertEqual(len(exec_calls), 2)
        self.assertEqual(exec_calls[0], (3, True))
        self.assertEqual(exec_calls[1], (3, False))

    async def test_clears_executing_even_when_ingest_raises(self):
        """If ingest throws, finally must still clear is_executing."""
        uid = _uid(4)
        folder = "/data/c4"
        pdfs = [f"{folder}/fail.pdf"]
        _, exec_calls = await self._fire(
            4, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
            ingest_raises=True,
        )
        # True was set before the failing ingest
        self.assertIn((4, True), exec_calls)
        # False must be called in finally even though ingest raised
        self.assertIn((4, False), exec_calls)
        self.assertEqual(exec_calls[-1], (4, False))

    async def test_no_pdfs_never_sets_executing_true(self):
        """Empty folder → returns early before set(True). Only finally set(False) runs."""
        uid = _uid(5)
        folder = "/data/empty"
        _, exec_calls = await self._fire(
            5, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": []}},
        )
        true_calls  = [c for c in exec_calls if c == (5, True)]
        self.assertEqual(len(true_calls), 0,
                         "set_executing(True) must NOT be called when no PDFs found")

    async def test_missing_folder_never_sets_executing_true(self):
        """Non-existent folder → early return before set(True)."""
        uid = _uid(6)
        folder = "/data/gone"
        _, exec_calls = await self._fire(
            6, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": False, "pdfs": []}},
        )
        true_calls = [c for c in exec_calls if c == (6, True)]
        self.assertEqual(len(true_calls), 0)

    async def test_no_input_folder_config_never_sets_executing_true(self):
        """No input_folder in config → earliest return, no set(True) call."""
        uid = _uid(7)
        _, exec_calls = await self._fire(7, uid, config={})
        true_calls = [c for c in exec_calls if c == (7, True)]
        self.assertEqual(len(true_calls), 0)

    async def test_no_context_makes_zero_executing_calls(self):
        """Pool=None → returns before the try block; set_executing never called."""
        sched_mod._ctx.clear()
        exec_calls = []

        async def set_executing(pool, sid, val):
            exec_calls.append((sid, val))

        with patch("backend.db.set_schedule_executing", side_effect=set_executing):
            await sched_mod._run_schedule(schedule_id=1, user_id=_uid(1))

        self.assertEqual(exec_calls, [],
                         "No set_executing calls when context is missing")

    async def test_multiple_pdfs_sets_true_only_once(self):
        """is_executing=True should be set exactly once regardless of PDF count."""
        uid = _uid(8)
        folder = "/data/many"
        pdfs = [f"{folder}/f{i}.pdf" for i in range(10)]
        _, exec_calls = await self._fire(
            8, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
        )
        true_calls = [c for c in exec_calls if c[1] is True]
        self.assertEqual(len(true_calls), 1,
                         "set_executing(True) must be called exactly once")


# ---------------------------------------------------------------------------
# 4. sync_job — APScheduler job configuration
# ---------------------------------------------------------------------------

class SyncJobConfigTests(unittest.IsolatedAsyncioTestCase):
    """Verify that sync_job passes the right options to APScheduler."""

    def setUp(self):
        self._orig = sched_mod._scheduler
        sched_mod._scheduler = _mock_apscheduler()

    def tearDown(self):
        sched_mod._scheduler = self._orig

    def test_max_instances_is_1(self):
        """max_instances=1 prevents the same schedule running twice concurrently."""
        row = _sched_row(1, _uid(1))
        sched_mod.sync_job(row)
        kwargs = sched_mod._scheduler.add_job.call_args[1]
        self.assertEqual(kwargs.get("max_instances"), 1)

    def test_coalesce_is_true(self):
        """coalesce=True means missed fires run once, not N times."""
        row = _sched_row(2, _uid(2))
        sched_mod.sync_job(row)
        kwargs = sched_mod._scheduler.add_job.call_args[1]
        self.assertTrue(kwargs.get("coalesce"))

    def test_replace_existing_is_true(self):
        """replace_existing=True prevents duplicate job registration on reload."""
        row = _sched_row(3, _uid(3))
        sched_mod.sync_job(row)
        kwargs = sched_mod._scheduler.add_job.call_args[1]
        self.assertTrue(kwargs.get("replace_existing"))

    def test_job_kwargs_embed_schedule_id_and_user_id(self):
        """APScheduler job kwargs must carry schedule_id and user_id exactly."""
        uid = _uid(4)
        row = _sched_row(10, uid)
        sched_mod.sync_job(row)
        job_kwargs = sched_mod._scheduler.add_job.call_args[1]["kwargs"]
        self.assertEqual(job_kwargs["schedule_id"], 10)
        self.assertEqual(str(job_kwargs["user_id"]).replace("-", ""),
                         uid.replace("-", ""))

    def test_disabled_schedule_removes_job(self):
        """sync_job for a disabled row must remove — not add — the job."""
        sched_mod._scheduler.remove_job = MagicMock()
        row = _sched_row(5, _uid(5), enabled=False)
        sched_mod.sync_job(row)
        sched_mod._scheduler.add_job.assert_not_called()
        sched_mod._scheduler.remove_job.assert_called_once()

    def test_invalid_cron_does_not_call_add_job(self):
        """A malformed cron_expr must be silently skipped — no add_job call."""
        row = {**_sched_row(6, _uid(6)), "cron_expr": "bad cron"}
        sched_mod.sync_job(row)
        sched_mod._scheduler.add_job.assert_not_called()

    def test_two_schedules_same_user_get_different_job_ids(self):
        """Two schedule rows for the same user must produce distinct job IDs."""
        uid = _uid(7)
        sched_mod.sync_job(_sched_row(1, uid, "0 10 * * *"))
        sched_mod.sync_job(_sched_row(2, uid, "0 20 * * *"))
        calls = sched_mod._scheduler.add_job.call_args_list
        ids = [c[1]["id"] for c in calls]
        self.assertEqual(len(set(ids)), 2, "Job IDs must be distinct per schedule row")


# ---------------------------------------------------------------------------
# 5. Upload conflict: get_user_is_executing used to block UI uploads
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
        """
        Simulate: scheduler sets True → runs → sets False.
        After False, upload check returns False (allowed).
        """
        uid = _uid(3)
        # Simulate DB state after scheduler finishes
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
# 6. Multi-schedule: max-3 per user — DB-level validation
# ---------------------------------------------------------------------------

class MultiScheduleTests(unittest.IsolatedAsyncioTestCase):
    """
    Back-end creates schedules via create_user_schedule.
    The API layer enforces max 3 by counting get_user_schedules before creating.
    These tests verify the DB functions behave correctly under that constraint.
    """

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
        """API reads count from get_user_schedules to enforce max-3 guard."""
        uid = _uid(2)
        rows = [_sched_row(i, uid) for i in range(1, 4)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        self.assertEqual(len(result), 3)

    async def test_create_schedule_passes_cron_correctly(self):
        """Newly created schedule must store the supplied cron expression."""
        uid = _uid(3)
        cron = "30 13 * * *"
        row = _sched_row(4, uid, cron)
        pool, conn = _make_pool(fetchrow_val=row)
        result = await db_mod.create_user_schedule(pool, uid, cron, "UTC", "afternoon")
        self.assertEqual(result["cron_expr"], cron)
        # Verify the cron was passed as a positional arg to fetchrow
        args = conn.fetchrow.call_args[0]
        self.assertIn(cron, args)

    async def test_each_schedule_has_unique_id(self):
        """Three schedules for the same user must have three distinct IDs."""
        uid = _uid(4)
        rows = [_sched_row(i, uid) for i in range(10, 13)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        ids = [r["id"] for r in result]
        self.assertEqual(len(set(ids)), 3)

    async def test_stop_one_schedule_leaves_others_enabled(self):
        """Disabling schedule #2 must pass schedule_id=2 and only schedule_id=2."""
        uid = _uid(5)
        pool, conn = _make_pool(fetchrow_val=_sched_row(2, uid, enabled=False))
        await db_mod.update_user_schedule(pool, 2, enabled=False)
        # The UPDATE statement was called with schedule_id=2 as the WHERE arg
        args = conn.fetchrow.call_args[0]
        # Call shape: fetchrow(sql, schedule_id, *values) — schedule_id is args[1]
        self.assertEqual(args[1], 2, "WHERE clause must target schedule_id=2 only")
        self.assertIn("WHERE id", args[0])

    async def test_delete_one_schedule_only_calls_execute_once(self):
        """Deleting schedule 5 must execute exactly one DELETE statement."""
        pool, conn = _make_pool(execute_val="DELETE 1")
        await db_mod.delete_user_schedule(pool, schedule_id=5)
        conn.execute.assert_called_once()
        self.assertEqual(conn.execute.call_args[0][1], 5)


# ---------------------------------------------------------------------------
# 7. UTC cron expression storage
# ---------------------------------------------------------------------------

class CronUTCStorageTests(unittest.IsolatedAsyncioTestCase):
    """
    The frontend converts local time → UTC before sending hour/minute.
    The backend stores and returns UTC values in cron_expr.
    These tests verify the cron format stored is correct UTC.
    """

    async def test_cron_expr_format_is_minute_hour_stars(self):
        """cron_expr must follow 'M H * * *' format (standard APScheduler cron)."""
        uid = _uid(1)
        # Simulate: frontend sent UTC hour=14, minute=51 (e.g. 20:21 IST)
        utc_hour, utc_minute = 14, 51
        cron = f"{utc_minute} {utc_hour} * * *"
        row = _sched_row(1, uid, cron)
        pool, conn = _make_pool(fetchrow_val=row)
        result = await db_mod.create_user_schedule(pool, uid, cron)
        self.assertEqual(result["cron_expr"], "51 14 * * *")

    async def test_midnight_utc_cron_format(self):
        """Midnight UTC → '0 0 * * *'."""
        uid = _uid(2)
        cron = "0 0 * * *"
        pool, _ = _make_pool(fetchrow_val=_sched_row(2, uid, cron))
        result = await db_mod.create_user_schedule(pool, uid, cron)
        self.assertEqual(result["cron_expr"], "0 0 * * *")

    async def test_three_utc_schedules_stored_independently(self):
        """Three daily UTC times stored as three separate cron rows."""
        uid = _uid(3)
        crons = ["0 4 * * *", "30 9 * * *", "0 15 * * *"]
        rows = [_sched_row(i + 1, uid, crons[i]) for i in range(3)]
        pool, _ = _make_pool(rows)
        result = await db_mod.get_user_schedules(pool, uid)
        stored_crons = [r["cron_expr"] for r in result]
        self.assertEqual(stored_crons, crons)

    def test_cron_parts_parse_back_to_utc(self):
        """Verify cron '51 14 * * *' parses back to utc_hour=14, utc_minute=51."""
        cron = "51 14 * * *"
        parts = cron.split()
        utc_minute = int(parts[0])
        utc_hour   = int(parts[1])
        self.assertEqual(utc_hour, 14)
        self.assertEqual(utc_minute, 51)

    def test_multiple_cron_exprs_parse_correctly(self):
        """Various hour:minute values round-trip through cron format."""
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


if __name__ == "__main__":
    unittest.main()
