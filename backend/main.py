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
from typing import Any, AsyncGenerator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
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
from .contracts import attach_vendor, build_purchase_order_contract
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
    CorrectionSaveRequest,
    ExtractionJobStartOut,
    ExtractionOut,
    HealthOut,
    JobOut,
    JobStatusOut,
    LoginRequest,
    SubscriptionCreate,
    SubscriptionLimitUpdate,
    SubscriptionOut,
    TemplateSaveResponse,
    TemplateCreate,
    TemplateListOut,
    TemplateOut,
    TokenOut,
    TopupCreate,
    TopupOut,
    TopupRequestCreate,
    TopupRequestOut,
    TopupRequestResolve,
    UserCreate,
    UserOut,
    UserResetPassword,
    VendorAliasCreate,
    VendorAliasOut,
    VendorCreate,
    VendorMappingSave,
    VendorOut,
    VendorOwnerAssign,
    validate_field_name_list,
)
from .object_store import ARTIFACTS_BUCKET, DOCUMENTS_BUCKET, get_store
from .mlflow_tracing import (
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
from .config import CORS_ALLOW_ORIGINS
from .config import DEFAULT_SUBSCRIPTION_LIMIT, SUBSCRIPTION_WARNING_THRESHOLD
from .config import PIPELINE_LOG_DIR

from pydantic import BaseModel

# DB connection error classes for clean 503 mapping.
# Imported defensively so the app can start even if asyncpg isn't installed.
try:
    import asyncpg as _asyncpg
    _DB_CONNECTION_ERRORS: tuple = (
        _asyncpg.PostgresConnectionError,
        _asyncpg.TooManyConnectionsError,
        _asyncpg.exceptions.ConnectionDoesNotExistError,
    )
except (ImportError, AttributeError):
    _DB_CONNECTION_ERRORS = ()


# ── Centralized logging (replaces inline basicConfig) ───────────────
configure_logging()
logger = logging.getLogger(__name__)


async def render_page_1_for_detection(file_bytes: bytes, filename: str) -> list[dict]:
    """Renders the first page of an uploaded PDF for vendor detection.

    All upstream callers run `_require_pdf` first, so non-PDFs cannot reach
    this function in normal operation. The assertion here is a safety net for
    new code paths that forget the guard."""
    if not filename.lower().endswith(".pdf") or not file_bytes.startswith(b"%PDF"):
        raise ValueError(
            f"render_page_1_for_detection received non-PDF: '{filename}'. "
            "Caller must invoke _require_pdf before reaching this function."
        )
    return await processor.pdf_to_images(file_bytes, max_pages=1)


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

    # Suppress uvicorn access log noise for the health check route.
    import logging as _logging

    class _QuietPaths(_logging.Filter):
        _SKIP = ("/health",)
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
    await app.state.pool.close()
    from .logging_config import shutdown_logging
    shutdown_logging()


def _guess_mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _require_pdf(filename: str, file_bytes: bytes) -> None:
    """Hard-block any upload that isn't a real PDF.

    Checks both the extension and the magic bytes (%PDF) so a renamed
    .docx/.csv/.xlsx can't sneak through. Raises 415 with a clear message
    listing what's allowed."""
    name = (filename or "").strip().lower()
    if not name.endswith(".pdf") or not file_bytes.startswith(b"%PDF"):
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_FILE_TYPE",
                "message": (
                    f"Only PDF files are accepted. '{filename or 'unknown'}' "
                    "was rejected. Word (.docx), Excel (.xlsx), CSV and other "
                    "formats are not supported — please upload a .pdf."
                ),
                "allowed_extensions": [".pdf"],
            },
        )


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
    vendor_id: str | None,
    format_type: str | None,
    header_fields: list[str],
    line_item_fields: list[str],
    source_type: str,
    source_ref: str | None = None,
    metadata: dict | None = None,
    trace_context: dict | None = None,
    reserved_pages: int | None = None,
) -> dict:
    # When vendor_id is None the document is submitted for auto-detection: the
    # normalize worker detects the vendor and fills in template/format/fields
    # (see worker._process_normalize → db.update_extraction_vendor). Template
    # validation only applies when the caller pre-selected a vendor.
    template_id: int | None = None
    resolved_format_type = format_type
    if vendor_id:
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
        template_id = tmpl["id"]
        resolved_format_type = format_type or tmpl["format_type"]

    object_key = f"documents/{vendor_id or '_pending'}/{uuid4().hex}_{filename}"
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

    # If caller didn't provide a trace context (e.g. /ingest), create a root
    # span here so all 4 pipeline stages share one MLflow trace.
    if not trace_context:
        with trace_extraction_root(
            extraction["id"], vendor_id or "", filename, 0,
            resolved_format_type or "", header_fields, line_item_fields,
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
        if not auth.lower().startswith("bearer "):
            return "-"
        raw = auth[7:]
        if not raw:
            return "-"
        from .auth import decode_token
        claims = decode_token(raw)
        return claims.get("email") or claims.get("sub") or "-"
    except Exception:
        return "-"


_QUIET_PATHS = frozenset({"/health"})


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


# -- Security headers -------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Only add CSP and nosniff to HTML responses.
        # Applying nosniff to static files on Windows can cause the browser to
        # reject CSS/JS served with wrong MIME types from Python's mimetypes module.
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data: blob:; "
                "connect-src 'self'"
            )
        return response


# -- App --------------------------------------------------------------------

