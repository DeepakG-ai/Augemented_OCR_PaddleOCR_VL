"""
page_logger.py -- Append-only page usage log for billing and SLA tracking.

One JSON line per extraction written to logs/page_usage/log.txt.
Never raises — billing log failures must not crash the pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "logs", "page_usage", "log.txt",
)
_lock = threading.Lock()


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
    """Append one JSON line to the page usage log. Best-effort — never raises."""
    try:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(record, default=str) + "\n"
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with _lock:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        logger.warning("page_logger: failed to write usage log: %s", exc)
