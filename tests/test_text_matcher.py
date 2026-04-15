from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "qwen_backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import text_matcher


class FindValueInOcrTests(unittest.TestCase):
    def test_exact_match_returns_exact_strategy(self) -> None:
        ocr_words = [
            {"text": "ACME", "box": [10, 10, 40, 20], "score": 0.99},
            {"text": "PO123", "box": [50, 10, 90, 20], "score": 0.98},
        ]

        location = text_matcher.find_value_in_ocr("ACME", ocr_words)

        self.assertIsNotNone(location)
        self.assertEqual(location["strategy"], "exact")
        self.assertEqual(location["box"], [10, 10, 40, 20])

    def test_multiword_value_prefers_token_chain_over_partial_contains(self) -> None:
        ocr_words = [
            {"text": "RED", "box": [0, 0, 10, 10], "score": 0.99},
            {"text": "PEPPER", "box": [12, 0, 30, 10], "score": 0.99},
        ]

        location = text_matcher.find_value_in_ocr("RED PEPPER", ocr_words)

        self.assertIsNotNone(location)
        self.assertEqual(location["strategy"], "token_chain")
        self.assertEqual(location["box"], [0, 0, 30, 10])
        self.assertEqual(location["matched_text"], "RED PEPPER")

    def test_token_inside_long_chunk_gets_tight_sub_box(self) -> None:
        ocr_words = [
            {
                "text": "7-60034-02664 001/001 4X2X2I BOWL CLIP MANGO",
                "box": [100, 200, 600, 230],
                "score": 0.98,
            }
        ]

        location = text_matcher.find_value_in_ocr("7-60034-02664", ocr_words)

        self.assertIsNotNone(location)
        self.assertIn(location["strategy"], ("token_chain", "contains_subspan"))
        self.assertEqual(location["box"][1], 200)
        self.assertEqual(location["box"][3], 230)
        # Tighter than the full chunk width.
        self.assertLess(location["box"][2] - location["box"][0], 500)

    def test_fuzzy_match_is_classified_low_confidence(self) -> None:
        ocr_words = [{"text": "P0123", "box": [5, 5, 25, 15], "score": 0.97}]

        location = text_matcher.find_value_in_ocr("PO123", ocr_words, fuzzy_threshold=0.7)

        self.assertIsNotNone(location)
        self.assertTrue(location["strategy"].startswith("fuzzy("))
        self.assertEqual(
            text_matcher._classify_confidence(location["strategy"]),
            "low",
        )


class ComputeFieldLocationsTests(unittest.TestCase):
    def test_multiline_header_uses_tight_line_box(self) -> None:
        ocr_pages = [
            {
                "page_number": 1,
                "words": [
                    {"text": "FRESH PRODUCTS, INC.", "box": [139, 247, 384, 267], "score": 0.95},
                    {"text": "PO BOX 933189", "box": [140, 269, 301, 286], "score": 0.95},
                    {"text": "CLEVELAND, OH 44193", "box": [139, 288, 374, 308], "score": 0.93},
                    {"text": "419-531-8472", "box": [139, 309, 290, 329], "score": 0.99},
                ],
            }
        ]
        extraction_result = {
            "vendor": "FRESH PRODUCTS, INC.\nPO BOX 933189\nCLEVELAND, OH 44193\n419-531-8472",
            "line_items": [],
        }

        locations = text_matcher.compute_field_locations(extraction_result, ocr_pages)
        self.assertEqual(locations["vendor"]["box"], [139, 247, 384, 267])
        self.assertEqual(locations["vendor"]["strategy"], "exact")
        self.assertIn("sub_locations", locations["vendor"])

    def test_duplicate_line_item_values_map_to_distinct_boxes(self) -> None:
        ocr_pages = [
            {
                "page_number": 1,
                "words": [
                    {"text": "RED", "box": [0, 0, 10, 10], "score": 0.99},
                    {"text": "PEPPER", "box": [12, 0, 30, 10], "score": 0.99},
                    {"text": "RED", "box": [0, 20, 10, 30], "score": 0.99},
                    {"text": "PEPPER", "box": [12, 20, 30, 30], "score": 0.99},
                ],
            }
        ]
        extraction_result = {
            "line_items": [
                {"description": "RED PEPPER"},
                {"description": "RED PEPPER"},
            ]
        }

        locations = text_matcher.compute_field_locations(extraction_result, ocr_pages)

        self.assertEqual(
            locations["line_item_0_description"]["box"],
            [0, 0, 30, 10],
        )
        self.assertEqual(
            locations["line_item_1_description"]["box"],
            [0, 20, 30, 30],
        )

    def test_header_and_line_item_locations_include_confidence(self) -> None:
        ocr_pages = [
            {
                "page_number": 1,
                "words": [
                    {"text": "ACME", "box": [10, 10, 40, 20], "score": 0.99},
                    {"text": "Widget", "box": [10, 40, 50, 55], "score": 0.98},
                    {"text": "10", "box": [60, 40, 72, 55], "score": 0.98},
                ],
            }
        ]
        extraction_result = {
            "vendor_name": "ACME",
            "line_items": [{"description": "Widget", "qty": "10"}],
        }

        locations = text_matcher.compute_field_locations(extraction_result, ocr_pages)

        self.assertEqual(locations["vendor_name"]["confidence"], "high")
        self.assertEqual(locations["line_item_0_description"]["confidence"], "high")
        self.assertEqual(locations["line_item_0_qty"]["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