app = FastAPI(
    title="Augmented OCR API",
    version="2.0.0",
    description="Production-grade document extraction with dynamic user-defined fields",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MaxUploadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
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
        error = dict(detail)
        error.setdefault("code", _HTTP_CODE_NAMES.get(status, "ERROR"))
        error.setdefault(
            "message",
            str(error.get("hint") or error.get("reason") or _HTTP_CODE_NAMES.get(status, "ERROR")),
        )
    else:
        error = {
            "code": _HTTP_CODE_NAMES.get(status, "ERROR"),
            "message": str(detail),
        }
    return {"error": error}


_LLM_UNAVAILABLE_MARKERS = (
    "all connection attempts failed",
    "connection refused",
    "connect error",
    "failed to establish a new connection",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "connection reset",
    "timed out",
    "timeout",
)


def _collect_extraction_errors(result: Any, page_results: Any) -> list[str]:
    errors: list[str] = []
    if isinstance(result, dict):
        raw_errors = result.get("errors")
        if isinstance(raw_errors, list):
            errors.extend(str(err) for err in raw_errors if err)
        elif raw_errors:
            errors.append(str(raw_errors))
        for key in ("_error", "error"):
            if result.get(key):
                errors.append(str(result[key]))
    if isinstance(page_results, list):
        for page_result in page_results:
            if isinstance(page_result, dict) and page_result.get("_error"):
                errors.append(str(page_result["_error"]))
    return list(dict.fromkeys(errors))


def _all_pages_failed(result: Any, page_results: Any) -> bool:
    if isinstance(result, dict) and result.get("_all_pages_failed"):
        return True
    if isinstance(page_results, list) and page_results:
        return all(isinstance(page_result, dict) and "_error" in page_result for page_result in page_results)
    return False


def _llm_unavailable(errors: list[str]) -> bool:
    text = " ".join(errors).lower()
    return any(marker in text for marker in _LLM_UNAVAILABLE_MARKERS)


def _raise_for_terminal_extraction_error(extraction: dict, result: Any, extraction_id: int) -> None:
    """Convert persisted LLM failure sentinels into HTTP errors for sync API clients."""
    page_results = extraction.get("page_results")
    errors = _collect_extraction_errors(result, page_results)
    llm_failed = extraction.get("error") == "llm_failed"
    if not llm_failed and not _all_pages_failed(result, page_results):
        return

    unavailable = _llm_unavailable(errors)
    raise HTTPException(
        status_code=503 if unavailable else 500,
        detail={
            "code": "LLM_UNAVAILABLE" if unavailable else "EXTRACTION_FAILED",
            "message": (
                "LLM service is unavailable. Make sure llama-server is running on port 8056 and retry."
                if unavailable
                else "Extraction failed during LLM processing."
            ),
            "extraction_id": extraction_id,
            "errors": errors[:5],
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.status_code, exc.detail),
        headers=getattr(exc, "headers", None) or {},
    )


def _serialisable_validation_errors(exc: RequestValidationError) -> list:
    """Return Pydantic v2 error dicts with all values JSON-serialisable.

    Pydantic v2 field_validator errors include ctx={'error': <ExceptionInstance>}
    which is not JSON-serialisable.  Convert any Exception in ctx to its str()
    representation so JSONResponse can encode the payload without crashing.

    Note: FastAPI's RequestValidationError.errors() does not accept keyword
    arguments — call it bare and sanitise the result ourselves.
    """
    safe: list[dict] = []
    for err in exc.errors():
        err_copy = dict(err)
        if "ctx" in err_copy and isinstance(err_copy["ctx"], dict):
            err_copy["ctx"] = {
                k: str(v) if isinstance(v, Exception) else v
                for k, v in err_copy["ctx"].items()
            }
        safe.append(err_copy)
    return safe


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "fields": _serialisable_validation_errors(exc),
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
    # Map DB connection/pool failures to a clean 503 before falling through to 500.
    if _DB_CONNECTION_ERRORS and isinstance(exc, _DB_CONNECTION_ERRORS):
        logger.error(
            "DB connection unavailable %s %s: %s",
            request.method, request.url.path, type(exc).__name__,
        )
        request.state.err_reason = "DATABASE_UNAVAILABLE"
        return JSONResponse(
            status_code=503,
            content={"error": {
                "code": "DATABASE_UNAVAILABLE",
                "message": "Service temporarily unavailable. Please retry.",
            }},
        )
    from .ocr_runner import OCRUnavailable
    if isinstance(exc, OCRUnavailable):
        logger.error(
            "PaddleOCR unavailable %s %s: %s",
            request.method, request.url.path, exc,
        )
        request.state.err_reason = "OCR_UNAVAILABLE"
        return JSONResponse(
            status_code=503,
            content={"error": {
                "code": "OCR_UNAVAILABLE",
                "message": "PaddleOCR is temporarily unavailable. Please retry.",
            }},
        )
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
            subscription_limit=user.get("subscription_limit", 0),
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
        subscription_limit=record.get("subscription_limit", 0),
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
    pool = request.app.state.pool
    await db_mod.expire_due_subscriptions(pool)
    rows = await db_mod.list_users(pool)
    out: list[UserOut] = []
    for r in rows:
        pending = r.get("pending_pages") or 0
        if r.get("sub_id") is not None:
            base_limit = r.get("base_limit") or 0
            topup_total = r.get("topup_total") or 0
            effective_limit = base_limit + topup_total
            used = r.get("used") or 0
            remaining = max(effective_limit - used - pending, 0)
            
            period_fields = {
                "period_start": r.get("period_start"),
                "period_end": r.get("period_end"),
                "base_limit": base_limit,
                "topup_total": topup_total,
                "effective_limit": effective_limit,
                "pages_used": used,
                "pages_remaining": remaining,
                "period_status": "active",
            }
        else:
            period_fields = {
                "period_start": None,
                "period_end": None,
                "base_limit": 0,
                "topup_total": 0,
                "effective_limit": 0,
                "pages_used": 0,
                "pages_remaining": 0,
                "period_status": "none",
            }
        out.append(UserOut(
            id=str(r["id"]),
            email=r["email"],
            role=r["role"],
            is_active=r.get("is_active", True),
            subscription_limit=r.get("subscription_limit", 0),
            created_at=r.get("created_at"),
            **period_fields,
        ))
    return out


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
    body: SubscriptionLimitUpdate,
    user: dict = Depends(require_admin),
):
    """Update a user's subscription page limit at runtime.

    Body: {"subscription_limit": 2000}
    """
    pool = request.app.state.pool
    ok = await db_mod.update_user_subscription_limit(pool, user_id, body.subscription_limit)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Admin %s updated subscription_limit for user %s to %d", user["id"], user_id, body.subscription_limit)
    return {"status": "updated", "user_id": user_id, "subscription_limit": body.subscription_limit}


# -- Admin: Subscriptions & Top-ups -----------------------------------------

