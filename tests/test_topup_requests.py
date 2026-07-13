"""
Tests for the user-initiated top-up request feature.

Covers:
  DB layer:
    - create_topup_request: invalid UUID, valid creation
    - list_topup_requests: with/without status filter
    - list_topup_requests_for_user: invalid UUID short-circuit, normal path
    - get_topup_request: returns None for unknown ID
    - resolve_topup_request: approve and reject paths, already-resolved guard
    - get_pending_topup_request_count: returns integer count

  API layer (client):
    POST /me/topup-requests
      - 201 on valid request (any positive integer pages, free-text period)
      - 403 when admin tries to submit
      - 422 on invalid pages (zero / negative / decimal / over-limit)
      - 422 on invalid period (empty / whitespace-only)
      - 422 on missing required fields
      - Pydantic v2 coerce_pages validator: string "1500" coerced to int
    GET /me/topup-requests
      - 200 with list for calling user
      - 401 when unauthenticated

  API layer (admin):
    GET  /admin/topup-requests
      - 200 returns all requests
      - 200 with ?status=pending filters correctly
      - 403 for client users
    GET  /admin/topup-requests/count
      - 200 returns {"pending": N}
      - 403 for client users
    POST /admin/topup-requests/{id}/approve
      - 200 auto-applies topup + marks request approved
      - 404 when request not found
      - 409 when request already resolved
      - 409 when user has no active subscription
      - 403 for client users
    POST /admin/topup-requests/{id}/reject
      - 200 marks request rejected
      - 404 when request not found
      - 409 when request already resolved
      - 403 for client users
"""
from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.db as db_mod
import backend.main as main
from backend.auth import get_current_user, require_admin
from backend.models import TopupRequestCreate

# ── Shared test constants ────────────────────────────────────────────────────

_CLIENT_UUID = "11111111-1111-1111-1111-111111111111"
_ADMIN_UUID  = "00000000-0000-0000-0000-000000000000"
_BAD_UUID    = "not-a-uuid"
_REQ_ID      = 42


# ── Pool / connection helpers (same pattern as test_subscriptions.py) ────────

@asynccontextmanager
async def _fake_acquire(conn):
    yield conn


@asynccontextmanager
async def _fake_txn():
    yield


def _make_pool(conn=None):
    pool = MagicMock()
    conn = conn or AsyncMock()
    conn.transaction = MagicMock(side_effect=lambda: _fake_txn())
    pool.acquire.return_value = _fake_acquire(conn)
    return pool, conn


# ── Row builders ─────────────────────────────────────────────────────────────

def _req_row(
    req_id: int = _REQ_ID,
    user_id: str = _CLIENT_UUID,
    pages: int = 1000,
    period: str = "6 months",
    status: str = "pending",
    resolution_note: str | None = None,
    resolved_by: str | None = None,
    resolved_at=None,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": req_id,
        "user_id": user_id,
        "user_email": "client@x.com",
        "requested_pages": pages,
        "requested_period": period,
        "note": None,
        "status": status,
        "resolution_note": resolution_note,
        "resolved_by": resolved_by,
        "resolved_by_email": None,
        "resolved_at": resolved_at,
        "created_at": now,
    }


def _user_row(uid: str = _CLIENT_UUID, role: str = "client") -> dict:
    return {
        "id": uid,
        "email": f"{uid[:6]}@x.com",
        "role": role,
        "is_active": True,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "subscription_limit": 0,
    }


def _topup_row() -> dict:
    return {
        "id": 99,
        "user_id": _CLIENT_UUID,
        "subscription_id": 7,
        "pages": 1000,
        "note": "Approved top-up request #42",
        "created_by": _ADMIN_UUID,
        "created_at": datetime.now(timezone.utc),
    }


def _admin_user() -> dict:
    return {"id": _ADMIN_UUID, "role": "admin", "email": "admin@x.com"}


def _client_user() -> dict:
    return {"id": _CLIENT_UUID, "role": "client", "email": "client@x.com"}


class _ScopedOverrides:
    """Temporarily replace FastAPI dependency overrides, restoring them on exit."""
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


# ═════════════════════════════════════════════════════════════════════════════
# 1.  DB LAYER TESTS
# ═════════════════════════════════════════════════════════════════════════════


