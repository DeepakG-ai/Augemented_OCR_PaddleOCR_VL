"""
main.py -- FastAPI application with REST endpoints, SSE streaming,
rate limiting, CORS, upload size guard, and lifespan management.

Fields are split into header_fields and line_item_fields.
No hardcoded field registry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

import cache as cache_mod
import db as db_mod
import extractor
import processor
from logging_config import configure_logging
from phoenix_tracing import setup_phoenix
from models import (
    ExtractionOut,
    HealthOut,
    TemplateSaveResponse,
    TemplateCreate,
    TemplateListOut,
    TemplateOut,
    VendorCreate,
    VendorOut,
)

load_dotenv()

# ── Centralized logging (replaces inline basicConfig) ───────────────
configure_logging()
logger = logging.getLogger("main")

# -- Config from env --------------------------------------------------------

LLM_URL = os.getenv("LLM_URL", "http://localhost:8001/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3vl")
RATE_LIMIT = os.getenv("RATE_LIMIT_PER_MINUTE", "30")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024

# Track active extractions for cancellation: {extraction_id: asyncio.Event}
_active_extractions: dict[int, asyncio.Event] = {}


# -- Rate limiter -----------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{RATE_LIMIT}/minute"])


# -- Upload size middleware -------------------------------------------------

class MaxUploadSizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_UPLOAD_BYTES."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_BYTES:
            return Response(
                content=json.dumps({"detail": f"Upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit"}),
                status_code=413,
                media_type="application/json",
            )
        return await call_next(request)


# -- Lifespan ---------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB pool + Redis + Phoenix. Shutdown: close both."""
    logger.info("Starting up -- creating DB pool and Redis client")
    app.state.pool = await db_mod.create_pool()
    await db_mod.init(app.state.pool)
    app.state.redis = await cache_mod.get_redis()
    setup_phoenix()
    logger.info("DB pool, Redis, and Phoenix ready")
    yield
    logger.info("Shutting down -- closing connections")
    await app.state.pool.close()
    await app.state.redis.close()


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
    row = await db_mod.upsert_vendor(request.app.state.pool, body.id, body.name)
    return VendorOut(**row)


