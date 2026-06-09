"""
layout_key.py - Compute the spatial-memory grouping key.

Phase-one spatial memory assumes one stable document format per client/template.
The database still calls this value layout_key, but the effective key is just:

    vendor_id + template_id

The field name and page number are stored in their own spatial_memory columns,
so the full reusable-memory identity is:

    vendor_id + template_id + field_key + page_number
"""
from __future__ import annotations

import logging

logger = logging.getLogger("layout_key")


def compute_layout_key(
    vendor_id: str,
    template_id: int | None,
    page_results: list[dict] | None = None,
) -> str:
    """Compute a deterministic key for spatial memory.

    Args:
        vendor_id: The vendor identifier.
        template_id: The template ID (may be None).
        page_results: Accepted for backwards-compatible call sites. It is not
            used because Qwen anchor boxes can shift slightly between otherwise
            identical documents.

    Returns:
        A stable, readable grouping key.
    """
    _ = page_results
    key = f"{str(vendor_id).strip()}:{template_id or 'default'}"

    logger.debug(
        "Layout key: %s (vendor=%s, template=%s)",
        key, vendor_id, template_id,
    )
    return key
