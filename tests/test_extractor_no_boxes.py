"""
Verify that the Fields Agent prompt contains no bbox/boxes references.

The system prompt and user message must be fields-only after the two-agent split.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.extractor as extractor


HEADER_FIELDS = ["supplier", "bill_to", "ship_to", "po_number", "order_date"]
LINE_ITEM_FIELDS = ["item_code", "description", "quantity", "unit_price", "amount"]


class ExtractorNoBboxTests(unittest.TestCase):

    def setUp(self):
        self.system_prompt = extractor.build_system_prompt(
            HEADER_FIELDS, LINE_ITEM_FIELDS,
            instructions=None, rules=[], format_type="single_po_multipage",
        )
        self.user_message = extractor.build_user_message(
            HEADER_FIELDS, LINE_ITEM_FIELDS, page_num=1, total_pages=3,
        )

    def test_system_prompt_no_boxes_keyword(self):
        self.assertNotIn("boxes", self.system_prompt.lower())

    def test_system_prompt_no_bounding_keyword(self):
        self.assertNotIn("bounding", self.system_prompt.lower())

    def test_user_message_no_boxes_key(self):
        import json
        # The JSON shape must have "fields" at the top level, not "boxes"
        self.assertNotIn('"boxes"', self.user_message)

    def test_user_message_has_fields_template(self):
        self.assertIn('"fields"', self.user_message)

    def test_user_message_all_header_fields_present(self):
        for field in HEADER_FIELDS:
            self.assertIn(field, self.user_message)

    def test_user_message_all_line_item_fields_present(self):
        for field in LINE_ITEM_FIELDS:
            self.assertIn(field, self.user_message)

    def test_system_prompt_version_v4(self):
        self.assertEqual(extractor.PROMPT_VERSION, "v4.0")

    def test_prompt_with_gold_examples_no_boxes(self):
        gold = [{"correction_diff": {"supplier": "ACME Corp"}}]
        prompt = extractor.build_system_prompt(
            HEADER_FIELDS, LINE_ITEM_FIELDS,
            instructions=None, rules=[], format_type="single_po_multipage",
            gold_examples=gold,
        )
        self.assertNotIn("boxes", prompt.lower())
        self.assertIn("ACME Corp", prompt)

    def test_auto_extract_mode_no_bbox(self):
        # When no fields are passed (auto-extract mode)
        msg = extractor.build_user_message([], [], page_num=1, total_pages=1)
        self.assertNotIn("boxes", msg.lower())
        self.assertNotIn("bounding", msg.lower())


if __name__ == "__main__":
    unittest.main()
