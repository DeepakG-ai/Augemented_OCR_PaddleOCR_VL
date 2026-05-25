"""
test_scheduler_isolation.py — Multi-client isolation, concurrency, and edge-case tests.

Covers the hard questions:
  - Does client A's _run_schedule ever touch client B's config or folder?
  - What if 100 clients all share "0 10 * * *" — do they get 100 independent jobs?
  - Client 1 @ 10:10am, Client 2 @ 10:12am, Client 3 @ 10:00am firing concurrently.
  - Client 1 has 5 vendor PDFs, client 2 has 10 — correct count per client.
  - Client A's missing folder must not stop client B's ingestion.
  - Disabling client A's schedule must not remove client B's APScheduler job.
  - No context set → silent failure, no exception.

All DB, APScheduler, and filesystem calls are mocked — no live services.
"""
from __future__ import annotations

import asyncio
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


def _mock_apscheduler():
    """Mock that mimics an AsyncIOScheduler's interface."""
    s = MagicMock()
    s.running = True
    s.add_job = MagicMock()
    s.remove_job = MagicMock()
    s.get_job = MagicMock(return_value=None)
    return s


class _FakePath:
    """Minimal pathlib.Path substitute used by _run_schedule tests."""
    def __init__(self, path_str: str, *, is_dir: bool = True, pdfs: list | None = None):
        self._s = path_str
        self._is_dir_val = is_dir
        self._pdfs = list(pdfs or [])

    def is_dir(self) -> bool:
        return self._is_dir_val

    def glob(self, pattern: str):
        # Return PDFs only for lowercase pattern; "*.PDF" returns empty so
        # the two glob() calls in _run_schedule don't double-count.
        return iter(self._pdfs if pattern == "*.pdf" else [])

    def __str__(self) -> str:
        return self._s

    def __fspath__(self) -> str:
        return self._s


def _path_factory(configs: dict):
    """
    Return a callable that replaces pathlib.Path(input_folder).
    configs: {path_str: {"is_dir": bool, "pdfs": [str, ...]}}
    Paths not in configs return is_dir=False.
    """
    def make(p):
        cfg = configs.get(str(p), {})
        return _FakePath(str(p), is_dir=cfg.get("is_dir", False), pdfs=cfg.get("pdfs", []))
    return make


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
        # Both have the same cron — that's fine, they have different job IDs in APScheduler
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
# 2. _run_schedule context isolation — the core isolation guarantee
# ---------------------------------------------------------------------------

