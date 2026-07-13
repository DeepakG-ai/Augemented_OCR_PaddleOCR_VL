"""
Tests for the missing-fields detection logic and qwen_layout_apply.

Covers:
 - Empty qwen_layout_boxes → all template fields are "missing"
 - All fields present → nothing missing → BBox Agent skipped
 - Partial match → only missing fields forwarded to BBox Agent
 - build_field_locations_from_layout: correct box denormalisation and matched_text
 - build_field_locations_from_layout: fields with no matching words are omitted
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.qwen_layout_apply import build_field_locations_from_layout


def _page(page_number: int = 1, width: int = 800, height: int = 1000, words=None) -> dict:
    return {"page_number": page_number, "width": width, "height": height, "words": words or []}


def _box_row(field_key: str, norm_box: list, field_type: str = "header", page_number: int = 1) -> dict:
    return {
        "field_key": field_key,
        "normalized_box": norm_box,
        "field_type": field_type,
        "page_number": page_number,
    }


class MissingFieldsDetectionTests(unittest.TestCase):
    """Simulate the missing-fields check that worker._process_llm performs."""

    def _missing(self, req_header, req_items, existing_keys):
        """Return (missing_header, missing_items) given existing DB keys."""
        missing_header = [f for f in req_header if f not in existing_keys]
        missing_items  = [f for f in req_items  if f not in existing_keys]
        return missing_header, missing_items

    def test_empty_table_all_missing(self):
        mh, mi = self._missing(["supplier", "bill_to"], ["qty", "amount"], set())
        self.assertEqual(sorted(mh), ["bill_to", "supplier"])
        self.assertEqual(sorted(mi), ["amount", "qty"])

    def test_all_present_nothing_missing(self):
        existing = {"supplier", "bill_to", "qty", "amount"}
        mh, mi = self._missing(["supplier", "bill_to"], ["qty", "amount"], existing)
        self.assertEqual(mh, [])
        self.assertEqual(mi, [])

    def test_partial_match_only_new_fields(self):
        existing = {"supplier", "qty"}
        mh, mi = self._missing(["supplier", "bill_to"], ["qty", "amount"], existing)
        self.assertEqual(mh, ["bill_to"])
        self.assertEqual(mi, ["amount"])

    def test_single_new_field_added_to_template(self):
        existing = {"supplier", "bill_to", "qty"}
        mh, mi = self._missing(["supplier", "bill_to", "new_field"], ["qty"], existing)
        self.assertEqual(mh, ["new_field"])
        self.assertEqual(mi, [])


class BuildFieldLocationsFromLayoutTests(unittest.TestCase):

    def test_basic_denormalisation(self):
        qwen_boxes = {
            "supplier": _box_row("supplier", [0.1, 0.2, 0.4, 0.26]),
        }
        words = [{"text": "ACME", "box": [80, 195, 320, 258], "score": 0.99}]
        pages = [_page(words=words)]
        locs = build_field_locations_from_layout(qwen_boxes, pages)
        self.assertIn("supplier", locs)
        loc = locs["supplier"]
        self.assertEqual(loc["strategy"], "qwen_anchor")
        self.assertEqual(loc["confidence"], "high")
        self.assertEqual(loc["page"], 1)
        self.assertIsInstance(loc["box"], list)
        self.assertEqual(len(loc["box"]), 4)

    def test_matched_text_from_result(self):
        # matched_text is populated from the extraction result when available
        qwen_boxes = {"supplier": _box_row("supplier", [0.0, 0.0, 0.5, 0.05])}
        words = [
            {"text": "ACME", "box": [5, 5, 100, 45], "score": 0.99},
            {"text": "Corp", "box": [110, 5, 200, 45], "score": 0.98},
        ]
        pages = [_page(words=words)]
        result = {"supplier": "ACME Corp"}
        locs = build_field_locations_from_layout(qwen_boxes, pages, result=result)
        self.assertIn("supplier", locs)
        self.assertEqual(locs["supplier"]["matched_text"], "ACME Corp")

    def test_field_always_present_regardless_of_words(self):
        # Pure pass-through: Qwen's box IS the answer — never silently drop fields
        qwen_boxes = {"supplier": _box_row("supplier", [0.9, 0.9, 1.0, 1.0])}
        words = [{"text": "ACME", "box": [5, 5, 100, 45], "score": 0.99}]
        pages = [_page(words=words)]
        locs = build_field_locations_from_layout(qwen_boxes, pages)
        self.assertIn("supplier", locs)

    def test_empty_qwen_boxes_returns_empty(self):
        locs = build_field_locations_from_layout({}, [_page()])
        self.assertEqual(locs, {})

    def test_missing_page_skipped(self):
        qwen_boxes = {"supplier": _box_row("supplier", [0.1, 0.1, 0.5, 0.3], page_number=99)}
        pages = [_page(page_number=1)]  # page 99 not in pages
        locs = build_field_locations_from_layout(qwen_boxes, pages)
        self.assertNotIn("supplier", locs)

    def test_normalized_box_as_list_accepted(self):
        qwen_boxes = {"bill_to": {"normalized_box": [0.0, 0.0, 0.5, 0.1], "field_type": "header", "page_number": 1}}
        words = [{"text": "Buyer", "box": [5, 5, 200, 90], "score": 0.99}]
        pages = [_page(words=words)]
        locs = build_field_locations_from_layout(qwen_boxes, pages)
        self.assertIn("bill_to", locs)

    def test_multiple_fields_on_multiple_pages(self):
        qwen_boxes = {
            "supplier": _box_row("supplier", [0.0, 0.0, 0.5, 0.05], page_number=1),
            "total":    _box_row("total",    [0.5, 0.9, 1.0, 1.0],  page_number=2),
        }
        words_p1 = [{"text": "ACME", "box": [5, 5, 390, 48], "score": 0.99}]
        words_p2 = [{"text": "100.00", "box": [405, 905, 795, 995], "score": 0.99}]
        pages = [_page(1, words=words_p1), _page(2, words=words_p2)]
        locs = build_field_locations_from_layout(qwen_boxes, pages)
        self.assertIn("supplier", locs)
        self.assertEqual(locs["supplier"]["page"], 1)
        self.assertIn("total", locs)
        self.assertEqual(locs["total"]["page"], 2)

    def test_line_item_expansion(self):
        """Column header boxes should be expanded into line_item_{row}_{col} keys."""
        qwen_boxes = {
            "supplier": _box_row("supplier", [0.0, 0.0, 0.3, 0.05]),
            "qty":      _box_row("qty", [0.0, 0.3, 0.1, 0.35], field_type="line_item_column"),
            "description": _box_row("description", [0.1, 0.3, 0.5, 0.35], field_type="line_item_column"),
        }
        pages = [_page()]
        result = {
            "supplier": "ACME",
            "line_items": [
                {"qty": 72, "description": "Widget A"},
                {"qty": 24, "description": "Widget B"},
            ],
        }
        locs = build_field_locations_from_layout(qwen_boxes, pages, result=result)

        # Header field
        self.assertIn("supplier", locs)
        self.assertEqual(locs["supplier"]["strategy"], "qwen_anchor")

        # Line items expanded
        self.assertIn("line_item_0_qty", locs)
        self.assertIn("line_item_0_description", locs)
        self.assertIn("line_item_1_qty", locs)
        self.assertIn("line_item_1_description", locs)
        self.assertEqual(locs["line_item_0_qty"]["strategy"], "qwen_column_header")
        self.assertEqual(locs["line_item_1_description"]["strategy"], "qwen_column_header")
        self.assertEqual(locs["line_item_0_qty"]["page"], 1)

        # Raw column key should NOT be in output
        self.assertNotIn("qty", locs)
        self.assertNotIn("description", locs)


if __name__ == "__main__":
    unittest.main()
