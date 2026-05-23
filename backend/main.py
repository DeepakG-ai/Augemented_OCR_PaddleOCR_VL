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
import hashlib
import mimetypes
import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import AsyncGenerator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from . import db as db_mod
from . import extractor
from . import processor
from . import page_logger
from .contracts import build_purchase_order_contract
from . import logging_config as plog
from .logging_config import configure_logging
from .auth import (
    assert_alias_access,
    assert_extraction_access,
    assert_job_access,
    assert_vendor_access,
    create_access_token,
    generate_api_key,
    get_current_user,
    get_current_user_or_api_key,
    get_current_user_sse,
    hash_password,
    require_admin,
    verify_password,
)
from .models import (
    ApiKeyCreate,
    ApiKeyOut,
    ExtractionJobStartOut,
    ExtractionOut,
    HealthOut,
    JobOut,
    JobStatusOut,
    LoginRequest,
    TemplateSaveResponse,
    TemplateCreate,
    TemplateListOut,
    TemplateOut,
    TokenOut,
    UserCreate,
    UserOut,
    UserResetPassword,
    VendorAliasCreate,
    VendorAliasOut,
    VendorCreate,
    VendorOut,
)
from .object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, get_store
from .scheduler import (
    init_scheduler, shutdown_scheduler, sync_job, remove_job,
    get_next_run_times, reload_all_schedules, set_context,
)
from .mlflow_tracing import (
    setup_mlflow,
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

from .config import LLM_URL, LLM_MODEL, RATE_LIMIT_PER_MINUTE as RATE_LIMIT, MAX_UPLOAD_BYTES, MAX_DOCUMENT_PAGES
from .config import DEFAULT_SUBSCRIPTION_LIMIT, SUBSCRIPTION_WARNING_THRESHOLD
from .config import PIPELINE_LOG_DIR

from pydantic import BaseModel


class ScheduleCreate(BaseModel):
    cron_expr: str
    timezone: str = "UTC"
    label: str = ""


class ScheduleUpdate(BaseModel):
    cron_expr: str | None = None
    timezone: str | None = None
    label: str | None = None
    enabled: bool | None = None


# ── Centralized logging (replaces inline basicConfig) ───────────────
configure_logging()
logger = logging.getLogger(__name__)


async def render_page_1_for_detection(file_bytes: bytes, filename: str) -> list[dict]:
    """
    Renders the first page of the file for vendor detection.
    """
    filename_lower = filename.lower()
    if filename_lower.endswith(".pdf"):
        return await processor.pdf_to_images(file_bytes, max_pages=1)
    else:
        return await processor.image_file_to_b64(file_bytes)


# -- Config SSE state -------------------------------------------------------
# Per-user list of open SSE queues for live config-change push.
_config_sse_queues: dict[str, list[asyncio.Queue]] = {}

_CONFIG_KEYS = frozenset({"input_folder", "output_folder", "upload_mode", "success_folder", "failed_folder"})
_VALID_UPLOAD_MODES = frozenset({"ui", "folder"})


def _broadcast_config_event(user_id: str, data: dict) -> None:
    for q in _config_sse_queues.get(user_id, []):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


async def _folder_ingest_callback(user_id: str, pdf_path: str) -> None:
    """Called by the watchdog thread (via asyncio bridge) when a new PDF lands."""
    # app is defined at module level after the routes section; access via the
    # global name — safe because this only runs after startup.
    pool = app.state.pool
    store = app.state.store
    try:
        import pathlib
        path = pathlib.Path(pdf_path)
        if not path.is_file():
            return
        file_bytes = path.read_bytes()
        filename = path.name

        # Hard page cap — same rule as /ingest/ui
        if filename.lower().endswith(".pdf"):
            try:
                pdf_page_count = processor.count_pdf_pages(file_bytes)
            except ValueError:
                logger.warning("folder_ingest: unreadable PDF path=%s", pdf_path)
                _broadcast_config_event(user_id, {
                    "type": "folder_ingest_error",
                    "path": pdf_path,
                    "reason": "unreadable_pdf",
                })
                return
            if pdf_page_count > MAX_DOCUMENT_PAGES:
                logger.warning(
                    "folder_ingest: PDF too large (%d pages, max %d) path=%s",
                    pdf_page_count, MAX_DOCUMENT_PAGES, pdf_path,
                )
                _broadcast_config_event(user_id, {
                    "type": "folder_ingest_error",
                    "path": pdf_path,
                    "reason": "document_too_large",
                    "pages": pdf_page_count,
                    "max_pages": MAX_DOCUMENT_PAGES,
                })
                return
        else:
            pdf_page_count = 1

        # Quota reservation — atomic, same as /ingest/ui
        quota = await db_mod.reserve_quota(pool, user_id, pdf_page_count)
        if not quota["allowed"]:
            logger.warning(
                "folder_ingest: quota exceeded user=%s used=%d limit=%d incoming=%d",
                user_id, quota["used"], quota["limit"], pdf_page_count,
            )
            _broadcast_config_event(user_id, {
                "type": "folder_ingest_error",
                "path": pdf_path,
                "reason": "quota_exceeded",
                "used": quota["used"],
                "limit": quota["limit"],
            })
            return

        # Lazy imports to avoid circular import at module load time.
        from . import geometry as geo_mod
        from . import vendor_detector as vd_mod
        from . import ocr_runner as ocr_mod

        _job_submitted = False
        try:
            # Render page 1 for vendor detection.
            rendered = await render_page_1_for_detection(file_bytes, filename)
            if not rendered:
                logger.warning("folder_ingest: no pages rendered path=%s", pdf_path)
                return
            page1 = rendered[0]
            page1_meta = {"page_number": 1, "width": page1.get("width", 0), "height": page1.get("height", 0)}
            geo_pages = geo_mod.compute_pdf_geometry(file_bytes, [page1_meta])
            page_words = (geo_pages[0].get("words", []) if geo_pages else [])
            if not page_words:
                ocr_pages = await ocr_mod.run_ocr_on_pages([{
                    "page_number": 1,
                    "image_b64": page1["image_b64"],
                    "mime_type": page1.get("mime_type", "image/jpeg"),
                }])
                page_words = ocr_pages[0].get("words", []) if ocr_pages else []

            match = await vd_mod.detect_vendor(pool, page_words, user_id=user_id)
            if match is None:
                logger.warning("folder_ingest: vendor not detected path=%s user=%s", pdf_path, user_id)
                _broadcast_config_event(user_id, {
                    "type": "folder_ingest_error",
                    "path": pdf_path,
                    "reason": "vendor_not_detected",
                })
                return

            tmpl = await db_mod.get_template(pool, match.vendor_id)
            if not tmpl:
                logger.warning("folder_ingest: no template for vendor=%s", match.vendor_id)
                _broadcast_config_event(user_id, {
                    "type": "folder_ingest_error",
                    "path": pdf_path,
                    "reason": "no_template",
                    "vendor_id": match.vendor_id,
                })
                return

            # Whoever logins (owns the watcher), bill to them.
            billing_user_id = user_id

            result = await _submit_ingestion_job(
                pool,
                store,
                file_bytes=file_bytes,
                filename=filename,
                vendor_id=match.vendor_id,
                format_type=tmpl.get("format_type", "single_po_multipage"),
                header_fields=list(tmpl.get("header_fields") or []),
                line_item_fields=list(tmpl.get("line_item_fields") or []),
                source_type="folder",
                source_ref=pdf_path,
                metadata={"billing_user_id": billing_user_id},
                reserved_pages=pdf_page_count,
            )
            _job_submitted = True
        finally:
            if not _job_submitted:
                try:
                    await db_mod.release_quota_reservation(pool, user_id, pdf_page_count)
                except Exception:
                    pass
        _broadcast_config_event(user_id, {
            "type": "folder_ingest_started",
            "path": pdf_path,
            "vendor_id": match.vendor_id,
            "vendor_name": match.vendor_name,
            "job_id": result["job"]["id"],
            "extraction_id": result["extraction"]["id"],
        })
        logger.info(
            "folder_ingest: queued path=%s vendor=%s job=%s",
            pdf_path, match.vendor_id, result["job"]["id"],
        )
    except Exception as exc:
        logger.exception("folder_ingest: unexpected error path=%s: %s", pdf_path, exc)
        _broadcast_config_event(user_id, {
            "type": "folder_ingest_error",
            "path": pdf_path,
            "reason": str(exc),
        })


async def _reconfigure_user_watcher(app_ref, user_id: str) -> None:
    """Start or stop the folder watcher for a user based on their current config."""
    watcher_mgr = getattr(app_ref.state, "watcher_mgr", None)
    if watcher_mgr is None:
        return
    config = await db_mod.get_user_config(app_ref.state.pool, user_id)
    upload_mode = config.get("upload_mode", "ui")
    input_folder = config.get("input_folder", "")
    if upload_mode == "folder" and input_folder:
        loop = asyncio.get_event_loop()
        watcher_mgr.start_for_user(user_id, input_folder, _folder_ingest_callback, loop)
    else:
        watcher_mgr.stop_for_user(user_id)


# -- Rate limiter -----------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{RATE_LIMIT}/minute"])





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
        try:
            _cl_int = int(content_length) if content_length else 0
        except ValueError:
            _cl_int = 0
        if _cl_int > MAX_UPLOAD_BYTES:
            return Response(
                content=json.dumps({"error": {"code": "FILE_TOO_LARGE", "message": f"Upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit"}}),
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
    """Startup: create DB pool + object store + MLflow + bootstrap admin. Shutdown: close pool."""
    logger.info("Starting up -- creating DB pool")
    app.state.pool = await db_mod.create_pool()
    await db_mod.init(app.state.pool)
    app.state.store = get_store()
    setup_mlflow()

    # Bootstrap admin user from env vars on first startup
    admin_email = (os.getenv("ADMIN_EMAIL") or "").strip()
    admin_pw = os.getenv("ADMIN_PASSWORD") or ""
    if admin_email and admin_pw:
        if admin_pw.startswith("CHANGE_ME"):
            logger.error(
                "ADMIN_PASSWORD is set to a placeholder value — bootstrap admin NOT created. "
                "Set a real password in .env before starting the application."
            )
        else:
            existing = await db_mod.get_user_by_email(app.state.pool, admin_email)
            if not existing:
                try:
                    await db_mod.create_user(
                        app.state.pool,
                        admin_email,
                        hash_password(admin_pw),
                        role="admin",
                    )
                    logger.info("Bootstrap admin user created: %s", admin_email)
                except Exception as exc:
                    logger.warning("Failed to bootstrap admin user %s: %s", admin_email, exc)

    # Folder watchers are disabled
    app.state.watcher_mgr = None

    # Start APScheduler
    from .config import DATABASE_URL as _DB_URL
    await init_scheduler(_DB_URL)
    set_context(app.state.pool, _folder_ingest_callback)
    await reload_all_schedules(app.state.pool)

    # Suppress uvicorn access log noise for high-frequency polling routes
    # (heartbeat, config-poll, scheduler-poll). Errors/warnings still surface.
    import logging as _logging

    class _QuietPaths(_logging.Filter):
        _SKIP = ("/api/client/heartbeat", "/api/config", "/api/scheduler", "/health")
        def filter(self, record: _logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(p in msg for p in self._SKIP)

    _q = _QuietPaths()
    _uv = _logging.getLogger("uvicorn.access")
    for _h in _uv.handlers:
        _h.addFilter(_q)

    logger.info("DB pool, object store, and MLflow ready")
    yield
    logger.info("Shutting down -- closing connections")
    shutdown_scheduler()
    if getattr(app.state, "watcher_mgr", None):
        app.state.watcher_mgr.stop_all()
    await app.state.pool.close()
    from .logging_config import shutdown_logging
    shutdown_logging()


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
    reserved_pages: int | None = None,
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
    if reserved_pages:
        document_metadata["reserved_pages"] = reserved_pages
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

    # If caller didn't provide a trace context (e.g. /ingest, folder-watcher),
    # create a root span here so all 4 pipeline stages share one MLflow trace.
    if not trace_context:
        with trace_extraction_root(
            extraction["id"], vendor_id, filename, 0,
            resolved_format_type, header_fields, line_item_fields,
        ):
            trace_context = current_trace_context()

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


# -- Access log middleware (ONE line per request — 2xx, 4xx AND 5xx) -------

def _peek_user(request: Request) -> str:
    """Best-effort caller id for the access line — no DB, never raises."""
    try:
        auth = request.headers.get("authorization", "")
        raw = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
        if not raw:
            return "-"
        from .auth import decode_token
        claims = decode_token(raw)
        return claims.get("email") or claims.get("sub") or "-"
    except Exception:
        return "-"


_QUIET_PATHS = frozenset({
    "/api/client/heartbeat", "/api/config", "/api/scheduler", "/health",
})


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = uuid.uuid4().hex[:4]
        request.state.req_id = rid
        stage_token = plog.current_stage.set("api")
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            ms = (time.perf_counter() - t0) * 1000
            try:
                # Skip INFO logging for high-frequency background polling routes.
                # Errors/warnings (4xx/5xx) still surface regardless of path.
                if status < 400 and request.url.path in _QUIET_PATHS:
                    pass
                else:
                    user = _peek_user(request)
                    base = f"{request.method} {request.url.path}"
                    tail = f"req={rid} user={user} -> {status}"
                    reason = getattr(request.state, "err_reason", None)
                    if reason:
                        tail += f' reason="{reason}"'
                    ref = getattr(request.state, "trace_ref", None)
                    if ref:
                        tail += f" trace={ref}"
                    line = f"{base}  | {tail} {ms:.0f}ms"
                    lg = logging.getLogger("api")
                    if status >= 500:
                        lg.error(line)
                    elif status in (401, 403):
                        lg.warning(line)
                    else:
                        lg.info(line)
            except Exception:
                pass
            plog.current_stage.reset(stage_token)


# -- App --------------------------------------------------------------------

app = FastAPI(
    title="Augmented OCR API",
    version="2.0.0",
    description="Production-grade document extraction with dynamic user-defined fields",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_middleware(MaxUploadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3002",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3002",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last → outermost: times and logs the full request lifecycle.
app.add_middleware(AccessLogMiddleware)


# ── Unified error envelope ──────────────────────────────────────────────────
# Every error — HTTPException, validation, rate limit, unhandled — returns:
#   { "error": { "code": "SNAKE_CASE", "message": "...", ...extra_fields } }

_HTTP_CODE_NAMES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    402: "PAYMENT_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    410: "GONE",
    413: "FILE_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "TIMEOUT",
}


def _error_body(status: int, detail) -> dict:
    """Build the standard { error: { code, message, ...extras } } envelope."""
    if isinstance(detail, dict):
        # Already structured — promote to top-level error object as-is.
        # Callers must include at least 'code' and 'message'.
        error = detail
    else:
        error = {
            "code": _HTTP_CODE_NAMES.get(status, "ERROR"),
            "message": str(detail),
        }
    return {"error": error}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.status_code, exc.detail),
        headers=getattr(exc, "headers", None) or {},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "fields": exc.errors(),
            }
        },
    )


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": f"Rate limit exceeded: {exc.detail}",
            }
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    ref = plog.error(
        f"{request.method} {request.url.path}",
        exc=exc, logger="api",
    )
    request.state.trace_ref = ref
    request.state.err_reason = type(exc).__name__
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Internal server error",
                "ref": ref,
            }
        },
    )