@app.delete("/vendors/{vendor_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_vendor(request: Request, vendor_id: str):
    pool = request.app.state.pool
    redis_client = request.app.state.redis
    # Invalidate cache
    await cache_mod.invalidate_vendor_cache(redis_client, vendor_id)
    deleted = await db_mod.delete_vendor(pool, vendor_id)
    if not deleted:
        raise HTTPException(404, detail="Vendor not found")
    return {"status": "deleted", "vendor_id": vendor_id}


# -- Templates --------------------------------------------------------------

@app.get("/vendors/{vendor_id}/template", response_model=TemplateOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_template(request: Request, vendor_id: str):
    tmpl = await db_mod.get_template(request.app.state.pool, vendor_id)
    if not tmpl:
        raise HTTPException(404, detail="No template configured for this vendor")
    return TemplateOut(**tmpl)


@app.post("/vendors/{vendor_id}/template", response_model=TemplateSaveResponse)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def save_template(request: Request, vendor_id: str, body: TemplateCreate):
    pool = request.app.state.pool
    redis_client = request.app.state.redis

    # Auto-upsert vendor — frontend may have created vendor locally while offline.
    # Use vendor_name from body if provided, otherwise fall back to vendor_id as name.
    vendor_name = (body.vendor_name or vendor_id).upper()
    await db_mod.upsert_vendor(pool, vendor_id, vendor_name)

    logger.info(
        "Saving template for vendor=%s format=%s headers=%s items=%s",
        vendor_id, body.format_type, body.header_fields, body.line_item_fields,
    )

    try:
        # Invalidate old cache
        await cache_mod.invalidate_vendor_cache(redis_client, vendor_id)

        # Build (or retrieve) cached prompt
        system_prompt, prompt_hash = await extractor.get_or_build_system_prompt(
            pool, redis_client, vendor_id,
            body.header_fields, body.line_item_fields,
            body.prompt_instructions, body.extraction_rules,
            body.format_type,
        )

        tmpl = await db_mod.get_template(pool, vendor_id)
        logger.info("Template saved OK vendor=%s hash=%s", vendor_id, prompt_hash[:12])
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
    pool = request.app.state.pool
    redis_client = request.app.state.redis

    # 1. Load vendor template (may not exist for auto-extract)
    tmpl = await db_mod.get_template(pool, vendor_id)

    # Resolve fields and format_type (form overrides template)
    req_header: list[str] = json.loads(header_fields) if header_fields else (tmpl["header_fields"] if tmpl else [])
    req_items: list[str] = json.loads(line_item_fields) if line_item_fields else (tmpl["line_item_fields"] if tmpl else [])
    req_format: str = format_type or (tmpl["format_type"] if tmpl else "single_po_multipage")

    # Build or retrieve system prompt
    if tmpl and tmpl.get("system_prompt"):
        system_prompt = tmpl["system_prompt"]
    else:
        # Build a fresh system prompt (auto-extract or no template saved yet)
        instructions = tmpl["prompt_instructions"] if tmpl else None
        rules = tmpl["extraction_rules"] if tmpl else []
        system_prompt = extractor.build_system_prompt(
            req_header, req_items, instructions, rules, req_format
        )

    # 2. Read + convert file to pages
    file_bytes = await file.read()
    filename = file.filename or "unknown"

    if filename.lower().endswith(".pdf"):
        pages = await processor.pdf_to_images(file_bytes)
    elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp")):
        pages = await processor.image_file_to_b64(file_bytes)
    else:
        raise HTTPException(400, detail=f"Unsupported file type: {filename}")

    # 3. Create extraction record
    template_id = tmpl["id"] if tmpl else None
    extraction_rec = await db_mod.create_extraction(
        pool, vendor_id, template_id, filename, len(pages),
        req_format, req_header, req_items,
    )
    extraction_id = extraction_rec["id"]

    # 4. Persist rendered pages so frontend can fetch them page-by-page
    await db_mod.save_pages(pool, extraction_id, pages)

    # 5. SSE streaming response
    cancel_event = asyncio.Event()
    _active_extractions[extraction_id] = cancel_event

    async def event_stream() -> AsyncGenerator[str, None]:
        start = time.perf_counter()
        error_msg: str | None = None
        final_result = None
        page_results = None
        was_cancelled = False
        last_completed_page = 0

        async def on_page_done(page_num: int, total_pages: int, page_result: dict | None) -> None:
            """SSE callback -- fires after each page is processed. Saves incrementally."""
            progress_events.append({
                "event": "progress",
                "status": "processing",
                "extraction_id": extraction_id,
                "page": page_num,
                "total_pages": total_pages,
            })
            # Save page result incrementally to DB
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
                )
            )

            # Poll for progress events while extraction runs
            last_sent = 0
            while not extract_task.done():
                await asyncio.sleep(0.3)
                while last_sent < len(progress_events):
                    evt = progress_events[last_sent]
                    yield f"data: {json.dumps(evt)}\n\n"
                    last_sent += 1

            output = await extract_task
            final_result = output["result"]
            page_results = output["page_results"]
            was_cancelled = output.get("cancelled", False)
            last_completed_page = output.get("last_completed_page", 0)

            # Flush remaining progress events
            while last_sent < len(progress_events):
                evt = progress_events[last_sent]
                yield f"data: {json.dumps(evt)}\n\n"
                last_sent += 1

        except Exception as exc:
            error_msg = str(exc)
            logger.error("Extraction %d failed: %s", extraction_id, error_msg)

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        # Clean up active tracking
        _active_extractions.pop(extraction_id, None)

        if error_msg:
            await db_mod.update_extraction_result(
                pool, extraction_id, None, None, "failed", elapsed_ms, error=error_msg
            )
            yield f"data: {json.dumps({'event': 'error', 'status': 'failed', 'error': error_msg})}\n\n"
        elif was_cancelled:
            status = "partial" if page_results else "cancelled"
            await db_mod.update_extraction_result(
                pool, extraction_id, final_result, page_results, status, elapsed_ms
            )
            yield f"data: {json.dumps({'event': 'cancelled', 'status': status, 'extraction_id': extraction_id, 'result': final_result, 'page_results': page_results, 'last_completed_page': last_completed_page, 'total_pages': len(pages), 'duration_ms': elapsed_ms})}\n\n"
        else:
            await db_mod.update_extraction_result(
                pool, extraction_id, final_result, page_results, "done", elapsed_ms
            )
            await cache_mod.set_cached_extraction(redis_client, extraction_id, {
                "id": extraction_id,
                "result": final_result,
                "page_results": page_results,
                "status": "done",
                "duration_ms": elapsed_ms,
            })
            yield f"data: {json.dumps({'event': 'done', 'status': 'done', 'extraction_id': extraction_id, 'result': final_result, 'total_pages': len(pages), 'duration_ms': elapsed_ms})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Cancel / Resume --------------------------------------------------------