class RunScheduleContextTests(unittest.IsolatedAsyncioTestCase):
    """
    _run_schedule receives (schedule_id, user_id) from APScheduler kwargs.
    It must ONLY read config for that user_id — never another client's.
    """

    def setUp(self):
        sched_mod._ctx.clear()

    async def _fire(self, schedule_id: int, user_id: str,
                    config: dict, path_configs: dict | None = None):
        """
        Fire _run_schedule for one client.
        Returns list of (user_id_str, path_str) from ingest_cb calls.
        """
        mock_pool = MagicMock()
        captured = []

        async def ingest_cb(uid, path):
            captured.append((str(uid), path))

        sched_mod.set_context(mock_pool, ingest_cb)

        config_calls = []

        async def get_config(pool, uid):
            config_calls.append(str(uid))
            return config if str(uid).replace("-", "") == user_id.replace("-", "") else {}

        factory = _path_factory(path_configs or {})

        with patch("backend.db.get_user_config", side_effect=get_config), \
             patch("backend.db.mark_schedule_ran", new_callable=AsyncMock), \
             patch("pathlib.Path", side_effect=factory):
            await sched_mod._run_schedule(schedule_id=schedule_id, user_id=user_id)

        return captured, config_calls

    async def test_reads_only_own_user_config(self):
        """get_user_config is called exactly once, with the scheduled user's ID."""
        uid = _uid(1)
        folder = "/data/client1"
        pdfs = [f"{folder}/vendor_{i}.pdf" for i in range(3)]
        _, config_calls = await self._fire(
            1, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
        )
        self.assertEqual(len(config_calls), 1)
        self.assertIn(uid.replace("-", ""), config_calls[0].replace("-", ""))

    async def test_ingest_always_carries_scheduled_user_id(self):
        """Every ingest_cb call must use the scheduled user's ID, not a global."""
        uid = _uid(2)
        folder = "/data/client2"
        pdfs = [f"{folder}/v{i}.pdf" for i in range(4)]
        calls, _ = await self._fire(
            2, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": pdfs}},
        )
        self.assertEqual(len(calls), 4)
        for ingest_uid, _ in calls:
            self.assertIn(uid.replace("-", ""), ingest_uid.replace("-", ""))

    async def test_no_input_folder_zero_ingest_calls(self):
        """Client with no input_folder configured → ingest never called."""
        uid = _uid(3)
        calls, _ = await self._fire(3, uid, config={})
        self.assertEqual(calls, [])

    async def test_missing_folder_zero_ingest_calls(self):
        """input_folder path does not exist on disk → ingest never called."""
        uid = _uid(4)
        folder = "/data/nowhere"
        calls, _ = await self._fire(
            4, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": False, "pdfs": []}},
        )
        self.assertEqual(calls, [])

    async def test_empty_folder_zero_ingest_calls(self):
        """Folder exists but has zero PDFs → ingest never called."""
        uid = _uid(5)
        folder = "/data/empty"
        calls, _ = await self._fire(
            5, uid,
            config={"input_folder": folder},
            path_configs={folder: {"is_dir": True, "pdfs": []}},
        )
        self.assertEqual(calls, [])

    async def test_no_context_does_not_raise(self):
        """_run_schedule with _ctx cleared must log and return — no exception."""
        sched_mod._ctx.clear()
        try:
            await sched_mod._run_schedule(schedule_id=1, user_id=_uid(1))
        except Exception as e:
            self.fail(f"Unexpected exception with no context: {e}")


# ---------------------------------------------------------------------------
# 3. PDF scale tests — correct ingest count per client
# ---------------------------------------------------------------------------

class PDFScaleTests(unittest.IsolatedAsyncioTestCase):
    """
    Client 1 has 5 vendor formats (5 PDFs).
    Client 2 has 10 vendor formats (10 PDFs).
    Scale up to 100 PDFs for one client.
    """

    def setUp(self):
        sched_mod._ctx.clear()

    async def _fire_and_count(self, user_id: str, folder: str, pdf_count: int) -> int:
        mock_pool = MagicMock()
        calls = []

        async def ingest_cb(uid, path):
            calls.append((uid, path))

        sched_mod.set_context(mock_pool, ingest_cb)
        pdfs = [f"{folder}/vendor_{i}.pdf" for i in range(pdf_count)]

        async def get_config(pool, uid):
            return {"input_folder": folder}

        factory = _path_factory({folder: {"is_dir": True, "pdfs": pdfs}})

        with patch("backend.db.get_user_config", side_effect=get_config), \
             patch("backend.db.mark_schedule_ran", new_callable=AsyncMock), \
             patch("pathlib.Path", side_effect=factory):
            await sched_mod._run_schedule(schedule_id=1, user_id=user_id)

        return len(calls)

    async def test_client_1_has_5_vendor_formats_ingests_5_pdfs(self):
        count = await self._fire_and_count(_uid(1), "/data/c1", pdf_count=5)
        self.assertEqual(count, 5)

    async def test_client_2_has_10_vendor_formats_ingests_10_pdfs(self):
        count = await self._fire_and_count(_uid(2), "/data/c2", pdf_count=10)
        self.assertEqual(count, 10)

    async def test_scale_100_vendor_formats_ingests_100_pdfs(self):
        """A client with 100 different PDF formats triggers 100 ingest calls."""
        count = await self._fire_and_count(_uid(3), "/data/c3", pdf_count=100)
        self.assertEqual(count, 100)

    async def test_ingest_count_does_not_bleed_between_clients(self):
        """
        Client 1 fires (5 PDFs), then Client 2 fires (10 PDFs).
        Each call's result is independent — no cross-contamination.
        """
        count1 = await self._fire_and_count(_uid(1), "/data/c1", pdf_count=5)
        sched_mod._ctx.clear()
        count2 = await self._fire_and_count(_uid(2), "/data/c2", pdf_count=10)
        self.assertEqual(count1, 5)
        self.assertEqual(count2, 10)


