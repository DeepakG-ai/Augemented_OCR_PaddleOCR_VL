"""
scheduler.py — Compute next_run from cron_expr. No runtime scheduler.

The server stores schedule metadata in the user_schedules table.
The client agent handles all timing and folder scanning.
This module provides a helper to compute the next fire time for UI display.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    """Given a daily cron expression 'minute hour * * *', return the next UTC fire time.

    Returns None if the expression is invalid or missing.
    """
    parts = (cron_expr or "").split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        return None
    try:
        minute, hour = int(parts[0]), int(parts[1])
    except (ValueError, TypeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = after or datetime.now(timezone.utc)
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if now < today else today + timedelta(days=1)
