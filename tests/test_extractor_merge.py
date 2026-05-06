from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

import backend.extractor as extractor


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
        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(
            merged["line_items"],
            [
                {"item": "A", "qty": 1, "price": 10, "_page": 1},
                {"item": "A", "qty": 1, "price": 10, "_page": 2},
            ],
        )

    def test_merge_results_injects_page_number_into_line_items(self) -> None:
        page_results = [
            {
                "_page": 1,
                "fields": {
                    "line_items": [{"item": "Page 1 Item"}]
                }
            },
            {
                "_page": 2,
                "fields": {
                    "line_items": [{"item": "Page 2 Item 1"}, {"item": "Page 2 Item 2"}]
                }
            },
        ]

        merged = extractor.merge_results(
            page_results,
            header_fields=[],
            line_item_fields=["item"],
        )

        self.assertEqual(
            merged["line_items"],
            [
                {"item": "Page 1 Item", "_page": 1},
                {"item": "Page 2 Item 1", "_page": 2},
                {"item": "Page 2 Item 2", "_page": 2},
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
        self.assertEqual(merged["line_items"], [{"item": "A", "_page": 1}, {"item": "B", "_page": 3}])

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
        self.assertEqual(merged["line_items"], [{"item": "A", "_page": 2}])

    def test_merge_results_combines_nested_header_objects(self) -> None:
        page_results = [
            {
                "_page": 1,
                "fields": {
                    "bill_to": {
                        "name": "SLACAN",
                        "address": "145 ROY BLVD.\nBRANTFORD ON N3T 6E3",
                        "postal_code": "N3T 6E3",
                    },
                    "line_items": [{"item": "A"}],
                },
            },
        ]

        merged = extractor.merge_results(
            page_results,
            header_fields=["bill_to"],
            line_item_fields=["item"],
        )

        self.assertEqual(
            merged["bill_to"],
            "SLACAN\n145 ROY BLVD.\nBRANTFORD ON N3T 6E3\nN3T 6E3",
        )
        self.assertEqual(merged["line_items"], [{"item": "A", "_page": 1}])

    def test_normalize_header_values_leaves_line_items_unchanged(self) -> None:
        result = {
            "ship_to": {"name": "SLACAN", "address": "145 ROY BLVD."},
            "line_items": [{"description": {"name": "KEEP", "address": "OBJECT"}}],
        }

        normalized = extractor.normalize_header_values(result)

        self.assertEqual(normalized["ship_to"], "SLACAN\n145 ROY BLVD.")
        self.assertEqual(
            normalized["line_items"],
            [{"description": {"name": "KEEP", "address": "OBJECT"}}],
        )


if __name__ == "__main__":
    unittest.main()