class CreateTopupRequestTests(unittest.IsolatedAsyncioTestCase):
    """db.create_topup_request"""

    async def test_invalid_uuid_raises_value_error(self) -> None:
        pool, _ = _make_pool()
        with self.assertRaises(ValueError):
            await db_mod.create_topup_request(
                pool, _BAD_UUID, requested_pages=1000,
                requested_period="6 months",
            )

    async def test_valid_request_inserts_and_returns_row(self) -> None:
        pool, conn = _make_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow.return_value = {
            "id": 1,
            "user_id": _CLIENT_UUID,
            "requested_pages": 500,
            "requested_period": "3 months",
            "note": "High volume",
            "status": "pending",
            "resolution_note": None,
            "resolved_by": None,
            "resolved_at": None,
            "created_at": now,
        }
        result = await db_mod.create_topup_request(
            pool, _CLIENT_UUID,
            requested_pages=500,
            requested_period="3 months",
            note="High volume",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["requested_pages"], 500)
        self.assertEqual(result["requested_period"], "3 months")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["note"], "High volume")

    async def test_user_id_stringified_in_output(self) -> None:
        """UUIDs returned from asyncpg should be converted to str."""
        pool, conn = _make_pool()
        import uuid as _uuid_mod
        raw_uuid = _uuid_mod.UUID(_CLIENT_UUID)
        conn.fetchrow.return_value = {
            "id": 2,
            "user_id": raw_uuid,          # asyncpg returns UUID object
            "requested_pages": 1000,
            "requested_period": "1 year",
            "note": None,
            "status": "pending",
            "resolution_note": None,
            "resolved_by": None,
            "resolved_at": None,
            "created_at": datetime.now(timezone.utc),
        }
        result = await db_mod.create_topup_request(
            pool, _CLIENT_UUID, requested_pages=1000, requested_period="1 year",
        )
        self.assertIsInstance(result["user_id"], str,
                              "user_id must be serialised to str")

    async def test_request_without_note_succeeds(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow.return_value = {
            "id": 3, "user_id": _CLIENT_UUID,
            "requested_pages": 2000, "requested_period": "1 month",
            "note": None, "status": "pending",
            "resolution_note": None, "resolved_by": None,
            "resolved_at": None, "created_at": datetime.now(timezone.utc),
        }
        result = await db_mod.create_topup_request(
            pool, _CLIENT_UUID, requested_pages=2000, requested_period="1 month",
        )
        self.assertIsNone(result["note"])


class ListTopupRequestsTests(unittest.IsolatedAsyncioTestCase):
    """db.list_topup_requests"""

    async def test_no_status_filter_fetches_all(self) -> None:
        pool, conn = _make_pool()
        conn.fetch.return_value = []
        await db_mod.list_topup_requests(pool, status=None)
        query = conn.fetch.call_args.args[0]
        self.assertNotIn("WHERE", query.upper().replace("LEFT JOIN", ""),
                         "No WHERE clause expected when status=None")

    async def test_status_filter_applied(self) -> None:
        pool, conn = _make_pool()
        conn.fetch.return_value = []
        await db_mod.list_topup_requests(pool, status="pending")
        args = conn.fetch.call_args.args
        self.assertIn("pending", args,
                      "status value must be passed as a query parameter")

    async def test_returns_list_of_dicts(self) -> None:
        pool, conn = _make_pool()
        now = datetime.now(timezone.utc)
        conn.fetch.return_value = [
            {
                "id": 1, "user_id": _CLIENT_UUID,
                "user_email": "client@x.com", "resolved_by_email": None,
                "requested_pages": 1000, "requested_period": "6 months",
                "note": None, "status": "pending",
                "resolution_note": None, "resolved_by": None,
                "resolved_at": None, "created_at": now,
            }
        ]
        results = await db_mod.list_topup_requests(pool)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "pending")


class ListTopupRequestsForUserTests(unittest.IsolatedAsyncioTestCase):
    """db.list_topup_requests_for_user"""

    async def test_invalid_uuid_returns_empty_list(self) -> None:
        pool, _ = _make_pool()
        result = await db_mod.list_topup_requests_for_user(pool, _BAD_UUID)
        self.assertEqual(result, [])

    async def test_queries_by_user_id(self) -> None:
        pool, conn = _make_pool()
        conn.fetch.return_value = []
        await db_mod.list_topup_requests_for_user(pool, _CLIENT_UUID)
        self.assertTrue(conn.fetch.called)
        args = conn.fetch.call_args.args
        # The UUID should be passed as a parameter (not interpolated)
        import uuid as _uuid_mod
        expected = _uuid_mod.UUID(_CLIENT_UUID)
        self.assertIn(expected, args,
                      "user_id must be passed as a parameterised query arg")


