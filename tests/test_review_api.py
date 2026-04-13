from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "qwen_backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main


class ReviewApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_get_extraction_ocr_returns_200_with_payload(self) -> None:
        payload = [{"page_number": 1, "words": [{"text": "ACME", "box": [1, 2, 3, 4], "score": 0.9}]}]

        with patch.object(main.db_mod, "get_ocr_data", new=AsyncMock(return_value=payload)):
            response = self.client.get("/extractions/123/ocr")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"extraction_id": 123, "ocr_pages": payload})

    def test_get_extraction_ocr_returns_404_when_missing(self) -> None:
        with patch.object(main.db_mod, "get_ocr_data", new=AsyncMock(return_value=None)):
            response = self.client.get("/extractions/123/ocr")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No OCR data found for this extraction")

    def test_save_corrections_requires_corrected_result(self) -> None:
        response = self.client.put("/extractions/123/corrections", json={"field_locations": {}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "corrected_result is required")

    def test_save_corrections_returns_404_for_missing_extraction(self) -> None:
        with patch.object(main.db_mod, "get_extraction", new=AsyncMock(return_value=None)):
            response = self.client.put(
                "/extractions/123/corrections",
                json={"corrected_result": {"vendor_name": "ACME"}, "field_locations": {}},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Extraction 123 not found")

    def test_save_corrections_returns_200_on_success(self) -> None:
        extraction = {
            "id": 123,
            "document_id": 1,
            "vendor_id": "V1",
            "vendor_name": "Vendor 1",
            "template_id": None,
            "filename": "test.pdf",
            "total_pages": 1,
            "format_type": "single_po_multipage",
            "header_fields": [],
            "line_item_fields": [],
            "result": {"vendor_name": "OLD"},
            "page_results": [],
            "field_locations": {},
            "ocr_data": None,
            "corrected_result": None,
            "correction_meta": None,
            "export_object_key": None,
            "progress": None,
            "cancel_requested": False,
            "status": "done",
            "error": None,
            "duration_ms": 10,
            "created_at": "2026-04-06T00:00:00Z",
            "updated_at": "2026-04-06T00:00:00Z",
        }
        with patch.object(main.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(main.db_mod, "save_corrections", new=AsyncMock(return_value=True)) as mock_save, \
             patch.object(main.db_mod, "create_review_event", new=AsyncMock(return_value=77)), \
             patch.object(main.db_mod, "ensure_job", new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "save_gold_example", new=AsyncMock(return_value=55)), \
             patch.object(main.cache_mod, "invalidate_vendor_cache", new=AsyncMock(return_value=None)):
            response = self.client.put(
                "/extractions/123/corrections",
                json={
                    "corrected_result": {"vendor_name": "ACME"},
                    "field_locations": {"vendor_name": {"page": 1, "box": [1, 2, 3, 4]}},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "saved")
        self.assertEqual(response.json()["extraction_id"], 123)
        self.assertEqual(mock_save.await_count, 1)


if __name__ == "__main__":
    unittest.main()
