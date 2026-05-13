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


# -- Subscription alerts log -----------------------------------------------

_ALERTS_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "logs", "page_usage", "alerts.log",
)
_alerts_lock = threading.Lock()


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
    """Append one JSON line to the alerts log when a user hits or nears their limit.

    alert_type:
        - 'warning'  — user is above the warning threshold (e.g. 90%) but not yet blocked
        - 'exceeded' — user is already over their limit and this upload was blocked

    overage = subscription_limit - total_extracted_pages
        negative → over limit (e.g. -3 means 3 pages over)
        positive → pages still available

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
        line = json.dumps(record, default=str) + "\n"
        os.makedirs(os.path.dirname(_ALERTS_LOG_PATH), exist_ok=True)
        with _alerts_lock:
            with open(_ALERTS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        logger.warning("page_logger: failed to write limit alert: %s", exc)
