from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qwen_backend.main as main


def _job(job_id: int, extraction_id: int, status: str, progress: dict | None = None) -> dict:
    return {
        "id": job_id,
        "extraction_id": extraction_id,
        "document_id": 1,
        "job_type": "llm",
        "status": status,
        "payload": {"x": 1},
        "progress": progress or {"stage": "queued"},
        "attempts": 1,
        "max_attempts": 3,
        "priority": 100,
        "locked_by": None,
        "locked_at": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "created_at": None,
        "updated_at": None,
    }


def _extraction(status: str) -> dict:
    return {
        "id": 10,
        "document_id": 1,
        "vendor_id": "V1",
        "vendor_name": "Vendor 1",
        "template_id": None,
        "filename": "invoice.pdf",
        "total_pages": 2,
        "format_type": "single_po_multipage",
        "header_fields": ["vendor_name"],
        "line_item_fields": ["item"],
        "result": {"vendor_name": "ACME"},
        "page_results": [{"_page": 1}, {"_page": 2}],
        "field_locations": {"vendor_name": {"page": 1}},
        "ocr_data": [{"page_number": 1, "words": [{"text": "ACME"}]}],
        "corrected_result": None,
        "correction_meta": None,
        "export_object_key": None,
        "progress": {"stage": "ocr", "message": "Running OCR"},
        "cancel_requested": False,
        "status": status,
        "error": None,
        "duration_ms": 42,
        "created_at": None,
        "updated_at": None,
    }


class InfrastructureSafetyTests(unittest.TestCase):
    def test_docker_compose_defines_all_core_services_with_healthchecks(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        for service in [
            "postgres:",
            "redis:",
            "minio:",
            "api:",
            "normalize-worker:",
            "ocr-worker:",
            "llm-worker:",
            "postprocess-worker:",
            "outbound-worker:",
            "phoenix:",
        ]:
            self.assertIn(service, compose)

        self.assertGreaterEqual(compose.count("healthcheck:"), 4)
        self.assertIn('--stage", "normalize"', compose)
        self.assertIn('--stage", "ocr"', compose)
        self.assertIn('--stage", "llm"', compose)
        self.assertIn('--stage", "postprocess"', compose)
        self.assertIn('--stage", "outbound"', compose)

    def test_db_module_contains_schema_and_locking_guards_for_workers(self) -> None:
        db_text = (ROOT / "qwen_backend" / "db.py").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS vendors", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS templates", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS documents", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS extractions", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS pages", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS jobs", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS review_events", db_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS integration_deliveries", db_text)
        self.assertIn("FOR UPDATE SKIP LOCKED", db_text)
        self.assertIn("status IN ('queued', 'running')", db_text)


class StreamingObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_job_stream_omits_heavy_fields_in_progress_event_and_includes_terminal_payload(self) -> None:
        progress_job = _job(9, 10, "running", {"stage": "ocr", "message": "Running OCR"})
        done_job = _job(11, 10, "done", {"stage": "done", "message": "done"})
        progress_extraction = _extraction("processing")
        done_extraction = _extraction("done")

        with patch.object(main.db_mod, "get_job", new=AsyncMock(side_effect=[progress_job, progress_job, done_job])), \
             patch.object(main.db_mod, "get_extraction", new=AsyncMock(side_effect=[progress_extraction, done_extraction])), \
             patch.object(main.db_mod, "get_latest_job_for_extraction", new=AsyncMock(return_value=None)):
            response = self.client.get("/jobs/9/stream")

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line[len("data: "):])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[0]["event"], "progress")
        self.assertNotIn("ocr_data", events[0]["extraction"])
        self.assertNotIn("page_results", events[0]["extraction"])
        self.assertNotIn("field_locations", events[0]["extraction"])
        self.assertEqual(events[-1]["event"], "done")
        self.assertIn("ocr_data", events[-1]["extraction"])
        self.assertIn("page_results", events[-1]["extraction"])


if __name__ == "__main__":
    unittest.main()
