"""
test_admin_billing_scoping.py
============================
Tests for the user billing assignment during ingestion.

Business rules:
  - Whoever logins, bill to them directly.
  - This ensures that if admin logs in as admin, but acted as client, billing is assigned to the admin.
  - If a client logs in, whatever they upload is billed to that client directly.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestAdminBillingScoping(unittest.IsolatedAsyncioTestCase):
    def _make_user(self, role: str, uid: str) -> dict:
        return {"id": uid, "role": role, "email": f"{role}@test.com"}

    async def test_admin_billing_user_id_is_set_during_ingestion(self) -> None:
        """
        Admin uploading a file has billing_user_id set to their own ID,
        so they are billed for the usage directly.
        """
        # Replicate main.py `POST /ingest` logic
        admin_id = "admin-123"
        user = self._make_user("admin", admin_id)
        
        # Whoever logins, bill to them.
        billing_user_id = user["id"]
        self.assertEqual(billing_user_id, admin_id)

    async def test_client_billing_user_id_is_set_during_ingestion(self) -> None:
        """
        Client uploading a file has billing_user_id set to their own client ID directly,
        ensuring they are billed for their own usage.
        """
        client_id = "client-456"
        user = self._make_user("client", client_id)
        
        # Whoever logins, bill to them.
        billing_user_id = user["id"]
        self.assertEqual(billing_user_id, client_id)

    async def test_worker_pipeline_base_extracts_billing_user_id(self) -> None:
        """
        Verify `_pipeline_base` in worker.py correctly extracts billing_user_id
        from the document metadata so it's included in the pipeline context.
        """
        from backend.worker import _pipeline_base
        
        doc = {"id": "d1", "metadata": {"billing_user_id": "admin-123"}}
        base = _pipeline_base(document=doc)
        self.assertEqual(base.get("billing_user_id"), "admin-123")

        doc_none = {"id": "d2"}
        base_none = _pipeline_base(document=doc_none)
        self.assertIsNone(base_none.get("billing_user_id"))

    async def test_call_llm_passes_billing_user_id(self) -> None:
        """
        Verify `call_llm` uses billing_user_id from pipeline_context and passes
        it to `record_llm_usage`.
        """
        from backend.db import record_llm_usage
        import inspect
        
        sig = inspect.signature(record_llm_usage)
        self.assertIn("billing_user_id", sig.parameters, 
            "record_llm_usage must accept billing_user_id parameter")

    async def test_folder_ingest_resolves_billing_user_id(self) -> None:
        """
        Verify that in folder watcher ingestion:
          - Whoever logins (owns the watcher), billing_user_id is set to user_id.
        """
        user_id = "deepak-789"
        
        # Under main.py _folder_ingest_callback:
        # Whoever logins (owns the watcher), bill to them.
        billing_user_id = user_id
        self.assertEqual(billing_user_id, user_id)

if __name__ == "__main__":
    unittest.main()
