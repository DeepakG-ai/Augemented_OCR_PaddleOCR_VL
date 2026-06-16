"""
config.py — Single source of truth for environment-driven configuration *values*.

Modules import the constants they need from here instead of calling os.getenv
themselves. Defaults match the historical per-file values, so upgrading changes
no behaviour.

Three deliberate exceptions do NOT route through this module:
  • Env *side-effects* that must be set BEFORE a third-party library imports —
    `os.environ.setdefault(...)` in `ocr_runner.py` (PaddleOCR) and
    `mlflow_tracing.py` (MLflow client). These configure those libraries, so they
    have to run at the top of their own module, ahead of the library import.
  • `auth.py` re-reads `SECRET_KEY` at call time on purpose, so a rotated secret
    (or a test override) takes effect without a process restart.
  • `main.py` reads `ADMIN_EMAIL` / `ADMIN_PASSWORD` once during the startup
    admin-bootstrap, and `logging_config.py` reads its CloudWatch settings where
    the handler is built.
Everything else — every plain configuration read — lives here.
"""
from __future__ import annotations

import ipaddress
import os
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {name!r} must be an integer, got: {raw!r}"
        )


def _env_int_min(name: str, default: int, minimum: int) -> int:
    value = _env_int(name, default)
    if value < minimum:
        raise ValueError(
            f"Environment variable {name!r} must be >= {minimum}, got: {value!r}"
        )
    return value


def _env_int_range(name: str, default: int, lo: int, hi: int) -> int:
    value = _env_int(name, default)
    if not lo <= value <= hi:
        raise ValueError(
            f"Environment variable {name!r} must be between {lo} and {hi}, got: {value!r}"
        )
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {name!r} must be a number, got: {raw!r}"
        )


