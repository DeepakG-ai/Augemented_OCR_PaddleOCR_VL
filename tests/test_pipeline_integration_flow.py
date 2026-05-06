from __future__ import annotations

import base64
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.worker as worker


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_bytes(self, bucket: str, object_key: str, data: bytes, content_type: str) -> None:
        self.objects[(bucket, object_key)] = data

    def get_bytes(self, bucket: str, object_key: str) -> bytes:
        return self.objects[(bucket, object_key)]


class WorkerFlowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_pipeline_flow_populates_extraction_artifacts(self) -> None:
        pool = object()
        store = _FakeStore()
        store.put_bytes(worker.DOCUMENTS_BUCKET, "documents/V1/input.pdf", b"%PDF-1.4 fake", "application/pdf")

        rendered_pages = [
            {
                "page_number": 1,
                "image_b64": base64.b64encode(b"page-1").decode("ascii"),
                "mime_type": "image/jpeg",
                "width": 800,
                "height": 1000,
            },
            {
                "page_number": 2,
                "image_b64": base64.b64encode(b"page-2").decode("ascii"),
                "mime_type": "image/jpeg",
                "width": 800,
                "height": 1000,
            },
        ]
        ocr_pages = [
            {"page_number": 1, "words": [{"text": "ACME", "box": [1, 2, 30, 20], "score": 0.99}]},
            {"page_number": 2, "words": [{"text": "Widget", "box": [2, 22, 50, 40], "score": 0.97}]},
        ]
        geometry_pages = [
            {"page_number": 1, "source": "paddleocr", "char_count": 0, "word_count": 0, "words": []},
            {"page_number": 2, "source": "paddleocr", "char_count": 0, "word_count": 0, "words": []},
        ]
        llm_output = {
            "result": {
                "vendor_name": "ACME",
                "po_number": "PO-1",
                "line_items": [{"description": "Widget", "qty": 2, "amount": 15.5}],
            },
            "page_results": [
                {"_page": 1, "vendor_name": "ACME", "po_number": "PO-1", "line_items": []},
                {"_page": 2, "line_items": [{"description": "Widget", "qty": 2, "amount": 15.5}]},
            ],
            "cancelled": False,
            "last_completed_page": 2,
        }
        field_locations = {
            "vendor_name": {"page": 1, "box": [1, 2, 30, 20], "strategy": "exact"},
            "line_item_0_description": {"page": 2, "box": [2, 22, 50, 40], "strategy": "exact"},
        }

        document = {
            "id": 31,
            "filename": "invoice.pdf",
            "object_key": "documents/V1/input.pdf",
            "status": "queued",
        }
        extraction = {
            "id": 21,
            "document_id": 31,
            "vendor_id": "V1",
            "vendor_name": "Vendor 1",
            "template_id": 5,
            "filename": "invoice.pdf",
            "total_pages": 0,
            "format_type": "single_po_multipage",
            "header_fields": ["vendor_name", "po_number"],
            "line_item_fields": ["description", "qty", "amount"],
            "result": None,
            "page_results": None,
            "field_locations": None,
            "ocr_data": None,
            "corrected_result": None,
            "correction_meta": None,
            "export_object_key": None,
            "progress": None,
            "cancel_requested": False,
            "status": "queued",
            "error": None,
            "duration_ms": None,
        }
        pages: list[dict] = []
        ensured_jobs: list[str] = []
        deliveries: list[dict] = []

        async def fake_get_document(_pool, document_id: int):
            self.assertEqual(document_id, 31)
            return dict(document)

        async def fake_update_document_status(_pool, document_id: int, status: str):
            self.assertEqual(document_id, 31)
            document["status"] = status

        async def fake_set_extraction_status(_pool, extraction_id: int, status: str, progress=None, error=None, duration_ms=None):
            self.assertEqual(extraction_id, 21)
            extraction["status"] = status
            if progress is not None:
                extraction["progress"] = progress
            extraction["error"] = error
            if duration_ms is not None:
                extraction["duration_ms"] = duration_ms

        async def fake_update_job_progress(_pool, job_id: int, progress: dict):
            self.assertIsInstance(job_id, int)
            self.assertIn("stage", progress)

        async def fake_save_pages(_pool, extraction_id: int, page_rows: list[dict]):
            self.assertEqual(extraction_id, 21)
            pages[:] = [dict(p) for p in page_rows]

        async def fake_set_total_pages(_pool, extraction_id: int, total_pages: int):
            self.assertEqual(extraction_id, 21)
            extraction["total_pages"] = total_pages

        async def fake_update_extraction_progress(_pool, extraction_id: int, progress: dict, status: str | None = None):
            self.assertEqual(extraction_id, 21)
            extraction["progress"] = progress
            if status is not None:
                extraction["status"] = status

        async def fake_ensure_job(_pool, extraction_id: int, document_id: int | None, job_type: str, payload: dict | None = None, priority: int = 100, max_attempts: int = 3):
            self.assertEqual(extraction_id, 21)
            ensured_jobs.append(job_type)
            return {
                "id": len(ensured_jobs),
                "extraction_id": extraction_id,
                "document_id": document_id,
                "job_type": job_type,
                "payload": payload or {},
                "priority": priority,
                "max_attempts": max_attempts,
            }

        async def fake_get_extraction(_pool, extraction_id: int):
            self.assertEqual(extraction_id, 21)
            return dict(extraction)

        async def fake_get_pages(_pool, extraction_id: int):
            self.assertEqual(extraction_id, 21)
            return [dict(p) for p in pages]

        async def fake_save_ocr_data(_pool, extraction_id: int, data: list[dict]):
            self.assertEqual(extraction_id, 21)
            extraction["ocr_data"] = data

        async def fake_update_extraction_result(
            _pool,
            extraction_id: int,
            result,
            page_results,
            status: str,
            duration_ms: int,
            error: str | None = None,
            page_results_partial: list[dict] | None = None,
            progress: dict | None = None,
        ):
            self.assertEqual(extraction_id, 21)
            extraction["status"] = status
            if result is not None:
                extraction["result"] = result
            if page_results is not None:
                extraction["page_results"] = page_results
            if page_results_partial:
                current = list(extraction.get("page_results") or [])
                current.extend(page_results_partial)
                extraction["page_results"] = current
            if progress is not None:
                extraction["progress"] = progress
            if error is not None:
                extraction["error"] = error
            if duration_ms:
                extraction["duration_ms"] = duration_ms

        async def fake_is_cancel_requested(_pool, extraction_id: int):
            self.assertEqual(extraction_id, 21)
            return False

        async def fake_is_postprocess_ready(_pool, extraction_id: int):
            self.assertEqual(extraction_id, 21)
            return extraction.get("result") is not None and extraction.get("ocr_data") is not None

        async def fake_save_field_locations(_pool, extraction_id: int, data: dict):
            self.assertEqual(extraction_id, 21)
            extraction["field_locations"] = data

        async def fake_save_export_artifact(_pool, extraction_id: int, object_key: str):
            self.assertEqual(extraction_id, 21)
            extraction["export_object_key"] = object_key

        async def fake_upsert_delivery(
            _pool,
            extraction_id: int,
            contract_type: str,
            target_type: str,
            status: str,
            payload=None,
            object_key: str | None = None,
            error: str | None = None,
        ):
            deliveries.append(
                {
                    "extraction_id": extraction_id,
                    "contract_type": contract_type,
                    "target_type": target_type,
                    "status": status,
                    "payload": payload,
                    "object_key": object_key,
                    "error": error,
                }
            )
            return deliveries[-1]

        with ExitStack() as stack:
            stack.enter_context(patch.object(worker, "get_store", return_value=store))
            stack.enter_context(patch.object(worker.processor, "pdf_to_images", new=AsyncMock(return_value=rendered_pages)))
            stack.enter_context(patch.object(worker.geometry, "compute_pdf_geometry", return_value=geometry_pages))
            stack.enter_context(patch.object(worker.ocr_runner, "run_ocr_on_pages", new=AsyncMock(return_value=ocr_pages)))
            stack.enter_context(patch.object(worker.extractor, "extract_document", new=AsyncMock(return_value=llm_output)))
            stack.enter_context(patch.object(worker.qwen_layout_apply, "build_field_locations_from_layout", return_value=field_locations))
            stack.enter_context(patch.object(worker, "build_excel_bytes", return_value=b"excel-bytes"))
            stack.enter_context(patch.object(worker.db_mod, "get_document", new=AsyncMock(side_effect=fake_get_document)))
            stack.enter_context(patch.object(worker.db_mod, "update_document_status", new=AsyncMock(side_effect=fake_update_document_status)))
            stack.enter_context(patch.object(worker.db_mod, "set_extraction_status", new=AsyncMock(side_effect=fake_set_extraction_status)))
            stack.enter_context(patch.object(worker.db_mod, "update_job_progress", new=AsyncMock(side_effect=fake_update_job_progress)))
            stack.enter_context(patch.object(worker.db_mod, "save_pages", new=AsyncMock(side_effect=fake_save_pages)))
            stack.enter_context(patch.object(worker.db_mod, "set_total_pages", new=AsyncMock(side_effect=fake_set_total_pages)))
            stack.enter_context(patch.object(worker.db_mod, "update_extraction_progress", new=AsyncMock(side_effect=fake_update_extraction_progress)))
            stack.enter_context(patch.object(worker.db_mod, "ensure_job", new=AsyncMock(side_effect=fake_ensure_job)))
            stack.enter_context(patch.object(worker.db_mod, "get_extraction", new=AsyncMock(side_effect=fake_get_extraction)))
            stack.enter_context(patch.object(worker.db_mod, "get_template", new=AsyncMock(return_value=None)))
            stack.enter_context(patch.object(worker.db_mod, "get_gold_examples", new=AsyncMock(return_value=[])))
            stack.enter_context(patch.object(worker.db_mod, "get_qwen_layout_boxes", new=AsyncMock(return_value={"vendor_name": {"normalized_box": [0.0, 0.0, 0.1, 0.05], "field_type": "header", "page_number": 1}})))
            stack.enter_context(patch.object(worker.db_mod, "upsert_qwen_layout_boxes", new=AsyncMock(return_value=0)))
            stack.enter_context(patch.object(worker.db_mod, "get_spatial_memory_for_layout", new=AsyncMock(return_value=[])))
            stack.enter_context(patch.object(worker.db_mod, "get_pages", new=AsyncMock(side_effect=fake_get_pages)))
            stack.enter_context(patch.object(worker.db_mod, "is_postprocess_ready", new=AsyncMock(side_effect=fake_is_postprocess_ready)))
            stack.enter_context(patch.object(worker.db_mod, "save_ocr_data", new=AsyncMock(side_effect=fake_save_ocr_data)))
            stack.enter_context(patch.object(worker.db_mod, "update_extraction_result", new=AsyncMock(side_effect=fake_update_extraction_result)))
            stack.enter_context(patch.object(worker.db_mod, "is_cancel_requested", new=AsyncMock(side_effect=fake_is_cancel_requested)))
            stack.enter_context(patch.object(worker.db_mod, "save_field_locations", new=AsyncMock(side_effect=fake_save_field_locations)))
            stack.enter_context(patch.object(worker.db_mod, "save_export_artifact", new=AsyncMock(side_effect=fake_save_export_artifact)))
            stack.enter_context(patch.object(worker.db_mod, "upsert_delivery", new=AsyncMock(side_effect=fake_upsert_delivery)))
            await worker._process_normalize(pool, {"id": 1, "extraction_id": 21, "document_id": 31})
            await worker._process_ocr(pool, {"id": 2, "extraction_id": 21, "document_id": 31})
            await worker._process_llm(pool, {"id": 3, "extraction_id": 21, "document_id": 31, "payload": {}})
            await worker._process_postprocess(pool, {"id": 4, "extraction_id": 21, "document_id": 31})
            await worker._process_outbound(pool, {"id": 5, "extraction_id": 21, "document_id": 31})

        self.assertEqual(extraction["total_pages"], 2)
        self.assertEqual(extraction["status"], "done")
        self.assertEqual(extraction["result"]["vendor_name"], "ACME")
        self.assertEqual([p["source"] for p in extraction["ocr_data"]], ["paddleocr", "paddleocr"])
        self.assertEqual([p["words"] for p in extraction["ocr_data"]], [p["words"] for p in ocr_pages])
        self.assertEqual(extraction["field_locations"], field_locations)
        self.assertTrue(extraction["export_object_key"].endswith("purchase_order.xlsx"))
        self.assertEqual(ensured_jobs, ["ocr", "llm", "postprocess", "outbound"])
        self.assertEqual(deliveries[-1]["status"], "delivered")
        self.assertIn((worker.EXPORTS_BUCKET, extraction["export_object_key"]), store.objects)


if __name__ == "__main__":
    unittest.main()
