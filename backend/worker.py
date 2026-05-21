"""
worker.py -- Stage-specific background workers for durable extraction jobs.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json as _json
import logging
import os
import time
from uuid import uuid4

from . import db as db_mod
from . import extractor
from . import geometry
from . import ocr_runner
from . import logging_config as plog
from . import processor
from . import qwen_layout_apply
from . import page_logger
from .logging_config import configure_logging, current_extraction_id, current_extraction_filename, get_logger
from .object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, get_store
from .config import LLM_URL, LLM_MODEL, WORKER_POLL_SECONDS as POLL_INTERVAL_SECONDS, DEBUG_DUMP_BBOX
from .mlflow_tracing import (
    current_trace_context,
    setup_mlflow,
    trace_named_step,
    trace_pipeline_stage,
    trace_span,
    use_trace_context,
)
from . import mlflow_tracing as mlf


configure_logging()
logger = logging.getLogger(__name__)

setup_mlflow()


def _pipeline_base(
    *,
    extraction: dict | None = None,
    document: dict | None = None,
    job: dict | None = None,
    extraction_id: int | None = None,
) -> dict:
    extraction = extraction or {}
    document = document or {}
    job = job or {}
    return {
        "extraction_id": extraction_id or extraction.get("id") or job.get("extraction_id"),
        "document_id": document.get("id") or extraction.get("document_id") or job.get("document_id"),
        "job_id": job.get("id"),
        "vendor_id": extraction.get("vendor_id") or document.get("vendor_id"),
        "vendor_name": extraction.get("vendor_name"),
        "filename": document.get("filename") or extraction.get("filename"),
        "billing_user_id": (document.get("metadata") or {}).get("billing_user_id"),
    }


def _job_trace_context(job: dict | None) -> dict:
    payload = (job or {}).get("payload") or {}
    trace_context = payload.get("trace_context") or {}
    return trace_context if isinstance(trace_context, dict) else {}


def _payload_with_trace(job: dict | None, payload: dict | None = None) -> dict:
    out = dict(payload or {})
    trace_context = _job_trace_context(job) or current_trace_context()
    if trace_context:
        out["trace_context"] = trace_context
    return out


def _field_location_count(field_locations) -> int:
    if isinstance(field_locations, list):
        return sum(len(v) for v in field_locations if isinstance(v, dict))
    if isinstance(field_locations, dict):
        return len(field_locations)
    return 0


def _result_summary(result) -> dict:
    if isinstance(result, list):
        return {"record_count": len(result)}
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else result
    line_items = fields.get("line_items", []) if isinstance(fields, dict) else []
    return {
        "fields": {
            key: value
            for key, value in fields.items()
            if key not in {"line_items", "boxes"} and not str(key).startswith("_")
        } if isinstance(fields, dict) else {},
        "line_items_count": len(line_items),
        "line_items_sample": line_items[:5] if isinstance(line_items, list) else [],
    }


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
                "source": page.get("source"),
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


async def _maybe_enqueue_postprocess(
    pool,
    extraction_id: int,
    document_id: int | None,
    trace_context: dict | None = None,
) -> None:
    # Lightweight check — avoids loading massive JSONB blobs (result, ocr_data, etc.)
    if not await db_mod.is_postprocess_ready(pool, extraction_id):
        return
    payload = {"extraction_id": extraction_id}
    if trace_context:
        payload["trace_context"] = trace_context
    await db_mod.ensure_job(pool, extraction_id, document_id, "postprocess", payload)


async def _process_normalize(pool, job: dict) -> None:
    store = get_store()
    extraction_id = job["extraction_id"]
    document = await db_mod.get_document(pool, job["document_id"])
    if not document:
        raise ValueError("Document not found for normalize job")
    base = _pipeline_base(document=document, job=job, extraction_id=extraction_id)
    logger.info("── NORMALIZE started ── ext=%s file=%s", extraction_id, document["filename"])

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

    with plog.timed("download") as t:
        raw = store.get_bytes(DOCUMENTS_BUCKET, document["object_key"])
    logger.info("Downloaded %s (%d bytes, %.0fms)", document["filename"], len(raw), t["ms"])
    filename = document["filename"].lower()
    is_pdf = filename.endswith(".pdf")

    # PDF rendering with MLflow tracing
    with trace_span(
        "pdf_rendering",
        kind="TOOL",
        input_data={"filename": document["filename"], "size_bytes": len(raw), "is_pdf": is_pdf},
        attributes=base,
    ) as render_trace:
        with plog.timed("render") as t:
            if is_pdf:
                rendered_pages = await processor.pdf_to_images(raw)
            else:
                rendered_pages = await processor.image_file_to_b64(raw)
        logger.info("Rendered %d %s page(s) (%.0fms)", len(rendered_pages), "PDF" if is_pdf else "image", t["ms"])
        render_trace["output"] = {
            "pages_rendered": len(rendered_pages),
            "document_type": "pdf" if is_pdf else "image",
            "page_dimensions": [
                {"page": p["page_number"], "w": p.get("width", 0), "h": p.get("height", 0)}
                for p in rendered_pages[:5]  # first 5 for brevity
            ],
        }

    # ── Compute per-page unified geometry (digital vs scanned) ──
    with trace_span(
        "page_classification",
        kind="TOOL",
        input_data={"total_pages": len(rendered_pages), "is_pdf": is_pdf},
        attributes=base,
    ) as classify_trace:
        if is_pdf:
            page_sizes = [
                {"page_number": p["page_number"], "width": p.get("width", 0), "height": p.get("height", 0)}
                for p in rendered_pages
            ]
            try:
                with plog.timed("geometry") as t:
                    page_geometry = geometry.compute_pdf_geometry(raw, page_sizes)
                sources = {g.get("source") for g in page_geometry}
                logger.info("PDF geometry computed: %d pages, sources=%s (%.0fms)",
                            len(page_geometry), sources, t["ms"])
            except Exception as exc:
                logger.warning(
                    "PDF text geometry failed for extraction %s: %s; treating pages as scanned",
                    extraction_id,
                    exc,
                    exc_info=True,
                )

                page_geometry = [geometry.image_page_geometry(p["page_number"]) for p in rendered_pages]
            geo_by_page = {g["page_number"]: g for g in page_geometry}
        else:
            # Non-PDF images always need OCR
            geo_by_page = {}
            for p in rendered_pages:
                g = geometry.image_page_geometry(p["page_number"])
                geo_by_page[p["page_number"]] = g

        # Build per-page classification summary for MLflow
        classification_pages = []
        for pn, g in sorted(geo_by_page.items()):
            classification_pages.append({
                "page_number": pn,
                "source": g.get("source", "unknown"),
                "char_count": g.get("char_count", 0),
                "word_count": len(g.get("words") or []),
            })
        digital_count = sum(1 for pg in classification_pages if pg["source"] == "pypdfium")
        scanned_count = len(classification_pages) - digital_count
        classify_trace["output"] = {
            "digital_pages": digital_count,
            "scanned_pages": scanned_count,
            "pages": classification_pages,
        }

    page_rows = []
    scanned_page_numbers = []
    for page in rendered_pages:
        suffix = ".jpg" if page.get("mime_type", "image/jpeg").endswith("jpeg") else ".bin"
        object_key = f"extractions/{extraction_id}/pages/page_{page['page_number']}{suffix}"
        page_bytes = base64.b64decode(page["image_b64"])
        store.put_bytes(ARTIFACTS_BUCKET, object_key, page_bytes, page.get("mime_type", "image/jpeg"))

        geo = geo_by_page.get(page["page_number"], {})
        page_source = geo.get("source")
        if page_source == geometry.SOURCE_SCANNED or page_source is None:
            scanned_page_numbers.append(page["page_number"])

        page_rows.append(
            {
                "page_number": page["page_number"],
                "object_key": object_key,
                "mime_type": page.get("mime_type", "image/jpeg"),
                "width": page.get("width", 0),
                "height": page.get("height", 0),
                "orig_width": page.get("orig_width", page.get("width", 0)),
                "orig_height": page.get("orig_height", page.get("height", 0)),
                "source": page_source,
                "char_count": geo.get("char_count"),
                "word_geometry": geo.get("words"),
            }
        )

    with plog.timed("save_pages") as t:
        await db_mod.save_pages(pool, extraction_id, page_rows)
    logger.info("Saved %d page artifacts (%d digital, %d scanned, %.0fms)",
                len(page_rows), len(page_rows) - len(scanned_page_numbers), len(scanned_page_numbers), t["ms"])
    with trace_named_step(
        "page_routing",
        kind="TOOL",
        input_data={"filename": document["filename"], "is_pdf": is_pdf},
        attributes=base,
    ) as routing_trace:
        routing_trace["output"] = {
            "total_pages": len(page_rows),
            "digital_pages": len(page_rows) - len(scanned_page_numbers),
            "scanned_pages": len(scanned_page_numbers),
            "pages": [
                {
                    "page_number": p["page_number"],
                    "source": p.get("source"),
                    "char_count": p.get("char_count") or 0,
                    "word_count": len(p.get("word_geometry") or []),
                }
                for p in page_rows
            ],
        }
    await db_mod.set_total_pages(pool, extraction_id, len(page_rows))
    await db_mod.update_extraction_progress(
        pool,
        extraction_id,
        {
            "stage": "normalize",
            "message": f"Rendered {len(page_rows)} page(s) ({len(page_rows) - len(scanned_page_numbers)} digital, {len(scanned_page_numbers)} scanned)",
            "total_pages": len(page_rows),
            "digital_pages": len(page_rows) - len(scanned_page_numbers),
            "scanned_pages": len(scanned_page_numbers),
        },
        status="processing",
    )
    await db_mod.update_document_status(pool, document["id"], "normalized")
    logger.info("── NORMALIZE completed ── ext=%s (%d pages: %d digital, %d scanned)",
                extraction_id, len(page_rows),
                len(page_rows) - len(scanned_page_numbers), len(scanned_page_numbers))
    if await _stop_if_cancelled(pool, extraction_id, "normalize", "Cancelled during page rendering"):
        return
    # Run OCR (scanned pages only) and LLM in parallel
    await db_mod.ensure_job(
        pool, extraction_id, document["id"], "ocr",
        _payload_with_trace(
            job,
            {"extraction_id": extraction_id, "scanned_page_numbers": scanned_page_numbers},
        ),
    )
    await db_mod.ensure_job(
        pool,
        extraction_id,
        document["id"],
        "llm",
        _payload_with_trace(job, {"extraction_id": extraction_id}),
    )


async def _process_ocr(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for OCR job")
    base = _pipeline_base(extraction=extraction_row, job=job)
    logger.info("── OCR started ── ext=%s", extraction_id)

    # Determine which pages need OCR (scanned pages only)
    payload = job.get("payload") or {}
    scanned_page_numbers = payload.get("scanned_page_numbers")

    # Load all page metadata (includes digital word_geometry)
    all_page_rows = await db_mod.get_pages(pool, extraction_id)

    # Fallback: if no scanned list in payload (old jobs / NULL source), check pages table
    if scanned_page_numbers is None:
        scanned_page_numbers = [
            p["page_number"] for p in all_page_rows
            if p.get("source") != "pypdfium"
        ]

    if not scanned_page_numbers:
        # All pages are digital — skip PaddleOCR entirely
        logger.info("All %d page(s) are digital — skipping PaddleOCR", len(all_page_rows))

        # Build unified ocr_data from digital word_geometry already stored in pages
        unified = []
        for p in all_page_rows:
            unified.append({
                "page_number": p["page_number"],
                "source": p.get("source", "pypdfium"),
                "char_count": p.get("char_count", 0),
                "word_count": len(p.get("word_geometry") or []),
                "words": p.get("word_geometry") or [],
            })
        with plog.timed("save_geometry") as t:
            await db_mod.save_ocr_data(pool, extraction_id, unified)
        total_w = sum(len(p.get("words") or []) for p in unified)
        logger.info("Unified geometry saved: %d pages, %d words (%.0fms)", len(unified), total_w, t["ms"])
        with trace_named_step(
            "ocr_unified_geometry",
            kind="TOOL",
            input_data={"scanned_page_numbers": scanned_page_numbers},
            attributes=base,
        ) as geometry_trace:
            geometry_trace["output"] = {
                "mode": "digital_geometry_reused",
                "pages": [
                    {
                        "page_number": p.get("page_number"),
                        "source": p.get("source"),
                        "word_count": p.get("word_count", len(p.get("words") or [])),
                    }
                    for p in unified
                ],
                "total_words": sum(len(p.get("words") or []) for p in unified),
            }
        await db_mod.update_job_progress(
            pool,
            job["id"],
            {"stage": "ocr", "pages_processed": 0, "message": "All pages digital — OCR skipped"},
        )
        await db_mod.update_extraction_progress(
            pool,
            extraction_id,
            {"stage": "ocr", "message": "All pages digital — OCR skipped"},
            status="processing",
        )
    else:
        await db_mod.update_extraction_progress(
            pool,
            extraction_id,
            {"stage": "ocr", "message": f"Running OCR on {len(scanned_page_numbers)} scanned page(s)"},
            status="processing",
        )
        # Load only scanned page images for OCR
        scanned_set = set(scanned_page_numbers)
        all_pages_loaded = await _load_pages(pool, extraction_id)
        scanned_pages = [p for p in all_pages_loaded if p["page_number"] in scanned_set]

        with trace_named_step(
            "paddleocr_scanned_pages",
            kind="TOOL",
            input_data={"scanned_page_numbers": scanned_page_numbers},
            attributes=base,
        ) as ocr_trace:
            with plog.timed("paddleocr") as t:
                ocr_pages = await ocr_runner.run_ocr_on_pages(scanned_pages)
            total_w = sum(len(p.get("words") or []) for p in ocr_pages)
            logger.info("PaddleOCR completed: %d scanned pages, %d words (%.0fms)",
                        len(scanned_page_numbers), total_w, t["ms"])
            ocr_trace["output"] = {
                "scanned_pages": len(scanned_page_numbers),
                "total_words": sum(len(p.get("words") or []) for p in ocr_pages),
                "pages": [
                    {
                        "page_number": p.get("page_number"),
                        "word_count": len(p.get("words") or []),
                        "sample_words": [w.get("text") for w in (p.get("words") or [])[:12]],
                    }
                    for p in ocr_pages
                ],
            }

        # Build base geometry from pages table (digital pages have word_geometry)
        base_geometry = []
        for p in all_page_rows:
            base_geometry.append({
                "page_number": p["page_number"],
                "source": p.get("source", "paddleocr"),
                "char_count": p.get("char_count", 0),
                "word_count": len(p.get("word_geometry") or []),
                "words": p.get("word_geometry") or [],
            })

        # Merge scanned OCR results into the base geometry
        unified = geometry.merge_scanned_into_geometry(base_geometry, ocr_pages)
        with plog.timed("save_geometry") as t:
            await db_mod.save_ocr_data(pool, extraction_id, unified)
        total_w = sum(len(p.get("words") or []) for p in unified)
        logger.info("Unified geometry saved: %d pages, %d words (%.0fms)", len(unified), total_w, t["ms"])

        await db_mod.update_job_progress(
            pool,
            job["id"],
            {"stage": "ocr", "pages_processed": len(ocr_pages), "scanned_pages": len(scanned_page_numbers)},
        )

    if await _stop_if_cancelled(pool, extraction_id, "ocr", "Cancelled during OCR"):
        return
    logger.info("── OCR completed ── ext=%s", extraction_id)
    await _maybe_enqueue_postprocess(pool, extraction_id, job["document_id"], _job_trace_context(job))


async def _process_llm(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for LLM job")
    document_row = await db_mod.get_document(pool, job["document_id"]) if job.get("document_id") else None
    base = _pipeline_base(extraction=extraction_row, document=document_row, job=job)
    logger.info("── LLM started ── ext=%s vendor=%s", extraction_id, extraction_row.get("vendor_id"))

    # Resolve the human who owns this extraction for MLflow trace tagging:
    # the explicit uploader (admin acting-as-client) first, else the vendor owner.
    uploader_email: str | None = None
    _owner_uid = base.get("billing_user_id")
    if not _owner_uid and extraction_row.get("vendor_id"):
        _vendor = await db_mod.get_vendor(pool, extraction_row["vendor_id"])
        _owner_uid = (_vendor or {}).get("user_id")
    if _owner_uid:
        _owner = await db_mod.get_user_by_id(pool, str(_owner_uid))
        uploader_email = (_owner or {}).get("email")

    with plog.timed("template") as t:
        tmpl = await db_mod.get_template(pool, extraction_row["vendor_id"])
    logger.info("Template loaded: id=%s (%.0fms)", (tmpl or {}).get("id"), t["ms"])
    req_header = extraction_row.get("header_fields") or ((tmpl.get("header_fields") or []) if tmpl else [])
    req_items = extraction_row.get("line_item_fields") or ((tmpl.get("line_item_fields") or []) if tmpl else [])
    # Template is source of truth; stale extraction record is fallback only
    req_format = (tmpl.get("format_type") if tmpl else None) or extraction_row.get("format_type") or "single_po_multipage"
    logger.info("LLM config: model=%s format=%s headers=%s line_items=%s",
                LLM_MODEL, req_format, req_header, req_items)

    # Always build fresh from current DB fields — no cached system_prompt column read.
    with trace_span(
        "system_prompt_built",
        kind="TOOL",
        input_data={
            "vendor_id": extraction_row["vendor_id"],
            "header_fields": req_header,
            "line_item_fields": req_items,
            "format_type": req_format,
        },
        attributes=base,
    ) as prompt_trace:
        gold_examples = await db_mod.get_gold_examples(pool, extraction_row["vendor_id"])




        _prompt_args = dict(
            header_fields=req_header,
            line_item_fields=req_items,
            instructions=tmpl["prompt_instructions"] if tmpl else None,
            rules=tmpl["extraction_rules"] if tmpl else [],
            format_type=req_format,
            gold_examples=gold_examples,
        )
        # Page 2+ system prompt: fields only (current behaviour)
        system_prompt = extractor.build_system_prompt(**_prompt_args, include_boxes=False)
        # Page 1 system prompt: fields + bounding boxes
        system_prompt_page1 = extractor.build_system_prompt(
            **_prompt_args,
            include_boxes=True,
        )
        prompt_trace["output"] = {
            "prompt_version": getattr(extractor, "PROMPT_VERSION", "unknown"),
            "gold_examples_count": len(gold_examples),
            "prompt_length": len(system_prompt),
            "prompt_page1_length": len(system_prompt_page1),
            "system_prompt": system_prompt,
        }
    if gold_examples:
        logger.info(
            "LLM prompt includes %d value-redacted correction hint set(s) for vendor=%s",
            len(gold_examples), extraction_row["vendor_id"],
        )

    pages = await _load_pages(pool, extraction_id)

    cancel_event = asyncio.Event()
    start = time.perf_counter()

    # Signal LLM stage start so SSE/pipeline updates immediately
    await db_mod.update_extraction_progress(
        pool,
        extraction_id,
        {"stage": "llm", "message": f"Starting vision extraction on {len(pages)} page(s)", "total_pages": len(pages)},
        status="processing",
    )

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
                None,
                page_results_partial=[page_result],
                progress=progress,
            )
        if await db_mod.is_cancel_requested(pool, extraction_id):
            cancel_event.set()

    # Trace name = the PDF filename so the MLflow Traces list is human-readable.
    # `trace_kind` is the stable marker for trace classification/cleanup
    # (don't key off the trace name now that it varies per document).
    _trace_name = extraction_row.get("filename") or f"extraction {extraction_id}"
    with mlf.span(
        _trace_name,
        mlf.AGENT,
        inputs={
            "page_count": len(pages),
            "header_fields": req_header,
            "line_item_fields": req_items,
            "format_type": req_format,
            "model": LLM_MODEL,
        },
    ) as field_trace:
        mlf.trace_tags(
            trace_kind="field_extraction",
            extraction_id=extraction_id,
            filename=extraction_row.get("filename"),
            user=uploader_email,
            vendor=extraction_row.get("vendor_id"),
            model=LLM_MODEL,
            format_type=req_format,
        )
        with plog.timed("extraction") as t:
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
                pipeline_context=base,
                pool=pool,
                system_prompt_page1=system_prompt_page1,
            )
            _result = output.get("result")
        _npr = len(output.get("page_results") or [])
        _nli = len((_result or {}).get("line_items", [])) if isinstance(_result, dict) else 0
        logger.info("Document extracted: %d page results, %d line items, cancelled=%s (%.0fms)",
                    _npr, _nli, output.get("cancelled", False), t["ms"])
        field_trace["outputs"] = {
            "page_results_count": len(output.get("page_results") or []),
            "cancelled": bool(output.get("cancelled")),
            "last_completed_page": output.get("last_completed_page", 0),
            "result": _result_summary(output.get("result")),
        }

    # ── Extract boxes from page 1 result and save to qwen_layout_boxes ──
    _page_results = output.get("page_results") or []
    if tmpl and _page_results:
        _p1 = next((pr for pr in _page_results if pr.get("_page") == 1 and "_error" not in pr), None)
        _p1_boxes = (_p1 or {}).get("boxes")
        if isinstance(_p1_boxes, dict) and _p1_boxes:
            vendor_id_llm = extraction_row["vendor_id"]
            template_id_llm = tmpl["id"]
            # Qwen3-VL uses relative 0-1000 coordinates (not Qwen2.5-VL absolute pixels).
            # Direct normalization: nx = coord / 1000.0 — no alignment factor needed.
            # processor.py already pre-aligns images to multiples of 32 so the model
            # never internally re-pads, keeping the 0-1000 grid exact.
            req_items_set = set(req_items)
            learned_boxes: dict = {}
            for field_key, raw_box in _p1_boxes.items():
                if not isinstance(raw_box, list) or len(raw_box) != 4:
                    continue
                nx0 = max(0.0, raw_box[0] / 1000.0)
                ny0 = max(0.0, raw_box[1] / 1000.0)
                nx1 = min(1.0, raw_box[2] / 1000.0)
                ny1 = min(1.0, raw_box[3] / 1000.0)
                if nx1 <= nx0 or ny1 <= ny0:
                    continue
                field_type = "line_item_column" if field_key in req_items_set else "header"
                learned_boxes[field_key] = {
                    "normalized_box": {"x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1},
                    "field_type": field_type,
                }
            if learned_boxes:
                await db_mod.upsert_qwen_layout_boxes(
                    pool, vendor_id_llm, template_id_llm, extraction_id, learned_boxes,
                )
                logger.info(
                    "Page 1 boxes: saved %d field(s) for vendor=%s template=%s",
                    len(learned_boxes), vendor_id_llm, template_id_llm,
                )


    elapsed_ms = int((time.perf_counter() - start) * 1000)
    _pr = output.get("page_results") or []
    if output.get("cancelled"):
        status = "partial" if _pr else "cancelled"
    else:
        # Don't set "done" here — postprocess worker sets the final status
        # after computing field-to-bounding-box mappings. Keeping "processing"
        # ensures the SSE stream stays open until field_locations are ready.
        status = "processing"

    _pr = output.get("page_results") or []
    _failed_pages = [
        {"page": pr["_page"], "error": pr["_error"], "error_type": page_logger.classify_error_type(pr["_error"])}
        for pr in _pr if "_error" in pr
    ]
    _extracted = len([pr for pr in _pr if "_error" not in pr])
    _result = output.get("result")
    _field_count = page_logger.count_result_fields(_result)
    if output.get("cancelled"):
        _log_status = "partial" if _pr else "cancelled"
    elif _failed_pages:
        _log_status = "partial"
    else:
        _log_status = "done"
    page_logger.append_log({
        "extraction_id": extraction_id,
        "filename": extraction_row.get("filename"),
        "vendor_id": extraction_row.get("vendor_id"),
        "attempt_number": job.get("attempts", 1),
        "total_pages": len(pages),
        "billable_pages": len(pages),
        "digital_pages": sum(1 for p in pages if p.get("source") == "pypdfium"),
        "scanned_pages": sum(1 for p in pages if p.get("source") == "paddleocr"),
        "qwen_extracted_pages": _extracted,
        "qwen_failed_pages": len(_failed_pages),
        "qwen_skipped_pages": len(pages) - len(_pr),
        "field_count": _field_count,
        "empty_result": _field_count == 0,
        "duration_ms": elapsed_ms,
        "status": _log_status,
        "errors": _failed_pages or None,
    })

    with trace_span(
        "llm_result_persisted",
        kind="TOOL",
        input_data={
            "extraction_id": extraction_id,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "page_count": len(pages),
            "last_completed_page": output.get("last_completed_page", 0),
            "cancelled": bool(output.get("cancelled")),
        },
        attributes=base,
    ) as persist_trace:
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
        persist_trace["output"] = {
            "status": status,
            "elapsed_ms": elapsed_ms,
            "result_summary": _result_summary(output.get("result")),
        }
    logger.info("LLM result persisted: status=%s elapsed=%dms", status, elapsed_ms)

    if status == "processing":
        await _maybe_enqueue_postprocess(pool, extraction_id, job["document_id"], _job_trace_context(job))


async def _process_postprocess(pool, job: dict) -> None:
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if not extraction_row:
        raise ValueError("Extraction not found for postprocess job")
    base = _pipeline_base(extraction=extraction_row, job=job)
    logger.info("── POSTPROCESS started ── ext=%s", extraction_id)

    result = extraction_row.get("result")
    if not result:
        raise ValueError("Postprocess prerequisites not satisfied (no result)")

    ocr_data = extraction_row.get("ocr_data")
    page_results = extraction_row.get("page_results")

    # ── Debug dump: save OCR + Qwen outputs for offline analysis ──
    if DEBUG_DUMP_BBOX:
        _project_root = os.path.dirname(os.path.dirname(__file__))
        _debug_dir = os.path.join(_project_root, "bbox", "pdle_output")
        _qwen_dir = os.path.join(_project_root, "bbox", "qwn_output")
        os.makedirs(_debug_dir, exist_ok=True)
        os.makedirs(_qwen_dir, exist_ok=True)
        try:
            if ocr_data:
                with open(os.path.join(_debug_dir, f"ocr_{extraction_id}.json"), "w", encoding="utf-8") as _f:
                    _json.dump(ocr_data, _f, indent=2, ensure_ascii=False)
            with open(os.path.join(_qwen_dir, f"qwen_{extraction_id}.json"), "w", encoding="utf-8") as _f:
                _json.dump({"result": result, "page_results": page_results}, _f, indent=2, ensure_ascii=False)
            logger.debug("Debug dump saved: ocr_%s.json / qwen_%s.json", extraction_id, extraction_id)
        except Exception as _e:
            logger.warning("Failed to save debug dump for extraction %s: %s", extraction_id, _e)

    # ── Build field_locations ──
    vendor_id = extraction_row.get("vendor_id")
    template_id = extraction_row.get("template_id")

    qwen_boxes: dict = {}
    if vendor_id and template_id:
        qwen_boxes = await db_mod.get_qwen_layout_boxes(pool, vendor_id, template_id)

    mapping_engine = "none"

    if qwen_boxes:
        logger.info("Postprocess: qwen_layout_apply (%d learned fields)", len(qwen_boxes))
        pages_db = await db_mod.get_pages(pool, extraction_id)
        ocr_by_page = {p.get("page_number"): p for p in (ocr_data or [])}
        pages_with_words = [
            {
                "page_number": p["page_number"],
                "words": ocr_by_page.get(p["page_number"], {}).get("words") or p.get("word_geometry") or [],
                "width": p.get("width", 0),
                "height": p.get("height", 0),
            }
            for p in pages_db
        ]
        with plog.timed("field_locations") as t:
            field_locations = qwen_layout_apply.build_field_locations_from_layout(
                qwen_boxes, pages_with_words, result,
                page_results=page_results,
            )
            mapping_engine = "qwen_layout"
        logger.info("Field locations built (qwen_layout): %d mappings (%.0fms)",
                    _field_location_count(field_locations), t["ms"])
    else:
        logger.warning("Postprocess: no layout boxes - empty field_locations")
        field_locations = {}

    with trace_named_step(
        "field_mapping",
        kind="TOOL",
        input_data={
            "mapping_engine": mapping_engine,
            "qwen_layout_box_count": len(qwen_boxes or {}),
            "has_ocr_data": bool(ocr_data),
            "page_results_count": len(page_results or []) if isinstance(page_results, list) else 0,
        },
        attributes=base,
    ) as mapping_trace:
        mapping_trace["output"] = {
            "mapping_engine": mapping_engine,
            "field_location_count": _field_location_count(field_locations),
            "field_locations": field_locations,
        }

    # ── Apply spatial memory (Phase 4) ──
    # After field_locations are built, apply saved regions from prior corrections.
    # This reads current document text inside saved geometry regions (never old values).
    try:
        from . import spatial_memory as _sm

        with trace_named_step(
            "spatial_memory.apply",
            kind="TOOL",
            input_data={"field_location_count": _field_location_count(field_locations)},
            attributes=base,
        ) as sm_trace:
            with plog.timed("spatial_memory") as t:
                result, field_locations, sm_applied = await _sm.apply_to_extraction(
                    pool, extraction_id, result,
                    field_locations,
                    page_geometry=ocr_data,
                )
            logger.info("Spatial memory: %d region(s) applied (%.0fms)", sm_applied, t["ms"])
            sm_trace["output"] = {
                "applied_count": sm_applied,
                "result_after_spatial_memory": _result_summary(result),
                "field_location_count": _field_location_count(field_locations),
            }
        if sm_applied:
            # Persist the updated result with spatial memory overrides
            await db_mod.update_extraction_result(
                pool, extraction_id, result, page_results, "processing", None,
            )
            logger.info(
                "Spatial memory: %d field(s) applied for extraction %d",
                sm_applied, extraction_id,
            )
    except Exception as exc:
        logger.warning(
            "Spatial memory apply failed for extraction %d: %s",
            extraction_id, exc,
        )


    with plog.timed("save_field_locs") as t:
        await db_mod.save_field_locations(pool, extraction_id, field_locations)
    logger.info("Field locations saved: %d mappings (%.0fms)",
                _field_location_count(field_locations), t["ms"])
    with trace_named_step(
        "final_result",
        kind="CHAIN",
        input_data={"extraction_id": extraction_id},
        attributes=base,
    ) as final_trace:
        final_trace["output"] = {
            "result": _result_summary(result),
            "field_location_count": _field_location_count(field_locations),
            "status": "done",
        }
    if await _stop_if_cancelled(pool, extraction_id, "postprocess", "Cancelled before completion"):
        return
    await db_mod.set_extraction_status(
        pool,
        extraction_id,
        "done",
        progress={"stage": "postprocess", "message": "Field mapping complete"},
    )
    try:
        tok = await db_mod.get_extraction_token_totals(pool, extraction_id)
        _final = await db_mod.get_extraction(pool, extraction_id) or extraction_row
        _pages = _final.get("total_pages")
        _fields = page_logger.count_result_fields(result)
        _dur = _final.get("duration_ms")
        _stage_tok = plog.current_stage.set("DONE")
        try:
            plog.info(
                "── EXTRACTION COMPLETE ──",
                status="done", pages=_pages,
                tok_in=tok["prompt_tokens"], tok_out=tok["completion_tokens"],
                calls=tok["llm_calls"],
                ms=float(_dur) if _dur else None,
            )
        finally:
            plog.current_stage.reset(_stage_tok)
        plog.result_block(
            extraction_id,
            status="done",
            filename=_final.get("filename"),
            vendor=_final.get("vendor_name") or _final.get("vendor_id"),
            pages=_pages,
            fields=_fields,
            tok_in=tok["prompt_tokens"],
            tok_out=tok["completion_tokens"],
            llm_calls=tok["llm_calls"],
            latency_total_s=(float(_dur) / 1000.0) if _dur else None,
            errors=None,
        )
    except Exception:
        logger.exception("post: result summary failed ext=%s", extraction_id)


async def process_job(pool, stage: str, job: dict) -> None:
    trace_context = _job_trace_context(job)
    ext_id = job.get("extraction_id")
    id_token = current_extraction_id.set(ext_id) if ext_id is not None else None
    # Set filename context for per-extraction log file naming
    extraction = await db_mod.get_extraction(pool, job["extraction_id"]) if job.get("extraction_id") else None
    document = await db_mod.get_document(pool, job["document_id"]) if job.get("document_id") else None
    fname = (document or {}).get("filename") or (extraction or {}).get("filename")
    fn_token = current_extraction_filename.set(fname) if fname else None
    try:
        with use_trace_context(trace_context):
            base = _pipeline_base(extraction=extraction, document=document, job=job)
            with trace_pipeline_stage(
                stage,
                extraction_id=base.get("extraction_id"),
                document_id=base.get("document_id"),
                job_id=base.get("job_id"),
                vendor_id=base.get("vendor_id"),
                filename=base.get("filename"),
                input_data={
                    "job_type": stage,
                    "payload_keys": sorted((job.get("payload") or {}).keys()),
                },
            ) as stage_trace:
                with plog.stage_span(stage, worker=plog.current_worker.get() or "worker"):
                    if stage == "normalize":
                        await _process_normalize(pool, job)
                    elif stage == "ocr":
                        await _process_ocr(pool, job)
                    elif stage == "llm":
                        await _process_llm(pool, job)
                    elif stage == "postprocess":
                        await _process_postprocess(pool, job)
                    else:
                        raise ValueError(f"Unknown worker stage: {stage}")
                stage_trace["output"] = {"status": "completed", "stage": stage}
    finally:
        if id_token is not None:
            current_extraction_id.reset(id_token)
        if fn_token is not None:
            current_extraction_filename.reset(fn_token)


async def run_worker(stage: str, worker_name: str) -> None:
    pool = await db_mod.create_pool()
    await db_mod.init(pool)
    plog.current_worker.set(worker_name)
    logger.info("Worker started stage=%s name=%s", stage, worker_name)
    # Recover orphaned running jobs left by a previous crashed worker
    recovered = await db_mod.recover_stale_jobs(pool, stage, stale_minutes=5)
    if recovered:
        logger.info("Startup recovery: reset %d stale '%s' job(s) to queued", recovered, stage)
    last_recovery = time.monotonic()
    RECOVERY_INTERVAL = 60.0  # seconds between periodic stale-job sweeps
    try:
        while True:
            # Periodic stale-job recovery (handles jobs that get stuck mid-flight)
            now = time.monotonic()
            if now - last_recovery >= RECOVERY_INTERVAL:
                recovered = await db_mod.recover_stale_jobs(pool, stage, stale_minutes=10)
                if recovered:
                    logger.info("Periodic recovery: reset %d stale '%s' job(s) to queued", recovered, stage)
                last_recovery = now

            job = await db_mod.claim_job(pool, stage, worker_name)
            if not job:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            logger.info("Claimed job id=%s stage=%s extraction=%s", job["id"], stage, job.get("extraction_id"))
            try:
                job_start = time.perf_counter()
                await process_job(pool, stage, job)
                await db_mod.complete_job(pool, job["id"], {"stage": stage, "message": "done"})
                elapsed = (time.perf_counter() - job_start) * 1000
                logger.info("Job completed: id=%s stage=%s ext=%s (%.0fms)",
                            job["id"], stage, job.get("extraction_id"), elapsed)
            except Exception as exc:
                logger.exception("Job failed id=%s stage=%s", job["id"], stage)
                if stage in ("normalize", "ocr", "llm", "postprocess") and job.get("extraction_id"):
                    try:
                        _exc_row = await db_mod.get_extraction(pool, job["extraction_id"])
                        _exc_total = (_exc_row or {}).get("total_pages") or 0
                        _exc_elapsed = int((time.perf_counter() - job_start) * 1000)
                        _exc_err = [{"page": None, "error": str(exc), "error_type": page_logger.classify_error_type(str(exc))}]
                        _exc_base = {
                            "extraction_id": job["extraction_id"],
                            "filename": (_exc_row or {}).get("filename"),
                            "vendor_id": (_exc_row or {}).get("vendor_id"),
                            "attempt_number": job.get("attempts", 1),
                            "duration_ms": _exc_elapsed,
                            "errors": _exc_err,
                        }
                        if stage == "normalize":
                            page_logger.append_log({
                                **_exc_base,
                                "total_pages": None,
                                "billable_pages": 0,
                                "digital_pages": None,
                                "scanned_pages": None,
                                "qwen_extracted_pages": 0,
                                "qwen_failed_pages": 0,
                                "qwen_skipped_pages": 0,
                                "field_count": 0,
                                "empty_result": True,
                                "status": "failed_before_page_count",
                            })
                        elif stage == "ocr":
                            page_logger.append_log({
                                **_exc_base,
                                "total_pages": _exc_total or None,
                                "billable_pages": 0,
                                "digital_pages": None,
                                "scanned_pages": None,
                                "qwen_extracted_pages": 0,
                                "qwen_failed_pages": 0,
                                "qwen_skipped_pages": _exc_total,
                                "field_count": 0,
                                "empty_result": True,
                                "status": "failed_ocr_stage",
                            })
                        elif stage == "llm":
                            _exc_pr = (_exc_row or {}).get("page_results") or []
                            _exc_extracted = len([pr for pr in _exc_pr if "_error" not in pr]) if isinstance(_exc_pr, list) else 0
                            _exc_failed_count = len([pr for pr in _exc_pr if "_error" in pr]) if isinstance(_exc_pr, list) else 0
                            page_logger.append_log({
                                **_exc_base,
                                "total_pages": _exc_total or None,
                                "billable_pages": _exc_total,
                                "digital_pages": None,
                                "scanned_pages": None,
                                "qwen_extracted_pages": _exc_extracted,
                                "qwen_failed_pages": _exc_failed_count,
                                "qwen_skipped_pages": max(0, _exc_total - (_exc_extracted + _exc_failed_count)),
                                "field_count": 0,
                                "empty_result": True,
                                "status": "error",
                            })
                        elif stage == "postprocess":
                            _exc_pr = (_exc_row or {}).get("page_results") or []
                            _exc_extracted = len([pr for pr in _exc_pr if "_error" not in pr]) if isinstance(_exc_pr, list) else 0
                            _exc_failed_count = len([pr for pr in _exc_pr if "_error" in pr]) if isinstance(_exc_pr, list) else 0
                            page_logger.append_log({
                                **_exc_base,
                                "total_pages": _exc_total or None,
                                "billable_pages": _exc_total,
                                "digital_pages": None,
                                "scanned_pages": None,
                                "qwen_extracted_pages": _exc_extracted,
                                "qwen_failed_pages": _exc_failed_count,
                                "qwen_skipped_pages": max(0, _exc_total - (_exc_extracted + _exc_failed_count)),
                                "field_count": page_logger.count_result_fields((_exc_row or {}).get("result")),
                                "empty_result": not bool((_exc_row or {}).get("result")),
                                "status": "postprocess_failed",
                            })
                    except Exception:
                        pass
                if job.get("extraction_id"):
                    await db_mod.set_extraction_status(
                        pool,
                        job["extraction_id"],
                        "failed",
                        progress={"stage": stage, "message": str(exc)},
                        error=str(exc),
                    )
                    try:
                        _fr = await db_mod.get_extraction(pool, job["extraction_id"])
                        _st = plog.current_stage.set("DONE")
                        try:
                            plog.error(
                                "── EXTRACTION FAILED ──",
                                failed_stage=stage,
                                pages=(_fr or {}).get("total_pages"),
                            )
                        finally:
                            plog.current_stage.reset(_st)
                        plog.result_block(
                            job["extraction_id"],
                            status="failed",
                            filename=(_fr or {}).get("filename"),
                            vendor=(_fr or {}).get("vendor_name") or (_fr or {}).get("vendor_id"),
                            pages=(_fr or {}).get("total_pages"),
                            failed_stage=stage,
                            errors=[f"{stage}  {type(exc).__name__}: {exc}"],
                        )
                    except Exception:
                        logger.exception("worker: failure summary failed")
                await db_mod.fail_job(pool, job["id"], str(exc), retryable=False)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["normalize", "ocr", "llm", "postprocess"])
    parser.add_argument("--name", default=f"worker-{uuid4().hex[:8]}")
    args = parser.parse_args()
    asyncio.run(run_worker(args.stage, args.name))


if __name__ == "__main__":
    main()
