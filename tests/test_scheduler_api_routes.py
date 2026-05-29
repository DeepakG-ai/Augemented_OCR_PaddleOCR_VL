"""
test_scheduler_api_routes.py — Route-level tests for scheduler/upload integration.

Closes two gaps left by the DB-unit tests in test_scheduler_edge_cases.py:

  1. UploadConflict409RouteTests  — POST /ingest/ui must return HTTP 409 when
     get_user_is_executing() is True.  The edge-case file only tested the DB
     helper; this file hits the actual FastAPI handler via TestClient.

  2. SchedulerStartLimitRouteTests — POST /api/scheduler/start must return 400
     when the user already has 3 schedules.  The edge-case file only tested
     get_user_schedules returning a list; this file exercises the route guard
     at backend/main.py:2746 (`if len(current) >= _SCHED_MAX`).

conftest.py wires get_current_user → fake admin
  {"id": "00000000-0000-0000-0000-000000000000", "role": "admin"}.

raise_server_exceptions=False is used on both clients so that downstream DB
failures (pool=object()) return 500 rather than raising in the test process —
we only care about 409 / 400 assertions, not what happens after them.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.main as main
from backend import db as db_mod

# conftest.py hard-codes this as the authenticated test user
_TEST_USER_ID = "00000000-0000-0000-0000-000000000000"


def _sched_row(schedule_id: int, user_id: str,
               cron: str = "0 10 * * *", enabled: bool = True,
               is_executing: bool = False) -> dict:
    return {
        "id": schedule_id, "user_id": user_id, "cron_expr": cron,
        "timezone": "UTC", "label": "daily run", "enabled": enabled,
        "is_executing": is_executing, "last_ran_at": None,
    }


# ---------------------------------------------------------------------------
# 1. Upload conflict — actual HTTP 409 from the route handler
# ---------------------------------------------------------------------------

class UploadConflict409RouteTests(unittest.TestCase):
    """
    POST /ingest/ui must return HTTP 409 when the scheduler is executing.

    Unlike UploadConflictLogicTests (which only tests get_user_is_executing at
    the DB level), these tests call the real FastAPI route through TestClient so
    the HTTPException(409) at backend/main.py:1126 is actually exercised.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        main.app.state.pool = object()
        # raise_server_exceptions=False: subsequent DB failures (pool=object())
        # come back as 500 rather than raising — we only assert on 409 / not-409.
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def _post_ingest(self, source_type: str = "ui"):
        return self.client.post(
            f"/ingest/{source_type}",
            files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
        )

    # -- Blocking path --------------------------------------------------------

    def test_executing_true_returns_409(self):
        """Scheduler running → POST /ingest/ui must return 409."""
        with patch.object(db_mod, "get_user_is_executing", new=AsyncMock(return_value=True)):
            resp = self._post_ingest("ui")
        self.assertEqual(resp.status_code, 409)

    def test_409_detail_names_scheduler(self):
        """The 409 body must mention 'Scheduler' so the user understands why."""
        with patch.object(db_mod, "get_user_is_executing", new=AsyncMock(return_value=True)):
            resp = self._post_ingest("ui")
        self.assertIn("Scheduler", resp.json().get("error", {}).get("message", ""))

    def test_409_is_not_raised_for_empty_detail(self):
        """Sanity: the 409 response body has a non-empty detail field."""
        with patch.object(db_mod, "get_user_is_executing", new=AsyncMock(return_value=True)):
            resp = self._post_ingest("ui")
        self.assertTrue(resp.json().get("error", {}).get("message", "").strip())

    # -- Non-blocking path ----------------------------------------------------

    def test_not_executing_does_not_return_409(self):
        """get_user_is_executing=False → check passes, response is NOT 409."""
        with patch.object(db_mod, "get_user_is_executing", new=AsyncMock(return_value=False)):
            resp = self._post_ingest("ui")
        self.assertNotEqual(resp.status_code, 409)

    def test_non_ui_source_is_never_checked(self):
        """source_type='rest' must skip the executing check entirely."""
        mock_fn = AsyncMock(return_value=True)  # executing=True, but must be ignored
        with patch.object(db_mod, "get_user_is_executing", new=mock_fn):
            resp = self._post_ingest("rest")
        # Must not be 409 — the check does not apply to non-UI uploads
        self.assertNotEqual(resp.status_code, 409)
        # The function must never have been called for a non-UI source_type
        mock_fn.assert_not_called()

    def test_check_uses_authenticated_user_id(self):
        """The executing check must query for the authenticated user's ID."""
        captured_uid = []

        async def _spy(pool, user_id):
            captured_uid.append(user_id)
            return True  # block so we get a clean 409 exit

        with patch.object(db_mod, "get_user_is_executing", side_effect=_spy):
            self.client.post(
                "/ingest/ui",
                files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
            )

        self.assertEqual(len(captured_uid), 1)
        # conftest.py's fake user has id _TEST_USER_ID
        self.assertEqual(captured_uid[0], _TEST_USER_ID)