def _env_float_range(name: str, default: float, lo: float, hi: float) -> float:
    value = _env_float(name, default)
    if not lo <= value <= hi:
        raise ValueError(
            f"Environment variable {name!r} must be between {lo} and {hi}, got: {value!r}"
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"Environment variable {name!r} must be a boolean, got: {raw!r}"
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://augocr:augocr@localhost:5432/augocr")

# ---------------------------------------------------------------------------
# LLM (Qwen3-VL via llama-server)
# ---------------------------------------------------------------------------

def _validate_llm_url(url: str) -> str:
    """Block SSRF targets: refuse non-http(s) schemes and known metadata endpoints."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"LLM_URL must use http or https, got: {url!r}")
    host = parsed.hostname or ""
    if host in {"169.254.169.254", "metadata.google.internal"}:
        raise ValueError(f"LLM_URL host is a blocked metadata endpoint: {host!r}")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass  # hostname (e.g. "localhost", "llm-server") — allowed
    else:
        if addr.is_link_local:
            raise ValueError(f"LLM_URL must not use a link-local address: {host!r}")
    return url

LLM_URL              = _validate_llm_url(os.getenv("LLM_URL", "http://localhost:8056/v1/chat/completions"))
LLM_MODEL            = os.getenv("LLM_MODEL", "qwen3vl")
LLM_TEMPERATURE      = _env_float_range("LLM_TEMPERATURE",      0.7, 0.0, 2.0)
LLM_TOP_P            = _env_float_range("LLM_TOP_P",            0.8, 0.0, 1.0)
LLM_PRESENCE_PENALTY = _env_float_range("LLM_PRESENCE_PENALTY", 1.5, -2.0, 2.0)
LLM_MAX_TOKENS_FIELDS = _env_int_min("LLM_MAX_TOKENS_FIELDS", 8192, 1)
LLM_TIMEOUT           = _env_float_range("LLM_TIMEOUT",          300.0, 1.0, 3600.0)
LLM_PAGE_BATCH_SIZE   = _env_int_min("LLM_PAGE_BATCH_SIZE", 1, 1)

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
WORKER_POLL_SECONDS = _env_float_range("WORKER_POLL_SECONDS", 1.0, 0.1, 3600.0)

# ---------------------------------------------------------------------------
# HTTP / API
# ---------------------------------------------------------------------------
# Validated as a positive int (it is interpolated into every "<N>/minute"
# slowapi limit string; a non-numeric value would silently break rate limiting).
RATE_LIMIT_PER_MINUTE = _env_int_min("RATE_LIMIT_PER_MINUTE", 30, 1)
MAX_UPLOAD_MB         = _env_int_min("MAX_UPLOAD_MB", 50, 1)
MAX_UPLOAD_BYTES      = MAX_UPLOAD_MB * 1024 * 1024
MAX_DOCUMENT_PAGES    = _env_int_min("MAX_DOCUMENT_PAGES", 100, 1)

_DEFAULT_CORS_ALLOW_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:3002",
    "http://localhost:8000",
    "http://localhost:8055",
    "http://localhost:8057",
    "http://localhost:8058",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3002",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8055",
    "http://127.0.0.1:8057",
    "http://127.0.0.1:8058",
)
CORS_ALLOW_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOW_ORIGINS", ",".join(_DEFAULT_CORS_ALLOW_ORIGINS)).split(",")
    if origin.strip()
]

# ---------------------------------------------------------------------------
# Iframe embedding (CSP frame-ancestors)
# ---------------------------------------------------------------------------
# Comma-separated list of origins allowed to embed this app in an <iframe>.
# When empty (default), the middleware sends X-Frame-Options: DENY.
# When set, it sends Content-Security-Policy: frame-ancestors 'self' <origins>
# instead, and omits X-Frame-Options (CSP takes precedence in modern browsers).
# Example: FRAME_ANCESTORS=https://portal.example.com,http://localhost:8080
FRAME_ANCESTORS: list[str] = [
    origin.strip().rstrip("/")
    for origin in os.getenv("FRAME_ANCESTORS", "").split(",")
    if origin.strip()
]

# ---------------------------------------------------------------------------
# PDF / image processing
# ---------------------------------------------------------------------------
MAX_LONG_SIDE_PX = _env_int_min("MAX_LONG_SIDE_PX", 1536, 1)
MAX_PIXELS       = _env_int_min("MAX_PIXELS",       1536 * 1120, 1)   # 1,720,320 px
JPEG_QUALITY     = _env_int_range("JPEG_QUALITY",   92, 1, 100)
DPI_FLOOR        = _env_int_min("DPI_FLOOR",        96, 1)
DPI_DEFAULT      = _env_int_min("DPI_DEFAULT",      128, 1)
PDF_WORKERS      = _env_int_min("PDF_WORKERS",      2, 1)
OCR_WORKERS      = _env_int_min("OCR_WORKERS",      3, 1)
OCR_DEVICE       = os.getenv("OCR_DEVICE",      "cpu")


# ---------------------------------------------------------------------------
# Object store (MinIO + local fallback)
# ---------------------------------------------------------------------------
MINIO_ENDPOINT         = os.getenv("MINIO_ENDPOINT",         "localhost:9000")
MINIO_ACCESS_KEY       = os.getenv("MINIO_ACCESS_KEY",       "minioadmin")
MINIO_SECRET_KEY       = os.getenv("MINIO_SECRET_KEY",       "minioadmin")
MINIO_SECURE           = _env_bool("MINIO_SECURE", False)
MINIO_DOCUMENTS_BUCKET = os.getenv("MINIO_DOCUMENTS_BUCKET", "augocr-documents")
MINIO_ARTIFACTS_BUCKET = os.getenv("MINIO_ARTIFACTS_BUCKET", "augocr-artifacts")
LOCAL_OBJECT_STORE_DIR = Path(os.getenv("LOCAL_OBJECT_STORE_DIR", ".local_object_store"))

# ---------------------------------------------------------------------------
# MLflow observability
# ---------------------------------------------------------------------------
MLFLOW_ENABLED      = _env_bool("MLFLOW_ENABLED", True)
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
PIPELINE_LOG_DIR = Path(os.getenv("PIPELINE_LOG_DIR", "logs/pipeline"))

# Append-only page-usage + alerts logs (used by page_logger.py). Defaults are
# project-root-relative so they match the historical per-file values.
_PROJECT_ROOT        = os.path.dirname(os.path.dirname(__file__))
PAGE_USAGE_LOG_PATH  = os.getenv("PAGE_USAGE_LOG_PATH",  os.path.join(_PROJECT_ROOT, "logs", "page_usage", "log.txt"))
PAGE_ALERTS_LOG_PATH = os.getenv("PAGE_ALERTS_LOG_PATH", os.path.join(_PROJECT_ROOT, "logs", "page_usage", "alerts.log"))

# ---------------------------------------------------------------------------
# Subscription / page limits
# ---------------------------------------------------------------------------
DEFAULT_SUBSCRIPTION_LIMIT     = _env_int_min("DEFAULT_SUBSCRIPTION_LIMIT", 0, 0)
SUBSCRIPTION_WARNING_THRESHOLD = _env_float_range("SUBSCRIPTION_WARNING_THRESHOLD", 0.9, 0.0, 1.0)  # 90%

# ---------------------------------------------------------------------------
# Debug toggles
# ---------------------------------------------------------------------------
DEBUG_DUMP_BBOX = _env_bool("DEBUG_DUMP_BBOX", False)