# ---------------------------------------------------------------------------
# 4. APScheduler job-ID isolation (sync_job / remove_job)
# ---------------------------------------------------------------------------

class SchedulerJobIsolationTests(unittest.IsolatedAsyncioTestCase):
    """
    Each schedule gets a unique APScheduler job ID ("user_sched_{schedule_id}").
    100 clients with the same cron → 100 distinct jobs, no shared state.
    Disabling one client's job must not touch another's.
    """

    def setUp(self):
        self._orig = sched_mod._scheduler
        sched_mod._scheduler = _mock_apscheduler()

    def tearDown(self):
        sched_mod._scheduler = self._orig

    def _add_job_kwargs(self, call_index: int) -> dict:
        return sched_mod._scheduler.add_job.call_args_list[call_index][1]

    def test_job_ids_are_unique_per_schedule(self):
        ids = {sched_mod._job_id(i) for i in range(1, 101)}
        self.assertEqual(len(ids), 100)

    def test_two_clients_same_cron_get_different_job_ids(self):
        uid_a, uid_b = _uid(1), _uid(2)
        sched_mod.sync_job(_sched_row(1, uid_a, "0 10 * * *"))
        sched_mod.sync_job(_sched_row(2, uid_b, "0 10 * * *"))

        self.assertEqual(sched_mod._scheduler.add_job.call_count, 2)
        id1 = self._add_job_kwargs(0)["id"]
        id2 = self._add_job_kwargs(1)["id"]
        self.assertNotEqual(id1, id2)
        self.assertEqual({id1, id2}, {"user_sched_1", "user_sched_2"})

    def test_sync_job_embeds_correct_user_id_in_apscheduler_kwargs(self):
        """APScheduler will pass these kwargs to _run_schedule at fire time."""
        uid = _uid(42)
        sched_mod.sync_job(_sched_row(42, uid, "0 10 * * *"))

        kwargs_passed = self._add_job_kwargs(0)["kwargs"]
        self.assertEqual(kwargs_passed["user_id"], uid)
        self.assertEqual(kwargs_passed["schedule_id"], 42)

    def test_three_clients_different_cron_times_three_independent_jobs(self):
        """Client 1 @ 10:10, Client 2 @ 10:12, Client 3 @ 10:00."""
        sched_mod.sync_job(_sched_row(1, _uid(1), "10 10 * * *"))
        sched_mod.sync_job(_sched_row(2, _uid(2), "12 10 * * *"))
        sched_mod.sync_job(_sched_row(3, _uid(3), "0 10 * * *"))

        self.assertEqual(sched_mod._scheduler.add_job.call_count, 3)
        ids = {self._add_job_kwargs(i)["id"] for i in range(3)}
        self.assertEqual(ids, {"user_sched_1", "user_sched_2", "user_sched_3"})

    def test_100_clients_same_cron_produce_100_distinct_jobs(self):
        for i in range(1, 101):
            sched_mod.sync_job(_sched_row(i, _uid(i), "0 10 * * *"))

        self.assertEqual(sched_mod._scheduler.add_job.call_count, 100)
        ids = {self._add_job_kwargs(i)["id"] for i in range(100)}
        self.assertEqual(len(ids), 100)  # all unique

    def test_disable_client_a_does_not_remove_client_b_job(self):
        """Pausing Client A's schedule must leave Client B's job untouched."""
        sched_mod.sync_job(_sched_row(2, _uid(2), enabled=True))   # B: active
        sched_mod.sync_job(_sched_row(1, _uid(1), enabled=False))  # A: disable

        # B was added once; A's job was removed (not B's)
        self.assertEqual(sched_mod._scheduler.add_job.call_count, 1)
        self.assertEqual(sched_mod._scheduler.remove_job.call_count, 1)
        removed = sched_mod._scheduler.remove_job.call_args[0][0]
        self.assertEqual(removed, "user_sched_1")  # A's ID, not B's

    def test_remove_job_targets_only_given_schedule_id(self):
        sched_mod.remove_job(5)
        sched_mod._scheduler.remove_job.assert_called_once_with("user_sched_5")

    def test_invalid_cron_does_not_register_job(self):
        """Malformed cron expression → no job added, no exception."""
        sched_mod.sync_job(_sched_row(99, _uid(99), cron="not-valid-cron"))
        sched_mod._scheduler.add_job.assert_not_called()

    def test_no_scheduler_sync_job_does_not_raise(self):
        """sync_job and remove_job must be safe before init_scheduler() is called."""
        sched_mod._scheduler = None
        try:
            sched_mod.sync_job(_sched_row(1, _uid(1)))
            sched_mod.remove_job(1)
        except Exception as e:
            self.fail(f"Unexpected exception with _scheduler=None: {e}")