class GetTopupRequestTests(unittest.IsolatedAsyncioTestCase):
    """db.get_topup_request"""

    async def test_returns_none_when_not_found(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow.return_value = None
        result = await db_mod.get_topup_request(pool, 9999)
        self.assertIsNone(result)

    async def test_returns_dict_when_found(self) -> None:
        pool, conn = _make_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow.return_value = {
            "id": _REQ_ID, "user_id": _CLIENT_UUID,
            "user_email": "client@x.com", "resolved_by_email": None,
            "requested_pages": 1000, "requested_period": "1 year",
            "note": None, "status": "pending",
            "resolution_note": None, "resolved_by": None,
            "resolved_at": None, "created_at": now,
        }
        result = await db_mod.get_topup_request(pool, _REQ_ID)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], _REQ_ID)
        self.assertEqual(result["status"], "pending")


class ResolveTopupRequestTests(unittest.IsolatedAsyncioTestCase):
    """db.resolve_topup_request"""

    async def test_approve_returns_updated_row(self) -> None:
        pool, conn = _make_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow.return_value = {
            "id": _REQ_ID, "user_id": _CLIENT_UUID,
            "requested_pages": 1000, "requested_period": "6 months",
            "note": None, "status": "approved",
            "resolution_note": "Looks good",
            "resolved_by": _ADMIN_UUID,
            "resolved_at": now, "created_at": now,
        }
        result = await db_mod.resolve_topup_request(
            pool, _REQ_ID, resolved_by=_ADMIN_UUID,
            status="approved", resolution_note="Looks good",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["resolution_note"], "Looks good")

    async def test_reject_returns_updated_row(self) -> None:
        pool, conn = _make_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow.return_value = {
            "id": _REQ_ID, "user_id": _CLIENT_UUID,
            "requested_pages": 500, "requested_period": "1 month",
            "note": None, "status": "rejected",
            "resolution_note": "Wait until next cycle",
            "resolved_by": _ADMIN_UUID,
            "resolved_at": now, "created_at": now,
        }
        result = await db_mod.resolve_topup_request(
            pool, _REQ_ID, resolved_by=_ADMIN_UUID,
            status="rejected", resolution_note="Wait until next cycle",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "rejected")

    async def test_returns_none_when_already_resolved(self) -> None:
        """UPDATE ... WHERE status = 'pending' returns nothing for a resolved row."""
        pool, conn = _make_pool()
        conn.fetchrow.return_value = None   # no matching pending row
        result = await db_mod.resolve_topup_request(
            pool, _REQ_ID, resolved_by=_ADMIN_UUID, status="approved",
        )
        self.assertIsNone(result)

    async def test_query_guards_on_pending_status(self) -> None:
        """The UPDATE must include a WHERE status = 'pending' guard."""
        pool, conn = _make_pool()
        conn.fetchrow.return_value = None
        await db_mod.resolve_topup_request(
            pool, _REQ_ID, resolved_by=_ADMIN_UUID, status="approved",
        )
        query = conn.fetchrow.call_args.args[0]
        self.assertIn("pending", query.lower(),
                      "UPDATE must guard against double-resolution via WHERE status='pending'")


class GetPendingTopupCountTests(unittest.IsolatedAsyncioTestCase):
    """db.get_pending_topup_request_count"""

    async def test_returns_integer_count(self) -> None:
        pool, conn = _make_pool()
        conn.fetchval.return_value = 3
        count = await db_mod.get_pending_topup_request_count(pool)
        self.assertEqual(count, 3)

    async def test_returns_zero_when_none_pending(self) -> None:
        pool, conn = _make_pool()
        conn.fetchval.return_value = 0
        count = await db_mod.get_pending_topup_request_count(pool)
        self.assertEqual(count, 0)

    async def test_handles_none_from_db(self) -> None:
        """fetchval can return None for COUNT(*) on an empty table."""
        pool, conn = _make_pool()
        conn.fetchval.return_value = None
        count = await db_mod.get_pending_topup_request_count(pool)
        self.assertEqual(count, 0)


