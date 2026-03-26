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
from models import (
    ExtractionOut,
    HealthOut,
    TemplateSaveResponse,
    TemplateCreate,
    TemplateOut,
    VendorCreate,
    VendorOut,
)

load_dotenv()

logger = logging.getLogger("main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# -- Config from env --------------------------------------------------------

LLM_URL = os.getenv("LLM_URL", "http://localhost:8001/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3vl")
RATE_LIMIT = os.getenv("RATE_LIMIT_PER_MINUTE", "30")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024


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
    """Startup: create DB pool + Redis. Shutdown: close both."""
    logger.info("Starting up -- creating DB pool and Redis client")
    app.state.pool = await db_mod.create_pool()
    await db_mod.init(app.state.pool)
    app.state.redis = await cache_mod.get_redis()
    logger.info("DB pool and Redis ready")
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

    # 1. Load vendor template
    tmpl = await db_mod.get_template(pool, vendor_id)
    if not tmpl:
        raise HTTPException(400, detail="Configure vendor template first via POST /vendors/{vendor_id}/template")

    system_prompt = tmpl["system_prompt"]
    if not system_prompt:
        raise HTTPException(400, detail="Template has no system prompt -- re-save template to rebuild")

    # Resolve fields and format_type (form overrides template)
    req_header: list[str] = json.loads(header_fields) if header_fields else tmpl["header_fields"]
    req_items: list[str] = json.loads(line_item_fields) if line_item_fields else tmpl["line_item_fields"]
    req_format: str = format_type or tmpl["format_type"]

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
    extraction_rec = await db_mod.create_extraction(
        pool, vendor_id, tmpl["id"], filename, len(pages),
        req_format, req_header, req_items,
    )
    extraction_id = extraction_rec["id"]

    # 4. Persist rendered pages so frontend can fetch them page-by-page
    await db_mod.save_pages(pool, extraction_id, pages)

    # 5. SSE streaming response
    async def event_stream() -> AsyncGenerator[str, None]:
        start = time.perf_counter()
        error_msg: str | None = None
        final_result = None
        page_results = None

        async def on_page_done(page_num: int, total_pages: int) -> None:
            """SSE callback -- fires after each page is processed."""
            progress_events.append({
                "event": "progress",
                "status": "processing",
                "page": page_num,
                "total_pages": total_pages,
            })

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

            # Flush remaining progress events
            while last_sent < len(progress_events):
                evt = progress_events[last_sent]
                yield f"data: {json.dumps(evt)}\n\n"
                last_sent += 1

        except Exception as exc:
            error_msg = str(exc)
            logger.error("Extraction %d failed: %s", extraction_id, error_msg)

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        if error_msg:
            await db_mod.update_extraction_result(
                pool, extraction_id, None, None, "failed", elapsed_ms, error=error_msg
            )
            yield f"data: {json.dumps({'event': 'error', 'status': 'failed', 'error': error_msg})}\n\n"
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