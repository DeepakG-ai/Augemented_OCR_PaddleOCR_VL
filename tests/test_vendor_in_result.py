"""
Tests for injecting the detected vendor name into served extraction results.

Covers:
  - backend.contracts.attach_vendor (pure, non-mutating helper)
      * dict injection (vendor first), no mutation of input
      * list / po_per_page injection per document
      * falsy vendor_name -> unchanged
      * idempotent when "vendor" already present
      * passthrough for scalars / None
  - GET /extractions/{id} surfaces vendor inside result JSON
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Make sure tests can import the backend regardless of how pytest is invoked.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never emit real MLflow traces.
os.environ.setdefault("SECRET_KEY", "test-only-secret-do-not-use-in-prod")
os.environ["MLFLOW_ENABLED"] = "false"

from fastapi.testclient import TestClient

import backend.main as main
from backend.auth import get_current_user
from backend.contracts import attach_vendor


def _client_user(uid: str = "client-uuid") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test"}


def _extraction_row(**overrides) -> dict:
    """A realistic extraction row covering every required ExtractionOut field."""
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = {
        "id": 1,
        "document_id": 10,
        "vendor_id": "vendor-1",
        "vendor_name": "Acme Corp",
        "template_id": 5,
        "filename": "po.pdf",
        "total_pages": 1,
        "format_type": "single_po_multipage",
        "header_fields": ["po_number"],
        "line_item_fields": ["sku"],
        "result": {"po_number": "4501", "line_items": []},
        "page_results": None,
        "field_locations": None,
        "ocr_data": None,
        "corrected_result": None,
        "correction_meta": None,
        "export_object_key": None,
        "progress": None,
        "cancel_requested": False,
        "status": "done",
        "error": None,
        "duration_ms": 1234,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


class _ScopedOverrides:
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


class AttachVendorUnitTests(unittest.TestCase):
    def test_dict_injection_vendor_first_no_mutation(self) -> None:
        original = {"po_number": "1"}
        out = attach_vendor(original, "Acme")
        self.assertEqual(out, {"vendor": "Acme", "po_number": "1"})
        # vendor must be the first key.
        self.assertEqual(list(out.keys())[0], "vendor")
        # Original must NOT be mutated.
        self.assertNotIn("vendor", original)
        self.assertIsNot(out, original)

    def test_list_po_per_page_injection(self) -> None:
        out = attach_vendor([{"a": 1}, {"b": 2}], "Acme")
        self.assertEqual(out, [{"vendor": "Acme", "a": 1}, {"vendor": "Acme", "b": 2}])

    def test_falsy_vendor_name_returns_input_unchanged(self) -> None:
        self.assertEqual(attach_vendor({"a": 1}, None), {"a": 1})
        self.assertEqual(attach_vendor({"a": 1}, ""), {"a": 1})
        self.assertNotIn("vendor", attach_vendor({"a": 1}, None))
        self.assertNotIn("vendor", attach_vendor({"a": 1}, ""))

    def test_idempotent_when_vendor_already_present(self) -> None:
        out = attach_vendor({"vendor": "Existing", "a": 1}, "Acme")
        self.assertEqual(out, {"vendor": "Existing", "a": 1})
        self.assertEqual(out["vendor"], "Existing")

    def test_passthrough_for_scalars_and_none(self) -> None:
        self.assertEqual(attach_vendor("scalar", "Acme"), "scalar")
        self.assertIsNone(attach_vendor(None, "Acme"))


class GetExtractionVendorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_result_carries_detected_vendor(self) -> None:
        row = _extraction_row()
        overrides = {get_current_user: lambda: _client_user()}
        with _ScopedOverrides(main.app, overrides), \
             patch.object(main, "assert_extraction_access",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "get_extraction",
                          new=AsyncMock(return_value=row)):
            r = self.client.get("/extractions/1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["result"]["vendor"], "Acme Corp")
        self.assertEqual(body["result"]["po_number"], "4501")


if __name__ == "__main__":
    unittest.main()
