from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qwen_backend.main as main
import qwen_backend.worker as worker


class LegacyRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def test_extract_route_is_explicitly_deprecated(self) -> None:
        response = self.client.post(
            "/extract",
            files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
            data={"vendor_id": "V1"},
        )

        self.assertEqual(response.status_code, 410)
        self.assertIn("/ingest/ui", response.json()["detail"])


class WorkerPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalize_job_updates_progress_with_pool_and_saves_object_rows(self) -> None:
        pool = object()
        job = {"id": 11, "extraction_id": 21, "document_id": 31}
        document = {
            "id": 31,
            "filename": "invoice.pdf",
            "object_key": "documents/v1/doc.pdf",
        }
        rendered_pages = [
            {
                "page_number": 1,
                "image_b64": base64.b64encode(b"page-1").decode("ascii"),
                "mime_type": "image/jpeg",
                "width": 100,
                "height": 200,
            }
        ]
        store = SimpleNamespace(get_bytes=MagicMock(return_value=b"%PDF"), put_bytes=MagicMock())

        with patch.object(worker, "get_store", return_value=store), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "update_document_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()) as mock_progress, \
             patch.object(worker.db_mod, "save_pages", new=AsyncMock()) as mock_save_pages, \
             patch.object(worker.db_mod, "set_total_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "ensure_job", new=AsyncMock()), \
             patch.object(worker.processor, "pdf_to_images", new=AsyncMock(return_value=rendered_pages)):
            await worker._process_normalize(pool, job)

        mock_progress.assert_awaited_once_with(
            pool,
            11,
            {"stage": "normalize", "message": "Downloading original document"},
        )
        saved_pages = mock_save_pages.await_args.args[2]
        self.assertEqual(saved_pages[0]["page_number"], 1)
        self.assertIn("object_key", saved_pages[0])
        self.assertEqual(saved_pages[0]["mime_type"], "image/jpeg")

    async def test_ocr_job_updates_progress_with_pool(self) -> None:
        pool = object()
        job = {"id": 12, "extraction_id": 22, "document_id": 32}
        extraction = {"id": 22}
        ocr_pages = [{"page_number": 1, "words": [{"text": "A", "box": [1, 2, 3, 4], "score": 0.9}]}]

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker, "_load_pages", new=AsyncMock(return_value=[{"page_number": 1, "image_b64": "abc"}])), \
             patch.object(worker.ocr_runner, "run_ocr_on_pages", new=AsyncMock(return_value=ocr_pages)), \
             patch.object(worker.db_mod, "save_ocr_data", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()) as mock_progress, \
             patch.object(worker, "_maybe_enqueue_postprocess", new=AsyncMock()):
            await worker._process_ocr(pool, job)

        mock_progress.assert_awaited_once_with(
            pool,
            12,
            {"stage": "ocr", "pages_processed": 1},
        )


if __name__ == "__main__":
    unittest.main()