# ═════════════════════════════════════════════════════════════════════════════
# 2.  PYDANTIC MODEL VALIDATOR TESTS (no HTTP, pure model)
# ═════════════════════════════════════════════════════════════════════════════


class TopupRequestModelValidatorTests(unittest.TestCase):
    """
    Tests for TopupRequestCreate Pydantic v2 validators exercised directly on
    the model — no HTTP stack, no middleware interference.
    """

    def _make(self, pages=1000, period="1 month", note=None):
        return TopupRequestCreate(
            requested_pages=pages,
            requested_period=period,
            note=note,
        )

    # ── requested_pages ──────────────────────────────────────────────────────

    def test_any_positive_integer_accepted(self) -> None:
        from pydantic import ValidationError
        for pages in (1, 7, 250, 999, 3333, 1_000_000, 9_999_999, 10_000_000):
            m = self._make(pages=pages)
            self.assertEqual(m.requested_pages, pages,
                             f"pages={pages} round-trips incorrectly")

    def test_zero_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(pages=0)

    def test_negative_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(pages=-1)

    def test_over_maximum_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(pages=10_000_001)

    def test_decimal_float_rejected_by_coerce_pages(self) -> None:
        """1.5 has a fractional part — coerce_pages must reject it."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            self._make(pages=1.5)
        err_str = str(ctx.exception)
        self.assertIn("whole number", err_str,
                      "Validator message should mention 'whole number'")

    def test_whole_float_accepted(self) -> None:
        """2.0 is a whole number — coerce_pages should accept and cast to int."""
        m = self._make(pages=2.0)
        self.assertEqual(m.requested_pages, 2)
        self.assertIsInstance(m.requested_pages, int)

    def test_numeric_string_coerced_to_int(self) -> None:
        """coerce_pages converts '1500' → 1500 (form-style callers)."""
        m = self._make(pages="1500")
        self.assertEqual(m.requested_pages, 1500)
        self.assertIsInstance(m.requested_pages, int)

    def test_non_numeric_string_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(pages="lots")

    # ── requested_period ─────────────────────────────────────────────────────

    def test_any_non_empty_period_accepted(self) -> None:
        for period in ("1 month", "3 months", "6 months", "1 year",
                       "Q3 extension", "18 months", "until project ends",
                       "2 years", "90 days", "custom label"):
            m = self._make(period=period)
            self.assertEqual(m.requested_period, period)

    def test_empty_period_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(period="")

    def test_whitespace_only_period_stripped_then_rejected(self) -> None:
        """strip_period strips whitespace; empty result fails min_length=1."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(period="   ")

    def test_period_leading_trailing_whitespace_stripped(self) -> None:
        """strip_period normalises '  1 year  ' → '1 year'."""
        m = self._make(period="  1 year  ")
        self.assertEqual(m.requested_period, "1 year")

    def test_period_max_length_enforced(self) -> None:
        """Period strings longer than 64 chars are rejected."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(period="x" * 65)

    # ── note ─────────────────────────────────────────────────────────────────

    def test_note_is_optional(self) -> None:
        m = self._make(note=None)
        self.assertIsNone(m.note)

    def test_note_max_length_enforced(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make(note="x" * 501)


# ═════════════════════════════════════════════════════════════════════════════
# 3.  API LAYER — CLIENT ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════


class ClientCreateTopupRequestTests(unittest.TestCase):
    """POST /me/topup-requests"""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        main.app.dependency_overrides.pop(get_current_user, None)

    def test_valid_request_returns_201(self) -> None:
        with patch.object(main.db_mod, "create_topup_request",
                          new=AsyncMock(return_value=_req_row())):
            r = self.client.post(
                "/me/topup-requests",
                json={
                    "requested_pages": 1000,
                    "requested_period": "6 months",
                    "note": "End-of-quarter spike",
                },
            )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["requested_pages"], 1000)
        self.assertEqual(body["status"], "pending")

    def test_request_without_note_returns_201(self) -> None:
        with patch.object(main.db_mod, "create_topup_request",
                          new=AsyncMock(return_value=_req_row())):
            r = self.client.post(
                "/me/topup-requests",
                json={"requested_pages": 500, "requested_period": "1 month"},
            )
        self.assertEqual(r.status_code, 201, r.text)

    def test_zero_pages_returns_422(self) -> None:
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 0, "requested_period": "1 month"},
        )
        self.assertEqual(r.status_code, 422)

    def test_negative_pages_returns_422(self) -> None:
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": -100, "requested_period": "6 months"},
        )
        self.assertEqual(r.status_code, 422)

    def test_missing_requested_period_returns_422(self) -> None:
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 1000},
        )
        self.assertEqual(r.status_code, 422)

    def test_missing_requested_pages_returns_422(self) -> None:
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_period": "1 year"},
        )
        self.assertEqual(r.status_code, 422)

    def test_arbitrary_page_counts_accepted(self) -> None:
        """Any positive integer up to 10 000 000 is accepted — no preset list."""
        for pages in (1, 750, 3333, 99999, 1_000_001, 7_500_000):
            with patch.object(main.db_mod, "create_topup_request",
                              new=AsyncMock(return_value=_req_row(pages=pages))):
                r = self.client.post(
                    "/me/topup-requests",
                    json={"requested_pages": pages, "requested_period": "3 months"},
                )
            self.assertEqual(r.status_code, 201,
                             f"pages={pages} should be valid but got {r.status_code}")

    def test_pages_exceeding_maximum_rejected(self) -> None:
        """Pages > 10 000 000 must be rejected (Pydantic le=10_000_000 constraint)."""
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 10_000_001, "requested_period": "1 year"},
        )
        self.assertEqual(r.status_code, 422,
                         "pages above 10 000 000 should be rejected")

    def test_float_pages_with_decimals_rejected(self) -> None:
        """Decimal floats like 1.5 must be rejected by coerce_pages validator."""
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 1.5, "requested_period": "1 month"},
        )
        self.assertEqual(r.status_code, 422,
                         "float with decimal fraction should be rejected")

    def test_string_pages_coerced_to_int(self) -> None:
        """coerce_pages must convert numeric strings like '1500' to int."""
        with patch.object(main.db_mod, "create_topup_request",
                          new=AsyncMock(return_value=_req_row(pages=1500))):
            r = self.client.post(
                "/me/topup-requests",
                # FastAPI will deserialise the JSON int, but validator handles str too
                json={"requested_pages": 1500, "requested_period": "2 months"},
            )
        self.assertEqual(r.status_code, 201,
                         "numeric page value should be accepted")
        self.assertEqual(r.json()["requested_pages"], 1500)

    def test_custom_period_strings_accepted(self) -> None:
        """Any non-empty free-text period label is valid — no preset list."""
        for period in ("Q3 extension", "18 months", "2 years", "until project ends"):
            with patch.object(main.db_mod, "create_topup_request",
                              new=AsyncMock(return_value=_req_row(period=period))):
                r = self.client.post(
                    "/me/topup-requests",
                    json={"requested_pages": 1000, "requested_period": period},
                )
            self.assertEqual(r.status_code, 201,
                             f"period='{period}' should be valid but got {r.status_code}")

    def test_empty_period_rejected(self) -> None:
        """Empty string is rejected (min_length=1)."""
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 1000, "requested_period": ""},
        )
        self.assertEqual(r.status_code, 422,
                         "empty period string should be rejected")

    def test_whitespace_only_period_rejected(self) -> None:
        """Whitespace-only period is stripped to '' by strip_period, then rejected."""
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 1000, "requested_period": "   "},
        )
        self.assertEqual(r.status_code, 422,
                         "whitespace-only period should be stripped then rejected")


class AdminCannotSubmitTopupRequestTests(unittest.TestCase):
    """Admin users must not be able to submit top-up requests (403)."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.dependency_overrides[get_current_user] = lambda: _admin_user()
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        main.app.dependency_overrides.pop(get_current_user, None)

    def test_admin_submit_returns_403(self) -> None:
        r = self.client.post(
            "/me/topup-requests",
            json={"requested_pages": 1000, "requested_period": "1 year"},
        )
        self.assertEqual(r.status_code, 403)