# -- Health -----------------------------------------------------------------

@app.get("/live")
async def liveness():
    return {"status": "ok"}

@app.get("/health")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def health(request: Request):
    try:
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    body = {"status": "ok" if db_status == "connected" else "error", "db": db_status}
    return JSONResponse(status_code=200 if db_status == "connected" else 503, content=body)



# -- Auth -------------------------------------------------------------------

@app.post("/auth/login", response_model=TokenOut)
@limiter.limit("20/minute")
async def login(request: Request, body: LoginRequest):
    pool = request.app.state.pool
    _sec = logging.getLogger("security")
    user = await db_mod.get_user_by_email(pool, body.email)
    if not user or not user.get("is_active", True):
        _sec.warning(
            "login.failed  email=%s  ip=%s  reason=%s",
            body.email, request.client.host, "user_not_found",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, user["hashed_pw"]):
        _sec.warning(
            "login.failed  email=%s  ip=%s  reason=%s",
            body.email, request.client.host, "bad_password",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(str(user["id"]), user["role"], user["email"])
    _sec.info(
        "login.success  user=%s  role=%s  ip=%s",
        user["email"], user["role"], request.client.host,
    )
    return TokenOut(
        access_token=token,
        user=UserOut(
            id=str(user["id"]),
            email=user["email"],
            role=user["role"],
            is_active=user.get("is_active", True),
            created_at=user.get("created_at"),
        ),
    )


@app.get("/auth/me", response_model=UserOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def auth_me(request: Request, user: dict = Depends(get_current_user)):
    pool = request.app.state.pool
    record = await db_mod.get_user_by_id(pool, user["id"])
    if not record:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut(
        id=str(record["id"]),
        email=record["email"],
        role=record["role"],
        is_active=record.get("is_active", True),
        created_at=record.get("created_at"),
    )


@app.get("/me/usage")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_my_usage(request: Request, user: dict = Depends(get_current_user)):
    """Return the calling user's page usage against their subscription limit."""
    pool = request.app.state.pool
    usage = await db_mod.get_user_billable_pages(pool, user["id"])
    return {
        "user_id": user["id"],
        "email": user.get("email"),
        **usage,
    }


# -- Admin: User Management -------------------------------------------------

@app.get("/admin/users", response_model=list[UserOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_list_users(request: Request, user: dict = Depends(require_admin)):
    rows = await db_mod.list_users(request.app.state.pool)
    return [
        UserOut(
            id=str(r["id"]),
            email=r["email"],
            role=r["role"],
            is_active=r.get("is_active", True),
            subscription_limit=r.get("subscription_limit", 0),
            created_at=r.get("created_at"),
        )
        for r in rows
    ]


@app.post("/admin/users", response_model=UserOut, status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_create_user(
    request: Request,
    body: UserCreate,
    user: dict = Depends(require_admin),
):
    pool = request.app.state.pool
    existing = await db_mod.get_user_by_email(pool, body.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    created = await db_mod.create_user(
        pool, body.email, hash_password(body.password), role=body.role,
    )
    return UserOut(
        id=str(created["id"]),
        email=created["email"],
        role=created["role"],
        is_active=created.get("is_active", True),
        subscription_limit=created.get("subscription_limit", 0),
        created_at=created.get("created_at"),
    )


@app.delete("/admin/users/{user_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_deactivate_user(
    request: Request,
    user_id: str,
    user: dict = Depends(require_admin),
):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    ok = await db_mod.deactivate_user(request.app.state.pool, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deactivated", "user_id": user_id}


@app.patch("/admin/users/{user_id}/reactivate")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_reactivate_user(
    request: Request,
    user_id: str,
    _user: dict = Depends(require_admin),
):
    ok = await db_mod.reactivate_user(request.app.state.pool, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "reactivated", "user_id": user_id}


@app.delete("/admin/users/{user_id}/hard")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_hard_delete_user(
    request: Request,
    user_id: str,
    user: dict = Depends(require_admin),
):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    ok = await db_mod.hard_delete_user(request.app.state.pool, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted", "user_id": user_id}


@app.patch("/admin/users/{user_id}/password")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_reset_user_password(
    request: Request,
    user_id: str,
    body: UserResetPassword,
    user: dict = Depends(require_admin),
):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot reset your own password via this endpoint")
    ok = await db_mod.reset_user_password(
        request.app.state.pool, user_id, hash_password(body.new_password)
    )
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "password_reset", "user_id": user_id}


@app.get("/admin/users/{user_id}/usage")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_get_user_usage(
    request: Request,
    user_id: str,
    user: dict = Depends(require_admin),
):
    """Get a user's current billable page usage and subscription limit."""
    pool = request.app.state.pool
    target = await db_mod.get_user_by_id(pool, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    usage = await db_mod.get_user_billable_pages(pool, user_id)
    return {
        "user_id": user_id,
        "email": target["email"],
        **usage,
    }


@app.patch("/admin/users/{user_id}/subscription-limit")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_update_subscription_limit(
    request: Request,
    user_id: str,
    body: dict,
    user: dict = Depends(require_admin),
):
    """Update a user's subscription page limit at runtime.

    Body: {"subscription_limit": 2000}
    """
    pool = request.app.state.pool
    new_limit = body.get("subscription_limit")
    if new_limit is None or not isinstance(new_limit, int) or new_limit < 0:
        raise HTTPException(status_code=400, detail="subscription_limit must be a non-negative integer")
    ok = await db_mod.update_user_subscription_limit(pool, user_id, new_limit)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Admin %s updated subscription_limit for user %s to %d", user["id"], user_id, new_limit)
    return {"status": "updated", "user_id": user_id, "subscription_limit": new_limit}


# -- Admin: API Key Management -----------------------------------------------

@app.post("/admin/api-keys", status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_create_api_key(
    request: Request,
    body: ApiKeyCreate,
    user: dict = Depends(require_admin),
):
    """Create a new API key for an existing client user."""
    pool = request.app.state.pool
    label = body.label.strip()

    # Validate owner user exists and is a client
    owner = await db_mod.get_user_by_id(pool, body.owner_user_id)
    if not owner:
        raise HTTPException(status_code=404, detail="User not found")
    if owner.get("role") not in ("client", "admin"):
        raise HTTPException(status_code=400, detail="API keys can only be created for client or admin users")

    # Generate key
    raw_key, key_hash, prefix = generate_api_key()
    from .auth import encrypt_api_key
    from datetime import datetime, timedelta, timezone as _tz
    import asyncpg as _asyncpg
    encrypted = encrypt_api_key(raw_key)
    expires_at = None
    if body.expires_days:
        expires_at = datetime.now(_tz.utc) + timedelta(days=body.expires_days)
    try:
        key_row = await db_mod.create_api_key(
            pool,
            user_id=body.owner_user_id,
            label=label,
            key_hash=key_hash,
            prefix=prefix,
            encrypted_key=encrypted,
            expires_at=expires_at,
        )
    except _asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"A key named '{label}' already exists for this user. Choose a different name.",
        )
    logger.info("Admin %s created API key '%s' for user %s (expires=%s)", user["id"], label, body.owner_user_id, expires_at)

    return {"id": key_row.get("id"), "raw_key": raw_key, "label": label, "prefix": prefix}


@app.get("/admin/api-keys/{key_id}/reveal")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_reveal_api_key(
    request: Request,
    key_id: int,
    _user: dict = Depends(require_admin),
):
    """Return the decrypted raw key for a given API key ID. Admin only."""
    pool = request.app.state.pool
    row = await db_mod.get_api_key_encrypted(pool, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="API key not found")
    if not row.get("encrypted_key"):
        raise HTTPException(
            status_code=404,
            detail="This key was created before encrypted storage was added. Deactivate it and create a new one.",
        )
    from .auth import decrypt_api_key
    raw = decrypt_api_key(row["encrypted_key"])
    return {"raw_key": raw, "label": row["label"]}


@app.get("/admin/api-keys", response_model=list[ApiKeyOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_list_api_keys(
    request: Request,
    _user: dict = Depends(require_admin),
):
    """List all API keys with usage stats."""
    rows = await db_mod.list_api_keys(request.app.state.pool)
    out = []
    for r in rows:
        out.append(ApiKeyOut(
            id=r["id"],
            user_id=str(r["user_id"]) if r.get("user_id") else None,
            label=r["label"],
            prefix=r["prefix"],
            is_active=r.get("is_active", True),
            owner_email=r.get("owner_email"),
            total_tokens=int(r.get("total_tokens") or 0),
            total_documents=int(r.get("total_documents") or 0),
            total_pages=int(r.get("total_pages") or 0),
            created_at=r.get("created_at"),
            last_used_at=r.get("last_used_at"),
            expires_at=r.get("expires_at"),
        ))
    return out


@app.patch("/admin/api-keys/{key_id}/deactivate")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_deactivate_api_key(
    request: Request,
    key_id: int,
    _user: dict = Depends(require_admin),
):
    """Deactivate an API key. It stops working immediately."""
    ok = await db_mod.deactivate_api_key(request.app.state.pool, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "deactivated", "key_id": key_id}


@app.patch("/admin/api-keys/{key_id}/reactivate")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_reactivate_api_key(
    request: Request,
    key_id: int,
    _user: dict = Depends(require_admin),
):
    """Reactivate a previously deactivated API key."""
    ok = await db_mod.activate_api_key(request.app.state.pool, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "reactivated", "key_id": key_id}


@app.delete("/admin/api-keys/{key_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_delete_api_key(
    request: Request,
    key_id: int,
    _user: dict = Depends(require_admin),
):
    """Hard-delete an API key. Usage history in llm_usage is preserved."""
    ok = await db_mod.delete_api_key(request.app.state.pool, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "deleted", "key_id": key_id}


# -- Vendors ----------------------------------------------------------------

@app.get("/vendors", response_model=list[VendorOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_vendors(
    request: Request,
    user: dict = Depends(get_current_user),
    user_id: str | None = None,  # admin-only: scope to a specific client
):
    """
    Return vendors visible to the caller.
    - Clients always see only their own vendors.
    - Admin sees all vendors by default, or a specific client's vendors
      when ?user_id=<client_uuid> is passed (used by the Extract page
      "Act As Client" dropdown to scope the vendor shortcut list).
    """
    if user["role"] == "admin":
        # Admin can optionally scope to a specific client; None = all vendors
        filter_user = user_id if user_id else None
    else:
        # Clients always see only their own vendors — ignore any user_id param
        filter_user = user["id"]
    rows = await db_mod.list_vendors(request.app.state.pool, user_id=filter_user)
    return [VendorOut(**r) for r in rows]


@app.post("/vendors", response_model=VendorOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def create_vendor(request: Request, body: VendorCreate, user: dict = Depends(get_current_user)):
    pool = request.app.state.pool

    if user["role"] == "admin":
        owner_id = body.user_id
        if not owner_id:
            raise HTTPException(
                status_code=400,
                detail="Admin must specify user_id when creating a new vendor",
            )
    else:
        # Clients always own vendors they create; ignore any user_id in body.
        owner_id = user["id"]

    # id is issued server-side; name is unique per owner, so a re-submitted
    # name returns the existing vendor rather than creating a duplicate.
    row = await db_mod.create_vendor_by_name(pool, body.name, user_id=owner_id)
    # Auto-insert vendor name as a detection alias (idempotent on the alias side)
    try:
        await db_mod.insert_vendor_alias(pool, row["id"], body.name.lower(), weight=1, source="auto_from_name")
    except Exception as exc:
        logger.warning("Failed to auto-insert vendor alias for %s: %s", row["id"], exc)
    return VendorOut(**row)


# -- Vendor Aliases ---------------------------------------------------------
# Registered BEFORE DELETE /vendors/{vendor_id} so that
# DELETE /vendors/aliases/{alias_id} is not swallowed by the broader route.

@app.get("/vendors/{vendor_id}/aliases", response_model=list[VendorAliasOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_aliases(request: Request, vendor_id: str, user: dict = Depends(get_current_user)):
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    return await db_mod.list_vendor_aliases(pool, vendor_id)


@app.post("/vendors/{vendor_id}/aliases", response_model=VendorAliasOut, status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def add_alias(
    request: Request,
    vendor_id: str,
    body: VendorAliasCreate,
    user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    row = await db_mod.insert_vendor_alias(pool, vendor_id, body.pattern, body.weight, source="manual")
    if row is None:
        raise HTTPException(409, detail="Alias pattern already exists for this vendor")
    return row


@app.delete("/vendors/aliases/{alias_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_alias(request: Request, alias_id: int, user: dict = Depends(get_current_user)):
    pool = request.app.state.pool
    await assert_alias_access(pool, alias_id, user)
    deleted = await db_mod.delete_vendor_alias(pool, alias_id)
    if not deleted:
        raise HTTPException(404, detail="Alias not found")
    return {"status": "deleted", "alias_id": alias_id}


@app.patch("/vendors/{vendor_id}/owner", response_model=VendorOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def assign_vendor_owner(request: Request, vendor_id: str, body: dict, _user: dict = Depends(require_admin)):
    pool = request.app.state.pool
    new_owner_id = body.get("user_id")
    if not new_owner_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    # Verify target user exists
    target = await db_mod.get_user_by_id(pool, new_owner_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target user not found")
    row = await db_mod.assign_vendor_owner(pool, vendor_id, new_owner_id)
    if not row:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return VendorOut(**row)


@app.delete("/vendors/{vendor_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_vendor(request: Request, vendor_id: str, user: dict = Depends(get_current_user)):
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    deleted = await db_mod.delete_vendor(pool, vendor_id)
    if not deleted:
        raise HTTPException(404, detail="Vendor not found")
    return {"status": "deleted", "vendor_id": vendor_id}


# -- Vendor Detection -------------------------------------------------------

@app.post("/detect-vendor")
@limiter.limit("10/minute")
async def detect_vendor_endpoint(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Detect vendor from uploaded PDF/image by analyzing page-1 text.

    Renders page 1 only, extracts text via pypdfium2 (digital) or PaddleOCR
    (scanned fallback), then matches against vendor_aliases in DB.

    Returns:
        200: {detected: true, vendor_id, vendor_name, score, page_source, matched_patterns}
        409: {detected: false, reason: "unknown_vendor", hint: "..."}
    """
    from . import geometry
    from . import vendor_detector
    from . import ocr_runner

    pool = request.app.state.pool
    file_bytes = await file.read()
    filename = (file.filename or "unknown").lower()

    # Render page 1 only
    rendered = await render_page_1_for_detection(file_bytes, filename)

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

    detect_user_id = None if user.get("role") == "admin" else user["id"]
    match = await vendor_detector.detect_vendor(pool, page_words, user_id=detect_user_id)
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

    # Defense-in-depth: confirm detected vendor belongs to this user
    await assert_vendor_access(pool, match.vendor_id, user)

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
async def get_template(request: Request, vendor_id: str, user: dict = Depends(get_current_user)):
    await assert_vendor_access(request.app.state.pool, vendor_id, user)
    tmpl_row = await db_mod.get_template(request.app.state.pool, vendor_id)
    if not tmpl_row:
        raise HTTPException(404, detail="No template configured for this vendor")
        
    tmpl = dict(tmpl_row)
    tmpl["system_prompt"] = None
    tmpl["user_prompt"] = None
    tmpl["system_prompt_page1"] = None
    tmpl["user_prompt_page1"] = None
    tmpl["system_prompt_page2"] = None
    tmpl["user_prompt_page2"] = None

    if user.get("role") == "admin":
        try:
            from . import extractor
            vendor = await db_mod.get_vendor(request.app.state.pool, vendor_id)
            gold_examples = await db_mod.get_gold_examples(request.app.state.pool, vendor_id)
            prompt_args = dict(
                header_fields=tmpl.get("header_fields") or [],
                line_item_fields=tmpl.get("line_item_fields") or [],
                instructions=tmpl.get("prompt_instructions"),
                rules=tmpl.get("extraction_rules") or [],
                format_type=tmpl.get("format_type") or "single_po_multipage",
                gold_examples=gold_examples,
            )
            tmpl["system_prompt_page1"] = extractor.build_system_prompt(
                **prompt_args,
                include_boxes=True,
            )
            tmpl["user_prompt_page1"] = extractor.build_user_message(
                tmpl.get("header_fields") or [],
                tmpl.get("line_item_fields") or [],
                page_num=1,
                total_pages=2,
                include_boxes=True,
            )
            tmpl["system_prompt_page2"] = extractor.build_system_prompt(**prompt_args, include_boxes=False)
            tmpl["user_prompt_page2"] = extractor.build_user_message(
                tmpl.get("header_fields") or [],
                tmpl.get("line_item_fields") or [],
                page_num=2,
                total_pages=2,
                include_boxes=False,
            )
            tmpl["system_prompt"] = tmpl["system_prompt_page1"]
            tmpl["user_prompt"] = tmpl["user_prompt_page1"]
        except Exception as e:
            logger.warning("Failed to build admin prompt preview: %s", e)
            tmpl["user_prompt"] = "Error building preview"
        
    return TemplateOut(**tmpl)


@app.get("/vendors/{vendor_id}/gold-corrections")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_vendor_gold_corrections(
    request: Request, vendor_id: str, user: dict = Depends(get_current_user),
):
    """Return latest saved gold correction per field for UI warnings."""
    await assert_vendor_access(request.app.state.pool, vendor_id, user)
    fields = await db_mod.get_latest_gold_correction_fields(request.app.state.pool, vendor_id)
    return {"vendor_id": vendor_id, "fields": fields}


@app.get("/extractions/{extraction_id}/spatial-memory-fields")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_spatial_memory_fields(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """Return field keys that have active spatial memory for this extraction's vendor+layout."""
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")

    vendor_id = extraction.get("vendor_id")
    template_id = extraction.get("template_id")
    if not vendor_id:
        return {"extraction_id": extraction_id, "fields": {}}

    from .layout_key import compute_layout_key
    lk = compute_layout_key(vendor_id, template_id, [])

    memories = await db_mod.get_spatial_memory_for_layout(pool, vendor_id, lk)
    fields = {m["field_key"]: {"page": m["page_number"]} for m in memories}
    return {"extraction_id": extraction_id, "vendor_id": vendor_id, "layout_key": lk, "fields": fields}


@app.post("/vendors/{vendor_id}/template", response_model=TemplateSaveResponse)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def save_template(
    request: Request,
    vendor_id: str,
    body: TemplateCreate,
    user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool

    # If vendor exists, the caller must own it; if it doesn't, the client
    # auto-creates it as their own (admins must create vendors via POST /vendors).
    existing = await db_mod.get_vendor(pool, vendor_id)
    if existing:
        await assert_vendor_access(pool, vendor_id, user)
        owner_id = None  # preserve existing owner via upsert
    else:
        if user["role"] == "admin":
            raise HTTPException(
                status_code=400,
                detail="Admin must create the vendor via POST /vendors before saving a template",
            )
        owner_id = user["id"]

    if not body.header_fields and not body.line_item_fields:
        raise HTTPException(
            status_code=400,
            detail="Template must contain at least one header field or line item column.",
        )

    # Auto-upsert vendor — frontend may have created vendor locally while offline.
    # Use vendor_name from body if provided, otherwise fall back to vendor_id as name.
    vendor_name = (body.vendor_name or vendor_id).upper()
    await db_mod.upsert_vendor(pool, vendor_id, vendor_name, user_id=owner_id)

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

        # ── ERP mapping: carry field renames forward (position-based) ──
        # If a mapped field is renamed in the template, move the mapping to
        # the new name and queue a notice. Orphaned source fields are pruned.
        try:
            existing_map = await db_mod.get_field_mapping(pool, vendor_id)
            if existing_map and tmpl:
                from . import field_mapper as _fm

                new_header = list(body.header_fields or [])
                new_line = list(body.line_item_fields or [])
                header_renames = _fm.detect_renames(
                    existing_map.get("header_snapshot"), new_header)
                line_renames = _fm.detect_renames(
                    existing_map.get("line_snapshot"), new_line)

                prev_header_map = existing_map.get("header_map") or {}
                prev_line_map = existing_map.get("line_map") or {}
                new_header_map = _fm.apply_renames(prev_header_map, header_renames)
                new_line_map = _fm.apply_renames(prev_line_map, line_renames)
                # Drop mappings whose source field no longer exists in the template.
                new_header_map = {k: v for k, v in new_header_map.items() if k in new_header}
                new_line_map = {k: v for k, v in new_line_map.items() if k in new_line}

                notices = list(existing_map.get("pending_notices") or [])
                now_iso = datetime.now(UTC).isoformat()
                for old, new in header_renames:
                    notices.append({
                        "section": "header", "old": old, "new": new,
                        "at": now_iso, "remapped": old in prev_header_map,
                    })
                for old, new in line_renames:
                    notices.append({
                        "section": "line", "old": old, "new": new,
                        "at": now_iso, "remapped": old in prev_line_map,
                    })

                await db_mod.upsert_field_mapping(
                    pool, vendor_id, tmpl["id"],
                    new_header_map, new_line_map,
                    new_header, new_line, notices,
                )
                if header_renames or line_renames:
                    logger.info(
                        "ERP mapping rename carried forward: vendor=%s header=%s line=%s",
                        vendor_id, header_renames, line_renames,
                    )
        except Exception as map_exc:
            logger.warning("ERP mapping rename check failed vendor=%s: %s", vendor_id, map_exc)

        return TemplateSaveResponse(
            template_id=tmpl["id"],
            prompt_hash=prompt_hash,
            system_prompt_preview=system_prompt[:200] if user.get("role") == "admin" else "",
        )
    except Exception:
        logger.error("Template save FAILED vendor=%s:\n%s", vendor_id, traceback.format_exc())
        raise


# -- ERP Field Mapping -----------------------------------------------------

@app.get("/vendors/{vendor_id}/mapping")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_vendor_mapping(
    request: Request, vendor_id: str, user: dict = Depends(get_current_user),
):
    """Return the ERP field mapping for a vendor plus the canonical targets."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    from . import field_mapper as _fm

    tmpl = await db_mod.get_template(pool, vendor_id)
    mapping = await db_mod.get_field_mapping(pool, vendor_id)
    return {
        "vendor_id": vendor_id,
        "template_id": (tmpl or {}).get("id"),
        "has_template": tmpl is not None,
        "source_header_fields": (tmpl or {}).get("header_fields") or [],
        "source_line_fields": (tmpl or {}).get("line_item_fields") or [],
        "target_header_fields": _fm.HEADER_TARGETS,
        "target_line_fields": _fm.LINE_TARGETS,
        "header_map": (mapping or {}).get("header_map") or {},
        "line_map": (mapping or {}).get("line_map") or {},
        "pending_notices": (mapping or {}).get("pending_notices") or [],
        "configured": mapping is not None,
    }


@app.post("/vendors/{vendor_id}/mapping")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def save_vendor_mapping(
    request: Request,
    vendor_id: str,
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """Save (upsert) a vendor's ERP field mapping. One mapping per vendor."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    from . import field_mapper as _fm

    tmpl = await db_mod.get_template(pool, vendor_id)
    if not tmpl:
        raise HTTPException(
            status_code=404,
            detail="Configure a template for this vendor before saving a mapping.",
        )

    header_map = body.get("header_map")
    line_map = body.get("line_map")
    if not isinstance(header_map, dict) or not isinstance(line_map, dict):
        raise HTTPException(400, detail="header_map and line_map must be objects.")

    # Keep only entries that point at a known canonical target field.
    header_map = {str(k): v for k, v in header_map.items() if v in _fm.HEADER_TARGETS}
    line_map = {str(k): v for k, v in line_map.items() if v in _fm.LINE_TARGETS}

    existing = await db_mod.get_field_mapping(pool, vendor_id)
    saved = await db_mod.upsert_field_mapping(
        pool, vendor_id, tmpl["id"], header_map, line_map,
        tmpl.get("header_fields") or [], tmpl.get("line_item_fields") or [],
        (existing or {}).get("pending_notices") or [],
    )
    logger.info(
        "ERP mapping saved: vendor=%s header=%d line=%d",
        vendor_id, len(header_map), len(line_map),
    )
    return {
        "status": "ok",
        "vendor_id": vendor_id,
        "header_map": saved["header_map"],
        "line_map": saved["line_map"],
    }


@app.delete("/vendors/{vendor_id}/mapping/notices")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def clear_vendor_mapping_notices(
    request: Request, vendor_id: str, user: dict = Depends(get_current_user),
):
    """Dismiss all pending rename notices for a vendor's mapping."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    if await db_mod.get_field_mapping(pool, vendor_id):
        await db_mod.set_field_mapping_notices(pool, vendor_id, [])
    return {"status": "ok"}


@app.get("/vendors/{vendor_id}/mapping/sample")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_vendor_mapping_sample(
    request: Request, vendor_id: str, user: dict = Depends(get_current_user),
):
    """Return the latest real extraction for the source panel: all header
    fields plus a single representative line item (not the whole list)."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)

    extractions = await db_mod.list_extractions(pool, vendor_id, limit=15)
    chosen = None
    for ext in extractions:
        if ext.get("result"):
            chosen = ext
            break
    if not chosen:
        return {"has_data": False}

    result = chosen.get("result")
    doc = result[0] if isinstance(result, list) and result else result
    if not isinstance(doc, dict):
        return {"has_data": False}

    header = {k: v for k, v in doc.items() if k != "line_items"}
    line_items = doc.get("line_items") or []
    line_item = line_items[0] if line_items and isinstance(line_items[0], dict) else {}
    return {
        "has_data": True,
        "extraction_id": chosen.get("id"),
        "filename": chosen.get("filename"),
        "format_type": chosen.get("format_type"),
        "header": header,
        "line_item": line_item,
        "line_item_count": len(line_items),
    }


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
    _user: dict = Depends(get_current_user),
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
async def cancel_extraction(extraction_id: int, _user: dict = Depends(get_current_user)):
    """Set the cancellation flag for a running extraction."""
    raise HTTPException(
        status_code=410,
        detail="The legacy /extract/cancel endpoint is deprecated. Use /jobs/extractions/{extraction_id}/cancel.",
    )


@app.post("/extract/resume/{extraction_id}")
@limiter.limit("10/minute")
async def resume_extraction(request: Request, extraction_id: int):
    """Resume an extraction from the last completed page."""
    raise HTTPException(
        status_code=410,
        detail="The legacy /extract/resume endpoint is deprecated. Use /jobs/extractions/{extraction_id}/resume.",
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
    act_as_client_id: str | None = Form(
        None,
        description=(
            "Admin-only: the client UUID whose vendors/aliases to use for "
            "vendor detection. Scopes detection to prevent cross-tenant "
            "template/alias collisions. Billing stays on admin's account."
        ),
    ),
    user: dict = Depends(get_current_user),
):
    if source_type not in {"ui", "rest", "email", "s3", "sftp", "partner"}:
        raise HTTPException(400, detail=f"Unsupported source_type '{source_type}'")

    pool = request.app.state.pool

    # Block UI uploads when the scheduler is actively executing for this user
    if source_type == "ui" and await db_mod.get_user_is_executing(pool, user["id"]):
        raise HTTPException(
            409,
            detail="Scheduler is currently running. Please wait for it to finish before uploading manually.",
        )

    # If caller pre-selected a vendor, enforce ownership before doing any work.
    if vendor_id:
        await assert_vendor_access(pool, vendor_id, user)
    file_bytes = await file.read()
    filename = file.filename or "unknown"
    detected_vendor = None
    try:
        req_header = json.loads(header_fields) if header_fields else []
        req_items = json.loads(line_item_fields) if line_item_fields else []
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_JSON_FIELD", "message": f"header_fields or line_item_fields is not valid JSON: {exc}"}) from exc

    # -- Page count + hard cap -------------------------------------------------
    if filename.lower().endswith(".pdf"):
        try:
            incoming_pages = processor.count_pdf_pages(file_bytes)
        except ValueError:
            raise HTTPException(400, detail="Could not read the uploaded PDF.")
        if incoming_pages > MAX_DOCUMENT_PAGES:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "DOCUMENT_TOO_LARGE",
                    "message": f"PDF has {incoming_pages} pages. Maximum allowed is {MAX_DOCUMENT_PAGES} pages.",
                    "pages": incoming_pages,
                    "max_pages": MAX_DOCUMENT_PAGES,
                },
            )
    else:
        incoming_pages = 1

    # -- Subscription quota check (atomic reservation) -------------------------
    # Admins are never subject to quota checks.
    usage_warning = None
    if user.get("role") != "admin":
        try:
            quota = await db_mod.reserve_quota(pool, user["id"], incoming_pages)
        except Exception as usage_exc:
            logger.error("Subscription quota check failed — blocking upload: %s", usage_exc)
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry.",
            )
        if not quota["allowed"]:
            _user_record = await db_mod.get_user_by_id(pool, user["id"])
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=(_user_record or {}).get("email"),
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="exceeded",
                filename=filename,
            )
            overage = max(quota["used"] - quota["limit"], 0)
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "QUOTA_EXCEEDED",
                    "message": (
                        f"Uploading this document ({incoming_pages} pages) would exceed your "
                        f"subscription limit of {quota['limit']} pages. "
                        f"You have {quota['remaining']} pages remaining. "
                        "Contact your administrator to increase your limit."
                    ),
                    "subscription_limit": quota["limit"],
                    "total_extracted_pages": quota["used"],
                    "incoming_pages": incoming_pages,
                    "remaining": quota["remaining"],
                    "overage": overage,
                },
            )
        # Set warning if near threshold or grace overage
        u_pct = quota["used"] / max(quota["limit"], 1)
        if quota["reason"] == "grace":
            _user_record = await db_mod.get_user_by_id(pool, user["id"])
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=(_user_record or {}).get("email"),
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="small_overage",
                filename=filename,
            )
            usage_warning = {
                "level": "warning",
                "message": (
                    f"This upload ({incoming_pages} pages) slightly exceeds your remaining "
                    f"quota ({quota['remaining']} pages). It has been allowed as a small overage."
                ),
                "subscription_limit": quota["limit"],
                "total_extracted_pages": quota["used"],
                "incoming_pages": incoming_pages,
                "remaining": quota["remaining"],
            }
        elif u_pct >= SUBSCRIPTION_WARNING_THRESHOLD:
            _user_record = await db_mod.get_user_by_id(pool, user["id"])
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=(_user_record or {}).get("email"),
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="warning",
                filename=filename,
            )
            usage_warning = {
                "level": "warning",
                "message": (
                    f"You have used {quota['used']} of {quota['limit']} pages "
                    f"({round(u_pct * 100, 1)}%). "
                    f"Only {quota['remaining']} pages remaining."
                ),
                "subscription_limit": quota["limit"],
                "total_extracted_pages": quota["used"],
                "remaining": quota["remaining"],
            }

    logger.info("File received: %s (%d bytes, source=%s, vendor=%s)",
                filename, len(file_bytes), source_type, vendor_id)

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
                from . import geometry as _geo
                from . import vendor_detector as _vd
                from . import ocr_runner as _ocr

                # Render page 1 only for detection
                with plog.timed("render_p1") as t:
                    rendered = await render_page_1_for_detection(file_bytes, filename)
                logger.info("Rendered page-1 for vendor detection (%d pages, %.0fms)", len(rendered), t["ms"])

                if not rendered:
                    logger.warning("Vendor detection failed: no rendered pages")
                    if user.get("role") != "admin":
                        try:
                            await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
                        except Exception:
                            pass
                    raise HTTPException(400, detail="Could not render any pages from the uploaded file")

                page1 = rendered[0]
                page1_meta = {"page_number": 1, "width": page1.get("width", 0), "height": page1.get("height", 0)}

                # Digital-first text extraction
                if filename.lower().endswith(".pdf"):
                    with plog.timed("geometry_p1") as t:
                        geo_pages = _geo.compute_pdf_geometry(file_bytes, [page1_meta])
                        geo_page = geo_pages[0] if geo_pages else {}
                    logger.info("Page-1 text: source=%s, %d chars, %d words (%.0fms)",
                                geo_page.get("source"), geo_page.get("char_count", 0),
                                len(geo_page.get("words", []) or []), t["ms"])
                    page_words = geo_pages[0].get("words", []) if geo_pages else []
                    page_source = (geo_pages[0].get("source") if geo_pages else None) or "paddleocr"
                else:
                    page_words = []
                    page_source = "paddleocr"

                # Scanned fallback if needed
                if not page_words:
                    logger.info("Page-1 has no digital text — falling back to PaddleOCR")
                    with plog.timed("paddleocr_p1") as t:
                        ocr_pages = await _ocr.run_ocr_on_pages([{
                            "page_number": 1,
                            "image_b64": page1["image_b64"],
                            "mime_type": page1.get("mime_type", "image/jpeg"),
                        }])
                        if ocr_pages:
                            page_words = ocr_pages[0].get("words", [])
                    logger.info("PaddleOCR fallback: %d words (%.0fms)", len(page_words), t["ms"])
                    page_source = "paddleocr"

                # Vendor detection is ALWAYS scoped to a specific client.
                # • For regular clients: always their own user_id.
                # • For admin: use act_as_client_id if supplied (chosen via UI
                #   dropdown); fall back to admin's own user_id.
                # We never pass None (global) to avoid cross-tenant collisions
                # where two clients both have a vendor named e.g. "Aegis".
                if user.get("role") == "admin":
                    _detect_uid = act_as_client_id if act_as_client_id else user["id"]
                    if not act_as_client_id:
                        logger.info(
                            "[VendorDetect] Admin upload with no act_as_client_id — "
                            "scoping to admin's own vendors (user_id=%s)",
                            user["id"],
                        )
                    else:
                        logger.info(
                            "[VendorDetect] Admin acting as client user_id=%s for vendor detection",
                            _detect_uid,
                        )
                else:
                    _detect_uid = user["id"]
                with plog.timed("vendor_match") as t:
                    match = await _vd.detect_vendor(pool, page_words, user_id=_detect_uid)
                if match:
                    logger.info("Vendor matched: %s (id=%s, score=%.2f, %.0fms)",
                                match.vendor_name, match.vendor_id, match.score, t["ms"])
                if match is None:
                    vendor_trace["output"] = {
                        "detected": False,
                        "reason": "unknown_vendor",
                        "page_source": page_source,
                        "word_count": len(page_words),
                    }
                    logger.warning("Unknown vendor blocked for %s (%d words)", filename, len(page_words))
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
                    if user.get("role") != "admin":
                        try:
                            await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
                        except Exception:
                            pass
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "reason": "unknown_vendor",
                            "hint": "Create the vendor and vendor id first, then retry this document.",
                            "word_count": len(page_words),
                        },
                    )
                vendor_id = match.vendor_id
                # Enforce ownership on the detected vendor — clients can't
                # ingest into a vendor they don't own, even if the document
                # text matches that vendor's aliases.
                await assert_vendor_access(pool, vendor_id, user)
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

        # Whoever logins, bill to them.
        billing_user_id = user["id"]

        try:
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
                metadata={"billing_user_id": billing_user_id} if billing_user_id else None,
                reserved_pages=incoming_pages if user.get("role") != "admin" else None,
            )
        except Exception:
            if user.get("role") != "admin":
                try:
                    await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
                except Exception:
                    pass
            raise
        resp = ExtractionJobStartOut(
            job_id=submitted["job"]["id"],
            extraction_id=submitted["extraction"]["id"],
            status=submitted["job"]["status"],
        )
        # Include detection result in response if auto-detected
        result = resp.model_dump() if hasattr(resp, "model_dump") else resp.dict()
        if detected_vendor:
            result["detected_vendor"] = detected_vendor
        if usage_warning:
            result["usage_warning"] = usage_warning
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
        logger.info("Ingestion job created: ext=%s vendor=%s file=%s",
                    submitted["extraction"]["id"], vendor_id, filename)
        return result


@app.get("/jobs/{job_id}", response_model=JobStatusOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_job_status(request: Request, job_id: int, user: dict = Depends(get_current_user)):
    pool = request.app.state.pool
    await assert_job_access(pool, job_id, user)
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
async def stream_job_status_sse(
    request: Request, job_id: int, user: dict = Depends(get_current_user_sse),
):
    """SSE stream for real-time job progress.

    One persistent connection replaces client-side polling.
    The server checks the DB every ~1 second and pushes changes
    as SSE events. Heavy JSONB fields (result, ocr_data, …) are
    only included in the terminal event to keep progress messages tiny.

    No rate limiter — this is one long-lived connection, not repeated requests.
    Auth: accepts Bearer header OR ?token= query param (EventSource cannot
    set headers).
    """
    pool = request.app.state.pool
    await assert_job_access(pool, job_id, user)
    job = await db_mod.get_job(pool, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    _HEAVY_KEYS = frozenset({
        "result", "page_results", "ocr_data", "field_locations",
        "corrected_result", "correction_meta",
    })

    def _serialize(obj):
        """JSON serializer for datetime, Decimal, and other non-serializable types."""
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        from decimal import Decimal
        if isinstance(obj, Decimal):
            return float(obj)
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
                if extraction and extraction.get("status") not in {"done", "failed", "partial", "cancelled", "unverified"}:
                    latest_job = await db_mod.get_latest_job_for_extraction(pool, extraction["id"])
                    if latest_job:
                        current_job = latest_job

            ext_status = extraction["status"] if extraction else None
            job_status = current_job["status"]
            
            # If this job belongs to an extraction pipeline, only the extraction's
            # status determines if we are done. Otherwise, use the job's status.
            if extraction:
                is_terminal = ext_status in ("done", "failed", "partial", "cancelled", "unverified")
            else:
                is_terminal = job_status in ("done", "failed", "cancelled")

            # Cheap fingerprint to detect state changes
            ext_progress = extraction.get("progress") if extraction else ""
            fingerprint = f"{job_status}|{ext_status}|{ext_progress}"

            if fingerprint != last_fingerprint or is_terminal:
                if is_terminal:
                    if ext_status in ("failed", "unverified") or job_status == "failed":
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
async def request_job_cancel(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    _TERMINAL = {"done", "failed", "partial", "cancelled"}
    if extraction["status"] in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONFLICT",
                "message": f"Cannot cancel extraction in terminal state '{extraction['status']}'",
                "current_status": extraction["status"],
            }
        )
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
async def queue_resume_extraction(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    if extraction["status"] not in ("partial", "cancelled", "failed", "cancelling"):
        raise HTTPException(400, detail=f"Cannot resume extraction with status '{extraction['status']}'")

    # -- Quota check (same rule as /ingest/ui) --------------------------------
    if user.get("role") != "admin":
        try:
            usage_info = await db_mod.get_user_billable_pages(pool, user["id"])
            u_used = usage_info["billable_pages"]
            u_limit = usage_info["subscription_limit"]
            if u_used >= u_limit:
                _user_record = await db_mod.get_user_by_id(pool, user["id"])
                page_logger.log_limit_alert(
                    user_id=user["id"],
                    email=(_user_record or {}).get("email"),
                    total_extracted_pages=u_used,
                    subscription_limit=u_limit,
                    alert_type="exceeded",
                    filename=extraction.get("filename"),
                )
                overage = u_used - u_limit
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "QUOTA_EXCEEDED",
                        "message": (
                            f"Page limit exceeded. "
                            f"Subscription: {u_limit} pages, "
                            f"Extracted: {u_used} pages, "
                            f"Overage: {overage} pages. "
                            "Contact your administrator to increase your limit."
                        ),
                        "subscription_limit": u_limit,
                        "total_extracted_pages": u_used,
                        "overage": overage,
                    },
                )
        except HTTPException:
            raise
        except Exception as usage_exc:
            logger.error("Subscription quota check failed — blocking upload: %s", usage_exc)
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry.",
            )

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
async def count_all_extractions(request: Request, user: dict = Depends(get_current_user)):
    filter_user = None if user["role"] == "admin" else user["id"]
    return {
        "count": await db_mod.count_all_extractions(
            request.app.state.pool, user_id=filter_user,
        )
    }


@app.get("/extractions/{extraction_id}", response_model=ExtractionOut)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)

    row = await db_mod.get_extraction(pool, extraction_id)
    if not row:
        raise HTTPException(404, detail="Extraction not found")

    return ExtractionOut(**row)


@app.delete("/extractions/{extraction_id}")
@limiter.limit("20/minute")
async def delete_extraction(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """
    Delete one extraction (history row + related artifacts), not the whole DB/vendor.
    """
    pool = request.app.state.pool
    store = request.app.state.store

    await assert_extraction_access(pool, extraction_id, user)
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
    # 3. Delete from object store
    deleted_objects = {"pages": 0, "exports": 0, "document": 0}

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
async def get_extraction_pages(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """
    Return all rendered page images for an extraction.
    Frontend uses this to populate the page viewer after upload.
    Each item: {page_number, image_b64, width, height}
    """
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)

    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")

    return await _load_page_payloads(pool, extraction_id)


@app.get("/vendors/{vendor_id}/extractions", response_model=list[ExtractionOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_vendor_extractions(
    request: Request, vendor_id: str, limit: int = 20,
    user: dict = Depends(get_current_user),
):
    await assert_vendor_access(request.app.state.pool, vendor_id, user)
    filter_user = None if user["role"] == "admin" else user["id"]
    rows = await db_mod.list_extractions(
        request.app.state.pool, vendor_id, limit, user_id=filter_user
    )
    return [ExtractionOut(**r) for r in rows]


# -- All Templates ----------------------------------------------------------

@app.get("/templates", response_model=list[TemplateListOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_all_templates(request: Request, user: dict = Depends(get_current_user)):
    filter_user = None if user["role"] == "admin" else user["id"]
    rows = await db_mod.list_all_templates(request.app.state.pool, user_id=filter_user)
    return [TemplateListOut(**r) for r in rows]


# -- Global Extraction History -----------------------------------------------

@app.get("/extractions", response_model=list[ExtractionOut])
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_all_extractions(
    request: Request, limit: int = 50, user: dict = Depends(get_current_user),
):
    filter_user = None if user["role"] == "admin" else user["id"]
    rows = await db_mod.list_all_extractions(
        request.app.state.pool, limit, user_id=filter_user,
    )
    return [ExtractionOut(**r) for r in rows]


# -- Upload Preview (pre-extraction page rendering) --------------------------

@app.post("/upload-preview")
@limiter.limit("10/minute")
async def upload_preview(
    request: Request,
    file: UploadFile = File(...),
    max_pages: int = Form(20),
    user: dict = Depends(get_current_user),
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

    total_pages = pages[0].get("doc_total_pages", len(pages)) if pages else 0
    return {"filename": filename, "total_pages": total_pages, "pages": pages}


# -- Review: OCR Data for Click-to-Select -----------------------------------

@app.get("/extractions/{extraction_id}/ocr")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_ocr(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """Return PaddleOCR word data for the click-to-select correction UI."""
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    ocr_data = await db_mod.get_ocr_data(pool, extraction_id)
    if ocr_data is None:
        raise HTTPException(404, detail="No OCR data found for this extraction")
    return {"extraction_id": extraction_id, "ocr_pages": ocr_data}


@app.get("/extractions/{extraction_id}/geometry")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_geometry(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """Return unified per-page geometry with source classification.

    Each page entry includes: page_number, source ('pypdfium'|'paddleocr'),
    char_count, word_count, and words [{text, box, score}].
    Falls back to ocr_data if page-level geometry is not available.
    """
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
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
async def save_extraction_corrections(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """Persist user corrections from the Review page.

    Saves to corrected_result (original result stays immutable).
    Auto-creates an audit gold example for this vendor if fields were changed.
    Future prompts use only value-redacted field hints from those examples.
    """
    pool_for_check = request.app.state.pool
    await assert_extraction_access(pool_for_check, extraction_id, user)
    body = await request.json()
    corrected_result = body.get("corrected_result")
    field_locations = body.get("field_locations", {})
    actor = body.get("actor") or "ui"
    reason_code = body.get("reason_code") or "manual_review"
    note = body.get("note")

    if corrected_result is None:
        raise HTTPException(400, detail="corrected_result is required")

    pool = request.app.state.pool

    # Get original extraction to compare and create an audit gold example.
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
    logger.info("Review correction received: ext=%s, changed=%s",
                extraction_id, list(correction_diff.keys()) if correction_diff else [])

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
    logger.info("Review correction saved: ext=%s, changed=%s",
                extraction_id, list(correction_diff.keys()))

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

    # Auto-create an audit gold example if any header fields were actually changed.
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



        except Exception as exc:
            logger.warning("Failed to create gold example for extraction %d: %s", extraction_id, exc)

    # Save spatial memory from manual corrections (Phase 3)
    spatial_saved = 0
    if field_locations and vendor_id:
        try:
            from . import spatial_memory as _sm
            spatial_saved = await _sm.save_from_corrections(
                pool, extraction_id, field_locations, corrected_result,
            )
            if spatial_saved:
                logger.info(
                    "Spatial memory: %d regions saved for extraction=%d vendor=%s",
                    spatial_saved, extraction_id, vendor_id,
                )

        except Exception as exc:
            logger.warning("Failed to save spatial memory for extraction %d: %s", extraction_id, exc)


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
async def get_extraction_reviews(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    return {"extraction_id": extraction_id, "reviews": await db_mod.list_review_events(pool, extraction_id)}


@app.get("/extractions/{extraction_id}/trace")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_trace(
    request: Request, extraction_id: int, limit: int | None = None,
    user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    events = plog.read_extraction_events(extraction_id, limit=limit)
    return {
        "summary": plog.build_extraction_trace_summary(extraction_id, events, extraction=extraction),
        "events": events,
    }


@app.get("/admin/logs/pipeline")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_pipeline_log(
    request: Request,
    lines: int = 200,
    _user: dict = Depends(require_admin),
):
    """Return the last N lines of the combined pipeline.log (admin only)."""
    log_path = PIPELINE_LOG_DIR / "pipeline.log"
    if not log_path.exists():
        return {"lines": [], "path": str(log_path), "exists": False}
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [ln.rstrip("\n") for ln in all_lines[-lines:]]
        return {"lines": tail, "path": str(log_path), "exists": True, "total_lines": len(all_lines)}
    except Exception as exc:
        raise HTTPException(500, detail=f"Failed to read pipeline log: {exc}")


USAGE_INPUT_USD_PER_1K = float(os.getenv("USAGE_INPUT_USD_PER_1K", "0.006"))
USAGE_OUTPUT_USD_PER_1K = float(os.getenv("USAGE_OUTPUT_USD_PER_1K", "0.018"))


def _parse_usage_date(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD") from exc


def _usage_date_range(
    date_from: str | None,
    date_to: str | None,
    *,
    default_today: bool = False,
) -> tuple[datetime | None, datetime | None, str | None, str | None]:
    start_day = _parse_usage_date(date_from, "date_from")
    end_day = _parse_usage_date(date_to, "date_to")
    if default_today and start_day is None and end_day is None:
        start_day = date.today()
        end_day = start_day
    if start_day and end_day and end_day < start_day:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from")

    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC) if start_day else None
    end_exclusive_day = end_day + timedelta(days=1) if end_day else None
    end_dt = datetime.combine(end_exclusive_day, datetime.min.time(), tzinfo=UTC) if end_exclusive_day else None
    return (
        start_dt,
        end_dt,
        start_day.isoformat() if start_day else None,
        end_day.isoformat() if end_day else None,
    )


def _usage_cost_estimate(input_tokens: int | float | None, output_tokens: int | float | None) -> float:
    input_cost = (float(input_tokens or 0) / 1000.0) * USAGE_INPUT_USD_PER_1K
    output_cost = (float(output_tokens or 0) / 1000.0) * USAGE_OUTPUT_USD_PER_1K
    return round(input_cost + output_cost, 6)


@app.get("/user/stats")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_user_stats(request: Request, user: dict = Depends(get_current_user)):
    """Usage stats scoped to the current user's vendors (works for both clients and admin)."""
    pool = request.app.state.pool
    uid = None if user["role"] == "admin" else user["id"]
    stats = await db_mod.get_usage_stats(pool, user_id=uid)
    days = await db_mod.get_llm_usage_daily_summary(pool, limit=30, user_id=uid)
    response = {"stats": stats, "days": days}
    # Include subscription limit info for non-admin users
    if uid is not None:
        try:
            response["subscription"] = await db_mod.get_user_billable_pages(pool, uid)
        except Exception:
            pass
    return response


@app.get("/admin/stats")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_admin_stats(request: Request, user: dict = Depends(require_admin)):
    """Aggregate dashboard stats (all users) for the admin dashboard."""
    pool = request.app.state.pool
    stats = await db_mod.get_usage_stats(pool)
    days = await db_mod.get_llm_usage_daily_summary(pool, limit=30)
    return {"stats": stats, "days": days}


@app.get("/admin/usage/clients")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_admin_usage_clients(request: Request, _: dict = Depends(require_admin)):
    """Per-client token/page breakdown for the admin client-usage view."""
    return await db_mod.get_usage_by_client(request.app.state.pool)


@app.get("/admin/usage/clients/{client_user_id}/documents")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_client_documents_usage(
    request: Request,
    client_user_id: str,
    limit: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    range: str | None = None,
    user: dict = Depends(require_admin),
):
    """Per-document token breakdown for a specific client (admin only)."""
    start_dt, end_dt, _, _ = _usage_date_range(
        date_from,
        date_to,
        default_today=(range != "all"),
    )
    return await db_mod.get_client_document_usage(
        request.app.state.pool,
        client_user_id,
        limit=limit,
        date_from=start_dt,
        date_to=end_dt,
    )


@app.get("/admin/usage/clients/{client_user_id}/dashboard")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_client_usage_dashboard(
    request: Request,
    client_user_id: str,
    limit: int = 100,
    date_from: str | None = None,
    date_to: str | None = None,
    range: str | None = None,
    user: dict = Depends(require_admin),
):
    """Full client-scoped usage dashboard for admins."""
    pool = request.app.state.pool
    start_dt, end_dt, start_label, end_label = _usage_date_range(
        date_from,
        date_to,
        default_today=(range != "all"),
    )
    client = await db_mod.get_user_by_id(pool, client_user_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    stats = await db_mod.get_usage_stats(
        pool,
        user_id=client_user_id,
        date_from=start_dt,
        date_to=end_dt,
    )
    days = await db_mod.get_client_daily_summary(
        pool,
        client_user_id,
        limit=30,
        date_from=start_dt,
        date_to=end_dt,
    )
    documents = await db_mod.get_client_document_usage(
        pool,
        client_user_id,
        limit=limit,
        date_from=start_dt,
        date_to=end_dt,
    )

    input_tokens = int(stats.get("total_input_tokens") or 0)
    output_tokens = int(stats.get("total_output_tokens") or 0)

    # All-time subscription usage (independent of date range)
    subscription = {}
    try:
        subscription = await db_mod.get_user_billable_pages(pool, client_user_id)
    except Exception:
        pass

    return {
        "client": {
            "user_id": client["id"],
            "email": client["email"],
            "role": client.get("role"),
            "is_active": client.get("is_active"),
        },
        "date_from": start_label,
        "date_to": end_label,
        "range": range or ("custom" if date_from or date_to else "today"),
        "subscription": subscription,
        "stats": {
            "todays_pdfs": int(stats.get("total_pdfs") or 0),
            "total_extractions": int(stats.get("total_extractions") or 0),
            "total_pages": int(stats.get("all_pages") or stats.get("total_pages") or 0),
            "billable_pages": int(stats.get("billable_pages") or 0),
            "unbilled_pages": int(stats.get("unbilled_pages") or 0),
            "failed_pages": int(stats.get("failed_pages") or 0),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "grand_total": int(stats.get("grand_total") or 0),
            "llm_calls": int(stats.get("total_llm_calls") or 0),
            "cost_estimate": _usage_cost_estimate(input_tokens, output_tokens),
            "currency": "USD",
        },
        "days": days,
        "documents": documents,
    }


@app.get("/admin/usage/extractions/{extraction_id}/pages")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_page_usage(
    request: Request,
    extraction_id: int,
    user: dict = Depends(require_admin),
):
    """Per-page token breakdown for a single extraction (admin drill-down)."""
    return await db_mod.get_extraction_page_usage(request.app.state.pool, extraction_id)


@app.get("/user/documents")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_user_documents(
    request: Request,
    limit: int = 200,
    date_from: str | None = None,
    date_to: str | None = None,
    range: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Per-document usage for the currently authenticated user."""
    pool = request.app.state.pool
    start_dt, end_dt, _, _ = _usage_date_range(
        date_from, date_to, default_today=(range != "all"),
    )
    return await db_mod.get_client_document_usage(
        pool,
        user["id"],
        limit=limit,
        date_from=start_dt,
        date_to=end_dt,
    )


@app.get("/user/extractions/{extraction_id}/pages")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_user_extraction_page_usage(
    request: Request,
    extraction_id: int,
    user: dict = Depends(get_current_user),
):
    """Per-page token breakdown for a user's own extraction."""
    pool = request.app.state.pool
    # Ownership check: extraction's vendor must belong to this user
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT e.id FROM extractions e
            JOIN vendors v ON v.id = e.vendor_id
            LEFT JOIN documents d ON d.id = e.document_id
            WHERE e.id = $1
              AND COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $2::UUID
            """,
            extraction_id,
            user["id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Extraction not found")
    return await db_mod.get_extraction_page_usage(pool, extraction_id)


@app.get("/admin/usage")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_admin_usage(
    request: Request,
    limit: int = 50,
    vendor_id: str | None = None,
    doc_id: str | None = None,
    include_calls: bool = False,
    user: dict = Depends(require_admin),
):
    """Return persisted LLM token usage summaries.

    The counters come directly from llama.cpp's OpenAI-compatible
    response["usage"] payload, so local Qwen3-VL image token counts should be
    reported separately from cloud-provider token formulas.
    """
    pool = request.app.state.pool
    documents = await db_mod.get_llm_usage_document_summary(pool, limit=limit, vendor_id=vendor_id)
    days = await db_mod.get_llm_usage_daily_summary(pool, limit=30, vendor_id=vendor_id)
    payload = {
        "documents": documents,
        "days": days,
        "notes": {
            "source": "llama.cpp response.usage from /v1/chat/completions",
            "comparison_rule": "Compare local vs cloud by cost per document, not raw image prompt tokens.",
        },
    }
    if include_calls:
        payload["calls"] = await db_mod.list_llm_usage_calls(
            pool,
            limit=limit,
            doc_id=doc_id,
            vendor_id=vendor_id,
        )
    return payload


@app.get("/extractions/{extraction_id}/contract")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_contract(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    return build_purchase_order_contract(extraction)


# -- Config API -------------------------------------------------------------

@app.get("/api/config")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_my_config(request: Request, user: dict = Depends(get_current_user)):
    """Return the calling user's runtime configuration."""
    config = await db_mod.get_user_config(request.app.state.pool, user["id"])
    watcher_mgr = getattr(request.app.state, "watcher_mgr", None)

    client_online = False
    heartbeat_str = config.get("last_client_heartbeat")
    if heartbeat_str:
        try:
            last_beat = datetime.fromisoformat(heartbeat_str)
            client_online = (datetime.now(UTC) - last_beat).total_seconds() < 60
        except Exception:
            pass

    return {
        "user_id": user["id"],
        "config": config,
        "watcher_active": watcher_mgr.is_active(user["id"]) if watcher_mgr else False,
        "client_online": client_online,
    }


@app.post("/api/client/heartbeat")
@limiter.limit("10/minute")
async def client_heartbeat(request: Request, user: dict = Depends(get_current_user)):
    """Client exe calls this every 30 s so the UI can show ACTIVE / INACTIVE."""
    await db_mod.set_user_config(
        request.app.state.pool,
        user["id"],
        {"last_client_heartbeat": datetime.now(UTC).isoformat()},
    )
    return {"status": "ok"}


@app.put("/api/config")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def update_my_config(request: Request, user: dict = Depends(get_current_user)):
    """Update the calling user's runtime configuration and restart watcher if needed."""
    body = await request.json()
    unknown = set(body) - _CONFIG_KEYS
    if unknown:
        raise HTTPException(400, detail=f"Unknown config keys: {sorted(unknown)}")
    if "upload_mode" in body and body["upload_mode"] not in _VALID_UPLOAD_MODES:
        raise HTTPException(400, detail=f"upload_mode must be one of {sorted(_VALID_UPLOAD_MODES)}")

    pool = request.app.state.pool
    updates = {k: str(v) for k, v in body.items() if k in _CONFIG_KEYS}
    await db_mod.set_user_config(pool, user["id"], updates)
    await _reconfigure_user_watcher(request.app, user["id"])

    config = await db_mod.get_user_config(pool, user["id"])
    watcher_mgr = getattr(request.app.state, "watcher_mgr", None)
    payload = {
        "user_id": user["id"],
        "config": config,
        "watcher_active": watcher_mgr.is_active(user["id"]) if watcher_mgr else False,
    }
    _broadcast_config_event(user["id"], {"type": "config_updated", **payload})
    return payload


@app.get("/api/config/stream")
async def config_sse_stream(request: Request, user: dict = Depends(get_current_user_sse)):
    """SSE stream — pushes config_updated and folder_ingest_* events to this user."""
    uid = user["id"]
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    _config_sse_queues.setdefault(uid, []).append(queue)

    async def _gen():
        try:
            config = await db_mod.get_user_config(request.app.state.pool, uid)
            watcher_mgr = getattr(request.app.state, "watcher_mgr", None)
            sse_client_online = False
            hb_str = config.get("last_client_heartbeat")
            if hb_str:
                try:
                    last_beat = datetime.fromisoformat(hb_str)
                    sse_client_online = (datetime.now(UTC) - last_beat).total_seconds() < 60
                except Exception:
                    pass
            connected_event = json.dumps({
                "type": "connected",
                "config": config,
                "watcher_active": watcher_mgr.is_active(uid) if watcher_mgr else False,
                "client_online": sse_client_online,
            })
            yield f"data: {connected_event}\n\n"
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _config_sse_queues[uid].remove(queue)
            except (KeyError, ValueError):
                pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/admin/config/users")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_get_all_configs(request: Request, _: dict = Depends(require_admin)):
    """Admin: list every user's config."""
    rows = await db_mod.get_all_user_configs(request.app.state.pool)
    watcher_mgr = getattr(request.app.state, "watcher_mgr", None)
    now = datetime.now(UTC)
    for row in rows:
        row["watcher_active"] = watcher_mgr.is_active(row["user_id"]) if watcher_mgr else False
        client_online = False
        heartbeat_str = row.get("config", {}).get("last_client_heartbeat")
        if heartbeat_str:
            try:
                last_beat = datetime.fromisoformat(heartbeat_str)
                client_online = (now - last_beat).total_seconds() < 60
            except Exception:
                pass
        row["client_online"] = client_online
    return rows


@app.get("/admin/config/users/{target_user_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_get_user_config(
    request: Request, target_user_id: str, _: dict = Depends(require_admin)
):
    """Admin: get one user's config."""
    config = await db_mod.get_user_config(request.app.state.pool, target_user_id)
    watcher_mgr = getattr(request.app.state, "watcher_mgr", None)
    client_online = False
    heartbeat_str = config.get("last_client_heartbeat")
    if heartbeat_str:
        try:
            last_beat = datetime.fromisoformat(heartbeat_str)
            client_online = (datetime.now(UTC) - last_beat).total_seconds() < 60
        except Exception:
            pass
    return {
        "user_id": target_user_id,
        "config": config,
        "watcher_active": watcher_mgr.is_active(target_user_id) if watcher_mgr else False,
        "client_online": client_online,
    }


@app.put("/admin/config/users/{target_user_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_update_user_config(
    request: Request, target_user_id: str, admin: dict = Depends(require_admin)
):
    """Admin: update any user's config and restart their watcher."""
    pool = request.app.state.pool
    target = await db_mod.get_user_by_id(pool, target_user_id)
    if not target:
        raise HTTPException(404, detail="User not found")
    body = await request.json()
    unknown = set(body) - _CONFIG_KEYS
    if unknown:
        raise HTTPException(400, detail=f"Unknown config keys: {sorted(unknown)}")
    if "upload_mode" in body and body["upload_mode"] not in _VALID_UPLOAD_MODES:
        raise HTTPException(400, detail=f"upload_mode must be one of {sorted(_VALID_UPLOAD_MODES)}")

    updates = {k: str(v) for k, v in body.items() if k in _CONFIG_KEYS}
    await db_mod.set_user_config(pool, target_user_id, updates)
    await _reconfigure_user_watcher(request.app, target_user_id)

    config = await db_mod.get_user_config(pool, target_user_id)
    watcher_mgr = getattr(request.app.state, "watcher_mgr", None)
    payload = {
        "user_id": target_user_id,
        "config": config,
        "watcher_active": watcher_mgr.is_active(target_user_id) if watcher_mgr else False,
    }
    _broadcast_config_event(target_user_id, {"type": "config_updated", **payload})
    return payload


# -- Vendor dashboard stats -------------------------------------------------

@app.get("/admin/dashboard/vendors")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_vendor_summary(request: Request, _: dict = Depends(require_admin)):
    """Admin: per-client summary — vendor_count + extraction_count."""
    return await db_mod.get_client_vendor_summary(request.app.state.pool)


@app.get("/admin/dashboard/clients/{target_user_id}/vendors")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_client_vendors(
    request: Request, target_user_id: str, _: dict = Depends(require_admin)
):
    """Admin: vendor list with stats for one client."""
    return await db_mod.get_client_vendors_with_stats(request.app.state.pool, target_user_id)


@app.get("/admin/dashboard/vendors/{vendor_id}/stats")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_vendor_stats(
    request: Request,
    vendor_id: str,
    days: int = 30,
    _: dict = Depends(require_admin),
):
    """Admin: per-day + per-page stats for one vendor."""
    pool = request.app.state.pool
    daily = await db_mod.get_vendor_extraction_daily(pool, vendor_id, limit=days)
    pages = await db_mod.get_vendor_page_stats(pool, vendor_id)
    return {"vendor_id": vendor_id, "daily": daily, "pages": pages}


@app.get("/user/dashboard/vendors")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def user_vendor_summary(request: Request, user: dict = Depends(get_current_user)):
    """Client: own vendor list with extraction counts."""
    return await db_mod.get_client_vendors_with_stats(request.app.state.pool, user["id"])


@app.get("/user/dashboard/vendors/{vendor_id}/stats")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def user_vendor_stats(
    request: Request,
    vendor_id: str,
    days: int = 30,
    user: dict = Depends(get_current_user),
):
    """Client: per-day + per-page breakdown for one of their own vendors."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    daily = await db_mod.get_vendor_extraction_daily(pool, vendor_id, limit=days)
    pages = await db_mod.get_vendor_page_stats(pool, vendor_id)
    return {"vendor_id": vendor_id, "daily": daily, "pages": pages}


# -- Schedule CRUD ----------------------------------------------------------

@app.get("/api/schedules")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_user_schedules(request: Request, user: dict = Depends(get_current_user)):
    return await db_mod.get_user_schedules(request.app.state.pool, user["id"])


@app.post("/api/schedules", status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def create_schedule(
    request: Request, body: ScheduleCreate, user: dict = Depends(get_current_user)
):
    row = await db_mod.create_user_schedule(
        request.app.state.pool, user["id"], body.cron_expr, body.timezone, body.label
    )
    if row:
        sync_job(row)
    return row


@app.get("/api/schedules/{schedule_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_schedule_route(
    request: Request, schedule_id: int, user: dict = Depends(get_current_user)
):
    row = await db_mod.get_schedule(request.app.state.pool, schedule_id)
    if not row or str(row.get("user_id")) != user["id"]:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return row


@app.put("/api/schedules/{schedule_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def update_schedule_route(
    request: Request,
    schedule_id: int,
    body: ScheduleUpdate,
    user: dict = Depends(get_current_user),
):
    existing = await db_mod.get_schedule(request.app.state.pool, schedule_id)
    if not existing or str(existing.get("user_id")) != user["id"]:
        raise HTTPException(status_code=404, detail="Schedule not found")
    row = await db_mod.update_user_schedule(
        request.app.state.pool, schedule_id,
        **{k: v for k, v in body.model_dump().items() if v is not None},
    )
    if row:
        sync_job(row)
    return row


@app.delete("/api/schedules/{schedule_id}", status_code=204)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_schedule_route(
    request: Request, schedule_id: int, user: dict = Depends(get_current_user)
):
    existing = await db_mod.get_schedule(request.app.state.pool, schedule_id)
    if not existing or str(existing.get("user_id")) != user["id"]:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db_mod.delete_user_schedule(request.app.state.pool, schedule_id)
    remove_job(schedule_id)


@app.get("/api/schedules/{schedule_id}/next")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def schedule_next_runs(
    request: Request,
    schedule_id: int,
    count: int = 5,
    user: dict = Depends(get_current_user),
):
    existing = await db_mod.get_schedule(request.app.state.pool, schedule_id)
    if not existing or str(existing.get("user_id")) != user["id"]:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"schedule_id": schedule_id, "next_runs": get_next_run_times(schedule_id, count)}


@app.get("/admin/schedules")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_list_schedules(request: Request, _: dict = Depends(require_admin)):
    return await db_mod.get_all_schedules(request.app.state.pool)


# -- Per-user scheduler (up to 3 daily times per user) ----------------------

_SCHED_MAX = 3


def _row_to_sched(row: dict) -> dict:
    """Convert a user_schedules DB row to API shape."""
    parts = (row.get("cron_expr") or "").split()
    utc_hour   = int(parts[1]) if len(parts) >= 2 else None
    utc_minute = int(parts[0]) if len(parts) >= 1 else None
    next_runs  = get_next_run_times(row["id"], 1)
    last_ran   = row.get("last_ran_at")
    last_ran_str = last_ran.isoformat() if last_ran else None
    return {
        "id":           row["id"],
        "enabled":      row.get("enabled", False),
        "is_executing": row.get("is_executing", False),
        "utc_hour":     utc_hour,
        "utc_minute":   utc_minute,
        "next_run":     next_runs[0] if next_runs else None,
        "last_ran_at":  last_ran_str,
    }


@app.get("/api/scheduler")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_user_scheduler(request: Request, user: dict = Depends(get_current_user)):
    rows = await db_mod.get_user_schedules(request.app.state.pool, user["id"])
    return {"schedules": [_row_to_sched(r) for r in rows], "max_schedules": _SCHED_MAX}


@app.post("/api/scheduler/start")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def start_user_scheduler(request: Request, user: dict = Depends(get_current_user)):
    """Create a new schedule or re-enable an existing one. Max 3 per user."""
    body = await request.json()
    utc_hour   = int(body.get("hour",   8))
    utc_minute = int(body.get("minute", 0))
    schedule_id = body.get("schedule_id")  # if provided, update that specific row
    if not (0 <= utc_hour <= 23 and 0 <= utc_minute <= 59):
        raise HTTPException(400, detail="hour must be 0-23 and minute must be 0-59")
    cron_expr = f"{utc_minute} {utc_hour} * * *"
    pool = request.app.state.pool

    if schedule_id:
        # Update existing — verify it belongs to this user
        existing = await db_mod.get_schedule(pool, int(schedule_id))
        if not existing or str(existing.get("user_id")) != str(user["id"]):
            raise HTTPException(404, detail="Schedule not found")
        row = await db_mod.update_user_schedule(pool, int(schedule_id), cron_expr=cron_expr, enabled=True)
    else:
        # Create new — enforce max limit
        current = await db_mod.get_user_schedules(pool, user["id"])
        if len(current) >= _SCHED_MAX:
            raise HTTPException(400, detail=f"Maximum {_SCHED_MAX} schedules allowed per user")
        row = await db_mod.create_user_schedule(pool, user["id"], cron_expr, "UTC", "daily run")

    if row:
        sync_job(row)
    return _row_to_sched(row)


@app.post("/api/scheduler/stop")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def stop_user_scheduler(request: Request, user: dict = Depends(get_current_user)):
    """Disable a specific schedule without deleting it."""
    body = await request.json()
    schedule_id = body.get("schedule_id")
    if not schedule_id:
        raise HTTPException(400, detail="schedule_id required")
    pool = request.app.state.pool
    existing = await db_mod.get_schedule(pool, int(schedule_id))
    if not existing or str(existing.get("user_id")) != str(user["id"]):
        raise HTTPException(404, detail="Schedule not found")
    row = await db_mod.update_user_schedule(pool, int(schedule_id), enabled=False)
    if row:
        sync_job(row)
    return _row_to_sched(row)


@app.delete("/api/scheduler/{schedule_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_user_schedule(
    request: Request, schedule_id: int, user: dict = Depends(get_current_user)
):
    """Delete a schedule entirely."""
    pool = request.app.state.pool
    existing = await db_mod.get_schedule(pool, schedule_id)
    if not existing or str(existing.get("user_id")) != str(user["id"]):
        raise HTTPException(404, detail="Schedule not found")
    remove_job(schedule_id)
    await db_mod.delete_user_schedule(pool, schedule_id)
    return {"deleted": True, "schedule_id": schedule_id}


@app.post("/api/scheduler/{schedule_id}/ran")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def mark_schedule_ran_endpoint(
    request: Request, schedule_id: int, user: dict = Depends(get_current_user)
):
    """Mark a schedule as run."""
    pool = request.app.state.pool
    existing = await db_mod.get_schedule(pool, schedule_id)
    if not existing or str(existing.get("user_id")) != str(user["id"]):
        raise HTTPException(404, detail="Schedule not found")
    await db_mod.mark_schedule_ran(pool, schedule_id)
    return {"status": "ok"}


# -- Programmatic Extraction API (API Key clients) ---------------------------

@app.post("/v1/extract")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def extract_via_api_key(
    request: Request,
    file: UploadFile = File(...),
    vendor_id: str | None = Form(None),
    async_mode: bool = Query(False, alias="async"),
    user: dict = Depends(get_current_user_or_api_key),
):
    """Synchronous/Asynchronous PDF extraction endpoint for programmatic (API-key) clients.

    Accepts a PDF via multipart form upload. Detects vendor from the API key
    owner's vendor aliases, loads their template, runs the full pipeline
    (render -> OCR -> Qwen3-VL -> normalize), records usage, and returns
    structured JSON. Supports idempotency via Idempotency-Key header.

    Authentication: X-API-Key header or JWT Bearer token.
    """
    pool = request.app.state.pool
    store = request.app.state.store

    file_bytes = await file.read()
    filename = file.filename or "unknown.pdf"

    # Compute file hash
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    idempotency_key = request.headers.get("Idempotency-Key")

    claim = None
    extraction_id = None

    if idempotency_key:
        claim = await db_mod.claim_idempotency(pool, user["id"], idempotency_key, file_sha256)
        if claim["status"] == "conflict":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONFLICT",
                    "message": "Idempotency key conflict: same key used with different payload/file.",
                }
            )
        elif claim["status"] == "duplicate":
            extraction_id = claim["extraction_id"]
            ext_status = claim["extraction_status"]

            if extraction_id is None or ext_status is None:
                # First request is still in-flight (claim exists but extraction not yet bound).
                # Return 202 so the caller retries rather than creating a duplicate extraction.
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "initializing",
                        "message": "Original request is still creating the extraction. Retry shortly.",
                        "retry_after_seconds": 2,
                    },
                    headers={"Retry-After": "2"},
                )

            if ext_status in ("failed", "cancelled"):
                # Terminal failure — evict and re-run as fresh
                await db_mod.delete_idempotency_claim(pool, user["id"], idempotency_key)
                claim = await db_mod.claim_idempotency(pool, user["id"], idempotency_key, file_sha256)
                extraction_id = None
            else:
                # Active or complete duplicate
                if ext_status in ("done", "complete", "partial"):
                    # Return cached result
                    extraction = await db_mod.get_extraction(pool, extraction_id)
                    if not extraction:
                        raise HTTPException(500, detail="Extraction record missing")
                    mapped_result = await db_mod.get_extraction_mapped_result(pool, extraction_id)
                    mapping_applied = mapped_result is not None
                    return {
                        "extraction_id": extraction_id,
                        "vendor_id": extraction.get("vendor_id"),
                        "pages": extraction.get("total_pages", 0),
                        "duration_ms": extraction.get("duration_ms"),
                        "mapping_applied": mapping_applied,
                        "completed_at": extraction.get("updated_at").isoformat() if extraction.get("updated_at") else datetime.now(UTC).isoformat(),
                        "result": mapped_result if mapping_applied else extraction.get("result"),
                        "cached": True,
                    }
                else:
                    # Active: queued or processing or cancelling
                    if async_mode:
                        return JSONResponse(
                            status_code=202,
                            content={
                                "status": ext_status,
                                "extraction_id": extraction_id,
                                "status_url": f"/extractions/{extraction_id}",
                            }
                        )
                    # For sync_mode, we skip submitting job and proceed directly to polling

    if extraction_id is None:
        # -- Page count + hard cap -------------------------------------------------
        if filename.lower().endswith(".pdf"):
            try:
                incoming_pages = processor.count_pdf_pages(file_bytes)
            except ValueError:
                raise HTTPException(400, detail="Could not read the uploaded PDF.")
            if incoming_pages > MAX_DOCUMENT_PAGES:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "DOCUMENT_TOO_LARGE",
                        "message": f"PDF has {incoming_pages} pages. Maximum allowed is {MAX_DOCUMENT_PAGES} pages.",
                        "pages": incoming_pages,
                        "max_pages": MAX_DOCUMENT_PAGES,
                    },
                )
        else:
            incoming_pages = 1

        # -- Subscription quota check (atomic reservation) -------------------------
        if user.get("role") != "admin":
            try:
                quota = await db_mod.reserve_quota(pool, user["id"], incoming_pages)
            except Exception as usage_exc:
                logger.error("Subscription quota check failed: %s", usage_exc)
                raise HTTPException(503, detail="Service temporarily unavailable. Please retry.")
            if not quota["allowed"]:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "QUOTA_EXCEEDED",
                        "message": (
                            f"Uploading this document ({incoming_pages} pages) would exceed your "
                            f"subscription limit of {quota['limit']} pages. "
                            f"You have {quota['remaining']} pages remaining."
                        ),
                        "subscription_limit": quota["limit"],
                        "total_extracted_pages": quota["used"],
                        "incoming_pages": incoming_pages,
                        "remaining": quota["remaining"],
                    },
                )

        _job_submitted = False
        try:
            logger.info("[v1/extract] File received: %s (%d bytes, auth=%s, user=%s)",
                        filename, len(file_bytes), user.get("auth_method"), user.get("email"))

            # -- Vendor detection (scoped to API key owner) ----------------------------
            if not vendor_id:
                from . import geometry as _geo
                from . import vendor_detector as _vd
                from . import ocr_runner as _ocr

                # Render page 1 only for detection
                rendered = await render_page_1_for_detection(file_bytes, filename)

                if not rendered:
                    raise HTTPException(400, detail="Could not render any pages from the uploaded file")

                page1 = rendered[0]
                page1_meta = {"page_number": 1, "width": page1.get("width", 0), "height": page1.get("height", 0)}

                # Digital-first text extraction
                page_words = []
                if filename.lower().endswith(".pdf"):
                    geo_pages = _geo.compute_pdf_geometry(file_bytes, [page1_meta])
                    page_words = geo_pages[0].get("words", []) if geo_pages else []

                # PaddleOCR fallback
                if not page_words:
                    ocr_pages = await _ocr.run_ocr_on_pages([{
                        "page_number": 1,
                        "image_b64": page1["image_b64"],
                        "mime_type": page1.get("mime_type", "image/jpeg"),
                    }])
                    page_words = ocr_pages[0].get("words", []) if ocr_pages else []

                # Always scope to this user's vendors
                detect_uid = user["id"]
                match = await _vd.detect_vendor(pool, page_words, user_id=detect_uid)
                if match is None:
                    raise HTTPException(
                        status_code=400,
                        detail="No vendor template found for this document. Please create a vendor and template first.",
                    )
                vendor_id = match.vendor_id

            # Enforce ownership
            await assert_vendor_access(pool, vendor_id, user)

            # -- Ingestion metadata including hash/key
            doc_metadata = {
                "billing_user_id": user["id"],
                "auth_method": user.get("auth_method"),
                "api_key_id": user.get("api_key_id"),
            }
            if idempotency_key:
                doc_metadata["idempotency_key"] = idempotency_key
                doc_metadata["file_sha256"] = file_sha256

            # -- Submit to the existing ingestion pipeline (async job) -----------------
            submitted = await _submit_ingestion_job(
                pool,
                store,
                file_bytes=file_bytes,
                filename=filename,
                vendor_id=vendor_id,
                format_type=None,       # always defer to template
                header_fields=[],
                line_item_fields=[],
                source_type="rest",
                source_ref=f"api_key:{user.get('api_key_id', 'jwt')}",
                metadata=doc_metadata,
                reserved_pages=incoming_pages if user.get("role") != "admin" else None,
            )
            _job_submitted = True

            extraction_id = submitted["extraction"]["id"]
            document_id = submitted["extraction"].get("document_id")

            if claim and claim.get("claim_id"):
                await db_mod.bind_idempotency_claim(pool, claim["claim_id"], extraction_id, document_id)

            if async_mode:
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "queued",
                        "extraction_id": extraction_id,
                        "status_url": f"/extractions/{extraction_id}",
                    }
                )
        except Exception:
            if user.get("role") != "admin" and not _job_submitted:
                try:
                    await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
                except Exception:
                    pass
            raise

    # -- Poll for completion (synchronous wait, scales with page count) ---------
    import asyncio as _aio
    # Fetch total pages for wait calculation if fresh run
    extraction_record = await db_mod.get_extraction(pool, extraction_id)
    total_pages = extraction_record.get("total_pages") if extraction_record else 1
    # Fallback to incoming_pages if total_pages is not populated yet
    if not total_pages or total_pages == 0:
        if filename.lower().endswith(".pdf"):
            try:
                total_pages = processor.count_pdf_pages(file_bytes)
            except Exception:
                total_pages = 1
        else:
            total_pages = 1

    max_wait = 480 if total_pages >= 20 else 300
    poll_interval = 1.0  # seconds
    elapsed = 0.0

    while elapsed < max_wait:
        extraction = await db_mod.get_extraction(pool, extraction_id)
        if not extraction:
            break
        ext_status = extraction.get("status", "")
        # partial is terminal for this poll check
        if ext_status in ("done", "complete", "failed", "cancelled", "partial"):
            break
        await _aio.sleep(poll_interval)
        elapsed += poll_interval

    # Fetch final extraction result
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(500, detail="Extraction record missing after job completion")

    ext_status = extraction.get("status", "unknown")
    if ext_status == "failed":
        raise HTTPException(
            status_code=500,
            detail={
                "code": "EXTRACTION_FAILED",
                "message": f"Extraction failed: {extraction.get('error', 'unknown error')}",
                "extraction_id": extraction_id,
            },
        )
    # partial is a valid terminal state now
    if ext_status not in ("done", "complete", "partial"):
        raise HTTPException(
            status_code=504,
            detail={
                "code": "EXTRACTION_TIMEOUT",
                "message": f"Extraction did not complete within {max_wait} seconds. Try again later.",
                "extraction_id": extraction_id,
                "status": ext_status,
            },
        )

    # If an ERP field mapping is configured for this vendor, postprocess has
    # stored a canonical-field copy — send that to the client instead of raw.
    mapped_result = await db_mod.get_extraction_mapped_result(pool, extraction_id)
    mapping_applied = mapped_result is not None
    return {
        "extraction_id": extraction_id,
        "vendor_id": extraction.get("vendor_id"),
        "pages": extraction.get("total_pages", 0),
        "duration_ms": extraction.get("duration_ms"),
        "mapping_applied": mapping_applied,
        "completed_at": extraction.get("updated_at").isoformat() if extraction.get("updated_at") else datetime.now(UTC).isoformat(),
        "result": mapped_result if mapping_applied else extraction.get("result"),
    }


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
