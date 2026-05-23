from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.extractor as extractor
import backend.main as main
import backend.worker as worker


def _job(job_id: int, extraction_id: int, status: str = "queued") -> dict:
    return {
        "id": job_id,
        "extraction_id": extraction_id,
        "document_id": 1,
        "job_type": "llm",
        "status": status,
        "payload": {},
        "progress": {"stage": "llm"},
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


class ExtractorAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_document_stops_future_batches_after_invalid_json_page(self) -> None:
        calls: list[int] = []

        async def fake_call_llm(image_b64: str, system_prompt: str, user_message: str, llm_url: str, model: str, mime_type: str = "image/jpeg", page_num: int = 0, total_pages: int = 0, **kwargs):
            calls.append(page_num)
            if page_num == 1:
                raise ValueError("LLM returned invalid JSON")
            return {"vendor_name": f"Page {page_num}", "line_items": [{"item": page_num}]}

        pages = [
            {"page_number": 1, "image_b64": "a"},
            {"page_number": 2, "image_b64": "b"},
            {"page_number": 3, "image_b64": "c"},
        ]

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)):
            output = await extractor.extract_document(
                pages=pages,
                header_fields=["vendor_name"],
                line_item_fields=["item"],
                system_prompt="system",
                format_type="single_po_multipage",
                llm_url="http://llm.local",
                model="qwen3vl",
            )

        self.assertEqual(calls, [1])
        self.assertTrue(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 0)
        self.assertEqual([pr["_page"] for pr in output["page_results"]], [1])
        self.assertIn("_error", output["page_results"][0])
        self.assertTrue(output["result"].get("_all_pages_failed"))

    async def test_extract_document_handles_zero_page_input(self) -> None:
        output = await extractor.extract_document(
            pages=[],
            header_fields=["vendor_name"],
            line_item_fields=["item"],
            system_prompt="system",
            format_type="single_po_multipage",
            llm_url="http://llm.local",
            model="qwen3vl",
        )

        self.assertEqual(output, {"result": None, "page_results": []})

    async def test_ocr_job_saves_empty_ocr_output(self) -> None:
        pool = object()
        job = {"id": 12, "extraction_id": 22, "document_id": 32, "payload": {"scanned_page_numbers": [1]}}
        extraction_row = {"id": 22}
        empty_ocr = [{"page_number": 1, "words": []}]
        page_rows = [{"page_number": 1, "source": "paddleocr", "char_count": 0, "word_geometry": []}]

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction_row)), \
             patch.object(worker.db_mod, "get_pages", new=AsyncMock(return_value=page_rows)), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker, "_load_pages", new=AsyncMock(return_value=[{"page_number": 1, "image_b64": "abc"}])), \
             patch.object(worker.ocr_runner, "run_ocr_on_pages", new=AsyncMock(return_value=empty_ocr)), \
             patch.object(worker.db_mod, "save_ocr_data", new=AsyncMock()) as mock_save_ocr, \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker, "_maybe_enqueue_postprocess", new=AsyncMock()) as mock_enqueue:
            await worker._process_ocr(pool, job)

        mock_save_ocr.assert_awaited_once_with(
            pool,
            22,
            [{"page_number": 1, "source": "paddleocr", "char_count": 0, "word_count": 0, "words": []}],
        )
        mock_enqueue.assert_awaited_once_with(pool, 22, 32, unittest.mock.ANY)


class WorkerFailurePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_worker_marks_stage_failed_when_stage_crashes(self) -> None:
        job = {"id": 77, "extraction_id": 88, "document_id": 99, "job_type": "maintenance"}

        async def stop_after_first_idle(_seconds: float) -> None:
            raise asyncio.CancelledError()

        conn_mock = AsyncMock()
        conn_mock.execute.return_value = "UPDATE 0"
        ctx_mock = AsyncMock()
        ctx_mock.__aenter__.return_value = conn_mock
        pool_obj = type("Pool", (), {"close": AsyncMock(), "acquire": lambda self: ctx_mock})()
        with patch.object(worker.db_mod, "create_pool", new=AsyncMock(return_value=pool_obj)), \
             patch.object(worker.db_mod, "init", new=AsyncMock()), \
             patch.object(worker.db_mod, "claim_job", new=AsyncMock(side_effect=[job, None])), \
             patch.object(worker, "process_job", new=AsyncMock(side_effect=RuntimeError("minio down"))), \
             patch.object(worker.db_mod, "complete_job", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()) as mock_status, \
             patch.object(worker.db_mod, "fail_job", new=AsyncMock()) as mock_fail_job, \
             patch.object(worker.asyncio, "sleep", new=AsyncMock(side_effect=stop_after_first_idle)):
            with self.assertRaises(asyncio.CancelledError):
                await worker.run_worker("maintenance", "maintenance-worker")

        mock_status.assert_awaited_once()
        self.assertEqual(mock_status.await_args.args[2], "failed")
        mock_fail_job.assert_awaited_once_with(pool_obj, 77, "minio down", retryable=False)


class ResumeApiAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_resume_endpoint_queues_from_first_missing_page_with_existing_results(self) -> None:
        extraction = {
            "id": 123,
            "document_id": 1,
            "vendor_id": "V1",
            "status": "partial",
            "page_results": [
                {"_page": 1, "vendor_name": "ACME"},
                {"_page": 2, "_error": "invalid json"},
            ],
        }
        pages = [{"page_number": 1}, {"page_number": 2}, {"page_number": 3}]
        queued_job = {"id": 900, "status": "queued"}

        with patch.object(main.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(main.db_mod, "list_jobs_for_extraction", new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "get_pages", new=AsyncMock(return_value=pages)), \
             patch.object(main.db_mod, "set_cancel_requested", new=AsyncMock()), \
             patch.object(main.db_mod, "enqueue_job", new=AsyncMock(return_value=queued_job)) as mock_enqueue, \
             patch.object(main.db_mod, "set_extraction_status", new=AsyncMock()):
            response = self.client.post("/jobs/extractions/123/resume")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "job_id": 900,
                "extraction_id": 123,
                "status": "queued",
                "detected_vendor": None,
                "usage_warning": None,
            },
        )
        self.assertEqual(mock_enqueue.await_args.kwargs["payload"]["start_from_page"], 2)
        self.assertEqual(mock_enqueue.await_args.kwargs["payload"]["existing_page_results"], extraction["page_results"])


if __name__ == "__main__":
    unittest.main()
