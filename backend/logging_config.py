"""
Central logging configuration for the backend.

This file is intentionally not named logging.py because that would shadow
Python's standard-library logging module.

Architecture:
  - Console handler (stdout) for docker logs
  - ExtractionLogHandler: tees every log line into extraction_{id}_{filename}.log
  - current_extraction_id / current_extraction_filename: context vars set by the worker
  - timed(): a simple context manager that tracks elapsed time
"""
from __future__ import annotations

import contextvars
import logging
import logging.config
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import LOG_LEVEL, PIPELINE_LOG_DIR as _PIPELINE_LOG_DIR_CFG

_PIPELINE_LOG_DIR = _PIPELINE_LOG_DIR_CFG
_EXTRACTION_LOG_DIR = _PIPELINE_LOG_DIR / "extractions"

# ── Context vars (set by worker.process_job) ──
current_extraction_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_extraction_id", default=None,
)
current_extraction_filename: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_extraction_filename", default=None,
)

_LOG_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


# ── Per-extraction file tee ──

def _safe_stem(name: str | None) -> str:
    """Sanitize a filename into a safe log-file stem (no extension, no slashes)."""
    if not name:
        return ""
    stem = Path(name).stem
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", stem)[:60]


def _extraction_log_path(ext_id: int, filename: str | None = None) -> Path:
    safe = _safe_stem(filename)
    name = f"extraction_{ext_id}_{safe}.log" if safe else f"extraction_{ext_id}.log"
    return _EXTRACTION_LOG_DIR / name


class ExtractionLogHandler(logging.Handler):
    """Root-level handler that tees every log record into the active extraction's .log file."""

    def emit(self, record: logging.LogRecord) -> None:
        ext_id = current_extraction_id.get()
        if ext_id is None:
            return
        try:
            msg = self.format(record)
            fname = current_extraction_filename.get()
            per_file = _extraction_log_path(ext_id, fname)
            _EXTRACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with per_file.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            self.handleError(record)


# ── Setup ──

def configure_logging() -> None:
    level = LOG_LEVEL
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": _LOG_FMT, "datefmt": _LOG_DATEFMT},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
            },
        },
        "root": {"handlers": ["console"], "level": level},
    })
    # Install per-extraction tee on root logger (idempotent)
    root = logging.getLogger()
    if not any(isinstance(h, ExtractionLogHandler) for h in root.handlers):
        tee = ExtractionLogHandler()
        tee.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
        tee.setLevel(level)
        root.addHandler(tee)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ── Simple timer ──

@contextmanager
def timed(label: str) -> Iterator[dict[str, Any]]:
    """Context manager that measures elapsed time.

    Usage::

        with plog.timed("document_rendered") as t:
            result = render(...)
        logger.info("Rendered %d pages (%.0fms)", len(result), t["ms"])

    The yielded dict gets ``ms`` set on exit.
    """
    ctx: dict[str, Any] = {}
    start = time.perf_counter()
    try:
        yield ctx
    finally:
        ctx["ms"] = round((time.perf_counter() - start) * 1000, 1)


# ── Log path helpers (used by API) ──

def log_paths(extraction_id: int | None = None) -> dict[str, str]:
    paths: dict[str, str] = {}
    if extraction_id is not None:
        matches = list(_EXTRACTION_LOG_DIR.glob(f"extraction_{int(extraction_id)}*.log"))
        if matches:
            paths["extraction_log"] = str(matches[0])
        else:
            paths["extraction_log"] = str(_extraction_log_path(int(extraction_id)))
    return paths


def read_extraction_events(extraction_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the timeline for one extraction from its log file."""
    matches = list(_EXTRACTION_LOG_DIR.glob(f"extraction_{int(extraction_id)}*.log"))
    if not matches:
        return []
    path = matches[0]

    _LINE_RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
        r" \| (?P<level>[A-Z]+)"
        r" +\| .+ \| (?P<message>.*)$"
    )
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            m = _LINE_RE.match(raw.rstrip("\n"))
            if m:
                events.append({
                    "ts": m.group("ts"),
                    "level": m.group("level"),
                    "message": m.group("message"),
                })
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def build_extraction_trace_summary(
    extraction_id: int,
    events: list[dict[str, Any]],
    *,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact trace summary from recorded events."""
    extraction = extraction or {}
    errors = [e for e in events if e.get("level") == "ERROR"]
    return {
        "extraction_id": extraction_id,
        "filename": extraction.get("filename"),
        "status": extraction.get("status"),
        "vendor_id": extraction.get("vendor_id"),
        "total_pages": extraction.get("total_pages"),
        "event_count": len(events),
        "errors": errors,
        "timeline": events,
        "log_paths": log_paths(extraction_id),
    }
