"""
contracts.py -- Normalized document contracts for downstream integrations.
"""
from __future__ import annotations

from typing import Any


def _header_fields(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "line_items"}


def _document_payload(result: Any, document_index: int) -> dict[str, Any]:
    if not isinstance(result, dict):
        result = {}
    return {
        "document_index": document_index,
        "header": _header_fields(result),
        "line_items": result.get("line_items") or [],
    }


def _multi_document_export_rows(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        base_row = {"document_index": document.get("document_index")}
        header = document.get("header") or {}
        if isinstance(header, dict):
            base_row.update(header)

        line_items = document.get("line_items") or []
        if not line_items:
            empty_row = dict(base_row)
            empty_row["line_items"] = ""
            rows.append(empty_row)
            continue

        for item in line_items:
            row = dict(base_row)
            if isinstance(item, dict):
                row.update(item)
            else:
                row["line_items"] = item
            rows.append(row)
    return rows


def build_purchase_order_contract(extraction: dict) -> dict[str, Any]:
    effective = extraction.get("corrected_result") or extraction.get("result") or {}
    review_meta = extraction.get("correction_meta") or {}

    documents: list[dict[str, Any]]
    header: dict[str, Any]
    line_items: list[Any]

    if isinstance(effective, list):
        documents = [_document_payload(result, idx + 1) for idx, result in enumerate(effective)]
        header = {"document_count": len(documents)}
        line_items = _multi_document_export_rows(documents)
    else:
        primary = effective if isinstance(effective, dict) else {}
        documents = [_document_payload(primary, 1)]
        header = _header_fields(primary)
        line_items = primary.get("line_items") or []

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
        "document_count": len(documents),
        "documents": documents,
        "header": header,
        "line_items": line_items,
        "review": {
            "canonical_source": "human" if extraction.get("corrected_result") else "machine",
            "fields_changed": review_meta.get("fields_changed", []),
            "reviewed_at": review_meta.get("corrected_at"),
            "reason_code": review_meta.get("reason_code"),
        },
    }