class ClientListMyTopupRequestsTests(unittest.TestCase):
    """GET /me/topup-requests"""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        main.app.dependency_overrides[get_current_user] = lambda: _client_user()
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        main.app.dependency_overrides.pop(get_current_user, None)

    def test_returns_200_with_list(self) -> None:
        with patch.object(main.db_mod, "list_topup_requests_for_user",
                          new=AsyncMock(return_value=[_req_row()])):
            r = self.client.get("/me/topup-requests")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 1)

    def test_returns_empty_list_when_no_requests(self) -> None:
        with patch.object(main.db_mod, "list_topup_requests_for_user",
                          new=AsyncMock(return_value=[])):
            r = self.client.get("/me/topup-requests")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_response_contains_expected_fields(self) -> None:
        row = _req_row()
        with patch.object(main.db_mod, "list_topup_requests_for_user",
                          new=AsyncMock(return_value=[row])):
            r = self.client.get("/me/topup-requests")
        item = r.json()[0]
        for field in ("id", "requested_pages", "requested_period", "status", "created_at"):
            self.assertIn(field, item, f"field '{field}' missing from response")

    def test_shows_approved_requests_in_history(self) -> None:
        approved = _req_row(status="approved", resolution_note="Done")
        with patch.object(main.db_mod, "list_topup_requests_for_user",
                          new=AsyncMock(return_value=[approved])):
            r = self.client.get("/me/topup-requests")
        self.assertEqual(r.json()[0]["status"], "approved")