@app.get("/admin/users/{user_id}/subscription")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_get_subscription(
    request: Request,
    user_id: str,
    user: dict = Depends(require_admin),
):
    """Return the user's current active subscription with usage details, or
    a 'none' shape if none exists."""
    pool = request.app.state.pool
    target = await db_mod.get_user_by_id(pool, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    quota = await db_mod.get_user_quota_v2(pool, user_id)
    return {
        "user_id": user_id,
        "email": target["email"],
        **quota,
    }


@app.post("/admin/users/{user_id}/subscriptions", status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_create_subscription(
    request: Request,
    user_id: str,
    body: SubscriptionCreate,
    user: dict = Depends(require_admin),
):
    """Create (or replace) the user's active subscription. The new row gets
    status='active'; any prior active row is marked 'superseded' in the same
    transaction. Period length is arbitrary — admin picks 1 month, 6 months,
    1 year, or any custom range."""
    from datetime import datetime as _dt, timezone as _tz

    pool = request.app.state.pool
    target = await db_mod.get_user_by_id(pool, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    period_start = body.period_start or _dt.now(_tz.utc)
    if body.period_end <= period_start:
        raise HTTPException(
            status_code=400,
            detail="period_end must be after period_start",
        )

    try:
        sub = await db_mod.create_subscription(
            pool,
            user_id=user_id,
            page_limit=body.page_limit,
            period_start=period_start,
            period_end=body.period_end,
            note=body.note,
            created_by=user["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not sub:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info(
        "Admin %s created subscription %d for user %s: %d pages, %s → %s",
        user["id"], sub["id"], user_id, body.page_limit,
        period_start.isoformat(), body.period_end.isoformat(),
    )
    return sub


@app.patch("/admin/users/{user_id}/subscriptions/{subscription_id}/cancel")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_cancel_subscription(
    request: Request,
    user_id: str,
    subscription_id: int,
    user: dict = Depends(require_admin),
):
    """Cancel an active subscription. After cancellation the user has no
    active subscription, so further uploads are blocked until a new period
    is created."""
    pool = request.app.state.pool
    ok = await db_mod.cancel_subscription(pool, user_id, subscription_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Active subscription not found for this user",
        )
    logger.info("Admin %s cancelled subscription %d for user %s",
                user["id"], subscription_id, user_id)
    return {"status": "cancelled", "user_id": user_id, "subscription_id": subscription_id}


@app.post("/admin/users/{user_id}/topups", status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_add_topup(
    request: Request,
    user_id: str,
    body: TopupCreate,
    user: dict = Depends(require_admin),
):
    """Grant extra pages to the user's CURRENT active subscription. These
    pages vanish when the subscription period ends, same as the base."""
    pool = request.app.state.pool
    target = await db_mod.get_user_by_id(pool, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        topup = await db_mod.add_topup(
            pool,
            user_id=user_id,
            pages=body.pages,
            note=body.note,
            created_by=user["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not topup:
        raise HTTPException(
            status_code=409,
            detail=(
                "User has no active subscription. Create a subscription period "
                "before adding top-ups."
            ),
        )
    logger.info(
        "Admin %s added %d-page top-up to user %s (subscription %d)",
        user["id"], body.pages, user_id, topup["subscription_id"],
    )
    return topup


@app.get("/admin/users/{user_id}/history")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_get_user_history(
    request: Request,
    user_id: str,
    user: dict = Depends(require_admin),
):
    """Full subscription + top-up history for a single user, used by the
    'View History' modal in the admin UI."""
    pool = request.app.state.pool
    target = await db_mod.get_user_by_id(pool, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    history = await db_mod.get_user_history(pool, user_id)
    return {
        "user_id": user_id,
        "email": target["email"],
        **history,
    }


# -- Top-up Requests ---------------------------------------------------------

@app.post("/me/topup-requests", status_code=201)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def create_topup_request(
    request: Request,
    body: TopupRequestCreate,
    user: dict = Depends(get_current_user),
):
    """User submits a top-up page request to the admin."""
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Admins do not submit top-up requests")
    pool = request.app.state.pool
    req = await db_mod.create_topup_request(
        pool,
        user_id=user["id"],
        requested_pages=body.requested_pages,
        requested_period=body.requested_period,
        note=body.note,
    )
    logger.info(
        "User %s submitted topup request: %d pages for %s",
        user["id"], body.requested_pages, body.requested_period,
    )
    return req


@app.get("/me/topup-requests")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_my_topup_requests(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Return all top-up requests the calling user has submitted."""
    pool = request.app.state.pool
    reqs = await db_mod.list_topup_requests_for_user(pool, user["id"])
    return reqs


@app.get("/admin/topup-requests")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_list_topup_requests(
    request: Request,
    status: str | None = None,
    user: dict = Depends(require_admin),
):
    """Admin: list all top-up requests, optionally filtered by status."""
    pool = request.app.state.pool
    reqs = await db_mod.list_topup_requests(pool, status=status)
    return reqs


@app.get("/admin/topup-requests/count")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_pending_topup_count(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin: return count of pending top-up requests (for notification badge)."""
    pool = request.app.state.pool
    count = await db_mod.get_pending_topup_request_count(pool)
    return {"pending": count}


@app.post("/admin/topup-requests/{request_id}/approve", status_code=200)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_approve_topup_request(
    request: Request,
    request_id: int,
    body: TopupRequestResolve,
    user: dict = Depends(require_admin),
):
    """Admin: approve a pending top-up request and automatically apply the top-up."""
    pool = request.app.state.pool
    try:
        result = await db_mod.approve_topup_atomically(
            pool,
            request_id=request_id,
            resolved_by=user["id"],
            resolution_note=body.resolution_note,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "not_found":
            raise HTTPException(status_code=404, detail="Top-up request not found")
        if msg == "no_active_subscription":
            raise HTTPException(
                status_code=409,
                detail="User has no active subscription. Create a subscription period before approving.",
            )
        if msg.startswith("already_"):
            raise HTTPException(status_code=409, detail=f"Request is already {msg[len('already_'):]}")
        raise HTTPException(status_code=400, detail=msg)

    logger.info(
        "Admin %s approved topup request #%d (%d pages) [atomic]",
        user["id"], request_id, result["request"]["requested_pages"],
    )
    return result


@app.post("/admin/topup-requests/{request_id}/reject", status_code=200)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_reject_topup_request(
    request: Request,
    request_id: int,
    body: TopupRequestResolve,
    user: dict = Depends(require_admin),
):
    """Admin: reject a pending top-up request."""
    pool = request.app.state.pool
    req = await db_mod.get_topup_request(pool, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Top-up request not found")
    if req["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Request is already {req['status']}")

    resolved = await db_mod.resolve_topup_request(
        pool,
        request_id=request_id,
        resolved_by=user["id"],
        status="rejected",
        resolution_note=body.resolution_note,
    )
    logger.info(
        "Admin %s rejected topup request #%d for user %s",
        user["id"], request_id, req["user_id"],
    )
    return resolved


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
    owner_user_id = str(body.owner_user_id)

    # Validate owner user exists and is a client
    owner = await db_mod.get_user_by_id(pool, owner_user_id)
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
            user_id=owner_user_id,
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
    logger.info("Admin %s created API key '%s' for user %s (expires=%s)", user["id"], label, owner_user_id, expires_at)

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
    logger.warning(
        "API_KEY_REVEALED key_id=%s label=%r actor=%s actor_email=%s ip=%s",
        key_id, row.get("label"), _user.get("id"), _user.get("email"),
        request.client.host if request.client else "unknown",
    )
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
            total_input_tokens=int(r.get("total_input_tokens") or 0),
            total_output_tokens=int(r.get("total_output_tokens") or 0),
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
        if not body.user_id:
            raise HTTPException(
                status_code=400,
                detail="Admin must specify user_id when creating a new vendor",
            )
        try:
            owner_id = str(uuid.UUID(str(body.user_id)))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="user_id must be a valid UUID")
        owner = await db_mod.get_user_by_id(pool, owner_id)
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")
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
async def assign_vendor_owner(request: Request, vendor_id: str, body: VendorOwnerAssign, _user: dict = Depends(require_admin)):
    pool = request.app.state.pool
    new_owner_id = str(body.user_id)
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
    _require_pdf(filename, file_bytes)

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


@app.delete("/vendors/{vendor_id}/gold-corrections/{field_key}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_vendor_gold_correction_field(
    request: Request, vendor_id: str, field_key: str, user: dict = Depends(get_current_user),
):
    """Delete saved prompt correction examples for one vendor field."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    deleted_count = await db_mod.delete_gold_correction_field(pool, vendor_id, field_key)
    plog.info(
        "gold_correction_field_deleted",
        vendor_id=vendor_id,
        field_key=field_key,
        deleted_count=deleted_count,
        deleted_by=user.get("sub"),
        role=user.get("role"),
    )
    return {
        "status": "deleted",
        "vendor_id": vendor_id,
        "field_key": field_key,
        "gold_correction_fields_deleted": deleted_count,
    }


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
    _raise_if_review_unavailable(extraction)
    await _raise_if_ocr_review_unavailable(pool, extraction_id)

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
                    schema_id=existing_map.get("schema_id"),
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
    """Return the ERP field mapping for a vendor plus the assigned schema targets."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    from . import field_mapper as _fm

    tmpl = await db_mod.get_template(pool, vendor_id)
    mapping = await db_mod.get_field_mapping(pool, vendor_id)
    all_schemas = await db_mod.get_all_schemas(pool)

    # Resolve schema: use assigned, or fall back to AP Automation (first system schema)
    schema_id = (mapping or {}).get("schema_id")
    schema = None
    if schema_id:
        schema = next((s for s in all_schemas if s["id"] == schema_id), None)
    if not schema:
        schema = next((s for s in all_schemas if s["slug"] == "ap_automation"), None)
    if not schema and all_schemas:
        schema = all_schemas[0]

    return {
        "vendor_id": vendor_id,
        "template_id": (tmpl or {}).get("id"),
        "has_template": tmpl is not None,
        "source_header_fields": (tmpl or {}).get("header_fields") or [],
        "source_line_fields": (tmpl or {}).get("line_item_fields") or [],
        "target_header_fields": (schema or {}).get("header_fields") or _fm.HEADER_TARGETS,
        "target_line_fields": (schema or {}).get("line_fields") or _fm.LINE_TARGETS,
        "schema_id": (schema or {}).get("id"),
        "schema_name": (schema or {}).get("name", "AP Automation"),
        "schemas": [{"id": s["id"], "name": s["name"], "slug": s["slug"],
                     "is_system": s["is_system"],
                     "header_fields": s.get("header_fields") or [],
                     "line_fields": s.get("line_fields") or []}
                    for s in all_schemas],
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
    body: VendorMappingSave,
    user: dict = Depends(require_admin),
):
    """Save (upsert) a vendor's ERP field mapping. Admin only."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)

    tmpl = await db_mod.get_template(pool, vendor_id)
    if not tmpl:
        raise HTTPException(
            status_code=404,
            detail="Configure a template for this vendor before saving a mapping.",
        )

    # Resolve the assigned schema so we can validate target field names.
    schema = None
    if body.schema_id:
        schema = await db_mod.get_schema_by_id(pool, body.schema_id)
        if not schema:
            raise HTTPException(status_code=404, detail=f"Schema {body.schema_id} not found")
    if not schema:
        schema = await db_mod.get_schema_by_slug(pool, "ap_automation")

    header_sources = set(tmpl.get("header_fields") or [])
    line_sources = set(tmpl.get("line_item_fields") or [])
    header_targets = set((schema or {}).get("header_fields") or [])
    line_targets = set((schema or {}).get("line_fields") or [])

    def _reject_invalid_mapping(kind: str, mapping: dict[str, str], sources: set[str], targets: set[str]) -> None:
        bad_sources = sorted(k for k in mapping if k not in sources)
        bad_targets = sorted({v for v in mapping.values() if targets and v not in targets})
        if bad_sources or bad_targets:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_MAPPING",
                    "message": f"{kind} mapping contains unknown source or target fields.",
                    "unknown_sources": bad_sources,
                    "unknown_targets": bad_targets,
                },
            )

    _reject_invalid_mapping("header", body.header_map, header_sources, header_targets)
    _reject_invalid_mapping("line", body.line_map, line_sources, line_targets)
    header_map = dict(body.header_map)
    line_map = dict(body.line_map)

    existing = await db_mod.get_field_mapping(pool, vendor_id)
    saved = await db_mod.upsert_field_mapping(
        pool, vendor_id, tmpl["id"], header_map, line_map,
        tmpl.get("header_fields") or [], tmpl.get("line_item_fields") or [],
        (existing or {}).get("pending_notices") or [],
        schema_id=(schema or {}).get("id"),
    )
    logger.info(
        "ERP mapping saved: vendor=%s schema=%s header=%d line=%d",
        vendor_id, (schema or {}).get("name"), len(header_map), len(line_map),
    )
    return {
        "status": "ok",
        "vendor_id": vendor_id,
        "schema_id": saved.get("schema_id"),
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

    # Strip leading-underscore meta keys (_page, _total_pages, _error) — these
    # are internal merge bookkeeping, never mappable, must not show in the UI.
    header = {k: v for k, v in doc.items()
              if k != "line_items" and not k.startswith("_")}
    line_items = doc.get("line_items") or []
    raw_line_item = line_items[0] if line_items and isinstance(line_items[0], dict) else {}
    line_item = {k: v for k, v in raw_line_item.items() if not k.startswith("_")}
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

@app.post(
    "/ingest/{source_type}",
    response_model=ExtractionJobStartOut,
    status_code=201,
    response_model_exclude_none=True,
)
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


    # If caller pre-selected a vendor, enforce ownership before doing any work.
    if vendor_id:
        await assert_vendor_access(pool, vendor_id, user)
    file_bytes = await file.read()
    filename = file.filename or "unknown"
    _require_pdf(filename, file_bytes)
    try:
        req_header = json.loads(header_fields) if header_fields else []
        req_items = json.loads(line_item_fields) if line_item_fields else []
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_JSON_FIELD", "message": f"header_fields or line_item_fields is not valid JSON: {exc}"}) from exc
    try:
        req_header = validate_field_name_list(req_header)
        req_items = validate_field_name_list(req_items)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_FIELD_LIST", "message": f"header_fields/line_item_fields: {exc}"}) from exc

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
            await db_mod.insert_quota_grace_event(
                pool,
                user_id=user["id"],
                event_type="exceeded",
                grace_pages_used=0,
                incoming_pages=incoming_pages,
                used_before=quota["used"],
                limit_at_time=quota["limit"],
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
            _user_email = (_user_record or {}).get("email")
            grace_pages_used = quota.get("grace_pages_used", 0)
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=_user_email,
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="small_overage",
                filename=filename,
                grace_pages_used=grace_pages_used,
            )
            logger.warning(
                "QUOTA_GRACE: user=%s email=%s limit=%d used=%d incoming=%d grace_pages_used=%d file=%s",
                user["id"], _user_email, quota["limit"], quota["used"],
                incoming_pages, grace_pages_used, filename,
            )
            await db_mod.insert_quota_grace_event(
                pool,
                user_id=user["id"],
                event_type="grace_used",
                grace_pages_used=grace_pages_used,
                incoming_pages=incoming_pages,
                used_before=quota["used"],
                limit_at_time=quota["limit"],
                filename=filename,
            )
            usage_warning = {
                "level": "warning",
                "message": (
                    f"This upload ({incoming_pages} pages) slightly exceeds your remaining "
                    f"quota ({quota['remaining']} pages). It has been allowed as a small overage "
                    f"({grace_pages_used} grace page{'s' if grace_pages_used != 1 else ''} used)."
                ),
                "subscription_limit": quota["limit"],
                "total_extracted_pages": quota["used"],
                "incoming_pages": incoming_pages,
                "remaining": quota["remaining"],
                "grace_pages_used": grace_pages_used,
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

    reservation_user_id = user["id"] if user.get("role") != "admin" else None
    reservation_released = False

    async def _release_reserved_quota_once() -> None:
        nonlocal reservation_released
        if not reservation_user_id or reservation_released:
            return
        try:
            await db_mod.release_quota_reservation(pool, reservation_user_id, incoming_pages)
        except Exception as exc:
            logger.warning("quota release failed before job submission user=%s: %s", reservation_user_id, exc)
        reservation_released = True

    logger.info("File received: %s (%d bytes, source=%s, vendor=%s)",
                filename, len(file_bytes), source_type, vendor_id)

    # Vendor detection used to run synchronously HERE (render page 1 → geometry
    # → PaddleOCR fallback → match). It now happens inside the normalize worker
    # so uploads return fast and don't depend on the OCR service being up. When
    # vendor_id is None the document is submitted for auto-detection; an unknown
    # vendor surfaces later as extraction status=failed / error=unknown_vendor
    # over the SSE stream (no synchronous 409 anymore).
    billing_user_id = user["id"]
    # Scope auto-detection to one client's vendors/aliases to avoid cross-tenant
    # collisions. Admins may act as a specific client; everyone else is scoped to
    # themselves. The worker reads metadata.detect_user_id directly.
    detect_user_id = user["id"]
    if user.get("role") == "admin" and act_as_client_id:
        try:
            act_as_client_id = str(uuid.UUID(str(act_as_client_id)))
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CLIENT_ID", "message": "act_as_client_id must be a valid UUID"})
        target_client = await db_mod.get_user_by_id(pool, act_as_client_id)
        if not target_client:
            raise HTTPException(status_code=404, detail={"code": "CLIENT_NOT_FOUND", "message": "act_as_client_id user not found"})
        detect_user_id = act_as_client_id
    ingest_metadata = {
        "billing_user_id": billing_user_id,
        "detect_user_id": detect_user_id,
    }

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
            metadata=ingest_metadata,
            reserved_pages=incoming_pages if user.get("role") != "admin" else None,
        )
    except Exception:
        await _release_reserved_quota_once()
        raise

    resp = ExtractionJobStartOut(
        job_id=submitted["job"]["id"],
        extraction_id=submitted["extraction"]["id"],
        status=submitted["job"]["status"],
    )
    result = resp.model_dump() if hasattr(resp, "model_dump") else resp.dict()
    if usage_warning:
        result["usage_warning"] = usage_warning
    logger.info("Ingestion job created: ext=%s vendor=%s file=%s",
                submitted["extraction"]["id"], vendor_id or "auto-detect", filename)
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

    # 1. Reject if prior jobs are still draining — BEFORE reserving any quota,
    #    so a 409 never leaks a reservation.
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
    incoming_pages = len(missing_pages)
    if incoming_pages <= 0:
        raise HTTPException(400, detail="All pages are already extracted. Nothing to resume.")
    start_from = missing_pages[0]
    document_id = extraction.get("document_id")

    # 2. Reserve quota atomically for just the missing pages (same hard cap as
    #    /ingest/ui and /v1/extract — the old soft get_user_billable_pages check
    #    let concurrent resumes all pass the same snapshot).
    quota_reserved = False
    if user.get("role") != "admin":
        try:
            quota = await db_mod.reserve_quota(pool, user["id"], incoming_pages)
        except Exception as usage_exc:
            logger.error("Subscription quota check failed — blocking resume: %s", usage_exc)
            raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please retry.")
        if not quota["allowed"]:
            _user_record = await db_mod.get_user_by_id(pool, user["id"])
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=(_user_record or {}).get("email"),
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="exceeded",
                filename=extraction.get("filename"),
            )
            await db_mod.insert_quota_grace_event(
                pool,
                user_id=user["id"],
                event_type="exceeded",
                grace_pages_used=0,
                incoming_pages=incoming_pages,
                used_before=quota["used"],
                limit_at_time=quota["limit"],
                filename=extraction.get("filename"),
            )
            overage = max(quota["used"] - quota["limit"], 0)
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "QUOTA_EXCEEDED",
                    "message": (
                        f"Resuming this document ({incoming_pages} pages) would exceed your "
                        f"subscription limit of {quota['limit']} pages. "
                        f"You have {quota['remaining']} pages remaining. "
                        "Contact your administrator to increase your limit."
                    ),
                    "subscription_limit": quota["limit"],
                    "total_extracted_pages": quota["used"],
                    "remaining": quota["remaining"],
                    "overage": overage,
                },
            )
        if quota["reason"] == "grace":
            _user_record = await db_mod.get_user_by_id(pool, user["id"])
            _user_email = (_user_record or {}).get("email")
            _grace_used = quota.get("grace_pages_used", 0)
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=_user_email,
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="small_overage",
                filename=extraction.get("filename"),
                grace_pages_used=_grace_used,
            )
            logger.warning(
                "QUOTA_GRACE (resume): user=%s email=%s limit=%d used=%d incoming=%d grace_pages_used=%d",
                user["id"], _user_email, quota["limit"], quota["used"],
                incoming_pages, _grace_used,
            )
            await db_mod.insert_quota_grace_event(
                pool,
                user_id=user["id"],
                event_type="grace_used",
                grace_pages_used=_grace_used,
                incoming_pages=incoming_pages,
                used_before=quota["used"],
                limit_at_time=quota["limit"],
                filename=extraction.get("filename"),
            )
        quota_reserved = True

    # 3. Enqueue — release the reservation if anything fails before job handoff.
    try:
        # Record the outstanding reservation on the document so the worker's
        # terminal release returns exactly the missing-page count (not the full
        # document size, which would over-release into other reservations).
        if quota_reserved and document_id:
            await db_mod.set_document_reserved_pages(pool, document_id, incoming_pages)

        await db_mod.set_cancel_requested(pool, extraction_id, False)
        job = await db_mod.enqueue_job(
            pool,
            extraction_id=extraction_id,
            document_id=document_id,
            job_type="llm",
            payload={
                "extraction_id": extraction_id,
                "start_from_page": start_from,
                "existing_page_results": existing_page_results,
                "reserved_pages": incoming_pages,
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
    except Exception:
        if quota_reserved:
            try:
                await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
            except Exception:
                pass
        raise

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

    _vendor_name = row.get("vendor_name")
    if row.get("result") is not None:
        row["result"] = attach_vendor(row["result"], _vendor_name)
    if row.get("corrected_result") is not None:
        row["corrected_result"] = attach_vendor(row["corrected_result"], _vendor_name)
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
    request: Request, vendor_id: str, limit: int = Query(20, ge=1, le=100),
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
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    filter_user = None if user["role"] == "admin" else user["id"]
    rows = await db_mod.list_all_extractions(
        request.app.state.pool, limit, offset, user_id=filter_user,
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
    _require_pdf(filename, file_bytes)

    # Clamp to a safe range — preview is cheaper than extraction and must never
    # render an unbounded page count (max_pages=0 previously rendered the whole PDF).
    max_pages = max(1, min(max_pages, 50))

    pages = await processor.pdf_to_images(file_bytes, max_pages=max_pages)
    total_pages = pages[0].get("doc_total_pages", len(pages)) if pages else 0
    return {"filename": filename, "total_pages": total_pages, "pages": pages}


# -- Review: OCR Data for Click-to-Select -----------------------------------

REVIEW_UNAVAILABLE_OCR_FAILED = "REVIEW_UNAVAILABLE_OCR_FAILED"
REVIEW_UNAVAILABLE_OCR_MESSAGE = (
    "OCR failed for this extraction. Drag/drop review, bounding boxes, and spatial memory are unavailable."
)

REVIEW_UNAVAILABLE_EXTRACTION_FAILED = "REVIEW_UNAVAILABLE_EXTRACTION_FAILED"
REVIEW_UNAVAILABLE_FAILED_MESSAGE = (
    "This extraction failed and cannot be reviewed. Re-run the extraction once the problem is fixed."
)
REVIEW_UNAVAILABLE_NOT_READY = "REVIEW_UNAVAILABLE_NOT_READY"
REVIEW_UNAVAILABLE_NOT_READY_MESSAGE = (
    "Review is available only after a successful extraction completes."
)


def _page_results_have_errors(page_results) -> bool:
    if not isinstance(page_results, list):
        return False
    return any(
        isinstance(page_result, dict) and page_result.get("_error")
        for page_result in page_results
    )


def _review_unavailable_detail(extraction: dict) -> dict | None:
    progress = extraction.get("progress") if isinstance(extraction.get("progress"), dict) else {}
    result = extraction.get("result")
    page_results = extraction.get("page_results")
    status = extraction.get("status")

    if status != "done":
        failed_status = status in {"failed", "partial", "cancelled", "unverified"}
        return {
            "code": REVIEW_UNAVAILABLE_EXTRACTION_FAILED if failed_status else REVIEW_UNAVAILABLE_NOT_READY,
            "message": REVIEW_UNAVAILABLE_FAILED_MESSAGE if failed_status else REVIEW_UNAVAILABLE_NOT_READY_MESSAGE,
            "extraction_id": extraction.get("id"),
            "status": status,
            "error": extraction.get("error"),
        }

    if extraction.get("error") or _all_pages_failed(result, page_results) or _page_results_have_errors(page_results):
        return {
            "code": REVIEW_UNAVAILABLE_EXTRACTION_FAILED,
            "message": REVIEW_UNAVAILABLE_FAILED_MESSAGE,
            "extraction_id": extraction.get("id"),
            "status": status,
            "error": extraction.get("error"),
            "errors": _collect_extraction_errors(result, page_results)[:5],
        }

    if progress.get("review_available") is False or progress.get("warning_code") or progress.get("ocr_error"):
        return {
            "code": progress.get("warning_code") or REVIEW_UNAVAILABLE_OCR_FAILED,
            "message": REVIEW_UNAVAILABLE_OCR_MESSAGE,
            "extraction_id": extraction.get("id"),
            "status": status,
            "ocr_error": progress.get("ocr_error"),
        }

    return None


def _raise_if_review_unavailable(extraction: dict) -> None:
    """Backend source of truth for review availability.

    Review and corrections are available only for a clean completed extraction.
    Any pipeline failure or explicit review-disabled warning blocks every review
    API path so the frontend and backend cannot disagree.
    """
    detail = _review_unavailable_detail(extraction)
    if detail:
        raise HTTPException(
            409,
            detail=detail,
        )


async def _raise_if_ocr_review_unavailable(pool, extraction_id: int) -> None:
    latest_ocr_job = await db_mod.get_latest_job_for_extraction_type(pool, extraction_id, "ocr")
    if latest_ocr_job and latest_ocr_job.get("status") == "failed":
        raise HTTPException(
            409,
            detail={
                "code": REVIEW_UNAVAILABLE_OCR_FAILED,
                "message": REVIEW_UNAVAILABLE_OCR_MESSAGE,
                "extraction_id": extraction_id,
                "ocr_error": latest_ocr_job.get("error"),
            },
        )


@app.get("/extractions/{extraction_id}/ocr")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_ocr(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    """Return PaddleOCR word data for the click-to-select correction UI."""
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    _raise_if_review_unavailable(extraction)
    await _raise_if_ocr_review_unavailable(pool, extraction_id)
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
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    _raise_if_review_unavailable(extraction)
    await _raise_if_ocr_review_unavailable(pool, extraction_id)
    pages = await db_mod.get_pages(pool, extraction_id)
    if not pages:
        raise HTTPException(404, detail="No pages found for this extraction")

    ocr_data = await db_mod.get_ocr_data(pool, extraction_id)
    if ocr_data is None:
        raise HTTPException(404, detail="No OCR data found for this extraction")
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
    request: Request,
    extraction_id: int,
    body: CorrectionSaveRequest,
    user: dict = Depends(get_current_user),
):
    """Persist user corrections from the Review page.

    Saves to corrected_result (original result stays immutable).
    Auto-creates an audit gold example for this vendor if fields were changed.
    Future prompts use only value-redacted field hints from those examples.
    """
    pool_for_check = request.app.state.pool
    await assert_extraction_access(pool_for_check, extraction_id, user)
    payload = body.model_dump(exclude_none=True)
    corrected_result = payload["corrected_result"]
    field_locations = payload.get("field_locations") or {}
    actor = payload.get("actor") or "ui"
    reason_code = payload.get("reason_code") or "manual_review"
    note = payload.get("note")

    pool = request.app.state.pool

    # Get original extraction to compare and create an audit gold example.
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail=f"Extraction {extraction_id} not found")
    _raise_if_review_unavailable(extraction)
    await _raise_if_ocr_review_unavailable(pool, extraction_id)
    if not extraction.get("ocr_data"):
        raise HTTPException(404, detail="No OCR data found for this extraction")

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
    _raise_if_review_unavailable(extraction)
    await _raise_if_ocr_review_unavailable(pool, extraction_id)
    return {"extraction_id": extraction_id, "reviews": await db_mod.list_review_events(pool, extraction_id)}


@app.get("/vendors/{vendor_id}/spatial-memory")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def list_vendor_spatial_memory(
    request: Request, vendor_id: str, user: dict = Depends(get_current_user),
):
    """List all active spatial memory (manual corrections) for a vendor."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    entries = await db_mod.list_spatial_memory_for_vendor(pool, vendor_id)
    plog.info(
        "spatial_memory_listed",
        vendor_id=vendor_id,
        count=len(entries),
        user_id=user.get("sub"),
    )
    return {"vendor_id": vendor_id, "entries": entries, "count": len(entries)}


@app.delete("/spatial-memory/{sm_id}")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def delete_spatial_memory_entry(
    request: Request, sm_id: int, user: dict = Depends(get_current_user),
):
    """Delete a specific spatial memory entry. User must have access to its vendor."""
    pool = request.app.state.pool
    entry = await db_mod.get_spatial_memory_by_id(pool, sm_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Spatial memory entry not found")

    # Admins may delete any entry; non-admins must own the vendor.
    if user.get("role") != "admin":
        await assert_vendor_access(pool, entry["vendor_id"], user)

    deleted = await db_mod.delete_spatial_memory_by_id(
        pool,
        sm_id,
        delete_gold_correction=True,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Spatial memory entry not found")
    gold_deleted = int(deleted.get("gold_correction_fields_deleted") or 0)

    plog.info(
        "spatial_memory_deleted",
        sm_id=sm_id,
        vendor_id=entry["vendor_id"],
        layout_key=entry.get("layout_key"),
        field_key=entry.get("field_key"),
        page_number=entry.get("page_number"),
        gold_correction_fields_deleted=gold_deleted,
        deleted_by=user.get("sub"),
        role=user.get("role"),
    )
    return {
        "status": "deleted",
        "sm_id": sm_id,
        "vendor_id": entry["vendor_id"],
        "field_key": entry.get("field_key"),
        "gold_correction_fields_deleted": gold_deleted,
    }


@app.get("/admin/quota-events")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_list_quota_events(
    request: Request,
    limit: int = Query(100, ge=1),
    _user: dict = Depends(require_admin),
):
    """Return recent quota grace/exceeded events for admin monitoring."""
    pool = request.app.state.pool
    events = await db_mod.get_admin_quota_events(pool, limit=min(limit, 500))
    out = []
    for e in events:
        out.append({
            "id": e["id"],
            "event_ts": e["event_ts"].isoformat() if e["event_ts"] else None,
            "event_type": e["event_type"],
            "email": e["email"],
            "grace_pages_used": e["grace_pages_used"],
            "incoming_pages": e["incoming_pages"],
            "used_before": e["used_before"],
            "limit_at_time": e["limit_at_time"],
            "filename": e["filename"],
        })
    return out


@app.get("/admin/spatial-memory")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_list_spatial_memory(
    request: Request,
    limit: int = Query(500, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _user: dict = Depends(require_admin),
):
    """Admin: list all active spatial memory entries across all vendors."""
    pool = request.app.state.pool
    entries = await db_mod.list_spatial_memory_all(pool, limit=limit, offset=offset)
    total = await db_mod.count_spatial_memory_all(pool)
    plog.info(
        "admin_spatial_memory_listed",
        count=len(entries),
        total=total,
        limit=limit,
        offset=offset,
        admin_id=_user.get("sub"),
    )
    return {"entries": entries, "count": len(entries), "total": total, "limit": limit, "offset": offset}


@app.get("/extractions/{extraction_id}/trace")
@limiter.limit(f"{RATE_LIMIT}/minute")
async def get_extraction_trace(
    request: Request, extraction_id: int, limit: int | None = Query(None, ge=1, le=1000),
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
        except Exception as exc:
            logger.warning("stats: failed to fetch subscription info for uid=%s: %s", uid, exc)
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
    limit: int = Query(50, ge=1, le=500),
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
    limit: int = Query(100, ge=1, le=500),
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
    except Exception as exc:
        logger.warning("admin stats: failed to fetch subscription info for user=%s: %s", client_user_id, exc)

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
    limit: int = Query(200, ge=1, le=500),
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
    limit: int = Query(50, ge=1, le=500),
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
    days: int = Query(30, ge=1, le=365),
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
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
):
    """Client: per-day + per-page breakdown for one of their own vendors."""
    pool = request.app.state.pool
    await assert_vendor_access(pool, vendor_id, user)
    daily = await db_mod.get_vendor_extraction_daily(pool, vendor_id, limit=days)
    pages = await db_mod.get_vendor_page_stats(pool, vendor_id)
    return {"vendor_id": vendor_id, "daily": daily, "pages": pages}


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
    _require_pdf(filename, file_bytes)

    # Compute file hash
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128 or any(ord(c) < 32 for c in idempotency_key):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_IDEMPOTENCY_KEY",
                    "message": "Idempotency-Key must be 1-128 printable characters.",
                },
            )

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
                    api_result = mapped_result if mapping_applied else extraction.get("result")
                    _raise_for_terminal_extraction_error(extraction, api_result, extraction_id)
                    return {
                        "extraction_id": extraction_id,
                        "vendor_id": extraction.get("vendor_id"),
                        "pages": extraction.get("total_pages", 0),
                        "duration_ms": extraction.get("duration_ms"),
                        "mapping_applied": mapping_applied,
                        "completed_at": extraction.get("updated_at").isoformat() if extraction.get("updated_at") else datetime.now(UTC).isoformat(),
                        "result": attach_vendor(api_result, extraction.get("vendor_name")),
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
        # All pre-submit work is wrapped in one try so that any failure before
        # the job is handed off releases the quota reservation AND deletes the
        # idempotency claim — otherwise a retry with the same key is poisoned.
        incoming_pages = 0
        quota_reserved = False
        _job_submitted = False
        try:
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
                quota_reserved = True

            logger.info("[v1/extract] File received: %s (%d bytes, auth=%s, user=%s)",
                        filename, len(file_bytes), user.get("auth_method"), user.get("email"))

            # Vendor detection moved into the normalize worker. When the caller
            # pre-selects a vendor, enforce ownership now; otherwise submit for
            # auto-detection (vendor_id=None) and let normalize detect it. An
            # unknown vendor surfaces below as status=failed / unknown_vendor,
            # which we map back to the original 400 for sync callers (async
            # callers discover it via status polling).
            if vendor_id:
                await assert_vendor_access(pool, vendor_id, user)

            # -- Ingestion metadata including hash/key
            doc_metadata = {
                "billing_user_id": user["id"],
                "detect_user_id": user["id"],
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
                # The extraction/job now exist and own the quota reservation, so we
                # must NOT delete the claim on failure here (that would let a retry
                # spawn a duplicate extraction). Retry the bind to ride out a
                # transient DB hiccup; if it ultimately fails the claim stays
                # unbound and self-heals when its 24h TTL lapses.
                for _bind_attempt in range(3):
                    try:
                        await db_mod.bind_idempotency_claim(pool, claim["claim_id"], extraction_id, document_id)
                        break
                    except Exception as bind_exc:
                        if _bind_attempt == 2:
                            logger.error(
                                "Idempotency claim bind failed after retries "
                                "(claim=%s ext=%s); claim will self-heal at TTL: %s",
                                claim["claim_id"], extraction_id, bind_exc,
                            )
                        else:
                            await asyncio.sleep(0.1 * (_bind_attempt + 1))

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
            if quota_reserved and not _job_submitted:
                try:
                    await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
                except Exception:
                    pass
            if claim and claim.get("claim_id") and not _job_submitted:
                try:
                    await db_mod.delete_idempotency_claim(pool, user["id"], idempotency_key)
                except Exception as del_exc:
                    logger.warning("Failed to delete orphaned idempotency claim: %s", del_exc)
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
        # Detection now happens in the worker. Preserve the original synchronous
        # contracts for the failures that used to be raised inline:
        #  • unknown / unconfigured vendor → 400 (client must set the vendor up)
        #  • OCR unavailable during detection → 503 (transient infra, retry)
        _err = extraction.get("error")
        if _err in ("unknown_vendor", "no_template", "no_fields"):
            raise HTTPException(
                status_code=400,
                detail="No vendor template found for this document. Please create a vendor and template first.",
            )
        if _err == "ocr_unavailable":
            raise HTTPException(
                status_code=503,
                detail="OCR was unavailable while processing this document. Please retry.",
            )
        if _err == "llm_failed":
            _raise_for_terminal_extraction_error(extraction, extraction.get("result"), extraction_id)
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
    api_result = mapped_result if mapping_applied else extraction.get("result")
    _raise_for_terminal_extraction_error(extraction, api_result, extraction_id)
    return {
        "extraction_id": extraction_id,
        "vendor_id": extraction.get("vendor_id"),
        "pages": extraction.get("total_pages", 0),
        "duration_ms": extraction.get("duration_ms"),
        "mapping_applied": mapping_applied,
        "completed_at": extraction.get("updated_at").isoformat() if extraction.get("updated_at") else datetime.now(UTC).isoformat(),
        "result": attach_vendor(api_result, extraction.get("vendor_name")),
    }


# -- Static Frontend --------------------------------------------------------

# Mount the static frontend directory so it's accessible at http://localhost:8055/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s. UI will not be served.", FRONTEND_DIR)


# -- Run with uvicorn -------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8055, reload=True)