# ---------------------------------------------------------------------------
# 2. Scheduler start — 3-schedule limit enforced at the route level
# ---------------------------------------------------------------------------

class SchedulerStartLimitRouteTests(unittest.TestCase):
    """
    POST /api/scheduler/start must return HTTP 400 when the user already has
    _SCHED_MAX (3) schedules.

    Unlike MultiScheduleTests (which tests get_user_schedules returning a list),
    these tests exercise the `if len(current) >= _SCHED_MAX` guard at
    backend/main.py:2746 via the real route handler.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        main.app.state.pool = object()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    # -- Limit enforcement ----------------------------------------------------

    def test_fourth_schedule_returns_400(self):
        """3 existing schedules → creating a 4th returns 400."""
        full = [_sched_row(i, _TEST_USER_ID) for i in range(1, 4)]
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=full)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 9, "minute": 0})
        self.assertEqual(resp.status_code, 400)

    def test_400_detail_mentions_maximum_and_count(self):
        """400 detail must say 'Maximum' and '3' so the cap is clear to the user."""
        full = [_sched_row(i, _TEST_USER_ID) for i in range(1, 4)]
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=full)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 9, "minute": 0})
        error_body = resp.json().get("error", {})
        message = error_body.get("message", "")
        self.assertIn("Maximum", message)
        self.assertIn("3", message)

    def test_exactly_three_existing_blocks_new_create(self):
        """At the limit (3) any new schedule attempt is blocked."""
        full = [_sched_row(i, _TEST_USER_ID) for i in range(1, 4)]
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=full)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 22, "minute": 0})
        self.assertEqual(resp.status_code, 400)

    # -- Allowed paths --------------------------------------------------------

    def test_two_existing_allows_third(self):
        """2 schedules → creating a 3rd returns 200."""
        two = [_sched_row(i, _TEST_USER_ID) for i in range(1, 3)]
        new_row = _sched_row(3, _TEST_USER_ID, "0 9 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=two)), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 9, "minute": 0})
        self.assertEqual(resp.status_code, 200)

    def test_zero_existing_allows_first(self):
        """No schedules yet → first create succeeds (200)."""
        new_row = _sched_row(1, _TEST_USER_ID, "0 8 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[])), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 8, "minute": 0})
        self.assertEqual(resp.status_code, 200)

    def test_update_existing_skips_limit_check(self):
        """
        schedule_id in body → update path; the 3-schedule cap must NOT block the request.
        get_user_schedules is called for the duplicate-time check (not for the cap).
        """
        existing_row = _sched_row(1, _TEST_USER_ID, "0 10 * * *")
        updated_row  = _sched_row(1, _TEST_USER_ID, "0 12 * * *")

        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=existing_row)), \
             patch.object(db_mod, "update_user_schedule", new=AsyncMock(return_value=updated_row)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[existing_row])):
            resp = self.client.post(
                "/api/scheduler/start",
                json={"hour": 12, "minute": 0, "schedule_id": 1},
            )

        # Update path is not blocked by the 3-schedule cap
        self.assertEqual(resp.status_code, 200)

    # -- Input validation (exercised at the route, not just the DB layer) -----

    def test_hour_above_23_returns_400(self):
        """hour=25 must be rejected before any DB call."""
        mock_db = AsyncMock()
        with patch.object(db_mod, "get_user_schedules", new=mock_db):
            resp = self.client.post("/api/scheduler/start", json={"hour": 25, "minute": 0})
        self.assertEqual(resp.status_code, 400)
        mock_db.assert_not_called()

    def test_minute_above_59_returns_400(self):
        """minute=60 must be rejected before any DB call."""
        mock_db = AsyncMock()
        with patch.object(db_mod, "get_user_schedules", new=mock_db):
            resp = self.client.post("/api/scheduler/start", json={"hour": 8, "minute": 60})
        self.assertEqual(resp.status_code, 400)
        mock_db.assert_not_called()

    def test_negative_hour_returns_400(self):
        """hour=-1 must be rejected before any DB call."""
        resp = self.client.post("/api/scheduler/start", json={"hour": -1, "minute": 0})
        self.assertEqual(resp.status_code, 400)

    def test_negative_minute_returns_400(self):
        """minute=-1 must be rejected before any DB call."""
        resp = self.client.post("/api/scheduler/start", json={"hour": 8, "minute": -1})
        self.assertEqual(resp.status_code, 400)

    def test_boundary_hour_23_is_valid(self):
        """hour=23 is at the boundary and must be accepted."""
        new_row = _sched_row(1, _TEST_USER_ID, "0 23 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[])), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 23, "minute": 0})
        self.assertNotEqual(resp.status_code, 400)

    def test_boundary_minute_59_is_valid(self):
        """minute=59 is at the boundary and must be accepted."""
        new_row = _sched_row(1, _TEST_USER_ID, "59 8 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[])), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 8, "minute": 59})
        self.assertNotEqual(resp.status_code, 400)


class SchedulerRunningRouteTests(unittest.TestCase):
    """The client agent marks schedules running only while it scans/uploads."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        main.app.state.pool = object()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def test_running_endpoint_sets_execution_flag(self):
        existing = _sched_row(7, _TEST_USER_ID, enabled=True)
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=existing)), \
             patch.object(db_mod, "set_schedule_executing", new=AsyncMock()) as mock_set:
            resp = self.client.post("/api/scheduler/7/running")

        self.assertEqual(resp.status_code, 200)
        mock_set.assert_awaited_once_with(unittest.mock.ANY, 7, True)

    def test_running_endpoint_rejects_disabled_schedule(self):
        existing = _sched_row(7, _TEST_USER_ID, enabled=False)
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=existing)), \
             patch.object(db_mod, "set_schedule_executing", new=AsyncMock()) as mock_set:
            resp = self.client.post("/api/scheduler/7/running")

        self.assertEqual(resp.status_code, 409)
        mock_set.assert_not_called()


