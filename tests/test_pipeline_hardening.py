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

import backend.main as main
import backend.worker as worker


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
        self.assertIn("/ingest/ui", response.json()["error"]["message"])


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
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value={"id": 21, "vendor_id": "vendor-1"})), \
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

    async def test_normalize_auto_detects_vendor_and_queues_jobs(self) -> None:
        # When the extraction has no vendor_id, normalize detects it from page-1
        # text, persists it, and queues both ocr and llm.
        pool = object()
        job = {"id": 14, "extraction_id": 24, "document_id": 34}
        document = {
            "id": 34,
            "filename": "invoice.pdf",
            "object_key": "documents/_pending/doc.pdf",
            "metadata": {"detect_user_id": "user-1"},
        }
        rendered_pages = [{
            "page_number": 1,
            "image_b64": base64.b64encode(b"page-1").decode("ascii"),
            "mime_type": "image/jpeg",
            "width": 100, "height": 200,
        }]
        # Digital page-1 geometry so detection reads words without OCR.
        digital_geo = [{
            "page_number": 1, "source": "pypdfium", "char_count": 12,
            "words": [{"text": "ACME", "box": [0, 0, 1, 1], "score": 1.0}],
        }]
        match = SimpleNamespace(
            vendor_id="vendor-1", vendor_name="Acme", score=99.0,
            matched_patterns=["ACME"], match_type="exact",
        )
        store = SimpleNamespace(get_bytes=MagicMock(return_value=b"%PDF"), put_bytes=MagicMock())

        with patch.object(worker, "get_store", return_value=store), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "update_document_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "save_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_total_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value={"id": 24, "vendor_id": None})), \
             patch.object(worker.geometry, "compute_pdf_geometry", return_value=digital_geo), \
             patch("backend.vendor_detector.detect_vendor", new=AsyncMock(return_value=match)), \
             patch.object(worker.db_mod, "get_template", new=AsyncMock(return_value={
                 "id": 5, "format_type": "single_po_multipage",
                 "header_fields": ["po_number"], "line_item_fields": ["qty"]})), \
             patch.object(worker.db_mod, "update_extraction_vendor", new=AsyncMock()) as mock_apply, \
             patch.object(worker.db_mod, "ensure_job", new=AsyncMock()) as mock_ensure, \
             patch.object(worker.processor, "pdf_to_images", new=AsyncMock(return_value=rendered_pages)):
            await worker._process_normalize(pool, job)

        mock_apply.assert_awaited_once()
        self.assertEqual(mock_apply.await_args.kwargs["vendor_id"], "vendor-1")
        self.assertEqual(mock_apply.await_args.kwargs["template_id"], 5)
        queued_stages = {call.args[3] for call in mock_ensure.await_args_list}
        self.assertEqual(queued_stages, {"ocr", "llm"})

    async def test_normalize_unknown_vendor_fails_and_releases_quota(self) -> None:
        # No vendor matches → extraction + document marked failed, quota released,
        # and NO ocr/llm jobs are queued.
        pool = object()
        job = {"id": 15, "extraction_id": 25, "document_id": 35}
        document = {
            "id": 35,
            "filename": "invoice.pdf",
            "object_key": "documents/_pending/doc.pdf",
            "metadata": {"detect_user_id": "user-1"},
        }
        rendered_pages = [{
            "page_number": 1,
            "image_b64": base64.b64encode(b"page-1").decode("ascii"),
            "mime_type": "image/jpeg",
            "width": 100, "height": 200,
        }]
        digital_geo = [{
            "page_number": 1, "source": "pypdfium", "char_count": 12,
            "words": [{"text": "XYZ", "box": [0, 0, 1, 1], "score": 1.0}],
        }]
        store = SimpleNamespace(get_bytes=MagicMock(return_value=b"%PDF"), put_bytes=MagicMock())

        with patch.object(worker, "get_store", return_value=store), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "update_document_status", new=AsyncMock()) as mock_doc_status, \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()) as mock_set_status, \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "save_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_total_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value={"id": 25, "vendor_id": None})), \
             patch.object(worker.geometry, "compute_pdf_geometry", return_value=digital_geo), \
             patch("backend.vendor_detector.detect_vendor", new=AsyncMock(return_value=None)), \
             patch.object(worker.db_mod, "release_quota_once", new=AsyncMock()) as mock_release, \
             patch.object(worker.db_mod, "ensure_job", new=AsyncMock()) as mock_ensure, \
             patch.object(worker.processor, "pdf_to_images", new=AsyncMock(return_value=rendered_pages)):
            await worker._process_normalize(pool, job)

        mock_release.assert_awaited_once_with(pool, 35, None)
        mock_ensure.assert_not_awaited()
        failed_calls = [c for c in mock_set_status.await_args_list
                        if c.args[2] == "failed" and c.kwargs.get("error") == "unknown_vendor"]
        self.assertEqual(len(failed_calls), 1)
        self.assertIn("failed", [c.args[2] for c in mock_doc_status.await_args_list])

    async def test_normalize_detected_vendor_without_template_fails(self) -> None:
        # A vendor can be detected but have no usable template. We must NOT queue
        # ocr/llm in that case — same gate preselected vendors hit at submit time.
        pool = object()
        job = {"id": 16, "extraction_id": 26, "document_id": 36}
        document = {
            "id": 36,
            "filename": "invoice.pdf",
            "object_key": "documents/_pending/doc.pdf",
            "metadata": {"detect_user_id": "user-1"},
        }
        rendered_pages = [{
            "page_number": 1,
            "image_b64": base64.b64encode(b"page-1").decode("ascii"),
            "mime_type": "image/jpeg", "width": 100, "height": 200,
        }]
        digital_geo = [{
            "page_number": 1, "source": "pypdfium", "char_count": 12,
            "words": [{"text": "ACME", "box": [0, 0, 1, 1], "score": 1.0}],
        }]
        match = SimpleNamespace(
            vendor_id="vendor-1", vendor_name="Acme", score=99.0,
            matched_patterns=["ACME"], match_type="exact",
        )
        store = SimpleNamespace(get_bytes=MagicMock(return_value=b"%PDF"), put_bytes=MagicMock())

        with patch.object(worker, "get_store", return_value=store), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "update_document_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()) as mock_set_status, \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "save_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_total_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value={"id": 26, "vendor_id": None})), \
             patch.object(worker.geometry, "compute_pdf_geometry", return_value=digital_geo), \
             patch("backend.vendor_detector.detect_vendor", new=AsyncMock(return_value=match)), \
             patch.object(worker.db_mod, "get_template", new=AsyncMock(return_value=None)), \
             patch.object(worker.db_mod, "update_extraction_vendor", new=AsyncMock()) as mock_apply, \
             patch.object(worker.db_mod, "release_quota_once", new=AsyncMock()) as mock_release, \
             patch.object(worker.db_mod, "ensure_job", new=AsyncMock()) as mock_ensure, \
             patch.object(worker.processor, "pdf_to_images", new=AsyncMock(return_value=rendered_pages)):
            await worker._process_normalize(pool, job)

        mock_apply.assert_not_awaited()
        mock_ensure.assert_not_awaited()
        mock_release.assert_awaited_once_with(pool, 36, None)
        failed = [c for c in mock_set_status.await_args_list
                  if c.args[2] == "failed" and c.kwargs.get("error") == "no_template"]
        self.assertEqual(len(failed), 1)

    async def test_normalize_ocr_failure_during_detect_is_not_unknown_vendor(self) -> None:
        # A scanned page-1 whose OCR is unavailable must fail as ocr_unavailable
        # (a retryable infra error), NOT be mislabeled unknown_vendor.
        pool = object()
        job = {"id": 17, "extraction_id": 27, "document_id": 37}
        document = {
            "id": 37,
            "filename": "scan.pdf",
            "object_key": "documents/_pending/scan.pdf",
            "metadata": {"detect_user_id": "user-1"},
        }
        rendered_pages = [{
            "page_number": 1,
            "image_b64": base64.b64encode(b"page-1").decode("ascii"),
            "mime_type": "image/jpeg", "width": 100, "height": 200,
        }]
        # Scanned page-1 with no digital words → detection must OCR it.
        scanned_geo = [{"page_number": 1, "source": "scanned", "char_count": 0, "words": []}]
        store = SimpleNamespace(get_bytes=MagicMock(return_value=b"%PDF"), put_bytes=MagicMock())
        detect_mock = AsyncMock(return_value=None)

        with patch.object(worker, "get_store", return_value=store), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "update_document_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()) as mock_set_status, \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "save_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "set_total_pages", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value={"id": 27, "vendor_id": None})), \
             patch.object(worker.geometry, "compute_pdf_geometry", return_value=scanned_geo), \
             patch.object(worker.ocr_runner, "run_ocr_on_pages", new=AsyncMock(side_effect=RuntimeError("ocr down"))), \
             patch("backend.vendor_detector.detect_vendor", new=detect_mock), \
             patch.object(worker.db_mod, "release_quota_once", new=AsyncMock()) as mock_release, \
             patch.object(worker.db_mod, "ensure_job", new=AsyncMock()) as mock_ensure, \
             patch.object(worker.processor, "pdf_to_images", new=AsyncMock(return_value=rendered_pages)):
            await worker._process_normalize(pool, job)

        detect_mock.assert_not_awaited()
        mock_ensure.assert_not_awaited()
        mock_release.assert_awaited_once_with(pool, 37, None)
        failed = [c for c in mock_set_status.await_args_list
                  if c.args[2] == "failed" and c.kwargs.get("error") == "ocr_unavailable"]
        self.assertEqual(len(failed), 1)

    async def test_ocr_job_updates_progress_with_pool(self) -> None:
        pool = object()
        job = {"id": 12, "extraction_id": 22, "document_id": 32}
        extraction = {"id": 22}
        ocr_pages = [{"page_number": 1, "words": [{"text": "A", "box": [1, 2, 3, 4], "score": 0.9}]}]

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "get_pages", new=AsyncMock(return_value=[{"page_number": 1, "source": "scanned", "char_count": 0, "word_geometry": []}])), \
             patch.object(worker, "_load_pages", new=AsyncMock(return_value=[{"page_number": 1, "image_b64": "abc"}])), \
             patch.object(worker.ocr_runner, "run_ocr_on_pages", new=AsyncMock(return_value=ocr_pages)), \
             patch.object(worker.db_mod, "save_ocr_data", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()) as mock_progress, \
             patch.object(worker, "_maybe_enqueue_postprocess", new=AsyncMock()):
            await worker._process_ocr(pool, job)

        mock_progress.assert_awaited_once_with(
            pool,
            12,
            {"stage": "ocr", "pages_processed": 1, "scanned_pages": 1},
        )

    async def test_ocr_page_error_fails_stage_instead_of_saving_blank_page(self) -> None:
        pool = object()
        job = {"id": 13, "extraction_id": 23, "document_id": 33}
        extraction = {"id": 23}
        ocr_pages = [{"page_number": 1, "words": [], "_ocr_error": "model missing"}]

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "get_pages", new=AsyncMock(return_value=[{"page_number": 1, "source": "scanned", "char_count": 0, "word_geometry": []}])), \
             patch.object(worker, "_load_pages", new=AsyncMock(return_value=[{"page_number": 1, "image_b64": "abc"}])), \
             patch.object(worker.ocr_runner, "run_ocr_on_pages", new=AsyncMock(return_value=ocr_pages)), \
             patch.object(worker.db_mod, "save_ocr_data", new=AsyncMock()) as mock_save_ocr:
            with self.assertRaisesRegex(RuntimeError, "PaddleOCR failed"):
                await worker._process_ocr(pool, job)

        mock_save_ocr.assert_not_awaited()

    async def test_cancelled_ocr_job_raises_sentinel_before_postprocess(self) -> None:
        pool = object()
        job = {"id": 14, "extraction_id": 24, "document_id": 34}
        extraction = {"id": 24, "document_id": 34, "result": None, "page_results": []}
        page_rows = [{"page_number": 1, "source": "pypdfium", "char_count": 10, "word_geometry": [{"text": "A"}]}]

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=None)), \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "get_pages", new=AsyncMock(return_value=page_rows)), \
             patch.object(worker.db_mod, "save_ocr_data", new=AsyncMock()), \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=True)), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker, "_maybe_enqueue_postprocess", new=AsyncMock()) as mock_enqueue:
            with self.assertRaises(worker.JobCancelled):
                await worker._process_ocr(pool, job)

        mock_enqueue.assert_not_awaited()

    async def test_llm_page_failure_marks_extraction_failed_without_postprocess(self) -> None:
        pool = object()
        job = {"id": 15, "extraction_id": 25, "document_id": 35, "payload": {}}
        extraction = {
            "id": 25,
            "document_id": 35,
            "filename": "invoice.pdf",
            "vendor_id": "V1",
            "header_fields": ["vendor_name"],
            "line_item_fields": ["sku"],
            "format_type": "single_po_multipage",
        }
        document = {"id": 35, "metadata": {"billing_user_id": "user-1"}}
        tmpl = {
            "id": 5,
            "header_fields": ["vendor_name"],
            "line_item_fields": ["sku"],
            "format_type": "single_po_multipage",
            "prompt_instructions": None,
            "extraction_rules": [],
        }
        llm_output = {
            "result": {"vendor_name": "ACME", "line_items": []},
            "page_results": [
                {"_page": 1, "fields": {"vendor_name": "ACME"}},
                {"_page": 2, "_error": "invalid json"},
            ],
            "cancelled": True,
            "last_completed_page": 1,
        }

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "get_template", new=AsyncMock(return_value=tmpl)), \
             patch.object(worker.db_mod, "get_gold_examples", new=AsyncMock(return_value=[])), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_result", new=AsyncMock()) as mock_update_result, \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "release_quota_once", new=AsyncMock()) as mock_release, \
             patch.object(worker, "_load_pages", new=AsyncMock(return_value=[{"page_number": 1}, {"page_number": 2}])), \
             patch.object(worker.extractor, "extract_document", new=AsyncMock(return_value=llm_output)), \
             patch.object(worker, "_maybe_enqueue_postprocess", new=AsyncMock()) as mock_postprocess, \
             patch.object(worker.page_logger, "append_log", new=MagicMock()):
            await worker._process_llm(pool, job)

        mock_update_result.assert_awaited_once()
        self.assertEqual(mock_update_result.await_args.args[4], "failed")
        self.assertEqual(mock_update_result.await_args.kwargs["error"], worker.LLM_FAILED_ERROR)
        self.assertEqual(mock_update_result.await_args.kwargs["progress"]["failed_pages"], [2])
        mock_release.assert_awaited_once_with(pool, 35, "user-1")
        mock_postprocess.assert_not_awaited()

    async def test_llm_json_only_mode_skips_page1_boxes_without_crashing(self) -> None:
        pool = object()
        job = {"id": 17, "extraction_id": 27, "document_id": 37, "payload": {}}
        extraction = {
            "id": 27,
            "document_id": 37,
            "filename": "invoice.pdf",
            "vendor_id": "V1",
            "header_fields": ["vendor_name"],
            "line_item_fields": ["sku"],
            "format_type": "single_po_multipage",
        }
        document = {
            "id": 37,
            "metadata": {
                "billing_user_id": "user-1",
                "include_layout_boxes": False,
            },
        }
        tmpl = {
            "id": 5,
            "header_fields": ["vendor_name"],
            "line_item_fields": ["sku"],
            "format_type": "single_po_multipage",
            "prompt_instructions": None,
            "extraction_rules": [],
        }
        llm_output = {
            "result": {"vendor_name": "ACME", "line_items": []},
            "page_results": [{"_page": 1, "fields": {"vendor_name": "ACME"}}],
            "cancelled": False,
            "last_completed_page": 1,
        }

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "get_user_by_id", new=AsyncMock(return_value=None)), \
             patch.object(worker.db_mod, "get_template", new=AsyncMock(return_value=tmpl)), \
             patch.object(worker.db_mod, "get_gold_examples", new=AsyncMock(return_value=[])), \
             patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_job_progress", new=AsyncMock()), \
             patch.object(worker.db_mod, "update_extraction_result", new=AsyncMock()) as mock_update_result, \
             patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(return_value=False)), \
             patch.object(worker.db_mod, "upsert_qwen_layout_boxes", new=AsyncMock()) as mock_upsert_boxes, \
             patch.object(worker, "_load_pages", new=AsyncMock(return_value=[{"page_number": 1, "image_b64": "abc"}])), \
             patch.object(worker.extractor, "extract_document", new=AsyncMock(return_value=llm_output)) as mock_extract, \
             patch.object(worker, "_maybe_enqueue_postprocess", new=AsyncMock()) as mock_postprocess, \
             patch.object(worker.page_logger, "append_log", new=MagicMock()):
            await worker._process_llm(pool, job)

        mock_extract.assert_awaited_once()
        self.assertIsNone(mock_extract.await_args.kwargs["system_prompt_page1"])
        self.assertFalse(mock_extract.await_args.kwargs["include_page1_boxes"])
        mock_upsert_boxes.assert_not_awaited()
        self.assertEqual(mock_update_result.await_args.args[4], "processing")
        mock_postprocess.assert_awaited_once()

    async def test_postprocess_finishes_done_with_warning_when_ocr_failed_after_llm_success(self) -> None:
        pool = object()
        job = {"id": 16, "extraction_id": 26, "document_id": 36}
        extraction = {
            "id": 26,
            "document_id": 36,
            "filename": "invoice.pdf",
            "vendor_id": "V1",
            "template_id": 5,
            "result": {"vendor_name": "ACME", "line_items": []},
            "page_results": [{"_page": 1, "vendor_name": "ACME"}],
            "ocr_data": None,
            "status": "processing",
        }
        document = {"id": 36, "metadata": {"billing_user_id": "user-1"}}
        latest_ocr_job = {"status": "failed", "error": "ocr model failed"}

        with patch.object(worker.db_mod, "get_extraction", new=AsyncMock(return_value=extraction)), \
             patch.object(worker.db_mod, "get_document", new=AsyncMock(return_value=document)), \
             patch.object(worker.db_mod, "get_latest_job_for_extraction_type", new=AsyncMock(return_value=latest_ocr_job)), \
             patch.object(worker.db_mod, "save_field_locations", new=AsyncMock()) as mock_save_locations, \
             patch.object(worker.db_mod, "get_field_mapping", new=AsyncMock(return_value=None)), \
             patch.object(worker.db_mod, "update_extraction_mapped_result", new=AsyncMock()) as mock_clear_mapped, \
             patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock()) as mock_set_status, \
             patch.object(worker.db_mod, "release_quota_once", new=AsyncMock()) as mock_release, \
             patch.object(worker.db_mod, "get_qwen_layout_boxes", new=AsyncMock()) as mock_layout_boxes:
            await worker._process_postprocess(pool, job)

        mock_layout_boxes.assert_not_awaited()
        mock_save_locations.assert_awaited_once_with(pool, 26, {})
        mock_clear_mapped.assert_awaited_once_with(pool, 26, None)
        self.assertEqual(mock_set_status.await_args.args[2], "done")
        self.assertNotIn("error", mock_set_status.await_args.kwargs)
        self.assertEqual(mock_set_status.await_args.kwargs["progress"]["warning_code"], worker.OCR_REVIEW_UNAVAILABLE_ERROR)
        self.assertEqual(mock_set_status.await_args.kwargs["progress"]["review_available"], False)
        mock_release.assert_awaited_once_with(pool, 36, "user-1")


if __name__ == "__main__":
    unittest.main()