# ═════════════════════════════════════════════════════════════════════════════
# 4.  API LAYER — ADMIN ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════


class AdminListTopupRequestsTests(unittest.TestCase):
    """GET /admin/topup-requests"""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_admin_gets_200(self) -> None:
        with patch.object(main.db_mod, "list_topup_requests",
                          new=AsyncMock(return_value=[_req_row()])):
            r = self.client.get("/admin/topup-requests")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)

    def test_status_filter_passed_to_db(self) -> None:
        with patch.object(main.db_mod, "list_topup_requests",
                          new=AsyncMock(return_value=[])) as mock_list:
            self.client.get("/admin/topup-requests?status=pending")
        mock_list.assert_awaited_once()
        _, kwargs = mock_list.call_args
        self.assertEqual(kwargs.get("status"), "pending")

    def test_no_filter_returns_all(self) -> None:
        all_reqs = [
            _req_row(status="pending"),
            _req_row(req_id=2, status="approved"),
            _req_row(req_id=3, status="rejected"),
        ]
        with patch.object(main.db_mod, "list_topup_requests",
                          new=AsyncMock(return_value=all_reqs)):
            r = self.client.get("/admin/topup-requests")
        self.assertEqual(len(r.json()), 3)

    def test_client_gets_403(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.get("/admin/topup-requests")
        self.assertEqual(r.status_code, 403)


class AdminTopupRequestCountTests(unittest.TestCase):
    """GET /admin/topup-requests/count"""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_returns_pending_count(self) -> None:
        with patch.object(main.db_mod, "get_pending_topup_request_count",
                          new=AsyncMock(return_value=5)):
            r = self.client.get("/admin/topup-requests/count")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pending"], 5)

    def test_returns_zero_when_empty(self) -> None:
        with patch.object(main.db_mod, "get_pending_topup_request_count",
                          new=AsyncMock(return_value=0)):
            r = self.client.get("/admin/topup-requests/count")
        self.assertEqual(r.json()["pending"], 0)

    def test_client_gets_403(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.get("/admin/topup-requests/count")
        self.assertEqual(r.status_code, 403)


class AdminApproveTopupRequestTests(unittest.TestCase):
    """POST /admin/topup-requests/{id}/approve"""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def _approve(self, req_id=_REQ_ID, body=None):
        return self.client.post(
            f"/admin/topup-requests/{req_id}/approve",
            json=body or {},
        )

    def _ok_result(self, pages: int = 1000, note: str | None = None) -> dict:
        return {
            "request": _req_row(status="approved", resolution_note=note),
            "topup": {**_topup_row(), "pages": pages},
        }

    def test_approve_returns_200_and_applies_topup(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(return_value=self._ok_result())):
            r = self._approve()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("request", body)
        self.assertIn("topup", body)
        self.assertEqual(body["request"]["status"], "approved")
        self.assertEqual(body["topup"]["pages"], 1000)

    def test_approve_with_resolution_note(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(return_value=self._ok_result(note="Monthly plan extended"))):
            r = self._approve(body={"resolution_note": "Monthly plan extended"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["request"]["resolution_note"], "Monthly plan extended")

    def test_approve_delegates_to_atomic_helper(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(return_value=self._ok_result(pages=2000))) as mock_approve:
            self._approve()
        mock_approve.assert_awaited_once()
        _, kwargs = mock_approve.call_args
        self.assertEqual(kwargs.get("request_id"), _REQ_ID)
        self.assertEqual(kwargs.get("resolved_by"), _ADMIN_UUID)

    def test_unknown_request_returns_404(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(side_effect=ValueError("not_found"))):
            r = self._approve(req_id=9999)
        self.assertEqual(r.status_code, 404)

    def test_already_approved_returns_409(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(side_effect=ValueError("already_approved"))):
            r = self._approve()
        self.assertEqual(r.status_code, 409)
        msg = r.json()["error"]["message"].lower()
        self.assertIn("approved", msg)

    def test_already_rejected_returns_409(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(side_effect=ValueError("already_rejected"))):
            r = self._approve()
        self.assertEqual(r.status_code, 409)

    def test_no_active_subscription_returns_409(self) -> None:
        """Atomic helper raises no_active_subscription when the user has none."""
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(side_effect=ValueError("no_active_subscription"))):
            r = self._approve()
        self.assertEqual(r.status_code, 409)
        msg = r.json()["error"]["message"].lower()
        self.assertIn("subscription", msg)

    def test_generic_value_error_returns_400(self) -> None:
        with patch.object(main.db_mod, "approve_topup_atomically",
                          new=AsyncMock(side_effect=ValueError("pages must be positive"))):
            r = self._approve()
        self.assertEqual(r.status_code, 400)

    def test_client_cannot_approve_returns_403(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self._approve()
        self.assertEqual(r.status_code, 403)


class AdminRejectTopupRequestTests(unittest.TestCase):
    """POST /admin/topup-requests/{id}/reject"""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def _reject(self, req_id=_REQ_ID, body=None):
        return self.client.post(
            f"/admin/topup-requests/{req_id}/reject",
            json=body or {},
        )

    def test_reject_returns_200(self) -> None:
        rejected = _req_row(status="rejected", resolution_note="Next cycle")
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=_req_row())), \
             patch.object(main.db_mod, "resolve_topup_request",
                          new=AsyncMock(return_value=rejected)):
            r = self._reject(body={"resolution_note": "Next cycle"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["resolution_note"], "Next cycle")

    def test_reject_without_note_returns_200(self) -> None:
        rejected = _req_row(status="rejected")
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=_req_row())), \
             patch.object(main.db_mod, "resolve_topup_request",
                          new=AsyncMock(return_value=rejected)):
            r = self._reject()
        self.assertEqual(r.status_code, 200)

    def test_unknown_request_returns_404(self) -> None:
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=None)):
            r = self._reject(req_id=9999)
        self.assertEqual(r.status_code, 404)

    def test_already_approved_returns_409(self) -> None:
        already = _req_row(status="approved")
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=already)):
            r = self._reject()
        self.assertEqual(r.status_code, 409)
        msg = r.json()["error"]["message"].lower()
        self.assertIn("approved", msg)

    def test_already_rejected_returns_409(self) -> None:
        already = _req_row(status="rejected")
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=already)):
            r = self._reject()
        self.assertEqual(r.status_code, 409)

    def test_client_cannot_reject_returns_403(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self._reject()
        self.assertEqual(r.status_code, 403)

    def test_reject_does_not_call_add_topup(self) -> None:
        """Rejecting must never attempt to add pages."""
        rejected = _req_row(status="rejected")
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=_req_row())), \
             patch.object(main.db_mod, "resolve_topup_request",
                          new=AsyncMock(return_value=rejected)), \
             patch.object(main.db_mod, "add_topup",
                          new=AsyncMock()) as mock_add:
            self._reject()
        mock_add.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════════