class ScheduleCrudLimitRouteTests(unittest.TestCase):
    """The older /api/schedules create path must also honor the max-3 cap."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        main.app.state.pool = object()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def test_crud_create_fourth_schedule_returns_400(self):
        full = [_sched_row(i, _TEST_USER_ID) for i in range(1, 4)]
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=full)), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock()) as mock_create:
            resp = self.client.post(
                "/api/schedules",
                json={"cron_expr": "0 9 * * *", "timezone": "UTC", "label": "extra"},
            )

        self.assertEqual(resp.status_code, 400)
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Duplicate schedule time — same user cannot have two schedules at the same time
# ---------------------------------------------------------------------------

class DuplicateScheduleTimeRouteTests(unittest.TestCase):
    """
    No two schedules for the same user can share the same fire time.

    Covers four paths:
      A. POST /api/scheduler/start (create)   — main UI route
      B. POST /api/scheduler/start (update)   — same route with schedule_id
      C. POST /api/schedules       (create)   — older CRUD route
      D. PUT  /api/schedules/{id}  (update)   — older CRUD update
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        main.app.state.pool = object()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    # ── A. POST /api/scheduler/start (create) ────────────────────────────────

    def test_create_duplicate_time_returns_400(self):
        """User already has 10:20 → creating another 10:20 returns 400."""
        existing = [_sched_row(1, _TEST_USER_ID, cron="20 10 * * *")]
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=existing)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 10, "minute": 20})
        self.assertEqual(resp.status_code, 400)

    def test_create_duplicate_400_detail_mentions_time(self):
        """400 body must contain the conflicting time (10:20) so the user knows what clashed."""
        existing = [_sched_row(1, _TEST_USER_ID, cron="20 10 * * *")]
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=existing)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 10, "minute": 20})
        self.assertIn("10:20", resp.json().get("error", {}).get("message", ""))

    def test_create_different_time_is_allowed(self):
        """User has 10:20 → creating at 11:30 is a different time and must not return 400."""
        existing = [_sched_row(1, _TEST_USER_ID, cron="20 10 * * *")]
        new_row   = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=existing)), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 11, "minute": 30})
        self.assertNotEqual(resp.status_code, 400)

    def test_create_duplicate_check_runs_before_insert(self):
        """create_user_schedule must NOT be called when the time is already taken."""
        existing = [_sched_row(1, _TEST_USER_ID, cron="20 10 * * *")]
        mock_create = AsyncMock()
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=existing)), \
             patch.object(db_mod, "create_user_schedule", new=mock_create):
            self.client.post("/api/scheduler/start", json={"hour": 10, "minute": 20})
        mock_create.assert_not_called()

    def test_create_zero_existing_allows_any_time(self):
        """No existing schedules → any time is unique and must not return 400."""
        new_row = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[])), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post("/api/scheduler/start", json={"hour": 10, "minute": 20})
        self.assertNotEqual(resp.status_code, 400)

    # ── B. POST /api/scheduler/start (update with schedule_id) ───────────────

    def test_update_to_duplicate_time_returns_400(self):
        """Schedule 1=10:20, Schedule 2=11:30. Updating Schedule 2 to 10:20 returns 400."""
        sched1 = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        sched2 = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched2)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1, sched2])):
            resp = self.client.post(
                "/api/scheduler/start", json={"hour": 10, "minute": 20, "schedule_id": 2}
            )
        self.assertEqual(resp.status_code, 400)

    def test_update_to_duplicate_time_check_runs_before_save(self):
        """update_user_schedule must NOT be called when the target time is already taken."""
        sched1 = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        sched2 = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        mock_update = AsyncMock()
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched2)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1, sched2])), \
             patch.object(db_mod, "update_user_schedule", new=mock_update):
            self.client.post(
                "/api/scheduler/start", json={"hour": 10, "minute": 20, "schedule_id": 2}
            )
        mock_update.assert_not_called()

    def test_update_same_schedule_to_its_own_time_is_allowed(self):
        """Updating Schedule 1 to 10:20 when it is already 10:20 must not conflict with itself."""
        sched1  = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        updated = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched1)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1])), \
             patch.object(db_mod, "update_user_schedule", new=AsyncMock(return_value=updated)):
            resp = self.client.post(
                "/api/scheduler/start", json={"hour": 10, "minute": 20, "schedule_id": 1}
            )
        self.assertNotEqual(resp.status_code, 400)

    def test_update_to_unique_time_is_allowed(self):
        """Schedule 1=10:20, Schedule 2=11:30. Updating Schedule 2 to 14:00 must not return 400."""
        sched1  = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        sched2  = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        updated = _sched_row(2, _TEST_USER_ID, cron="0 14 * * *")
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched2)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1, sched2])), \
             patch.object(db_mod, "update_user_schedule", new=AsyncMock(return_value=updated)):
            resp = self.client.post(
                "/api/scheduler/start", json={"hour": 14, "minute": 0, "schedule_id": 2}
            )
        self.assertNotEqual(resp.status_code, 400)

    # ── C. POST /api/schedules (older CRUD create) ────────────────────────────

    def test_crud_create_duplicate_cron_returns_400(self):
        """POST /api/schedules with a cron_expr already used by the same user → 400."""
        existing = [_sched_row(1, _TEST_USER_ID, cron="20 10 * * *")]
        mock_create = AsyncMock()
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=existing)), \
             patch.object(db_mod, "create_user_schedule", new=mock_create):
            resp = self.client.post(
                "/api/schedules",
                json={"cron_expr": "20 10 * * *", "timezone": "UTC", "label": "dup"},
            )
        self.assertEqual(resp.status_code, 400)
        mock_create.assert_not_called()

    def test_crud_create_unique_cron_returns_201(self):
        """POST /api/schedules with a new cron_expr returns 201."""
        existing = [_sched_row(1, _TEST_USER_ID, cron="20 10 * * *")]
        new_row   = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        with patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=existing)), \
             patch.object(db_mod, "create_user_schedule", new=AsyncMock(return_value=new_row)):
            resp = self.client.post(
                "/api/schedules",
                json={"cron_expr": "30 11 * * *", "timezone": "UTC", "label": "new"},
            )
        self.assertEqual(resp.status_code, 201)

    # ── D. PUT /api/schedules/{id} (older CRUD update) ────────────────────────

    def test_crud_update_cron_to_duplicate_returns_400(self):
        """PUT /api/schedules/2 changing cron to one already used by schedule 1 → 400."""
        sched1 = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        sched2 = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        mock_update = AsyncMock()
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched2)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1, sched2])), \
             patch.object(db_mod, "update_user_schedule", new=mock_update):
            resp = self.client.put(
                "/api/schedules/2",
                json={"cron_expr": "20 10 * * *"},
            )
        self.assertEqual(resp.status_code, 400)
        mock_update.assert_not_called()

    def test_crud_update_non_cron_field_skips_duplicate_check(self):
        """PUT /api/schedules/1 changing only the label must not trigger the time-conflict check."""
        sched1 = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        updated = dict(sched1, label="renamed")
        mock_get_all = AsyncMock()
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched1)), \
             patch.object(db_mod, "get_user_schedules", new=mock_get_all), \
             patch.object(db_mod, "update_user_schedule", new=AsyncMock(return_value=updated)):
            self.client.put("/api/schedules/1", json={"label": "renamed"})
        mock_get_all.assert_not_called()

    def test_crud_update_cron_to_own_value_is_allowed(self):
        """PUT /api/schedules/1 setting the same cron_expr it already has must not block."""
        sched1 = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched1)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1])), \
             patch.object(db_mod, "update_user_schedule", new=AsyncMock(return_value=sched1)):
            resp = self.client.put(
                "/api/schedules/1",
                json={"cron_expr": "20 10 * * *"},
            )
        self.assertNotEqual(resp.status_code, 400)

    def test_crud_update_cron_to_unique_time_is_allowed(self):
        """PUT /api/schedules/2 changing to a time nobody else uses must not return 400."""
        sched1  = _sched_row(1, _TEST_USER_ID, cron="20 10 * * *")
        sched2  = _sched_row(2, _TEST_USER_ID, cron="30 11 * * *")
        updated = _sched_row(2, _TEST_USER_ID, cron="0 14 * * *")
        with patch.object(db_mod, "get_schedule", new=AsyncMock(return_value=sched2)), \
             patch.object(db_mod, "get_user_schedules", new=AsyncMock(return_value=[sched1, sched2])), \
             patch.object(db_mod, "update_user_schedule", new=AsyncMock(return_value=updated)):
            resp = self.client.put(
                "/api/schedules/2",
                json={"cron_expr": "0 14 * * *"},
            )
        self.assertNotEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
