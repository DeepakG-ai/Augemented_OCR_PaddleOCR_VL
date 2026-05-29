"""Idempotency guarantees for db.release_quota_once.

The terminal quota release runs before the worker marks the job complete. If the
worker crashes in that window, stale-job recovery re-runs the job and the release
fires again. release_quota_once must make that second release a no-op so a double
release can't steal pending quota from another in-flight upload by the same user.
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


def _make_pool(fetchrow_val):
    """Pool whose connection supports fetchrow/execute and a transaction() CM."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_val)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


_UID = "00000000-0000-0000-0000-000000000001"


class ReleaseQuotaOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_releases_reserved_pages_and_clears_metadata(self):
        pool, conn = _make_pool({"metadata": {"billing_user_id": _UID, "reserved_pages": 4}})
        released = await db_mod.release_quota_once(pool, document_id=99, user_id=_UID)

        self.assertEqual(released, 4)
        # Two UPDATEs: decrement pending_pages, then strip reserved_pages.
        self.assertEqual(conn.execute.await_count, 2)
        decrement_sql = conn.execute.await_args_list[0].args[0]
        self.assertIn("pending_pages", decrement_sql)
        self.assertEqual(conn.execute.await_args_list[0].args[1], 4)
        clear_sql = conn.execute.await_args_list[1].args[0]
        self.assertIn("reserved_pages", clear_sql)

    async def test_second_call_is_noop_after_metadata_cleared(self):
        # Re-run after recovery: reserved_pages already stripped from metadata.
        pool, conn = _make_pool({"metadata": {"billing_user_id": _UID}})
        released = await db_mod.release_quota_once(pool, document_id=99, user_id=_UID)

        self.assertEqual(released, 0)
        conn.execute.assert_not_awaited()  # no pending decrement, no double release

    async def test_zero_reserved_pages_is_noop(self):
        pool, conn = _make_pool({"metadata": {"billing_user_id": _UID, "reserved_pages": 0}})
        released = await db_mod.release_quota_once(pool, document_id=99, user_id=_UID)
        self.assertEqual(released, 0)
        conn.execute.assert_not_awaited()

    async def test_missing_document_is_noop(self):
        pool, conn = _make_pool(None)
        released = await db_mod.release_quota_once(pool, document_id=99, user_id=_UID)
        self.assertEqual(released, 0)
        conn.execute.assert_not_awaited()

    async def test_none_document_id_short_circuits(self):
        pool, conn = _make_pool({"metadata": {"reserved_pages": 3}})
        released = await db_mod.release_quota_once(pool, document_id=None, user_id=_UID)
        self.assertEqual(released, 0)
        pool.acquire.assert_not_called()

    async def test_billing_user_falls_back_to_metadata_when_uid_missing(self):
        # Worker cancel/failure paths pass user_id=None and rely on the doc metadata.
        pool, conn = _make_pool({"metadata": {"billing_user_id": _UID, "reserved_pages": 2}})
        released = await db_mod.release_quota_once(pool, document_id=99, user_id=None)
        self.assertEqual(released, 2)
        decrement_sql = conn.execute.await_args_list[0].args[0]
        self.assertIn("pending_pages", decrement_sql)
        self.assertEqual(conn.execute.await_args_list[0].args[1], 2)

    async def test_metadata_as_json_string_is_parsed(self):
        # asyncpg can hand back jsonb as a string depending on codec setup.
        pool, conn = _make_pool({"metadata": '{"billing_user_id": "%s", "reserved_pages": 5}' % _UID})
        released = await db_mod.release_quota_once(pool, document_id=99, user_id=None)
        self.assertEqual(released, 5)


if __name__ == "__main__":
    unittest.main()
