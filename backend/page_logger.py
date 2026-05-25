"""
page_logger.py -- Append-only page usage log for billing and SLA tracking.

One human-readable line per extraction written to logs/page_usage/log.txt.
Never raises — billing log failures must not crash the pipeline.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_LOG_PATH = os.environ.get(
    "PAGE_USAGE_LOG_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "page_usage", "log.txt"),
)

_ALERTS_LOG_PATH = os.environ.get(
    "PAGE_ALERTS_LOG_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "page_usage", "alerts.log"),
)

# Persistent open handles — created on first use, reused on subsequent calls.
# Tests reset these to None in setUp to force re-open against a temp path.
_log_file = None
_alerts_file = None
_handle_lock = threading.Lock()


def _get_log_file():
    """Return the persistent append handle for the usage log, opening it on first call."""
    global _log_file
    if _log_file is None or _log_file.closed:
        with _handle_lock:
            if _log_file is None or _log_file.closed:
                os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
                _log_file = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
    return _log_file


def _get_alerts_file():
    """Return the persistent append handle for the alerts log, opening it on first call."""
    global _alerts_file
    if _alerts_file is None or _alerts_file.closed:
        with _handle_lock:
            if _alerts_file is None or _alerts_file.closed:
                os.makedirs(os.path.dirname(_ALERTS_LOG_PATH), exist_ok=True)
                _alerts_file = open(_ALERTS_LOG_PATH, "a", encoding="utf-8", buffering=1)
    return _alerts_file


def _fmt_value(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, (list, tuple)):
        return "[" + "; ".join(_fmt_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt_value(v)}" for k, v in value.items()) + "}"
    text = str(value).replace("\n", " ").strip()
    return text if text else "-"


def _format_record(record: dict, *, prefix: str) -> str:
    ts = record.get("ts") or datetime.now(timezone.utc).isoformat()
    ordered_keys = [
        "status",
        "extraction_id",
        "filename",
        "vendor_id",
        "attempt_number",
        "total_pages",
        "billable_pages",
        "digital_pages",
        "scanned_pages",
        "qwen_extracted_pages",
        "qwen_failed_pages",
        "qwen_skipped_pages",
        "field_count",
        "empty_result",
        "duration_ms",
        "errors",
    ]
    seen = set(ordered_keys) | {"ts"}
    parts = [f"{key}={_fmt_value(record.get(key))}" for key in ordered_keys if key in record]
    parts.extend(f"{key}={_fmt_value(value)}" for key, value in record.items() if key not in seen)
    return f"{ts} | {prefix} | " + "  ".join(parts)


def classify_error_type(error: str) -> str:
    e = error.lower()
    if "timeout" in e:
        return "timeout"
    if "http" in e or "connect" in e or "network" in e:
        return "llm_http_error"
    if "json" in e or "decode" in e or "parse" in e:
        return "invalid_json"
    return "extraction_error"


def count_result_fields(result) -> int:
    """Count non-null, non-empty header fields in the extraction result."""
    if isinstance(result, dict):
        return sum(
            1 for k, v in result.items()
            if k != "line_items" and v is not None and v != ""
        )
    if isinstance(result, list):
        return sum(
            sum(1 for k, v in r.items() if k != "line_items" and v is not None and v != "")
            for r in result if isinstance(r, dict)
        )
    return 0


def append_log(record: dict) -> None:
    """Append one human-readable line to the page usage log. Best-effort; never raises."""
    try:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = _format_record(record, prefix="PAGE_USAGE") + "\n"
        f = _get_log_file()
        f.write(line)
        f.flush()
    except Exception as exc:
        logger.warning("page_logger: failed to write usage log: %s", exc)


def log_limit_alert(
    *,
    user_id: str,
    email: str | None = None,
    total_extracted_pages: int,
    subscription_limit: int,
    alert_type: str = "warning",
    extraction_id: int | None = None,
    filename: str | None = None,
) -> None:
    """Append one line to the alerts log when a user hits or nears their limit.

    alert_type: 'warning' | 'exceeded' | 'small_overage'
    Never raises — billing alerts must not crash the pipeline.
    """
    try:
        overage = subscription_limit - total_extracted_pages
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "alert_type": alert_type,
            "user_id": user_id,
            "email": email,
            "filename": filename,
            "subscription_limit": subscription_limit,
            "total_extracted_pages": total_extracted_pages,
            "overage": overage,
        }
        if extraction_id is not None:
            record["extraction_id"] = extraction_id
        line = _format_record(record, prefix="LIMIT_ALERT") + "\n"
        f = _get_alerts_file()
        f.write(line)
        f.flush()
    except Exception as exc:
        logger.warning("page_logger: failed to write limit alert: %s", exc)
