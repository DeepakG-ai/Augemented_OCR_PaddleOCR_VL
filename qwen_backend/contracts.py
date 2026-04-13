"""
contracts.py -- Normalized document contracts for downstream integrations.
"""
from __future__ import annotations

from typing import Any


def _header_fields(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "line_items"}


def build_purchase_order_contract(extraction: dict) -> dict[str, Any]:
    effective = extraction.get("corrected_result") or extraction.get("result") or {}
    review_meta = extraction.get("correction_meta") or {}

    return {
        "contract_version": "purchase_order.v1",
        "document_type": "purchase_order",
        "extraction_id": extraction["id"],
        "vendor_id": extraction.get("vendor_id"),
        "vendor_name": extraction.get("vendor_name"),
        "filename": extraction.get("filename"),
        "source": {
            "format_type": extraction.get("format_type"),
            "total_pages": extraction.get("total_pages"),
            "status": extraction.get("status"),
        },
        "header": _header_fields(effective) if isinstance(effective, dict) else {},
        "line_items": (effective.get("line_items") or []) if isinstance(effective, dict) else [],
        "review": {
            "canonical_source": "human" if extraction.get("corrected_result") else "machine",
            "fields_changed": review_meta.get("fields_changed", []),
            "reviewed_at": review_meta.get("corrected_at"),
            "reason_code": review_meta.get("reason_code"),
        },
    }
