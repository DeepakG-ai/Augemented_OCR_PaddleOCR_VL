"""
main.py -- FastAPI application with REST endpoints, SSE streaming,
rate limiting, CORS, upload size guard, and lifespan management.

Fields are split into header_fields and line_item_fields.
No hardcoded field registry.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncGenerator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

try:
    from . import db as db_mod
    from . import extractor
    from . import processor
    from . import page_logger
    from .contracts import build_purchase_order_contract
    from . import logging_config as plog
    from .logging_config import configure_logging
    from .models import (
        ExtractionJobStartOut,
        ExtractionOut,
        HealthOut,
        JobOut,
        JobStatusOut,
        TemplateSaveResponse,
        TemplateCreate,
        TemplateListOut,
        TemplateOut,
        VendorAliasCreate,
        VendorAliasOut,
        VendorCreate,
        VendorOut,
    )
    from .object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, EXPORTS_BUCKET, get_store
    from .phoenix_tracing import (
        setup_phoenix,
        get_current_context,
        attach_context,
        detach_context,
        current_trace_context,
        use_trace_context,
        trace_extraction_pipeline,
        trace_extraction_root,
        trace_span,
        trace_named_step,
        trace_file_upload,
        trace_pdf_rendering,
        trace_db_persist,
        trace_paddle_ocr,
    )
except ImportError:
    import db as db_mod
    import extractor
    import processor
    import page_logger  # type: ignore[no-redef]
    from contracts import build_purchase_order_contract
    import logging_config as plog
    from logging_config import configure_logging
    from models import (
        ExtractionJobStartOut,
        ExtractionOut,
        HealthOut,
        JobOut,
        JobStatusOut,
        TemplateSaveResponse,
        TemplateCreate,
        TemplateListOut,
        TemplateOut,
        VendorAliasCreate,
        VendorAliasOut,
        VendorCreate,
        VendorOut,
    )
    from object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, EXPORTS_BUCKET, get_store
    from phoenix_tracing import (
        setup_phoenix,
        get_current_context,
        attach_context,
        detach_context,
        current_trace_context,
        use_trace_context,
        trace_extraction_pipeline,
        trace_extraction_root,
        trace_span,
        trace_named_step,
        trace_file_upload,
        trace_pdf_rendering,
        trace_db_persist,
        trace_paddle_ocr,
    )

from .config import LLM_URL, LLM_MODEL, RATE_LIMIT_PER_MINUTE as RATE_LIMIT, MAX_UPLOAD_BYTES

# ── Centralized logging (replaces inline basicConfig) ───────────────
configure_logging()
logger = logging.getLogger(__name__)

# -- Rate limiter -----------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{RATE_LIMIT}/minute"])

# -- Active extractions tracking for cancel/resume --------------------------
_active_extractions: dict[str, asyncio.Event] = {}


# -- Upload size middleware -------------------------------------------------

class MaxUploadSizeMiddleware(BaseHTTPMiddleware):
    """Reject uploads exceeding MAX_UPLOAD_BYTES.

    Checks Content-Length header first (fast path), then wraps
    request.receive() to count actual streamed bytes -- catches
    chunked-encoding uploads that omit Content-Length.
    """

    async def dispatch(self, request: Request, call_next):
        # Fast path: reject if Content-Length header already exceeds limit
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_BYTES:
            return Response(
                content=json.dumps({"detail": f"Upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit"}),
                status_code=413,
                media_type="application/json",
            )

        # Stream-counting path: intercept receive() to count actual bytes
        total_bytes = 0
        original_receive = request.receive

        async def _counting_receive():
            nonlocal total_bytes
            message = await original_receive()
            body = message.get("body", b"")
            total_bytes += len(body)
            if total_bytes > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit",
                )
            return message

        request._receive = _counting_receive
        return await call_next(request)


# -- Lifespan ---------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB pool + object store + Phoenix. Shutdown: close pool."""
    logger.info("Starting up -- creating DB pool")
    app.state.pool = await db_mod.create_pool()
    await db_mod.init(app.state.pool)
    app.state.store = get_store()
    setup_phoenix()
    logger.info("DB pool, object store, and Phoenix ready")
    yield
    logger.info("Shutting down -- closing connections")
    await app.state.pool.close()


def _guess_mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


async def _load_page_payloads(pool, extraction_id: int) -> list[dict]:
    store = get_store()
    pages = await db_mod.get_pages(pool, extraction_id)
    payloads = []
    for page in pages:
        raw = store.get_bytes(ARTIFACTS_BUCKET, page["object_key"])
        payloads.append(
            {
                "page_number": page["page_number"],
                "image_b64": base64.b64encode(raw).decode("ascii"),
                "mime_type": page.get("mime_type", "image/jpeg"),
                "width": page.get("width"),
                "height": page.get("height"),
            }
        )
    return payloads


async def _submit_ingestion_job(
    pool,
    store,
    *,
    file_bytes: bytes,
    filename: str,
    vendor_id: str,
    format_type: str,
    header_fields: list[str],
    line_item_fields: list[str],
    source_type: str,
    source_ref: str | None = None,
    metadata: dict | None = None,
    trace_context: dict | None = None,
) -> dict:
    tmpl = await db_mod.get_template(pool, vendor_id)
    if not tmpl:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "no_template",
                "vendor_id": vendor_id,
                "hint": "Create a template with at least one field for this vendor before extracting.",
            },
        )
    all_tmpl_fields = list(tmpl.get("header_fields") or []) + list(tmpl.get("line_item_fields") or [])
    if not all_tmpl_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "no_fields",
                "vendor_id": vendor_id,
                "hint": "The template for this vendor has no fields. Add at least one header or line item field before extracting.",
            },
        )

    object_key = f"documents/{vendor_id}/{uuid4().hex}_{filename}"
    mime_type = _guess_mime_type(filename)
    store.put_bytes(DOCUMENTS_BUCKET, object_key, file_bytes, mime_type)
    document_metadata = dict(metadata or {})
    if trace_context:
        document_metadata["trace_context"] = trace_context

    document = await db_mod.create_document(
        pool,
        vendor_id=vendor_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(file_bytes),
        object_key=object_key,
        source_type=source_type,
        source_ref=source_ref,
        metadata=document_metadata,
    )

    template_id = tmpl["id"]
    resolved_format_type = format_type or tmpl["format_type"]
    extraction = await db_mod.create_extraction(
        pool,
        vendor_id=vendor_id,
        template_id=template_id,
        filename=filename,
        total_pages=0,
        format_type=resolved_format_type,
        header_fields=header_fields,
        line_item_fields=line_item_fields,
        document_id=document["id"],
    )
    job = await db_mod.enqueue_job(
        pool,
        extraction_id=extraction["id"],
        document_id=document["id"],
        job_type="normalize",
        payload={
            "extraction_id": extraction["id"],
            "document_id": document["id"],
            "trace_context": trace_context or {},
        },
    )
    await db_mod.set_extraction_status(
        pool,
        extraction["id"],
        "queued",
        progress={"stage": "queued", "message": "Queued for background processing"},
    )
    return {"job": job, "extraction": extraction}


# -- App --------------------------------------------------------------------