# ---------------------------------------------------------------------------
# 5. Concurrent firing — clients at the same cron time
# ---------------------------------------------------------------------------

class ConcurrentFiringTests(unittest.IsolatedAsyncioTestCase):
    """
    Client 1 at 10:10, Client 2 at 10:12, Client 3 at 10:00.
    Client 1 and Client 3 share 10:00 — both must fire independently with
    no context bleed between them.
    """

    def setUp(self):
        sched_mod._ctx.clear()

    async def test_concurrent_clients_read_own_config_only(self):
        """
        asyncio.gather fires Client 1 and Client 3 simultaneously.
        Each must query get_user_config with its own user_id, ingest its own PDFs.
        """
        uid_1, uid_3 = _uid(1), _uid(3)
        mock_pool = MagicMock()
        ingest_calls = []

        async def ingest_cb(uid, path):
            ingest_calls.append((str(uid).replace("-", ""), path))

        sched_mod.set_context(mock_pool, ingest_cb)

        config_access = []

        async def get_config(pool, uid):
            uid_s = str(uid).replace("-", "")
            config_access.append(uid_s)
            return {
                uid_1.replace("-", ""): {"input_folder": "/data/c1"},
                uid_3.replace("-", ""): {"input_folder": "/data/c3"},
            }.get(uid_s, {})

        path_configs = {
            "/data/c1": {"is_dir": True, "pdfs": [f"/data/c1/v{i}.pdf" for i in range(5)]},
            "/data/c3": {"is_dir": True, "pdfs": [f"/data/c3/v{i}.pdf" for i in range(3)]},
        }
        factory = _path_factory(path_configs)

        with patch("backend.db.get_user_config", side_effect=get_config), \
             patch("backend.db.mark_schedule_ran", new_callable=AsyncMock), \
             patch("pathlib.Path", side_effect=factory):
            await asyncio.gather(
                sched_mod._run_schedule(schedule_id=1, user_id=uid_1),
                sched_mod._run_schedule(schedule_id=3, user_id=uid_3),
            )

        c1_calls = [c for c in ingest_calls if c[0] == uid_1.replace("-", "")]
        c3_calls = [c for c in ingest_calls if c[0] == uid_3.replace("-", "")]
        self.assertEqual(len(c1_calls), 5, "Client 1 must ingest its 5 PDFs")
        self.assertEqual(len(c3_calls), 3, "Client 3 must ingest its 3 PDFs")

        # get_user_config called once per client
        self.assertEqual(config_access.count(uid_1.replace("-", "")), 1)
        self.assertEqual(config_access.count(uid_3.replace("-", "")), 1)

    async def test_three_concurrent_clients_all_isolated(self):
        """All three clients fire at once — each ingests exactly its own PDFs."""
        uid_1, uid_2, uid_3 = _uid(1), _uid(2), _uid(3)
        mock_pool = MagicMock()
        ingest_calls = []

        async def ingest_cb(uid, path):
            ingest_calls.append(str(uid).replace("-", ""))

        sched_mod.set_context(mock_pool, ingest_cb)

        expected = {
            uid_1.replace("-", ""): {"input_folder": "/data/c1"},
            uid_2.replace("-", ""): {"input_folder": "/data/c2"},
            uid_3.replace("-", ""): {"input_folder": "/data/c3"},
        }

        async def get_config(pool, uid):
            return expected.get(str(uid).replace("-", ""), {})

        path_configs = {
            "/data/c1": {"is_dir": True, "pdfs": [f"/data/c1/v{i}.pdf" for i in range(5)]},
            "/data/c2": {"is_dir": True, "pdfs": [f"/data/c2/v{i}.pdf" for i in range(10)]},
            "/data/c3": {"is_dir": True, "pdfs": [f"/data/c3/v{i}.pdf" for i in range(7)]},
        }
        factory = _path_factory(path_configs)

        with patch("backend.db.get_user_config", side_effect=get_config), \
             patch("backend.db.mark_schedule_ran", new_callable=AsyncMock), \
             patch("pathlib.Path", side_effect=factory):
            await asyncio.gather(
                sched_mod._run_schedule(schedule_id=1, user_id=uid_1),
                sched_mod._run_schedule(schedule_id=2, user_id=uid_2),
                sched_mod._run_schedule(schedule_id=3, user_id=uid_3),
            )

        c1 = ingest_calls.count(uid_1.replace("-", ""))
        c2 = ingest_calls.count(uid_2.replace("-", ""))
        c3 = ingest_calls.count(uid_3.replace("-", ""))
        self.assertEqual(c1, 5)
        self.assertEqual(c2, 10)
        self.assertEqual(c3, 7)
        self.assertEqual(len(ingest_calls), 22)  # 5 + 10 + 7, no extras


