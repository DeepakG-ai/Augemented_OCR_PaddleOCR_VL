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

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://augocr:augocr@localhost:5432/augocr")

# ---------------------------------------------------------------------------
# LLM (Qwen3-VL via llama-server)
# ---------------------------------------------------------------------------
LLM_URL              = os.getenv("LLM_URL",   "http://localhost:8001/v1/chat/completions")
LLM_MODEL            = os.getenv("LLM_MODEL", "qwen3vl")
LLM_TEMPERATURE      = float(os.getenv("LLM_TEMPERATURE",      "0.6"))
LLM_TOP_P            = float(os.getenv("LLM_TOP_P",            "0.95"))
LLM_PRESENCE_PENALTY = float(os.getenv("LLM_PRESENCE_PENALTY", "1.0"))
LLM_MAX_TOKENS_FIELDS = int(os.getenv("LLM_MAX_TOKENS_FIELDS", "6000"))
LLM_MAX_TOKENS_BBOX   = int(os.getenv("LLM_MAX_TOKENS_BBOX",   "1500"))
LLM_TIMEOUT           = float(os.getenv("LLM_TIMEOUT",          "300"))

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "1.0"))

# ---------------------------------------------------------------------------
# HTTP / API
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE = os.getenv("RATE_LIMIT_PER_MINUTE", "30")
MAX_UPLOAD_MB         = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES      = MAX_UPLOAD_MB * 1024 * 1024

# ---------------------------------------------------------------------------
# PDF / image processing
# ---------------------------------------------------------------------------
MAX_LONG_SIDE_PX = int(os.getenv("MAX_LONG_SIDE_PX", "960"))
MAX_PIXELS       = int(os.getenv("MAX_PIXELS",       str(960 * 720)))   # 691,200 px
JPEG_QUALITY     = int(os.getenv("JPEG_QUALITY",     "92"))
DPI_FLOOR        = int(os.getenv("DPI_FLOOR",        "96"))
DPI_DEFAULT      = int(os.getenv("DPI_DEFAULT",      "120"))
PDF_WORKERS      = int(os.getenv("PDF_WORKERS",      "2"))

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
DEFAULT_SUBSCRIPTION_LIMIT    = int(os.getenv("DEFAULT_SUBSCRIPTION_LIMIT", "0"))
SUBSCRIPTION_WARNING_THRESHOLD = float(os.getenv("SUBSCRIPTION_WARNING_THRESHOLD", "0.9"))  # 90%

# ---------------------------------------------------------------------------
# Debug toggles
# ---------------------------------------------------------------------------
DEBUG_DUMP_BBOX = bool(os.getenv("DEBUG_DUMP_BBOX"))