app = FastAPI(
    title="Augmented OCR API",
    version="2.0.0",
    description="Production-grade document extraction with dynamic user-defined fields",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(MaxUploadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Global exception handler (logs ALL unhandled errors to terminal) ------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled %s on %s %s:\n%s",
        type(exc).__name__,
        request.method,
        request.url.path,
        traceback.format_exc(),
    )
    return Response(
        content=json.dumps({"detail": "Internal Server Error"}),
        status_code=500,
        media_type="application/json",
    )


# -- Health -----------------------------------------------------------------

@app.get("/health", response_model=HealthOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def health(request: Request):
    try:
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    return HealthOut(status="ok", db=db_status)


# -- Vendors ----------------------------------------------------------------

@app.get("/vendors", response_model=list[VendorOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_vendors(request: Request):
    rows = await db_mod.list_vendors(request.app.state.pool)
    return [VendorOut(**r) for r in rows]


@app.post("/vendors", response_model=VendorOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def create_vendor(request: Request, body: VendorCreate):
    pool = request.app.state.pool
    row = await db_mod.upsert_vendor(pool, body.id, body.name)
    # Auto-insert vendor name as a detection alias
    try:
        await db_mod.insert_vendor_alias(pool, body.id, body.name.lower(), weight=1, source="auto_from_name")
    except Exception as exc:
        logger.warning("Failed to auto-insert vendor alias for %s: %s", body.id, exc)
    return VendorOut(**row)


# -- Vendor Aliases ---------------------------------------------------------
# Registered BEFORE DELETE /vendors/{vendor_id} so that
# DELETE /vendors/aliases/{alias_id} is not swallowed by the broader route.

@app.get("/vendors/{vendor_id}/aliases", response_model=list[VendorAliasOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_aliases(request: Request, vendor_id: str):
    pool = request.app.state.pool
    return await db_mod.list_vendor_aliases(pool, vendor_id)


@app.post("/vendors/{vendor_id}/aliases", response_model=VendorAliasOut, status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def add_alias(request: Request, vendor_id: str, body: VendorAliasCreate):
    pool = request.app.state.pool
    row = await db_mod.insert_vendor_alias(pool, vendor_id, body.pattern, body.weight, source="manual")
    if row is None:
        raise HTTPException(409, detail="Alias pattern already exists for this vendor")
    return row


@app.delete("/vendors/aliases/{alias_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_alias(request: Request, alias_id: int):
    pool = request.app.state.pool
    deleted = await db_mod.delete_vendor_alias(pool, alias_id)
    if not deleted:
        raise HTTPException(404, detail="Alias not found")
    return {"status": "deleted", "alias_id": alias_id}


@app.delete("/vendors/{vendor_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_vendor(request: Request, vendor_id: str):
    pool = request.app.state.pool
    deleted = await db_mod.delete_vendor(pool, vendor_id)
    if not deleted:
        raise HTTPException(404, detail="Vendor not found")
    return {"status": "deleted", "vendor_id": vendor_id}


# -- Vendor Detection -------------------------------------------------------

@app.post("/detect-vendor")
@limiter.limit("10/minute")
async def detect_vendor_endpoint(request: Request, file: UploadFile = File(...)):
    """Detect vendor from uploaded PDF/image by analyzing page-1 text.

    Renders page 1 only, extracts text via pypdfium2 (digital) or PaddleOCR
    (scanned fallback), then matches against vendor_aliases in DB.

    Returns:
        200: {detected: true, vendor_id, vendor_name, score, page_source, matched_patterns}
        409: {detected: false, reason: "unknown_vendor", hint: "..."}
    """
    try:
        from . import geometry
        from . import vendor_detector
        from . import ocr_runner
    except ImportError:
        import geometry
        import vendor_detector
        import ocr_runner

    pool = request.app.state.pool
    file_bytes = await file.read()
    filename = (file.filename or "unknown").lower()

    # Render page 1 only
    if filename.endswith(".pdf"):
        rendered = await processor.pdf_to_images(file_bytes, max_pages=1)
    else:
        rendered = await processor.image_file_to_b64(file_bytes)

    if not rendered:
        raise HTTPException(400, detail="Could not render any pages from the uploaded file")

    page1 = rendered[0]
    page1_meta = {"page_number": 1, "width": page1.get("width", 0), "height": page1.get("height", 0)}

    # Get page-1 text via geometry (digital-first, scanned-fallback)
    page_source = "paddleocr"
    if filename.endswith(".pdf"):
        geo_pages = geometry.compute_pdf_geometry(file_bytes, [page1_meta])
        geo_page = geo_pages[0] if geo_pages else {}
        page_words = geo_page.get("words", [])
        page_source = geo_page.get("source") or page_source
    else:
        page_words = []

    if not page_words:
        ocr_pages = await ocr_runner.run_ocr_on_pages([{
            "page_number": 1,
            "image_b64": page1["image_b64"],
            "mime_type": page1.get("mime_type", "image/jpeg"),
        }])
        page_words = ocr_pages[0].get("words", []) if ocr_pages else []
        page_source = "paddleocr"

    match = await vendor_detector.detect_vendor(pool, page_words)
    if match is None:
        raise HTTPException(
            status_code=409,
            detail={
                "detected": False,
                "reason": "unknown_vendor",
                "hint": "Create the vendor and vendor id first, then retry this document.",
                "page_source": page_source,
                "word_count": len(page_words),
            },
        )

    return {
        "detected": True,
        "vendor_id": match.vendor_id,
        "vendor_name": match.vendor_name,
        "score": match.score,
        "page_source": page_source,
        "matched_patterns": match.matched_patterns,
    }
@app.get("/vendors/{vendor_id}/template", response_model=TemplateOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_template(request: Request, vendor_id: str):
    tmpl_row = await db_mod.get_template(request.app.state.pool, vendor_id)
    if not tmpl_row:
        raise HTTPException(404, detail="No template configured for this vendor")
        
    tmpl = dict(tmpl_row)
    try:
        from . import extractor
        tmpl["user_prompt"] = extractor.build_user_message(
            tmpl.get("header_fields") or [],
            tmpl.get("line_item_fields") or [],
            page_num=1,
            total_pages=1
        )
    except Exception as e:
        logger.warning("Failed to build user prompt preview: %s", e)
        tmpl["user_prompt"] = "Error building preview"
        
    return TemplateOut(**tmpl)


@app.get("/vendors/{vendor_id}/gold-corrections")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_vendor_gold_corrections(request: Request, vendor_id: str):
    """Return latest saved gold correction per field for UI warnings."""
    fields = await db_mod.get_latest_gold_correction_fields(request.app.state.pool, vendor_id)
    return {"vendor_id": vendor_id, "fields": fields}


@app.get("/extractions/{extraction_id}/spatial-memory-fields")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_spatial_memory_fields(request: Request, extraction_id: int):
    """Return field keys that have active spatial memory for this extraction's vendor+layout."""
    pool = request.app.state.pool
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")

    vendor_id = extraction.get("vendor_id")
    template_id = extraction.get("template_id")
    if not vendor_id:
        return {"extraction_id": extraction_id, "fields": {}}

    try:
        from .layout_key import compute_layout_key
    except ImportError:
        from layout_key import compute_layout_key
    lk = compute_layout_key(vendor_id, template_id, [])

    memories = await db_mod.get_spatial_memory_for_layout(pool, vendor_id, lk)
    fields = {m["field_key"]: {"page": m["page_number"]} for m in memories}
    return {"extraction_id": extraction_id, "vendor_id": vendor_id, "layout_key": lk, "fields": fields}


@app.post("/vendors/{vendor_id}/template", response_model=TemplateSaveResponse)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def save_template(request: Request, vendor_id: str, body: TemplateCreate):
    pool = request.app.state.pool

    # Auto-upsert vendor — frontend may have created vendor locally while offline.
    # Use vendor_name from body if provided, otherwise fall back to vendor_id as name.
    vendor_name = (body.vendor_name or vendor_id).upper()
    await db_mod.upsert_vendor(pool, vendor_id, vendor_name)

    logger.info(
        "Saving template for vendor=%s format=%s headers=%s items=%s",
        vendor_id, body.format_type, body.header_fields, body.line_item_fields,
    )

    try:

        # Build prompt fresh from current fields
        gold_examples = await db_mod.get_gold_examples(pool, vendor_id)
        prompt_hash = extractor.compute_prompt_hash(
            body.header_fields, body.line_item_fields,
            body.prompt_instructions, body.extraction_rules or [],
            body.format_type, gold_examples=gold_examples,
        )
        system_prompt = extractor.build_system_prompt(
            body.header_fields, body.line_item_fields,
            body.prompt_instructions, body.extraction_rules or [],
            body.format_type, gold_examples=gold_examples,
        )
        await db_mod.upsert_template(
            pool, vendor_id, body.format_type,
            body.header_fields, body.line_item_fields,
            body.prompt_instructions, body.extraction_rules or [],
            system_prompt, prompt_hash,
        )

        tmpl = await db_mod.get_template(pool, vendor_id)
        logger.info("Template saved OK vendor=%s hash=%s", vendor_id, prompt_hash[:12])

        # Remove stale layout boxes and spatial memory for fields no longer in template
        valid_fields = list(body.header_fields or []) + list(body.line_item_fields or [])
        if tmpl:
            stale_boxes = await db_mod.delete_stale_qwen_layout_boxes(
                pool, vendor_id, tmpl["id"], valid_fields
            )
            stale_mem = await db_mod.delete_stale_spatial_memory(pool, vendor_id, valid_fields)
            if stale_boxes or stale_mem:
                logger.info(
                    "Template change cleanup: vendor=%s removed %d layout boxes, %d spatial memory rows",
                    vendor_id, stale_boxes, stale_mem,
                )

        return TemplateSaveResponse(
            template_id=tmpl["id"],
            prompt_hash=prompt_hash,
            system_prompt_preview=system_prompt[:200],
        )
    except Exception:
        logger.error("Template save FAILED vendor=%s:\n%s", vendor_id, traceback.format_exc())
        raise


# -- Extraction (with SSE progress streaming) ------------------------------

@app.post("/extract")
@limiter.limit("10/minute")
async def extract(
    request: Request,
    file: UploadFile = File(...),
    vendor_id: str = Form(...),
    header_fields: str = Form(None),       # JSON string list, optional
    line_item_fields: str = Form(None),    # JSON string list, optional
    format_type: str = Form(None),         # optional, falls back to template
):
    """
    Upload a PDF/image, extract fields via LLM.
    Returns SSE stream with page-by-page progress + final result.
    """
    raise HTTPException(
        status_code=410,
        detail="The SSE /extract endpoint is deprecated. Use POST /ingest/ui then GET /jobs/{job_id}/stream for real-time progress.",
    )


# -- Cancel / Resume --------------------------------------------------------

@app.post("/extract/cancel/{extraction_id}")
async def cancel_extraction(extraction_id: int):
    """Set the cancellation flag for a running extraction."""
    raise HTTPException(
        status_code=410,
        detail="The legacy /extract/cancel endpoint is deprecated. Use /jobs/extractions/{extraction_id}/cancel.",
    )

    cancel_event = _active_extractions.get(extraction_id)
    if not cancel_event:
        raise HTTPException(404, detail="No active extraction with that ID")
    cancel_event.set()
    return {"status": "cancelling", "extraction_id": extraction_id}


@app.post("/extract/resume/{extraction_id}")
@limiter.limit("10/minute")
async def resume_extraction(request: Request, extraction_id: int):
    """Resume an extraction from the last completed page."""
    raise HTTPException(
        status_code=410,
        detail="The legacy /extract/resume endpoint is deprecated. Use /jobs/extractions/{extraction_id}/resume.",
    )

    pool = request.app.state.pool

    # 1. Load existing extraction record
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")

    if extraction["status"] not in ("partial", "cancelled", "failed"):
        raise HTTPException(400, detail=f"Cannot resume extraction with status '{extraction['status']}'")

    vendor_id = extraction["vendor_id"]
    filename = extraction["filename"]

    # 2. Load template and existing data
    tmpl = await db_mod.get_template(pool, vendor_id)

    req_header = extraction.get("header_fields") or (tmpl["header_fields"] if tmpl else [])
    req_items = extraction.get("line_item_fields") or (tmpl["line_item_fields"] if tmpl else [])
    # Prefer current template format over stale extraction record — template is the source of truth
    req_format = (tmpl["format_type"] if tmpl else None) or extraction.get("format_type") or "single_po_multipage"

    # Get system prompt from template, or build fresh from extraction's saved fields
    if tmpl and tmpl.get("system_prompt"):
        system_prompt = tmpl["system_prompt"]
    else:
        instructions = tmpl["prompt_instructions"] if tmpl else None
        rules = tmpl["extraction_rules"] if tmpl else []
        system_prompt = extractor.build_system_prompt(
            req_header, req_items, instructions, rules, req_format
        )

    # 3. Get page images and existing results
    pages = await db_mod.get_pages(pool, extraction_id)
    if not pages:
        raise HTTPException(400, detail="No page images found for this extraction")

    existing_page_results = extraction.get("page_results") or []
    # Find pages that succeeded (no _error)
    completed_page_nums = {pr.get("_page") for pr in existing_page_results
                           if "_error" not in pr and pr.get("_page") is not None}
    # Find the first page that still needs work (failed or never attempted)
    all_page_nums = {p["page_number"] for p in pages}
    missing_pages = sorted(all_page_nums - completed_page_nums)
    start_from = missing_pages[0] if missing_pages else len(pages) + 1

    # 4. SSE streaming response for resume
    cancel_event = asyncio.Event()
    _active_extractions[extraction_id] = cancel_event

    # Capture OTel context for SSE generator
    parent_otel_ctx = get_current_context()

    async def event_stream() -> AsyncGenerator[str, None]:
        ctx_token = attach_context(parent_otel_ctx)

        try:
          with trace_extraction_pipeline(
            extraction_id, vendor_id, filename, len(pages),
            req_format, req_header, req_items,
          ) as pipeline:
            start = time.perf_counter()
            error_msg: str | None = None
            final_result = None
            page_results = None
            was_cancelled = False
            last_completed_page = start_from - 1

            yield f"data: {json.dumps({'event': 'resume', 'status': 'resuming', 'start_from_page': start_from, 'total_pages': len(pages)})}\n\n"

            async def on_page_done(page_num: int, total_pages: int, page_result: dict | None) -> None:
                progress_events.append({
                    "event": "progress", "status": "processing",
                    "extraction_id": extraction_id,
                    "page": page_num, "total_pages": total_pages,
                })
                if page_result is not None:
                    try:
                        await db_mod.update_extraction_result(
                            pool, extraction_id, None, None, "processing", 0,
                            page_results_partial=[page_result],
                        )
                    except Exception as exc:
                        logger.warning("Failed to save incremental page result: %s", exc)

            progress_events: list[dict] = []

            try:
                extract_task = asyncio.create_task(
                    extractor.extract_document(
                        pages=pages,
                        header_fields=req_header,
                        line_item_fields=req_items,
                        system_prompt=system_prompt,
                        format_type=req_format,
                        llm_url=LLM_URL,
                        model=LLM_MODEL,
                        on_page_done=on_page_done,
                        cancel_event=cancel_event,
                        start_from_page=start_from,
                        existing_page_results=existing_page_results,
                    )
                )

                last_sent = 0
                while not extract_task.done():
                    await asyncio.sleep(0.3)
                    while last_sent < len(progress_events):
                        yield f"data: {json.dumps(progress_events[last_sent])}\n\n"
                        last_sent += 1

                output = await extract_task
                final_result = output["result"]
                page_results = output["page_results"]
                was_cancelled = output.get("cancelled", False)
                last_completed_page = output.get("last_completed_page", 0)

                while last_sent < len(progress_events):
                    yield f"data: {json.dumps(progress_events[last_sent])}\n\n"
                    last_sent += 1

            except Exception as exc:
                error_msg = str(exc)
                logger.error("Resume extraction %d failed: %s", extraction_id, error_msg)

            elapsed_ms = int((time.perf_counter() - start) * 1000)
            _active_extractions.pop(extraction_id, None)

            if error_msg:
                pipeline["status"] = "failed"
                pipeline["error"] = error_msg
                await db_mod.update_extraction_result(
                    pool, extraction_id, None, None, "failed", elapsed_ms, error=error_msg
                )
                yield f"data: {json.dumps({'event': 'error', 'status': 'failed', 'error': error_msg})}\n\n"
            elif was_cancelled:
                status = "partial" if page_results else "cancelled"
                pipeline["status"] = status
                pipeline["result"] = final_result
                await db_mod.update_extraction_result(
                    pool, extraction_id, final_result, page_results, status, elapsed_ms
                )
                yield f"data: {json.dumps({'event': 'cancelled', 'status': status, 'extraction_id': extraction_id, 'result': final_result, 'page_results': page_results, 'last_completed_page': last_completed_page, 'total_pages': len(pages), 'duration_ms': elapsed_ms})}\n\n"
            else:
                # Trace PaddleOCR geometry for review.
                field_locations = {}
                try:
                    try:
                        from . import ocr_runner
                    except ImportError:
                        import ocr_runner  # type: ignore[no-redef]

                    with trace_paddle_ocr(len(pages)) as ocr_ctx:
                        ocr_pages = await ocr_runner.run_ocr_on_pages(pages)
                        ocr_ctx["pages_processed"] = len(ocr_pages)
                        ocr_ctx["total_words"] = sum(len(p.get("words", [])) for p in ocr_pages)

                    with trace_db_persist(extraction_id, "persist_ocr"):
                        await db_mod.save_ocr_data(pool, extraction_id, ocr_pages)
                        await db_mod.save_field_locations(pool, extraction_id, field_locations)

                except Exception as ocr_exc:
                    logger.warning("OCR geometry save failed for resume %d: %s", extraction_id, ocr_exc)

                # ── Trace: Final DB persist ──
                with trace_db_persist(extraction_id, "persist_result"):
                    await db_mod.update_extraction_result(
                        pool, extraction_id, final_result, page_results, "done", elapsed_ms
                    )

                pipeline["status"] = "done"
                pipeline["result"] = final_result
                yield f"data: {json.dumps({'event': 'done', 'status': 'done', 'extraction_id': extraction_id, 'result': final_result, 'field_locations': field_locations, 'total_pages': len(pages), 'duration_ms': elapsed_ms})}\n\n"

        finally:
            detach_context(ctx_token)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# -- Durable Job APIs -------------------------------------------------------

@app.post("/ingest/{source_type}", response_model=ExtractionJobStartOut)
@limiter.limit("10/minute")
async def ingest_document(
    request: Request,
    source_type: str,
    file: UploadFile = File(...),
    vendor_id: str | None = Form(None),
    format_type: str = Form(None),          # None → always defer to template
    header_fields: str = Form(None),
    line_item_fields: str = Form(None),
    source_ref: str = Form(None),
):
    if source_type not in {"ui", "rest", "email", "s3", "sftp", "partner"}:
        raise HTTPException(400, detail=f"Unsupported source_type '{source_type}'")

    pool = request.app.state.pool
    file_bytes = await file.read()
    filename = file.filename or "unknown"
    detected_vendor = None
    req_header = json.loads(header_fields) if header_fields else []
    req_items = json.loads(line_item_fields) if line_item_fields else []
    plog.event(
        "file_received",
        stage="ingest",
        filename=filename,
        source_type=source_type,
        size_bytes=len(file_bytes),
        vendor_id=vendor_id,
    )

    with trace_extraction_pipeline(
        0,
        vendor_id or "auto_detect",
        filename,
        0,
        format_type or "template_default",
        req_header,
        req_items,
    ) as pipeline:
        trace_context = current_trace_context()

        with trace_named_step(
            "vendor_detection",
            input_data={"filename": filename, "preselected_vendor_id": vendor_id},
        ) as vendor_trace:
            # If vendor_id not provided, detect from page-1 text
            if not vendor_id:
                try:
                    from . import geometry as _geo
                    from . import vendor_detector as _vd
                    from . import ocr_runner as _ocr
                except ImportError:
                    import geometry as _geo
                    import vendor_detector as _vd
                    import ocr_runner as _ocr

                # Render page 1 only for detection
                with plog.timed("page1_rendered_for_vendor_detection", stage="vendor_detection", filename=filename) as log_ctx:
                    if filename.lower().endswith(".pdf"):
                        rendered = await processor.pdf_to_images(file_bytes, max_pages=1)
                    else:
                        rendered = await processor.image_file_to_b64(file_bytes)
                    log_ctx["pages_rendered"] = len(rendered)

                if not rendered:
                    plog.event(
                        "vendor_detection_failed",
                        stage="vendor_detection",
                        filename=filename,
                        status="error",
                        reason="no_rendered_pages",
                    )
                    raise HTTPException(400, detail="Could not render any pages from the uploaded file")

                page1 = rendered[0]
                page1_meta = {"page_number": 1, "width": page1.get("width", 0), "height": page1.get("height", 0)}

                # Digital-first text extraction
                if filename.lower().endswith(".pdf"):
                    with plog.timed("page1_pypdfium_geometry", stage="vendor_detection", filename=filename) as log_ctx:
                        geo_pages = _geo.compute_pdf_geometry(file_bytes, [page1_meta])
                        geo_page = geo_pages[0] if geo_pages else {}
                        log_ctx["page_source"] = geo_page.get("source")
                        log_ctx["char_count"] = geo_page.get("char_count", 0)
                        log_ctx["word_count"] = len(geo_page.get("words", []) or [])
                    page_words = geo_pages[0].get("words", []) if geo_pages else []
                    page_source = (geo_pages[0].get("source") if geo_pages else None) or "paddleocr"
                else:
                    page_words = []
                    page_source = "paddleocr"

                # Scanned fallback if needed
                if not page_words:
                    plog.event(
                        "page1_no_digital_words",
                        stage="vendor_detection",
                        filename=filename,
                        fallback="paddleocr",
                    )
                    with plog.timed("page1_paddleocr_fallback", stage="vendor_detection", filename=filename) as log_ctx:
                        ocr_pages = await _ocr.run_ocr_on_pages([{
                            "page_number": 1,
                            "image_b64": page1["image_b64"],
                            "mime_type": page1.get("mime_type", "image/jpeg"),
                        }])
                        if ocr_pages:
                            page_words = ocr_pages[0].get("words", [])
                        log_ctx["word_count"] = len(page_words)
                        log_ctx["sample_words"] = [w.get("text") for w in page_words[:12]]
                    page_source = "paddleocr"

                with plog.timed("vendor_matched", stage="vendor_detection", filename=filename, word_count=len(page_words)) as log_ctx:
                    match = await _vd.detect_vendor(pool, page_words)
                    if match is not None:
                        log_ctx["vendor_id"] = match.vendor_id
                        log_ctx["vendor_name"] = match.vendor_name
                        log_ctx["match_type"] = getattr(match, "match_type", "unknown")
                        log_ctx["score"] = match.score
                        log_ctx["matched_patterns"] = match.matched_patterns
                if match is None:
                    vendor_trace["output"] = {
                        "detected": False,
                        "reason": "unknown_vendor",
                        "page_source": page_source,
                        "word_count": len(page_words),
                    }
                    plog.event(
                        "unknown_vendor_blocked",
                        stage="vendor_detection",
                        filename=filename,
                        status="blocked",
                        word_count=len(page_words),
                        hint="Create the vendor and vendor id first, then retry this document.",
                    )
                    page_logger.append_log({
                        "extraction_id": None,
                        "filename": filename,
                        "vendor_id": None,
                        "total_pages": None,
                        "billable_pages": 0,
                        "qwen_extracted_pages": 0,
                        "qwen_failed_pages": 0,
                        "qwen_skipped_pages": 0,
                        "duration_ms": None,
                        "status": "blocked_unknown_vendor",
                        "errors": None,
                    })
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "reason": "unknown_vendor",
                            "hint": "Create the vendor and vendor id first, then retry this document.",
                            "word_count": len(page_words),
                        },
                    )
                vendor_id = match.vendor_id
                detected_vendor = {
                    "vendor_id": match.vendor_id,
                    "vendor_name": match.vendor_name,
                    "score": match.score,
                    "match_type": getattr(match, "match_type", "unknown"),
                    "matched_patterns": match.matched_patterns,
                }
                vendor_trace["output"] = {
                    "detected": True,
                    "page_source": page_source,
                    "word_count": len(page_words),
                    **detected_vendor,
                }
            else:
                vendor_trace["output"] = {"mode": "preselected", "vendor_id": vendor_id}

        submitted = await _submit_ingestion_job(
            pool,
            request.app.state.store,
            file_bytes=file_bytes,
            filename=filename,
            vendor_id=vendor_id,
            format_type=format_type,
            header_fields=req_header,
            line_item_fields=req_items,
            source_type=source_type,
            source_ref=source_ref,
            trace_context=trace_context,
        )
        resp = ExtractionJobStartOut(
            job_id=submitted["job"]["id"],
            extraction_id=submitted["extraction"]["id"],
            status=submitted["job"]["status"],
        )
        # Include detection result in response if auto-detected
        result = resp.model_dump() if hasattr(resp, "model_dump") else resp.dict()
        if detected_vendor:
            result["detected_vendor"] = detected_vendor
        pipeline["status"] = "queued"
        pipeline["extraction_id"] = submitted["extraction"]["id"]
        pipeline["document_id"] = submitted["extraction"].get("document_id")
        pipeline["job_id"] = submitted["job"]["id"]
        pipeline["vendor_id"] = vendor_id
        pipeline["result"] = {
            "extraction_id": submitted["extraction"]["id"],
            "job_id": submitted["job"]["id"],
            "vendor_id": vendor_id,
            "vendor_name": (detected_vendor or {}).get("vendor_name"),
        }
        plog.event(
            "ingestion_job_created",
            stage="ingest",
            extraction_id=submitted["extraction"]["id"],
            document_id=submitted["extraction"].get("document_id"),
            job_id=submitted["job"]["id"],
            vendor_id=vendor_id,
            vendor_name=(detected_vendor or {}).get("vendor_name"),
            filename=filename,
            status=submitted["job"]["status"],
            template_id=submitted["extraction"].get("template_id"),
            format_type=submitted["extraction"].get("format_type"),
            log_paths=plog.log_paths(submitted["extraction"]["id"]),
        )
        return result


@app.get("/jobs/{job_id}", response_model=JobStatusOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_job_status(request: Request, job_id: int):
    pool = request.app.state.pool
    job = await db_mod.get_job(pool, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    extraction = await db_mod.get_extraction(pool, job["extraction_id"]) if job.get("extraction_id") else None
    if extraction and extraction.get("status") not in {"done", "failed", "partial", "cancelled"}:
        latest_job = await db_mod.get_latest_job_for_extraction(pool, extraction["id"])
        if latest_job:
            job = latest_job
    return JobStatusOut(
        job=JobOut(**job),
        extraction=ExtractionOut(**extraction) if extraction else None,
    )


@app.get("/jobs/{job_id}/stream")
async def stream_job_status_sse(request: Request, job_id: int):
    """SSE stream for real-time job progress.

    One persistent connection replaces client-side polling.
    The server checks the DB every ~1 second and pushes changes
    as SSE events. Heavy JSONB fields (result, ocr_data, …) are
    only included in the terminal event to keep progress messages tiny.

    No rate limiter — this is one long-lived connection, not repeated requests.
    """
    pool = request.app.state.pool
    job = await db_mod.get_job(pool, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    _HEAVY_KEYS = frozenset({
        "result", "page_results", "ocr_data", "field_locations",
        "corrected_result", "correction_meta",
    })

    def _serialize(obj):
        """JSON serializer for datetime and other non-serializable types."""
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    def _slim(ext):
        """Strip heavy JSONB fields for progress events."""
        if not ext:
            return None
        return {k: v for k, v in ext.items() if k not in _HEAVY_KEYS}

    async def _generate():
        last_fingerprint = None

        while True:
            if await request.is_disconnected():
                break

            current_job = await db_mod.get_job(pool, job_id)
            if not current_job:
                yield f"data: {json.dumps({'event': 'error', 'error': 'Job not found'})}\n\n"
                break

            extraction = None
            if current_job.get("extraction_id"):
                extraction = await db_mod.get_extraction(pool, current_job["extraction_id"])
                
                # Fetch the latest job for this extraction because the pipeline
                # creates new sequential jobs for ocr, llm, and postprocess.
                if extraction and extraction.get("status") not in {"done", "failed", "partial", "cancelled"}:
                    latest_job = await db_mod.get_latest_job_for_extraction(pool, extraction["id"])
                    if latest_job:
                        current_job = latest_job

            ext_status = extraction["status"] if extraction else None
            job_status = current_job["status"]
            
            # If this job belongs to an extraction pipeline, only the extraction's
            # status determines if we are done. Otherwise, use the job's status.
            if extraction:
                is_terminal = ext_status in ("done", "failed", "partial", "cancelled")
            else:
                is_terminal = job_status in ("done", "failed", "cancelled")

            # Cheap fingerprint to detect state changes
            ext_progress = extraction.get("progress") if extraction else ""
            fingerprint = f"{job_status}|{ext_status}|{ext_progress}"

            if fingerprint != last_fingerprint or is_terminal:
                if is_terminal:
                    if ext_status == "failed" or job_status == "failed":
                        etype = "failed"
                    elif ext_status == "done":
                        etype = "done"
                    else:
                        etype = "partial"
                    payload = {"event": etype, "job": current_job, "extraction": extraction}
                else:
                    payload = {
                        "event": "progress",
                        "job": {k: v for k, v in current_job.items() if k != "payload"},
                        "extraction": _slim(extraction),
                    }

                yield f"data: {json.dumps(payload, default=_serialize)}\n\n"
                last_fingerprint = fingerprint

            if is_terminal:
                break

            await asyncio.sleep(1.0)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/jobs/extractions/{extraction_id}/cancel")
@limiter.limit("10/minute")
async def request_job_cancel(request: Request, extraction_id: int):
    pool = request.app.state.pool
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    await db_mod.set_cancel_requested(pool, extraction_id, True)
    cancel_counts = await db_mod.cancel_jobs_for_extraction(pool, extraction_id)
    has_running_work = cancel_counts.get("cancelling", 0) > 0
    next_status = "cancelling" if has_running_work else "cancelled"
    next_message = "Cancellation requested" if has_running_work else "Cancelled before execution"
    await db_mod.set_extraction_status(
        pool,
        extraction_id,
        next_status,
        progress={"stage": "cancel", "message": next_message},
    )
    latest_job = await db_mod.get_latest_job_for_extraction(pool, extraction_id)
    return {"status": next_status, "extraction_id": extraction_id, "job_id": latest_job["id"] if latest_job else None}


@app.post("/jobs/extractions/{extraction_id}/resume", response_model=ExtractionJobStartOut)
@limiter.limit("10/minute")
async def queue_resume_extraction(request: Request, extraction_id: int):
    pool = request.app.state.pool
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    if extraction["status"] not in ("partial", "cancelled", "failed", "cancelling"):
        raise HTTPException(400, detail=f"Cannot resume extraction with status '{extraction['status']}'")

    jobs = await db_mod.list_jobs_for_extraction(pool, extraction_id)
    inflight_jobs = [job for job in jobs if job["status"] in ("queued", "running", "cancelling")]
    if inflight_jobs:
        raise HTTPException(409, detail="Cannot resume while prior jobs are still draining")

    pages = await db_mod.get_pages(pool, extraction_id)
    if not pages:
        raise HTTPException(400, detail="No rendered pages available for this extraction")

    existing_page_results = extraction.get("page_results") or []
    completed_page_nums = {pr.get("_page") for pr in existing_page_results
                           if "_error" not in pr and pr.get("_page") is not None}
    all_page_nums = {p["page_number"] for p in pages}
    missing_pages = sorted(all_page_nums - completed_page_nums)
    start_from = missing_pages[0] if missing_pages else len(pages) + 1

    await db_mod.set_cancel_requested(pool, extraction_id, False)
    job = await db_mod.enqueue_job(
        pool,
        extraction_id=extraction_id,
        document_id=extraction.get("document_id"),
        job_type="llm",
        payload={
            "extraction_id": extraction_id,
            "start_from_page": start_from,
            "existing_page_results": existing_page_results,
        },
    )
    if job is None:
        raise HTTPException(409, detail="A resume job is already queued or running for this extraction")
    await db_mod.set_extraction_status(
        pool,
        extraction_id,
        "queued",
        progress={"stage": "resume", "message": f"Queued resume from page {start_from}"},
    )
    return ExtractionJobStartOut(job_id=job["id"], extraction_id=extraction_id, status=job["status"])


# -- Extraction queries -----------------------------------------------------

@app.get("/extractions/count")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def count_all_extractions(request: Request):
    return {"count": await db_mod.count_all_extractions(request.app.state.pool)}


@app.get("/extractions/{extraction_id}", response_model=ExtractionOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction(request: Request, extraction_id: int):
    pool = request.app.state.pool

    row = await db_mod.get_extraction(pool, extraction_id)
    if not row:
        raise HTTPException(404, detail="Extraction not found")

    return ExtractionOut(**row)


@app.delete("/extractions/{extraction_id}")
@limiter.limit("20/minute")
async def delete_extraction(request: Request, extraction_id: int):
    """
    Delete one extraction (history row + related artifacts), not the whole DB/vendor.
    """
    pool = request.app.state.pool
    store = request.app.state.store

    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")

    if extraction.get("status") in {"queued", "processing", "cancelling"}:
        raise HTTPException(
            409,
            detail="Extraction is active. Cancel it first, then delete.",
        )

    document_id = extraction.get("document_id")
    document_key = None
    if document_id:
        doc = await db_mod.get_document(pool, document_id)
        if doc:
            document_key = doc.get("object_key")

    page_keys = await db_mod.get_page_object_keys(pool, extraction_id)
    delivery_keys = await db_mod.list_delivery_object_keys(pool, extraction_id)

    export_keys: set[str] = set()
    export_object_key = extraction.get("export_object_key")
    if export_object_key:
        export_keys.add(export_object_key)
        if export_object_key.endswith(".xlsx"):
            export_keys.add(export_object_key[:-5] + ".csv")
    for key in delivery_keys:
        export_keys.add(key)

    deleted = await db_mod.delete_extraction(pool, extraction_id)
    if not deleted:
        raise HTTPException(404, detail="Extraction not found")

    deleted_document = False
    if document_id and await db_mod.count_extractions_for_document(pool, document_id) == 0:
        deleted_document = await db_mod.delete_document(pool, document_id)



    deleted_objects = {"pages": 0, "exports": 0, "document": 0}

    for object_key in set(page_keys):
        try:
            store.delete_object(ARTIFACTS_BUCKET, object_key)
            deleted_objects["pages"] += 1
        except Exception:
            logger.warning("Failed deleting page object %s", object_key, exc_info=True)

    for object_key in export_keys:
        try:
            store.delete_object(EXPORTS_BUCKET, object_key)
            deleted_objects["exports"] += 1
        except Exception:
            logger.warning("Failed deleting export object %s", object_key, exc_info=True)

    if deleted_document and document_key:
        try:
            store.delete_object(DOCUMENTS_BUCKET, document_key)
            deleted_objects["document"] = 1
        except Exception:
            logger.warning("Failed deleting document object %s", document_key, exc_info=True)

    return {
        "status": "deleted",
        "extraction_id": extraction_id,
        "deleted_document": deleted_document,
        "deleted_objects": deleted_objects,
    }


@app.get("/extractions/{extraction_id}/pages")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_pages(request: Request, extraction_id: int):
    """
    Return all rendered page images for an extraction.
    Frontend uses this to populate the page viewer after upload.
    Each item: {page_number, image_b64, width, height}
    """
    pool = request.app.state.pool

    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")

    return await _load_page_payloads(pool, extraction_id)


@app.get("/vendors/{vendor_id}/extractions", response_model=list[ExtractionOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_vendor_extractions(request: Request, vendor_id: str, limit: int = 20):
    rows = await db_mod.list_extractions(request.app.state.pool, vendor_id, limit)
    return [ExtractionOut(**r) for r in rows]


# -- All Templates ----------------------------------------------------------

@app.get("/templates", response_model=list[TemplateListOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_all_templates(request: Request):
    rows = await db_mod.list_all_templates(request.app.state.pool)
    return [TemplateListOut(**r) for r in rows]


# -- Global Extraction History -----------------------------------------------

@app.get("/extractions", response_model=list[ExtractionOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_all_extractions(request: Request, limit: int = 50):
    rows = await db_mod.list_all_extractions(request.app.state.pool, limit)
    return [ExtractionOut(**r) for r in rows]


# -- Upload Preview (pre-extraction page rendering) --------------------------

@app.post("/upload-preview")
@limiter.limit("10/minute")
async def upload_preview(
    request: Request,
    file: UploadFile = File(...),
    max_pages: int = Form(20),
):
    """
    Upload a PDF/image and get rendered page images back for preview.
    No extraction or LLM call. Just page rendering.
    Capped at max_pages (default 20) to avoid slow rendering of large scanned PDFs.
    """
    file_bytes = await file.read()
    filename = file.filename or "unknown"

    if filename.lower().endswith(".pdf"):
        pages = await processor.pdf_to_images(file_bytes, max_pages=max_pages)
    elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp")):
        pages = await processor.image_file_to_b64(file_bytes)
    else:
        raise HTTPException(400, detail=f"Unsupported file type: {filename}")

    return {"filename": filename, "total_pages": len(pages), "pages": pages}


# -- Review: OCR Data for Click-to-Select -----------------------------------

@app.get("/extractions/{extraction_id}/ocr")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_ocr(request: Request, extraction_id: int):
    """Return PaddleOCR word data for the click-to-select correction UI."""
    ocr_data = await db_mod.get_ocr_data(request.app.state.pool, extraction_id)
    if ocr_data is None:
        raise HTTPException(404, detail="No OCR data found for this extraction")
    return {"extraction_id": extraction_id, "ocr_pages": ocr_data}


@app.get("/extractions/{extraction_id}/geometry")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_geometry(request: Request, extraction_id: int):
    """Return unified per-page geometry with source classification.

    Each page entry includes: page_number, source ('pypdfium'|'paddleocr'),
    char_count, word_count, and words [{text, box, score}].
    Falls back to ocr_data if page-level geometry is not available.
    """
    pool = request.app.state.pool
    pages = await db_mod.get_pages(pool, extraction_id)
    if not pages:
        raise HTTPException(404, detail="No pages found for this extraction")

    ocr_data = await db_mod.get_ocr_data(pool, extraction_id)
    ocr_by_page = {
        entry.get("page_number"): entry
        for entry in (ocr_data or [])
        if isinstance(entry, dict)
    }

    # Build geometry from pages table if available, merging scanned OCR words
    # from extraction.ocr_data because scanned pages store empty page geometry
    # until the OCR worker fills the unified payload.
    geometry_pages = []
    has_geometry = any(p.get("source") is not None for p in pages)

    if has_geometry:
        for p in pages:
            words = p.get("word_geometry") or []
            ocr_entry = ocr_by_page.get(p["page_number"])
            if ocr_entry and (p.get("source") != "pypdfium" or not words):
                words = ocr_entry.get("words") or []
            geometry_pages.append({
                "page_number": p["page_number"],
                "source": p.get("source") or (ocr_entry or {}).get("source"),
                "char_count": p.get("char_count", 0) or (ocr_entry or {}).get("char_count", 0),
                "word_count": len(words),
                "words": words,
            })
    else:
        # Fallback: use ocr_data for old extractions without geometry columns
        if ocr_data:
            for entry in ocr_data:
                words = entry.get("words") or []
                geometry_pages.append({
                    "page_number": entry.get("page_number", 0),
                    "source": entry.get("source"),
                    "char_count": entry.get("char_count", 0),
                    "word_count": len(words),
                    "words": words,
                })

    return {
        "extraction_id": extraction_id,
        "pages": geometry_pages,
    }


# -- Review: Save Corrections -----------------------------------------------

import re

def _normalize_str(val):
    if isinstance(val, str):
        return re.sub(r'\s+', ' ', val).strip()
    return val

def _compute_correction_diff(original, corrected) -> dict:
    """Compute which fields changed between original and corrected results.

    Returns a dict of {field_name: {"original": ..., "corrected": ...}} for
    changed fields only. Line items are compared as a whole array.
    """
    diff: dict = {}
    
    if isinstance(original, list) or isinstance(corrected, list):
        if not isinstance(original, list): original = [original] if original and isinstance(original, dict) else []
        if not isinstance(corrected, list): corrected = [corrected] if corrected and isinstance(corrected, dict) else []
        
        max_len = max(len(original), len(corrected))
        for i in range(max_len):
            orig_doc = original[i] if i < len(original) else {}
            corr_doc = corrected[i] if i < len(corrected) else {}
            if not isinstance(orig_doc, dict): orig_doc = {}
            if not isinstance(corr_doc, dict): corr_doc = {}
            
            all_keys = set(list(orig_doc.keys()) + list(corr_doc.keys()))
            for key in all_keys:
                orig_val = orig_doc.get(key)
                corr_val = corr_doc.get(key)
                if _normalize_str(orig_val) != _normalize_str(corr_val):
                    diff_key = f"doc_{i}_{key}"
                    diff[diff_key] = {"original": orig_val, "corrected": corr_val}
        return diff

    if not isinstance(original, dict): original = {}
    if not isinstance(corrected, dict): corrected = {}
    
    all_keys = set(list(original.keys()) + list(corrected.keys()))
    for key in all_keys:
        orig_val = original.get(key)
        corr_val = corrected.get(key)
        if _normalize_str(orig_val) != _normalize_str(corr_val):
            diff[key] = {"original": orig_val, "corrected": corr_val}
    return diff

@app.put("/extractions/{extraction_id}/corrections")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def save_extraction_corrections(request: Request, extraction_id: int):
    """Persist user corrections from the Review page.

    Saves to corrected_result (original result stays immutable).
    Auto-creates a gold example for this vendor if fields were changed.
    Invalidates prompt cache so next extraction uses the gold example.
    """
    body = await request.json()
    corrected_result = body.get("corrected_result")
    field_locations = body.get("field_locations", {})
    actor = body.get("actor") or "ui"
    reason_code = body.get("reason_code") or "manual_review"
    note = body.get("note")

    if corrected_result is None:
        raise HTTPException(400, detail="corrected_result is required")

    pool = request.app.state.pool

    # Get original extraction to compare and create gold example
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail=f"Extraction {extraction_id} not found")

    original_result = extraction.get("result") or {}
    base = {
        "extraction_id": extraction_id,
        "document_id": extraction.get("document_id"),
        "vendor_id": extraction.get("vendor_id"),
        "vendor_name": extraction.get("vendor_name"),
        "filename": extraction.get("filename"),
    }
    trace_context: dict = {}
    if extraction.get("document_id"):
        try:
            document = await db_mod.get_document(pool, extraction["document_id"])
            metadata = (document or {}).get("metadata") or {}
            trace_context = metadata.get("trace_context") if isinstance(metadata, dict) else {}
            if not isinstance(trace_context, dict):
                trace_context = {}
        except Exception as exc:
            logger.debug("Skipping review trace context lookup for extraction %s: %s", extraction_id, exc)
            trace_context = {}

    # Compute correction diff
    correction_diff = _compute_correction_diff(original_result, corrected_result)
    plog.event(
        "review_correction_received",
        stage="review",
        **base,
        actor=actor,
        reason_code=reason_code,
        changed_fields=list(correction_diff.keys()) if correction_diff else [],
        correction_diff=correction_diff,
        field_location_count=(
            sum(len(v) for v in field_locations if isinstance(v, dict))
            if isinstance(field_locations, list)
            else len(field_locations or {})
        ),
    )

    correction_meta = {
        "corrected_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fields_changed": list(correction_diff.keys()) if correction_diff else [],
        "reason_code": reason_code,
        "actor": actor,
    }

    updated = await db_mod.save_corrections(
        pool,
        extraction_id,
        corrected_result,
        field_locations,
        correction_meta=correction_meta,
    )
    if not updated:
        raise HTTPException(404, detail=f"Extraction {extraction_id} not found")
    plog.event("review_correction_saved", stage="review", **base, changed_fields=list(correction_diff.keys()))

    review_event_id = await db_mod.create_review_event(
        pool,
        extraction_id=extraction_id,
        actor=actor,
        reason_code=reason_code,
        note=note,
        before_result=original_result,
        after_result=corrected_result,
        before_locations=extraction.get("field_locations") or {},
        after_locations=field_locations,
        diff=correction_diff,
    )

    # Auto-create gold example if any header fields were actually changed.
    # Exclude all line-item data: "line_items" (single PO) and "doc_N_line_items" (po_per_page).
    # Old table rows as few-shot examples add noise, not signal (AGENTS.md §7).
    gold_id = None
    vendor_id = extraction.get("vendor_id")
    header_only_diff = {
        k: v for k, v in (correction_diff or {}).items()
        if k != "line_items" and not k.endswith("_line_items")
    }
    if header_only_diff and vendor_id:
        try:
            gold_id = await db_mod.save_gold_example(
                pool, vendor_id, extraction_id,
                original_result, corrected_result,
                correction_diff=header_only_diff,
            )
            logger.info("Gold example %d created for vendor=%s extraction=%d (changed: %s)",
                        gold_id, vendor_id, extraction_id, ", ".join(header_only_diff.keys()))
            plog.event(
                "gold_correction_saved",
                stage="review",
                **base,
                gold_example_id=gold_id,
                changed_fields=list(header_only_diff.keys()),
                latest_per_field=True,
            )


        except Exception as exc:
            logger.warning("Failed to create gold example for extraction %d: %s", extraction_id, exc)

    # Save spatial memory from manual corrections (Phase 3)
    spatial_saved = 0
    if field_locations and vendor_id:
        try:
            try:
                from . import spatial_memory as _sm
            except ImportError:
                import spatial_memory as _sm
            spatial_saved = await _sm.save_from_corrections(
                pool, extraction_id, field_locations, corrected_result,
            )
            if spatial_saved:
                logger.info(
                    "Spatial memory: %d regions saved for extraction=%d vendor=%s",
                    spatial_saved, extraction_id, vendor_id,
                )
            plog.event(
                "review_spatial_memory_saved",
                stage="review",
                **base,
                saved_count=spatial_saved,
            )
        except Exception as exc:
            logger.warning("Failed to save spatial memory for extraction %d: %s", extraction_id, exc)
            plog.event(
                "review_spatial_memory_failed",
                stage="review",
                status="error",
                **base,
                error=str(exc),
            )

    with use_trace_context(trace_context):
        with trace_named_step(
            "manual_review_corrections",
            kind="TOOL",
            input_data={
                "extraction_id": extraction_id,
                "actor": actor,
                "reason_code": reason_code,
            },
            attributes=base,
        ) as review_trace:
            review_trace["output"] = {
                "fields_changed": list(correction_diff.keys()) if correction_diff else [],
                "correction_diff": correction_diff,
                "field_location_count": (
                    sum(len(v) for v in field_locations if isinstance(v, dict))
                    if isinstance(field_locations, list)
                    else len(field_locations or {})
                ),
                "gold_example_id": gold_id,
                "review_event_id": review_event_id,
                "spatial_memory_saved": spatial_saved,
                "corrected_result": corrected_result,
            }

    outbound_payload = {"extraction_id": extraction_id, "trigger": "review"}
    if trace_context:
        outbound_payload["trace_context"] = trace_context
    await db_mod.ensure_job(
        pool,
        extraction_id=extraction_id,
        document_id=extraction.get("document_id"),
        job_type="outbound",
        payload=outbound_payload,
    )

    return {
        "status": "saved",
        "extraction_id": extraction_id,
        "fields_changed": list(correction_diff.keys()) if correction_diff else [],
        "gold_example_id": gold_id,
        "review_event_id": review_event_id,
        "spatial_memory_saved": spatial_saved,
    }


@app.get("/extractions/{extraction_id}/reviews")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_reviews(request: Request, extraction_id: int):
    extraction = await db_mod.get_extraction(request.app.state.pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    return {"extraction_id": extraction_id, "reviews": await db_mod.list_review_events(request.app.state.pool, extraction_id)}


@app.get("/extractions/{extraction_id}/trace")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_trace(request: Request, extraction_id: int, limit: int | None = None):
    extraction = await db_mod.get_extraction(request.app.state.pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    events = plog.read_extraction_events(extraction_id, limit=limit)
    return {
        "summary": plog.build_extraction_trace_summary(extraction_id, events, extraction=extraction),
        "events": events,
    }


@app.get("/extractions/{extraction_id}/contract")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_contract(request: Request, extraction_id: int):
    extraction = await db_mod.get_extraction(request.app.state.pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    return build_purchase_order_contract(extraction)


@app.get("/extractions/{extraction_id}/export.xlsx")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def download_extraction_excel(request: Request, extraction_id: int):
    pool = request.app.state.pool
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    object_key = extraction.get("export_object_key")
    if not object_key:
        raise HTTPException(404, detail="Excel export not available yet")
    payload = request.app.state.store.get_bytes(EXPORTS_BUCKET, object_key)
    filename = f"extraction_{extraction_id}.xlsx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/extractions/{extraction_id}/export.csv")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def download_extraction_csv(request: Request, extraction_id: int):
    pool = request.app.state.pool
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    xlsx_key = extraction.get("export_object_key")
    if not xlsx_key:
        raise HTTPException(404, detail="CSV export not available yet")
    csv_key = xlsx_key.replace(".xlsx", ".csv")
    try:
        payload = request.app.state.store.get_bytes(EXPORTS_BUCKET, csv_key)
    except Exception as exc:
        raise HTTPException(404, detail="CSV export not available yet") from exc
    filename = f"extraction_{extraction_id}.csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



# -- Static Frontend --------------------------------------------------------

# Mount the static frontend directory so it's accessible at http://localhost:8000/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s. UI will not be served.", FRONTEND_DIR)


# -- Run with uvicorn -------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