# ---------------------------------------------------------------------------
# 6. Fault isolation — one client failure must not block others
# ---------------------------------------------------------------------------

class FaultIsolationTests(unittest.IsolatedAsyncioTestCase):
    """
    Client A has a missing folder / bad config. Client B should still ingest
    its 5 PDFs successfully.
    """

    def setUp(self):
        sched_mod._ctx.clear()

    async def _run_two_clients(
        self,
        uid_a: str, config_a: dict, paths_a: dict,
        uid_b: str, config_b: dict, paths_b: dict,
    ) -> tuple[int, int]:
        mock_pool = MagicMock()
        calls = []

        async def ingest_cb(uid, path):
            calls.append(str(uid).replace("-", ""))

        sched_mod.set_context(mock_pool, ingest_cb)

        all_configs = {
            uid_a.replace("-", ""): config_a,
            uid_b.replace("-", ""): config_b,
        }

        async def get_config(pool, uid):
            return all_configs.get(str(uid).replace("-", ""), {})

        factory = _path_factory({**paths_a, **paths_b})

        with patch("backend.db.get_user_config", side_effect=get_config), \
             patch("backend.db.mark_schedule_ran", new_callable=AsyncMock), \
             patch("pathlib.Path", side_effect=factory):
            await asyncio.gather(
                sched_mod._run_schedule(schedule_id=1, user_id=uid_a),
                sched_mod._run_schedule(schedule_id=2, user_id=uid_b),
            )

        a = calls.count(uid_a.replace("-", ""))
        b = calls.count(uid_b.replace("-", ""))
        return a, b

    async def test_client_a_missing_folder_does_not_block_client_b(self):
        uid_a, uid_b = _uid(10), _uid(11)
        b_pdfs = [f"/data/b/v{i}.pdf" for i in range(5)]

        a_count, b_count = await self._run_two_clients(
            uid_a, {"input_folder": "/data/missing_a"},
            {"/data/missing_a": {"is_dir": False, "pdfs": []}},
            uid_b, {"input_folder": "/data/b"},
            {"/data/b": {"is_dir": True, "pdfs": b_pdfs}},
        )
        self.assertEqual(a_count, 0, "Client A folder missing → 0 ingests")
        self.assertEqual(b_count, 5, "Client B must still ingest its 5 PDFs")

    async def test_client_a_no_folder_config_does_not_block_client_b(self):
        uid_a, uid_b = _uid(12), _uid(13)
        b_pdfs = [f"/data/b2/v{i}.pdf" for i in range(10)]

        a_count, b_count = await self._run_two_clients(
            uid_a, {},                        # A has no input_folder
            {},
            uid_b, {"input_folder": "/data/b2"},
            {"/data/b2": {"is_dir": True, "pdfs": b_pdfs}},
        )
        self.assertEqual(a_count, 0)
        self.assertEqual(b_count, 10)

    async def test_client_a_empty_folder_does_not_affect_client_b_count(self):
        uid_a, uid_b = _uid(14), _uid(15)
        b_pdfs = [f"/data/b3/v{i}.pdf" for i in range(7)]

        a_count, b_count = await self._run_two_clients(
            uid_a, {"input_folder": "/data/a_empty"},
            {"/data/a_empty": {"is_dir": True, "pdfs": []}},
            uid_b, {"input_folder": "/data/b3"},
            {"/data/b3": {"is_dir": True, "pdfs": b_pdfs}},
        )
        self.assertEqual(a_count, 0)
        self.assertEqual(b_count, 7)

    async def test_100_clients_one_missing_folder_rest_ingest(self):
        """
        100 clients fire concurrently. Client 1's folder is missing.
        Clients 2-100 each have 1 PDF and must all ingest successfully.
        """
        mock_pool = MagicMock()
        calls = []

        async def ingest_cb(uid, path):
            calls.append(str(uid).replace("-", ""))

        sched_mod.set_context(mock_pool, ingest_cb)

        uid_1 = _uid(1)
        other_uids = [_uid(i) for i in range(2, 101)]

        path_configs = {}
        for uid in other_uids:
            folder = f"/data/client_{uid[-4:]}"
            path_configs[folder] = {"is_dir": True, "pdfs": [f"{folder}/doc.pdf"]}
        # Client 1's folder is missing
        path_configs["/data/c1_missing"] = {"is_dir": False, "pdfs": []}

        async def get_config(pool, uid):
            uid_s = str(uid).replace("-", "")
            if uid_s == uid_1.replace("-", ""):
                return {"input_folder": "/data/c1_missing"}
            folder = f"/data/client_{uid_s[-4:]}"
            return {"input_folder": folder}

        factory = _path_factory(path_configs)

        tasks = [sched_mod._run_schedule(i, _uid(i)) for i in range(1, 101)]

        with patch("backend.db.get_user_config", side_effect=get_config), \
             patch("backend.db.mark_schedule_ran", new_callable=AsyncMock), \
             patch("pathlib.Path", side_effect=factory):
            await asyncio.gather(*tasks)

        client1_calls = calls.count(uid_1.replace("-", ""))
        total_others = sum(calls.count(u.replace("-", "")) for u in other_uids)

        self.assertEqual(client1_calls, 0, "Client 1's missing folder → 0 ingests")
        self.assertEqual(total_others, 99, "Clients 2-100 each ingest 1 PDF")


if __name__ == "__main__":
    unittest.main()
