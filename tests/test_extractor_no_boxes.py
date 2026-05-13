"""
Verify that the Fields Agent prompt (page 2+) contains no bbox/boxes references.

After the v5.2 single-agent refactor:
  - Page 1 (include_boxes=True) DOES contain box instructions.
  - Page 2+ (include_boxes=False, the default) must remain fields-only.
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
    """Page 2+ prompt must be strictly fields-only (no boxes)."""

    def setUp(self):
        # Default (include_boxes=False) = page 2+ behaviour
        self.system_prompt = extractor.build_system_prompt(
            HEADER_FIELDS, LINE_ITEM_FIELDS,
            instructions=None, rules=[], format_type="single_po_multipage",
            include_boxes=False,
        )
        self.user_message = extractor.build_user_message(
            HEADER_FIELDS, LINE_ITEM_FIELDS, page_num=2, total_pages=3,
            include_boxes=False,
        )

    def test_system_prompt_no_boxes_keyword(self):
        self.assertNotIn("boxes", self.system_prompt.lower())

    def test_system_prompt_no_bounding_keyword(self):
        self.assertNotIn("bounding", self.system_prompt.lower())

    def test_user_message_no_boxes_key(self):
        self.assertNotIn('"boxes"', self.user_message)

    def test_user_message_has_fields_template(self):
        self.assertIn('"fields"', self.user_message)

    def test_user_message_all_header_fields_present(self):
        for field in HEADER_FIELDS:
            self.assertIn(field, self.user_message)

    def test_user_message_all_line_item_fields_present(self):
        for field in LINE_ITEM_FIELDS:
            self.assertIn(field, self.user_message)

    def test_system_prompt_version_v5(self):
        self.assertEqual(extractor.PROMPT_VERSION, "v5.2")

    def test_prompt_with_gold_examples_no_boxes_or_value_leak(self):
        gold = [{"correction_diff": {"supplier": {"original": "Wrong Co", "corrected": "ACME Corp"}}}]
        prompt = extractor.build_system_prompt(
            HEADER_FIELDS, LINE_ITEM_FIELDS,
            instructions=None, rules=[], format_type="single_po_multipage",
            gold_examples=gold, include_boxes=False,
        )
        self.assertNotIn("boxes", prompt.lower())
        self.assertIn("supplier", prompt)
        self.assertIn("Values are intentionally redacted", prompt)
        self.assertNotIn("ACME Corp", prompt)
        self.assertNotIn("Wrong Co", prompt)

    def test_auto_extract_mode_no_bbox(self):
        msg = extractor.build_user_message([], [], page_num=1, total_pages=1)
        self.assertNotIn("boxes", msg.lower())
        self.assertNotIn("bounding", msg.lower())


if __name__ == "__main__":
    unittest.main()
