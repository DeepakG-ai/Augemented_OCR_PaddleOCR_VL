"""
config.py — Single source of truth for all environment-driven configuration.

All os.getenv calls live here. Every module imports the constants it needs
from this module instead of reading environment variables directly.
Defaults match the existing per-file values so no behaviour changes on upgrade.
"""
from __future__ import annotations

import os
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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://augocr:augocr@localhost:5432/augocr")

# ---------------------------------------------------------------------------
# LLM (Qwen3-VL via llama-server)
# ---------------------------------------------------------------------------
LLM_URL              = os.getenv("LLM_URL",   "http://localhost:8001/v1/chat/completions")
LLM_MODEL            = os.getenv("LLM_MODEL", "qwen3vl")
LLM_TEMPERATURE      = _env_float("LLM_TEMPERATURE",      0.7)
LLM_TOP_P            = _env_float("LLM_TOP_P",            0.8)
LLM_PRESENCE_PENALTY = _env_float("LLM_PRESENCE_PENALTY", 1.5)
LLM_MAX_TOKENS_FIELDS = _env_int("LLM_MAX_TOKENS_FIELDS", 8192)
LLM_TIMEOUT           = _env_float("LLM_TIMEOUT",          300.0)
LLM_PAGE_BATCH_SIZE   = _env_int_min("LLM_PAGE_BATCH_SIZE", 1, 1)

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
WORKER_POLL_SECONDS = _env_float("WORKER_POLL_SECONDS", 1.0)

# ---------------------------------------------------------------------------
# HTTP / API
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE = os.getenv("RATE_LIMIT_PER_MINUTE", "30")
MAX_UPLOAD_MB         = _env_int("MAX_UPLOAD_MB", 50)
MAX_UPLOAD_BYTES      = MAX_UPLOAD_MB * 1024 * 1024
MAX_DOCUMENT_PAGES    = _env_int("MAX_DOCUMENT_PAGES", 100)

# ---------------------------------------------------------------------------
# PDF / image processing
# ---------------------------------------------------------------------------
MAX_LONG_SIDE_PX = _env_int("MAX_LONG_SIDE_PX", 1536)
MAX_PIXELS       = _env_int("MAX_PIXELS",       1536 * 1120)   # 1,720,320 px
JPEG_QUALITY     = _env_int("JPEG_QUALITY",     92)
DPI_FLOOR        = _env_int("DPI_FLOOR",        96)
DPI_DEFAULT      = _env_int("DPI_DEFAULT",      128)
PDF_WORKERS      = _env_int("PDF_WORKERS",      2)
OCR_DEVICE       = os.getenv("OCR_DEVICE",      "cpu")


# ---------------------------------------------------------------------------
# Object store (MinIO + local fallback)
# ---------------------------------------------------------------------------
MINIO_ENDPOINT         = os.getenv("MINIO_ENDPOINT",         "localhost:9000")
MINIO_ACCESS_KEY       = os.getenv("MINIO_ACCESS_KEY",       "minioadmin")
MINIO_SECRET_KEY       = os.getenv("MINIO_SECRET_KEY",       "minioadmin")
MINIO_SECURE           = os.getenv("MINIO_SECURE", "false").lower() in {"1", "true", "yes", "on"}
MINIO_DOCUMENTS_BUCKET = os.getenv("MINIO_DOCUMENTS_BUCKET", "augocr-documents")
MINIO_ARTIFACTS_BUCKET = os.getenv("MINIO_ARTIFACTS_BUCKET", "augocr-artifacts")
LOCAL_OBJECT_STORE_DIR = Path(os.getenv("LOCAL_OBJECT_STORE_DIR", ".local_object_store"))

# ---------------------------------------------------------------------------
# MLflow observability
# ---------------------------------------------------------------------------
MLFLOW_ENABLED      = os.getenv("MLFLOW_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
PIPELINE_LOG_DIR = Path(os.getenv("PIPELINE_LOG_DIR", "logs/pipeline"))

# ---------------------------------------------------------------------------
# Subscription / page limits
# ---------------------------------------------------------------------------
DEFAULT_SUBSCRIPTION_LIMIT     = _env_int("DEFAULT_SUBSCRIPTION_LIMIT", 0)
SUBSCRIPTION_WARNING_THRESHOLD = _env_float("SUBSCRIPTION_WARNING_THRESHOLD", 0.9)  # 90%

# ---------------------------------------------------------------------------
# Debug toggles
# ---------------------------------------------------------------------------
DEBUG_DUMP_BBOX = bool(os.getenv("DEBUG_DUMP_BBOX"))
