"""Tests for the job-queue hot-path indexes added to db._QUEUE_INDEX_SQL.

These guard that the DDL stays aligned with the two queue queries it exists to
serve (claim_job / recover_stale_jobs) and that it remains idempotent, since
init() re-runs it on every boot. They are pure string assertions so they run
without a live Postgres.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import _QUEUE_INDEX_SQL


class QueueIndexSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        # Collapse whitespace so column-list assertions are insensitive to the
        # indentation/newlines used in the DDL literal.
        self.sql = " ".join(_QUEUE_INDEX_SQL.lower().split())

    def test_claim_index_matches_claim_job_query(self) -> None:
        # claim_job(): WHERE job_type AND status='queued' ORDER BY priority, created_at
        self.assertIn("create index if not exists jobs_claim_idx", self.sql)
        self.assertIn("on jobs (job_type, priority, created_at)", self.sql)
        self.assertIn("where status = 'queued'", self.sql)

    def test_stale_index_matches_recovery_query(self) -> None:
        # recover_stale_jobs(): WHERE job_type AND status='running' AND updated_at < cutoff
        self.assertIn("create index if not exists jobs_stale_idx", self.sql)
        self.assertIn("on jobs (job_type, updated_at)", self.sql)
        self.assertIn("where status = 'running'", self.sql)

    def test_indexes_are_idempotent(self) -> None:
        # Two indexes, both guarded with IF NOT EXISTS (init runs on every boot).
        self.assertEqual(self.sql.count("create index"), 2)
        self.assertEqual(self.sql.count("if not exists"), 2)

    def test_init_executes_the_queue_index_sql(self) -> None:
        # The constant is wired into init() (not just declared and forgotten).
        init_src = (ROOT / "backend" / "db.py").read_text(encoding="utf-8")
        self.assertIn("await conn.execute(_QUEUE_INDEX_SQL)", init_src)


if __name__ == "__main__":
    unittest.main()