@app.post("/extract/cancel/{extraction_id}")
async def cancel_extraction(extraction_id: int):
    """Set the cancellation flag for a running extraction."""
    cancel_event = _active_extractions.get(extraction_id)
    if not cancel_event:
        raise HTTPException(404, detail="No active extraction with that ID")
    cancel_event.set()
    return {"status": "cancelling", "extraction_id": extraction_id}


@app.post("/extract/resume/{extraction_id}")
@limiter.limit("10/minute")
async def resume_extraction(request: Request, extraction_id: int):
    """Resume an extraction from the last completed page."""
    pool = request.app.state.pool
    redis_client = request.app.state.redis

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
    req_format = extraction.get("format_type") or (tmpl["format_type"] if tmpl else "single_po_multipage")

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
    completed_page_nums = {pr["_page"] for pr in existing_page_results if "_error" not in pr}
    # Find the first page that still needs work (failed or never attempted)
    all_page_nums = {p["page_number"] for p in pages}
    missing_pages = sorted(all_page_nums - completed_page_nums)
    start_from = missing_pages[0] if missing_pages else len(pages) + 1

    # 4. SSE streaming response for resume
    cancel_event = asyncio.Event()
    _active_extractions[extraction_id] = cancel_event

    async def event_stream() -> AsyncGenerator[str, None]:
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
            await db_mod.update_extraction_result(
                pool, extraction_id, None, None, "failed", elapsed_ms, error=error_msg
            )
            yield f"data: {json.dumps({'event': 'error', 'status': 'failed', 'error': error_msg})}\n\n"
        elif was_cancelled:
            status = "partial" if page_results else "cancelled"
            await db_mod.update_extraction_result(
                pool, extraction_id, final_result, page_results, status, elapsed_ms
            )
            yield f"data: {json.dumps({'event': 'cancelled', 'status': status, 'extraction_id': extraction_id, 'result': final_result, 'page_results': page_results, 'last_completed_page': last_completed_page, 'total_pages': len(pages), 'duration_ms': elapsed_ms})}\n\n"
        else:
            await db_mod.update_extraction_result(
                pool, extraction_id, final_result, page_results, "done", elapsed_ms
            )
            yield f"data: {json.dumps({'event': 'done', 'status': 'done', 'extraction_id': extraction_id, 'result': final_result, 'total_pages': len(pages), 'duration_ms': elapsed_ms})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# -- Extraction queries -----------------------------------------------------

@app.get("/extractions/{extraction_id}", response_model=ExtractionOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction(request: Request, extraction_id: int):
    pool = request.app.state.pool

    row = await db_mod.get_extraction(pool, extraction_id)
    if not row:
        raise HTTPException(404, detail="Extraction not found")

    return ExtractionOut(**row)


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

    pages = await db_mod.get_pages(pool, extraction_id)
    return pages


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
):
    """
    Upload a PDF/image and get rendered page images back for preview.
    No extraction or LLM call. Just page rendering.
    """
    file_bytes = await file.read()
    filename = file.filename or "unknown"

    if filename.lower().endswith(".pdf"):
        pages = await processor.pdf_to_images(file_bytes)
    elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp")):
        pages = await processor.image_file_to_b64(file_bytes)
    else:
        raise HTTPException(400, detail=f"Unsupported file type: {filename}")

    return {"filename": filename, "total_pages": len(pages), "pages": pages}


# -- Static Frontend --------------------------------------------------------

# Mount the static frontend directory so it's accessible at http://localhost:8000/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "qwen_frontend")

if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s. UI will not be served.", FRONTEND_DIR)


# -- Run with uvicorn -------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)