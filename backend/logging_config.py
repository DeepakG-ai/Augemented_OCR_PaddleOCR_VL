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

if __package__:
    from .config import (
        LOG_LEVEL,
        PIPELINE_LOG_DIR          as _PIPELINE_LOG_DIR_CFG,
        PIPELINE_LOG_MAX_BYTES    as _PIPELINE_MAX_BYTES,
        PIPELINE_LOG_BACKUP_COUNT as _PIPELINE_BACKUP_COUNT,
        PIPELINE_LOG_VALUES       as _PIPELINE_LOG_VALUES,
        PIPELINE_LOG_VALUE_LIMIT  as _PIPELINE_VALUE_LIMIT,
    )
else:
    from config import (  # type: ignore[no-redef]
        LOG_LEVEL,
        PIPELINE_LOG_DIR          as _PIPELINE_LOG_DIR_CFG,
        PIPELINE_LOG_MAX_BYTES    as _PIPELINE_MAX_BYTES,
        PIPELINE_LOG_BACKUP_COUNT as _PIPELINE_BACKUP_COUNT,
        PIPELINE_LOG_VALUES       as _PIPELINE_LOG_VALUES,
        PIPELINE_LOG_VALUE_LIMIT  as _PIPELINE_VALUE_LIMIT,
    )

_PIPELINE_LOCK = threading.Lock()
_PIPELINE_LOGGER_NAME = "pipeline"
_PIPELINE_LOG_DIR = _PIPELINE_LOG_DIR_CFG
_PIPELINE_CENTRAL_LOG = _PIPELINE_LOG_DIR / "pipeline.jsonl"
_PIPELINE_EXTRACTION_DIR = _PIPELINE_LOG_DIR / "extractions"


def configure_logging() -> None:
    level = LOG_LEVEL

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


def read_extraction_events(extraction_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the structured JSONL timeline for one extraction."""
    path = _PIPELINE_EXTRACTION_DIR / f"extraction_{int(extraction_id)}.jsonl"
    if not path.exists():
        return []

    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"ts": None, "event": "trace_decode_error", "stage": "trace", "raw": line})

    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def build_extraction_trace_summary(
    extraction_id: int,
    events: list[dict[str, Any]],
    *,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact one-PDF trace summary from recorded events."""
    extraction = extraction or {}
    stage_counts: dict[str, int] = {}
    errors: list[dict[str, Any]] = []
    pages: dict[int, dict[str, Any]] = {}
    manual_overrides: list[dict[str, Any]] = []
    final_result: Any = extraction.get("corrected_result") or extraction.get("result")

    for item in events:
        stage = str(item.get("stage") or "unknown")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        details = item.get("details") or {}

        if item.get("status") == "error" or item.get("event", "").endswith("_failed"):
            errors.append({
                "ts": item.get("ts"),
                "stage": stage,
                "event": item.get("event"),
                "details": details,
            })

        page_num = details.get("page") or details.get("page_number")
        if isinstance(page_num, int):
            page_entry = pages.setdefault(page_num, {"page": page_num})
            for key in ("source", "word_count", "char_count", "line_item_count", "fields", "result"):
                if key in details:
                    page_entry[key] = details[key]

        if item.get("event") in {"manual_overrides_detected", "review_correction_received"}:
            diff = details.get("correction_diff") or {}
            if isinstance(diff, dict):
                for field_key, change in diff.items():
                    if isinstance(change, dict):
                        manual_overrides.append({
                            "field": field_key,
                            "original": change.get("original"),
                            "corrected": change.get("corrected"),
                        })

    return {
        "extraction_id": extraction_id,
        "filename": extraction.get("filename"),
        "status": extraction.get("status"),
        "vendor_id": extraction.get("vendor_id"),
        "vendor_name": extraction.get("vendor_name"),
        "total_pages": extraction.get("total_pages"),
        "stage_counts": stage_counts,
        "pages": [pages[k] for k in sorted(pages)],
        "manual_overrides": manual_overrides,
        "errors": errors,
        "final_result": _clean(final_result),
        "log_paths": log_paths(extraction_id),
    }
