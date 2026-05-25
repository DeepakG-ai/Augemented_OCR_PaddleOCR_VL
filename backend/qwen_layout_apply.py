"""
qwen_layout_apply.py — Map saved qwen_layout_boxes to field_locations for the Review UI.

Converts Qwen's normalized boxes into pixel-space field_locations:
  - Header fields → strategy "qwen_anchor"
  - Line item columns → expanded into line_item_{row}_{col} keys, strategy "qwen_column_header"

No OCR/pypdfium2 word snapping. The box Qwen returned IS the box we show.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _denormalize_box(normalized: dict, page_width: int, page_height: int) -> list[int]:
    return [
        int(round(normalized["x0"] * page_width)),
        int(round(normalized["y0"] * page_height)),
        int(round(normalized["x1"] * page_width)),
        int(round(normalized["y1"] * page_height)),
    ]


def build_field_locations_from_layout(
    qwen_boxes: dict[str, dict],
    pages_with_words: list[dict],
    result: dict | list | None = None,
    page_results: list[dict] | None = None,
) -> dict | list[dict]:
    """Map saved qwen_layout_boxes to pixel-space field_locations for the Review UI.

    Produces two kinds of entries:
      - Header fields:  {field_key: {page, box, strategy: "qwen_anchor"}}
      - Line item cells: {line_item_{row}_{col}: {page, box, strategy: "qwen_column_header"}}

    Args:
        qwen_boxes: {field_key: {"normalized_box": {x0,y0,x1,y1},
                                  "field_type": "header"|"line_item_column",
                                  "page_number": int}}
        pages_with_words: [{page_number, width, height, ...}]
        result: extraction result (dict or list)
        page_results: per-page raw results for row-to-page mapping

    Returns:
        {field_key: {page, box, strategy, confidence, matched_text}}
        OR a list of such dicts if result is a list (po_per_page).
    """
    if not qwen_boxes:
        return [] if isinstance(result, list) else {}

    pages_by_num = {
        p.get("page_number"): p
        for p in pages_with_words
        if p.get("page_number") is not None
    }

    # Separate header boxes from line-item column boxes
    header_boxes: dict[str, dict] = {}
    column_boxes: dict[str, dict] = {}
    for key, box_info in qwen_boxes.items():
        ft = box_info.get("field_type", "header")
        if ft == "line_item_column":
            column_boxes[key] = box_info
        else:
            header_boxes[key] = box_info

    def _make_loc(box_info: dict, page_num: int, strategy: str, matched_text: str = "") -> dict | None:
        """Build a single field_location entry."""
        page = pages_by_num.get(page_num)
        if not page:
            return None
        pw, ph = page.get("width", 0), page.get("height", 0)
        if pw <= 0 or ph <= 0:
            return None

        nbox = box_info.get("normalized_box")
        if not nbox:
            return None
        if isinstance(nbox, dict):
            nbox_dict = nbox
        elif isinstance(nbox, list) and len(nbox) == 4:
            nbox_dict = {"x0": nbox[0], "y0": nbox[1], "x1": nbox[2], "y1": nbox[3]}
        else:
            return None

        pixel_box = _denormalize_box(nbox_dict, pw, ph)
        return {
            "page": page_num,
            "box": pixel_box,
            "matched_text": matched_text,
            "word_boxes": [],
            "strategy": strategy,
            "confidence": "high",
        }

    # ── po_per_page: one loc-dict per result record ──
    if isinstance(result, list):
        final_list = []
        for i, record in enumerate(result):
            page_num = i + 1
            locs = _build_single_result_locs(
                record, header_boxes, column_boxes, page_num, _make_loc,
            )
            final_list.append(locs)
        mapped_count = sum(len(d) for d in final_list)
        logger.info("qwen_layout_apply (multi): mapped %d fields across %d records", mapped_count, len(result))
        return final_list

    # ── Standard single/multipage result ──
    if not isinstance(result, dict):
        result = {}

    locs: dict[str, dict] = {}

    # Header fields
    for field_key, box_info in header_boxes.items():
        page_num = box_info.get("page_number", 1)
        field_value = result.get(field_key)
        matched_text = str(field_value)[:50] if field_value is not None else ""
        entry = _make_loc(box_info, page_num, "qwen_anchor", matched_text)
        if entry:
            locs[field_key] = entry

    # Line item columns → expand into line_item_{row}_{col} keys
    line_items = result.get("line_items", [])
    if not isinstance(line_items, list):
        line_items = []

    # Build row → page mapping from page_results
    row_page_map = _build_row_page_map(page_results)

    for row_idx, row in enumerate(line_items):
        if not isinstance(row, dict):
            continue
        row_page = row_page_map.get(row_idx, 1)

        for col_name, cell_value in row.items():
            if cell_value is None:
                continue
            comp_key = f"line_item_{row_idx}_{col_name}"

            # Find the column header box
            col_box_info = column_boxes.get(col_name)
            if col_box_info:
                box_page = col_box_info.get("page_number", 1)
                # Use the row's page for page assignment, but the column header box coords
                entry = _make_loc(col_box_info, row_page, "qwen_column_header", col_name)
                if entry:
                    locs[comp_key] = entry
                else:
                    locs[comp_key] = {
                        "page": row_page,
                        "box": None,
                        "strategy": "qwen_column_header_missing",
                        "confidence": "low",
                        "matched_text": col_name,
                    }
            else:
                locs[comp_key] = {
                    "page": row_page,
                    "box": None,
                    "strategy": "qwen_column_header_missing",
                    "confidence": "low",
                    "matched_text": col_name,
                }

    logger.info(
        "qwen_layout_apply: mapped %d field_locations (%d headers, %d line-item cells)",
        len(locs),
        sum(1 for k in locs if not k.startswith("line_item_")),
        sum(1 for k in locs if k.startswith("line_item_")),
    )
    return locs


def _build_single_result_locs(
    record: dict,
    header_boxes: dict,
    column_boxes: dict,
    page_num: int,
    make_loc_fn,
) -> dict:
    """Build field_locations for a single po_per_page record."""
    locs: dict = {}
    if not isinstance(record, dict):
        return locs

    # Headers
    for field_key, box_info in header_boxes.items():
        field_value = record.get(field_key)
        matched_text = str(field_value)[:50] if field_value is not None else ""
        entry = make_loc_fn(box_info, page_num, "qwen_anchor", matched_text)
        if entry:
            locs[field_key] = entry

    # Line items
    line_items = record.get("line_items", [])
    if not isinstance(line_items, list):
        line_items = []
    for row_idx, row in enumerate(line_items):
        if not isinstance(row, dict):
            continue
        for col_name, cell_value in row.items():
            if cell_value is None:
                continue
            comp_key = f"line_item_{row_idx}_{col_name}"
            col_box_info = column_boxes.get(col_name)
            if col_box_info:
                entry = make_loc_fn(col_box_info, page_num, "qwen_column_header", col_name)
                if entry:
                    locs[comp_key] = entry
    return locs


def _build_row_page_map(page_results: list[dict] | None) -> dict[int, int]:
    """Map each row index in the merged line_items to its source page number.

    Uses the per-page results to count how many line items came from each page,
    then assigns row indices sequentially.
    """
    row_page: dict[int, int] = {}
    if not page_results:
        return row_page

    row_offset = 0
    for pr in sorted(page_results, key=lambda p: p.get("_page", 0)):
        if "_error" in pr:
            continue
        pr_fields = pr.get("fields", pr)
        if not isinstance(pr_fields, dict):
            continue
        pr_items = pr_fields.get("line_items", [])
        if not isinstance(pr_items, list):
            continue
        page_num = pr.get("_page", 1)
        for i in range(len(pr_items)):
            row_page[row_offset + i] = page_num
        row_offset += len(pr_items)

    return row_page
