"""
qwen_bbox_parser.py -- Parse Qwen v3 {fields, boxes} output into field_locations.

Converts Qwen anchor bounding boxes into the field_locations dict consumed by
the Review UI. Header fields get their own anchor box; line-item rows all
share the column header box for their column.

PaddleOCR is NOT used here — it remains available for drag-and-drop only.
"""
from __future__ import annotations

from typing import Any

try:
    from .logging_config import get_logger
except ImportError:
    from logging_config import get_logger

logger = get_logger(__name__)


def _valid_box(box: Any) -> bool:
    """Return True if box is a 4-element list of numbers with positive area."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        x0, y0, x1, y1 = [int(v) for v in box]
        return x1 > x0 and y1 > y0
    except (ValueError, TypeError):
        return False


def _denormalize_box(
    box: list[int],
    img_width: int,
    img_height: int
) -> list[int]:
    """
    Qwen3-VL outputs bbox in 0-1000 normalized grid.
    Convert to absolute pixel coords of the ORIGINAL image.
    """
    x0, y0, x1, y1 = box
    return [
        int(x0 / 1000 * img_width),
        int(y0 / 1000 * img_height),
        int(x1 / 1000 * img_width),
        int(y1 / 1000 * img_height),
    ]


def parse_qwen_page_result(page_result: dict) -> tuple[dict, dict]:
    """
    Extract fields and boxes from a single page's Qwen result.

    Returns:
        (fields_dict, boxes_dict)
        Both may be empty dicts if the keys are missing or invalid.
    """
    fields = page_result.get("fields")
    boxes = page_result.get("boxes")

    if not isinstance(fields, dict):
        fields = {}
    if not isinstance(boxes, dict):
        boxes = {}

    return fields, boxes


def build_field_locations(
    merged_result: dict,
    page_results: list[dict] | None = None,
    pages: list[dict] | None = None,
) -> dict[str, dict]:
    """
    Build field_locations from a merged Qwen v3 result.

    For header fields:
        - Uses the anchor box from the first page that has it in boxes.
        - Strategy: "qwen_anchor"

    For line items:
        - All rows in one column share the column header box.
        - Strategy: "qwen_column_header"

    Args:
        merged_result: The merged extraction result (just fields now).
        page_results: The per-page raw results for page association.
        pages: The db rows for pages, providing orig_width and orig_height.

    Returns:
        {field_key: {page, box, strategy, confidence, matched_text}}
    """
    locations: dict[str, dict] = {}

    if not page_results:
        return {}
    
    # Check if this is a v3 extraction (has 'boxes' in page_results)
    is_v3 = any("boxes" in pr for pr in page_results)
    if not is_v3:
        logger.info("Result is not v3 format — skipping qwen_bbox_parser")
        return {}

    fields = merged_result  # The merged result is now purely the fields

    # Map page number to dimensions
    page_dims = {}
    if pages:
        for p in pages:
            w = p.get("orig_width") or p.get("width") or 1000
            h = p.get("orig_height") or p.get("height") or 1000
            page_dims[p.get("page_number", 1)] = (w, h)

    # Build a flat lookup: key -> {page, box}
    anchor_lookup: dict[str, dict] = {}
    for pr in page_results:
        page_num = pr.get("_page", 1)
        page_boxes = pr.get("boxes", {})
        if not isinstance(page_boxes, dict):
            continue
        
        pw, ph = page_dims.get(page_num, (1000, 1000))
        
        for key, box in page_boxes.items():
            if key not in anchor_lookup and _valid_box(box):
                anchor_lookup[key] = {
                    "page": page_num,
                    "box": _denormalize_box([int(v) for v in box], pw, ph),
                }

    # ── Header fields ──
    _meta_keys = {"line_items", "_format", "boxes"}
    for field_name, field_value in fields.items():
        if field_name in _meta_keys:
            continue
        if field_value is None:
            continue

        anchor = anchor_lookup.get(field_name)
        if anchor:
            locations[field_name] = {
                "page": anchor["page"],
                "box": anchor["box"],
                "strategy": "qwen_anchor",
                "confidence": "high",
                "matched_text": str(field_value)[:50],
            }
            logger.debug("Header '%s' → qwen_anchor page=%d box=%s",
                         field_name, anchor["page"], anchor["box"])
        else:
            # Field has a value but no anchor box from Qwen
            locations[field_name] = {
                "page": 1,
                "box": None,
                "strategy": "qwen_anchor_missing",
                "confidence": "low",
                "matched_text": str(field_value)[:50],
            }
            logger.debug("Header '%s' → no anchor box from Qwen", field_name)

    # ── Line items ──
    line_items = fields.get("line_items", [])
    if not isinstance(line_items, list):
        line_items = []

    # Determine which page each row's column header belongs to.
    # For multi-page: rows from page N use page N's column header box.
    # Build page-to-column-box mapping.
    page_column_boxes: dict[int, dict[str, list[int]]] = {}
    for pr in page_results:
        page_num = pr.get("_page", 1)
        page_boxes = pr.get("boxes", {})
        if isinstance(page_boxes, dict):
            pw, ph = page_dims.get(page_num, (1000, 1000))
            col_boxes = {}
            for key, box in page_boxes.items():
                if _valid_box(box):
                    col_boxes[key] = _denormalize_box([int(v) for v in box], pw, ph)
            page_column_boxes[page_num] = col_boxes

    # Figure out which page each row belongs to.
    # In v3, page_results tells us how many line items came from each page.
    row_page_map: dict[int, int] = {}
    if page_results:
        row_offset = 0
        for pr in sorted(page_results, key=lambda p: p.get("_page", 0)):
            if "_error" in pr:
                continue
            pr_fields = pr.get("fields", {})
            pr_items = pr_fields.get("line_items", [])
            if not isinstance(pr_items, list):
                continue
            page_num = pr.get("_page", 1)
            for i in range(len(pr_items)):
                row_page_map[row_offset + i] = page_num
            row_offset += len(pr_items)

    for row_idx, row in enumerate(line_items):
        if not isinstance(row, dict):
            continue

        row_page = row_page_map.get(row_idx, 1)

        for col_name, cell_value in row.items():
            if cell_value is None:
                continue

            key_name = f"line_item_{row_idx}_{col_name}"

            # Find column header box for this page
            page_boxes = page_column_boxes.get(row_page, {})
            col_box = page_boxes.get(col_name)

            # Fallback: try the first page that has this column
            if not col_box:
                col_box = anchor_lookup.get(col_name, {}).get("box")
                if col_box:
                    row_page = anchor_lookup.get(col_name, {}).get("page", row_page)

            if col_box:
                locations[key_name] = {
                    "page": row_page,
                    "box": col_box,
                    "strategy": "qwen_column_header",
                    "confidence": "high",
                    "matched_text": col_name,
                }
            else:
                locations[key_name] = {
                    "page": row_page,
                    "box": None,
                    "strategy": "qwen_column_header_missing",
                    "confidence": "low",
                    "matched_text": col_name,
                }

    logger.info(
        "Built %d field_locations from Qwen boxes (%d headers, %d line-item cells)",
        len(locations),
        sum(1 for k in locations if not k.startswith("line_item_")),
        sum(1 for k in locations if k.startswith("line_item_")),
    )

    return locations
