"""
worker.py -- Stage-specific background workers for durable extraction jobs.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import time
from uuid import uuid4

if __package__:
    from . import db as db_mod
    from . import extractor
    from . import ocr_runner
    from . import processor
    from . import text_matcher
    from .contracts import build_purchase_order_contract
    from .exporter import build_csv_bytes, build_excel_bytes
    from .logging_config import configure_logging
    from .object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, EXPORTS_BUCKET, get_store
else:
    import db as db_mod
    import extractor
    import ocr_runner
    import processor
    import text_matcher
    from contracts import build_purchase_order_contract
    from exporter import build_csv_bytes, build_excel_bytes
    from logging_config import configure_logging
    from object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, EXPORTS_BUCKET, get_store


configure_logging()
logger = logging.getLogger("worker")

LLM_URL = os.getenv("LLM_URL", "http://localhost:8001/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3vl")
POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "1.0"))


async def _load_pages(pool, extraction_id: int) -> list[dict]:
    store = get_store()
    page_rows = await db_mod.get_pages(pool, extraction_id)
    pages = []
    for page in page_rows:
        raw = store.get_bytes(ARTIFACTS_BUCKET, page["object_key"])
        pages.append(
            {
                "page_number": page["page_number"],
                "image_b64": base64.b64encode(raw).decode("ascii"),
                "mime_type": page.get("mime_type", "image/jpeg"),
                "width": page.get("width"),
                "height": page.get("height"),
            }
        )
    return pages


async def _stop_if_cancelled(pool, extraction_id: int, stage: str, message: str) -> bool:
    if not await db_mod.is_cancel_requested(pool, extraction_id):
        return False
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        return True
    status = "partial" if (extraction_row.get("result") or extraction_row.get("page_results")) else "cancelled"
    await db_mod.set_extraction_status(
        pool,
        extraction_id,
        status,
        progress={"stage": stage, "message": message},
    )
    return True


async def _maybe_enqueue_postprocess(pool, extraction_id: int, document_id: int | None) -> None:
    # Lightweight check — avoids loading massive JSONB blobs (result, ocr_data, etc.)
    if not await db_mod.is_postprocess_ready(pool, extraction_id):
        return
    await db_mod.ensure_job(pool, extraction_id, document_id, "postprocess", {"extraction_id": extraction_id})


async def _process_normalize(pool, job: dict) -> None:
    store = get_store()
    extraction_id = job["extraction_id"]
    document = await db_mod.get_document(pool, job["document_id"])
    if not document:
        raise ValueError("Document not found for normalize job")

    await db_mod.update_document_status(pool, document["id"], "processing")
    await db_mod.set_extraction_status(
        pool,
        extraction_id,
        "processing",
        progress={"stage": "normalize", "message": "Rendering document pages"},
    )
    await db_mod.update_job_progress(
        pool,
        job["id"],
        {"stage": "normalize", "message": "Downloading original document"},
    )

    raw = store.get_bytes(DOCUMENTS_BUCKET, document["object_key"])
    filename = document["filename"].lower()
    if filename.endswith(".pdf"):
        rendered_pages = await processor.pdf_to_images(raw)
    else:
        rendered_pages = await processor.image_file_to_b64(raw)

    page_rows = []
    for page in rendered_pages:
        suffix = ".jpg" if page.get("mime_type", "image/jpeg").endswith("jpeg") else ".bin"
        object_key = f"extractions/{extraction_id}/pages/page_{page['page_number']}{suffix}"
        page_bytes = base64.b64decode(page["image_b64"])
        store.put_bytes(ARTIFACTS_BUCKET, object_key, page_bytes, page.get("mime_type", "image/jpeg"))
        page_rows.append(
            {
                "page_number": page["page_number"],
                "object_key": object_key,
                "mime_type": page.get("mime_type", "image/jpeg"),
                "width": page.get("width", 0),
                "height": page.get("height", 0),
            }
        )

    await db_mod.save_pages(pool, extraction_id, page_rows)
    await db_mod.set_total_pages(pool, extraction_id, len(page_rows))
    await db_mod.update_extraction_progress(
        pool,
        extraction_id,
        {
            "stage": "normalize",
            "message": f"Rendered {len(page_rows)} page(s)",
            "total_pages": len(page_rows),
        },
        status="processing",
    )
    await db_mod.update_document_status(pool, document["id"], "normalized")
    if await _stop_if_cancelled(pool, extraction_id, "normalize", "Cancelled during page rendering"):
        return
    # Run OCR and LLM in parallel
    await db_mod.ensure_job(pool, extraction_id, document["id"], "ocr", {"extraction_id": extraction_id})
    await db_mod.ensure_job(pool, extraction_id, document["id"], "llm", {"extraction_id": extraction_id})


async def _process_ocr(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for OCR job")

    await db_mod.update_extraction_progress(
        pool,
        extraction_id,
        {"stage": "ocr", "message": "Running OCR"},
        status="processing",
    )
    pages = await _load_pages(pool, extraction_id)
    ocr_pages = await ocr_runner.run_ocr_on_pages(pages)
    await db_mod.save_ocr_data(pool, extraction_id, ocr_pages)
    await db_mod.update_job_progress(
        pool,
        job["id"],
        {"stage": "ocr", "pages_processed": len(ocr_pages)},
    )
    if await _stop_if_cancelled(pool, extraction_id, "ocr", "Cancelled during OCR"):
        return
    await _maybe_enqueue_postprocess(pool, extraction_id, job["document_id"])


async def _process_llm(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for LLM job")

    tmpl = await db_mod.get_template(pool, extraction_row["vendor_id"])
    req_header = extraction_row.get("header_fields") or (tmpl["header_fields"] if tmpl else [])
    req_items = extraction_row.get("line_item_fields") or (tmpl["line_item_fields"] if tmpl else [])
    req_format = extraction_row.get("format_type") or (tmpl["format_type"] if tmpl else "single_po_multipage")

    if tmpl and tmpl.get("system_prompt"):
        system_prompt = tmpl["system_prompt"]
    else:
        system_prompt = extractor.build_system_prompt(
            req_header,
            req_items,
            tmpl["prompt_instructions"] if tmpl else None,
            tmpl["extraction_rules"] if tmpl else [],
            req_format,
        )

    pages = await _load_pages(pool, extraction_id)
    cancel_event = asyncio.Event()
    start = time.perf_counter()

    async def on_page_done(page_num: int, total_pages: int, page_result: dict | None) -> None:
        progress = {
            "stage": "llm",
            "message": f"Extracting page {page_num}/{total_pages}",
            "page": page_num,
            "total_pages": total_pages,
        }
        await db_mod.update_job_progress(pool, job["id"], progress)
        await db_mod.update_extraction_progress(pool, extraction_id, progress, status="processing")
        if page_result is not None:
            await db_mod.update_extraction_result(
                pool,
                extraction_id,
                None,
                None,
                "processing",
                0,
                page_results_partial=[page_result],
                progress=progress,
            )
        if await db_mod.is_cancel_requested(pool, extraction_id):
            cancel_event.set()

    output = await extractor.extract_document(
        pages=pages,
        header_fields=req_header,
        line_item_fields=req_items,
        system_prompt=system_prompt,
        format_type=req_format,
        llm_url=LLM_URL,
        model=LLM_MODEL,
        on_page_done=on_page_done,
        cancel_event=cancel_event,
        start_from_page=job.get("payload", {}).get("start_from_page", 1),
        existing_page_results=job.get("payload", {}).get("existing_page_results"),
    )

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if output.get("cancelled"):
        status = "partial" if output.get("page_results") else "cancelled"
    else:
        # Don't set "done" here — postprocess worker sets the final status
        # after computing field-to-bounding-box mappings. Keeping "processing"
        # ensures the SSE stream stays open until field_locations are ready.
        status = "processing"

    await db_mod.update_extraction_result(
        pool,
        extraction_id,
        output["result"],
        output["page_results"],
        status,
        elapsed_ms,
        progress={
            "stage": "llm",
            "message": "LLM extraction complete, awaiting field mapping" if status == "processing" else status,
            "last_completed_page": output.get("last_completed_page", 0),
            "total_pages": len(pages),
        },
    )
    if status == "processing":
        await _maybe_enqueue_postprocess(pool, extraction_id, job["document_id"])


async def _process_postprocess(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for postprocess job")

    result = extraction_row.get("result")
    if not result:
        raise ValueError("Postprocess prerequisites not satisfied (no result)")

    ocr_data = extraction_row.get("ocr_data")
    if not ocr_data:
        raise ValueError("Postprocess prerequisites not satisfied (no ocr_data)")

    page_results = extraction_row.get("page_results")

    # Run CPU-bound text matching in a thread executor to avoid blocking
    # the async event loop (prevents healthcheck timeouts / Docker Code 137)
    loop = asyncio.get_running_loop()
    field_locations = await loop.run_in_executor(
        None,
        text_matcher.compute_field_locations,
        result,
        ocr_data,
        page_results,
    )

    await db_mod.save_field_locations(pool, extraction_id, field_locations)
    if await _stop_if_cancelled(pool, extraction_id, "postprocess", "Cancelled before outbound delivery"):
        return
    await db_mod.set_extraction_status(
        pool,
        extraction_id,
        "done",
        progress={"stage": "postprocess", "message": "PaddleOCR field mapping complete"},
    )
    await db_mod.ensure_job(pool, extraction_id, job["document_id"], "outbound", {"extraction_id": extraction_id})


async def _process_outbound(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for outbound job")
    if await _stop_if_cancelled(pool, extraction_id, "outbound", "Cancelled before export delivery"):
        return

    contract = build_purchase_order_contract(extraction_row)
    store = get_store()

    excel_bytes = build_excel_bytes(contract)
    xlsx_key = f"exports/extractions/{extraction_id}/purchase_order.xlsx"
    store.put_bytes(
        EXPORTS_BUCKET,
        xlsx_key,
        excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    csv_bytes = build_csv_bytes(contract)
    csv_key = f"exports/extractions/{extraction_id}/purchase_order.csv"
    store.put_bytes(
        EXPORTS_BUCKET,
        csv_key,
        csv_bytes,
        "text/csv",
    )

    await db_mod.save_export_artifact(pool, extraction_id, xlsx_key)
    await db_mod.upsert_delivery(
        pool,
        extraction_id,
        contract_type=contract["contract_version"],
        target_type="excel",
        status="delivered",
        payload=contract,
        object_key=xlsx_key,
    )
    await db_mod.upsert_delivery(
        pool,
        extraction_id,
        contract_type=contract["contract_version"],
        target_type="csv",
        status="delivered",
        payload=contract,
        object_key=csv_key,
    )


async def process_job(pool, stage: str, job: dict) -> None:
    if stage == "normalize":
        await _process_normalize(pool, job)
    elif stage == "ocr":
        await _process_ocr(pool, job)
    elif stage == "llm":
        await _process_llm(pool, job)
    elif stage == "postprocess":
        await _process_postprocess(pool, job)
    elif stage == "outbound":
        await _process_outbound(pool, job)
    else:
        raise ValueError(f"Unknown worker stage: {stage}")


async def run_worker(stage: str, worker_name: str) -> None:
    pool = await db_mod.create_pool()
    await db_mod.init(pool)
    logger.info("Worker started stage=%s name=%s", stage, worker_name)
    try:
        while True:
            job = await db_mod.claim_job(pool, stage, worker_name)
            if not job:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            logger.info("Claimed job id=%s stage=%s extraction=%s", job["id"], stage, job.get("extraction_id"))
            try:
                await process_job(pool, stage, job)
                await db_mod.complete_job(pool, job["id"], {"stage": stage, "message": "done"})
            except Exception as exc:
                logger.exception("Job failed id=%s stage=%s", job["id"], stage)
                if job.get("extraction_id") and stage != "outbound":
                    await db_mod.set_extraction_status(
                        pool,
                        job["extraction_id"],
                        "failed",
                        progress={"stage": stage, "message": str(exc)},
                        error=str(exc),
                    )
                elif job.get("extraction_id") and stage == "outbound":
                    await db_mod.upsert_delivery(
                        pool,
                        extraction_id=job["extraction_id"],
                        contract_type="purchase_order.v1",
                        target_type="excel",
                        status="failed",
                        error=str(exc),
                    )
                await db_mod.fail_job(pool, job["id"], str(exc), retryable=False)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["normalize", "ocr", "llm", "postprocess", "outbound"])
    parser.add_argument("--name", default=f"worker-{uuid4().hex[:8]}")
    args = parser.parse_args()
    asyncio.run(run_worker(args.stage, args.name))


if __name__ == "__main__":
    main()
