"""
field_mapper.py -- ERP field mapping.

Renames a vendor's extracted (Qwen) field names to the program's fixed
canonical "custom" fields before the JSON is sent to a client system.

A mapping is stored once per vendor/template. `header_map` and `line_map`
are {source_field_name: canonical_target_name} dicts. The merge logic
(single_po_multipage / po_per_page) runs upstream — this only renames keys.
"""
from __future__ import annotations

from typing import Any

# Fixed canonical target fields the program ("custom" system) expects.
HEADER_TARGETS: list[str] = [
    "vendor_name",
    "vendor_address",
    "invoice_number",
    "invoice_date",
    "po_number",
    "invoice_total",
    "invoice_subtotal",
    "tax_amount",
    "freight_amount",
    "terms",
]

LINE_TARGETS: list[str] = [
    "item",
    "line_description",
    "quantity_ordered",
    "quantity_received",
    "unit_price",
    "line_total",
    "uom",
]


def _map_document(doc: dict, header_map: dict, line_map: dict) -> dict:
    """Map a single PO document to the canonical shape.

    Output always carries every canonical target key; unmapped targets are
    null. Source fields with no mapping are dropped.
    """
    out: dict[str, Any] = {t: None for t in HEADER_TARGETS}
    for source_field, target_field in header_map.items():
        if target_field in out:
            out[target_field] = doc.get(source_field)

    items: list[dict] = []
    for raw_item in doc.get("line_items") or []:
        if not isinstance(raw_item, dict):
            continue
        mapped_item: dict[str, Any] = {t: None for t in LINE_TARGETS}
        for source_field, target_field in line_map.items():
            if target_field in mapped_item:
                mapped_item[target_field] = raw_item.get(source_field)
        items.append(mapped_item)
    out["items"] = items
    return out


def apply_mapping(result: Any, mapping: dict) -> Any:
    """Apply a stored field mapping to a merged extraction result.

    Handles both result shapes:
      - dict  -> single_po_multipage / single_page  (returns a dict)
      - list  -> po_per_page                        (returns a list of dicts)
    """
    header_map = mapping.get("header_map") or {}
    line_map = mapping.get("line_map") or {}

    if isinstance(result, list):
        return [
            _map_document(doc if isinstance(doc, dict) else {}, header_map, line_map)
            for doc in result
        ]
    if isinstance(result, dict):
        return _map_document(result, header_map, line_map)
    return result


def detect_renames(
    old_fields: list[str] | None, new_fields: list[str] | None,
) -> list[tuple[str, str]]:
    """Detect field renames by position.

    If the field list is the same length, any slot whose name changed is
    treated as a rename. A length change is an add/remove (not a rename) and
    yields no pairs — positions can no longer be aligned safely.
    """
    old_fields = old_fields or []
    new_fields = new_fields or []
    if len(old_fields) != len(new_fields):
        return []
    return [(o, n) for o, n in zip(old_fields, new_fields) if o != n]


def apply_renames(field_map: dict, renames: list[tuple[str, str]]) -> dict:
    """Return a copy of field_map with renamed source keys carried forward."""
    updated = dict(field_map)
    for old_name, new_name in renames:
        if old_name in updated:
            updated[new_name] = updated.pop(old_name)
    return updated
