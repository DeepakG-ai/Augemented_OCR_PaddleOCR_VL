from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "qwen_backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import extractor


class MergeResultsTests(unittest.TestCase):
    def test_merge_results_preserves_duplicate_line_items(self) -> None:
        page_results = [
            {
                "_page": 1,
                "vendor_name": "ACME",
                "line_items": [{"item": "A", "qty": 1, "price": 10}],
            },
            {
                "_page": 2,
                "line_items": [{"item": "A", "qty": 1, "price": 10}],
            },
        ]

        merged = extractor.merge_results(
            page_results,
            header_fields=["vendor_name"],
            line_item_fields=["item", "qty", "price"],
        )

        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(
            merged["line_items"],
            [
                {"item": "A", "qty": 1, "price": 10},
                {"item": "A", "qty": 1, "price": 10},
            ],
        )

    def test_merge_results_ignores_error_pages(self) -> None:
        page_results = [
            {"_page": 1, "vendor_name": "ACME", "line_items": [{"item": "A"}]},
            {"_page": 2, "_error": "bad page", "line_items": [{"item": "SHOULD_NOT_APPEAR"}]},
            {"_page": 3, "line_items": [{"item": "B"}]},
        ]

        merged = extractor.merge_results(
            page_results,
            header_fields=["vendor_name"],
            line_item_fields=["item"],
        )

        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(merged["line_items"], [{"item": "A"}, {"item": "B"}])

    def test_merge_results_auto_extract_uses_first_valid_page_headers(self) -> None:
        page_results = [
            {"_page": 1, "_error": "bad page"},
            {
                "_page": 2,
                "vendor_name": "ACME",
                "po_number": "PO-1",
                "line_items": [{"item": "A"}],
            },
        ]

        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])

        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(merged["po_number"], "PO-1")
        self.assertEqual(merged["line_items"], [{"item": "A"}])


if __name__ == "__main__":
    unittest.main()
