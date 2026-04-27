"""
Central logging configuration for the backend.

This file is intentionally not named logging.py because that would shadow
Python's standard-library logging module.
"""
from __future__ import annotations

import json
import logging
import logging.config
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

load_dotenv()

_PIPELINE_LOCK = threading.Lock()
_PIPELINE_LOGGER_NAME = "pipeline"
_PIPELINE_LOG_DIR = Path(os.getenv("PIPELINE_LOG_DIR", "logs/pipeline"))
_PIPELINE_CENTRAL_LOG = _PIPELINE_LOG_DIR / "pipeline.jsonl"
_PIPELINE_EXTRACTION_DIR = _PIPELINE_LOG_DIR / "extractions"
_PIPELINE_MAX_BYTES = int(os.getenv("PIPELINE_LOG_MAX_BYTES", str(25 * 1024 * 1024)))
_PIPELINE_BACKUP_COUNT = int(os.getenv("PIPELINE_LOG_BACKUP_COUNT", "10"))
_PIPELINE_LOG_VALUES = os.getenv("PIPELINE_LOG_VALUES", "1").lower() not in {"0", "false", "no"}
_PIPELINE_VALUE_LIMIT = int(os.getenv("PIPELINE_LOG_VALUE_LIMIT", "160"))


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
            }
        },
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    })


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _ensure_pipeline_logger() -> logging.Logger:
    logger = logging.getLogger(_PIPELINE_LOGGER_NAME)
    existing_handlers = [
        h for h in logger.handlers
        if getattr(h, "_augocr_pipeline_jsonl_handler", False)
    ]
    if existing_handlers:
        for extra in existing_handlers[1:]:
            logger.removeHandler(extra)
            extra.close()
        return logger

    _PIPELINE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _PIPELINE_EXTRACTION_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        _PIPELINE_CENTRAL_LOG,
        maxBytes=_PIPELINE_MAX_BYTES,
        backupCount=_PIPELINE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler._augocr_pipeline_jsonl_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _clean(value: Any, *, is_value: bool = False) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v, is_value=_looks_like_value_key(str(k))) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, is_value=is_value) for v in value[:50]]
    if isinstance(value, tuple):
        return [_clean(v, is_value=is_value) for v in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            if is_value and not _PIPELINE_LOG_VALUES:
                return "<redacted>"
            if len(value) > _PIPELINE_VALUE_LIMIT:
                return value[:_PIPELINE_VALUE_LIMIT] + "...<truncated>"
        return value
    return str(value)


def _looks_like_value_key(key: str) -> bool:
    return key in {"value", "old_value", "new_value", "matched_text", "current_text", "raw_text"}


def event(
    event_name: str,
    *,
    stage: str,
    extraction_id: int | None = None,
    document_id: int | None = None,
    job_id: int | None = None,
    vendor_id: str | None = None,
    vendor_name: str | None = None,
    filename: str | None = None,
    duration_ms: float | int | None = None,
    status: str = "ok",
    **details: Any,
) -> None:
    """Write one structured extraction pipeline event as JSONL."""
    payload: dict[str, Any] = {
        "ts": _now(),
        "event": event_name,
        "stage": stage,
        "status": status,
    }
    if extraction_id is not None:
        payload["extraction_id"] = extraction_id
    if document_id is not None:
        payload["document_id"] = document_id
    if job_id is not None:
        payload["job_id"] = job_id
    if vendor_id:
        payload["vendor_id"] = vendor_id
    if vendor_name:
        payload["vendor_name"] = vendor_name
    if filename:
        payload["filename"] = filename
    if duration_ms is not None:
        payload["duration_ms"] = round(float(duration_ms), 3)
    if details:
        payload["details"] = _clean(details)

    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    logger = _ensure_pipeline_logger()
    with _PIPELINE_LOCK:
        logger.info(line)
        if extraction_id is not None:
            _PIPELINE_EXTRACTION_DIR.mkdir(parents=True, exist_ok=True)
            per_file = _PIPELINE_EXTRACTION_DIR / f"extraction_{int(extraction_id)}.jsonl"
            with per_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


@contextmanager
def timed(event_name: str, *, stage: str, **base: Any) -> Iterator[dict[str, Any]]:
    """Emit one structured event with duration when the block exits."""
    start = time.perf_counter()
    details: dict[str, Any] = {}
    try:
        yield details
    except Exception as exc:
        event(
            event_name,
            stage=stage,
            duration_ms=_duration_ms(start),
            status="error",
            error_type=type(exc).__name__,
            error=str(exc),
            **base,
            **details,
        )
        raise
    else:
        event(
            event_name,
            stage=stage,
            duration_ms=_duration_ms(start),
            **base,
            **details,
        )


def log_paths(extraction_id: int | None = None) -> dict[str, str]:
    paths = {"central": str(_PIPELINE_CENTRAL_LOG)}
    if extraction_id is not None:
        paths["extraction"] = str(_PIPELINE_EXTRACTION_DIR / f"extraction_{int(extraction_id)}.jsonl")
    return paths