# 5.  INTEGRATION / EDGE-CASE TESTS
# ═════════════════════════════════════════════════════════════════════════════


class TopupRequestIdempotencyTests(unittest.TestCase):
    """Verify that double-approval is blocked at the API layer, not just DB."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_second_approve_after_first_sees_409(self) -> None:
        # The atomic helper's FOR UPDATE row lock serialises approvals: the first
        # succeeds, the second sees status != 'pending' and raises already_approved.
        approved_req = _req_row(status="approved")
        ok_result = {"request": approved_req, "topup": _topup_row()}

        call_count = {"n": 0}

        async def _approve(pool, request_id, resolved_by, resolution_note=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ok_result
            raise ValueError("already_approved")

        with patch.object(main.db_mod, "approve_topup_atomically", side_effect=_approve):
            r1 = self.client.post(f"/admin/topup-requests/{_REQ_ID}/approve", json={})
            r2 = self.client.post(f"/admin/topup-requests/{_REQ_ID}/approve", json={})

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 409)

    def test_reject_after_approve_sees_409(self) -> None:
        approved_req = _req_row(status="approved")
        with patch.object(main.db_mod, "get_topup_request",
                          new=AsyncMock(return_value=approved_req)):
            r = self.client.post(f"/admin/topup-requests/{_REQ_ID}/reject", json={})
        self.assertEqual(r.status_code, 409)


class TopupRequestUserIsolationTests(unittest.TestCase):
    """GET /me/topup-requests returns only the calling user's requests."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_list_passes_calling_user_id_to_db(self) -> None:
        user_a = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                  "role": "client", "email": "a@x.com"}
        with _ScopedOverrides(main.app, {get_current_user: lambda: user_a}), \
             patch.object(main.db_mod, "list_topup_requests_for_user",
                          new=AsyncMock(return_value=[])) as mock_list:
            self.client.get("/me/topup-requests")
        mock_list.assert_awaited_once()
        _, kwargs = mock_list.call_args
        # user_id must be the caller's, not any other user
        self.assertEqual(kwargs.get("user_id") or mock_list.call_args.args[1],
                         "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class TopupRequestApproveAppliesCorrectUserTests(unittest.IsolatedAsyncioTestCase):
    """approve_topup_atomically must apply the topup to the request owner, atomically."""

    async def test_topup_applied_to_request_owner(self) -> None:
        import uuid as _uuid_mod
        owner = _uuid_mod.UUID(_CLIENT_UUID)
        now = datetime.now(timezone.utc)
        pool, conn = _make_pool()
        conn.fetchrow.side_effect = [
            # 1. locked request row (pending)
            {"id": _REQ_ID, "user_id": owner, "requested_pages": 1000, "status": "pending"},
            # 2. active subscription lookup
            {"id": 7},
            # 3. inserted topup row
            {"id": 99, "user_id": owner, "subscription_id": 7, "pages": 1000,
             "note": "x", "created_by": _uuid_mod.UUID(_ADMIN_UUID), "created_at": now},
            # 4. updated (approved) request row
            {"id": _REQ_ID, "user_id": owner, "requested_pages": 1000,
             "requested_period": "6 months", "note": None, "status": "approved",
             "resolution_note": None, "resolved_by": _uuid_mod.UUID(_ADMIN_UUID),
             "resolved_at": now, "created_at": now},
        ]
        result = await db_mod.approve_topup_atomically(
            pool, request_id=_REQ_ID, resolved_by=_ADMIN_UUID,
        )
        # The INSERT into topups (3rd fetchrow) must use the request owner's id.
        insert_call = conn.fetchrow.await_args_list[2]
        self.assertIn("INSERT INTO topups", insert_call.args[0])
        self.assertEqual(insert_call.args[1], owner,
                         "Topup must be applied to the request owner, not the admin")
        self.assertEqual(insert_call.args[3], 1000)
        self.assertEqual(result["request"]["status"], "approved")
        self.assertEqual(result["topup"]["pages"], 1000)

    async def test_non_pending_request_raises_already(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow.side_effect = [
            {"id": _REQ_ID, "user_id": _CLIENT_UUID, "requested_pages": 1000, "status": "approved"},
        ]
        with self.assertRaises(ValueError) as ctx:
            await db_mod.approve_topup_atomically(pool, request_id=_REQ_ID, resolved_by=_ADMIN_UUID)
        self.assertEqual(str(ctx.exception), "already_approved")

    async def test_no_active_subscription_raises(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow.side_effect = [
            {"id": _REQ_ID, "user_id": _CLIENT_UUID, "requested_pages": 1000, "status": "pending"},
            None,  # no active subscription
        ]
        with self.assertRaises(ValueError) as ctx:
            await db_mod.approve_topup_atomically(pool, request_id=_REQ_ID, resolved_by=_ADMIN_UUID)
        self.assertEqual(str(ctx.exception), "no_active_subscription")


if __name__ == "__main__":
    unittest.main()
