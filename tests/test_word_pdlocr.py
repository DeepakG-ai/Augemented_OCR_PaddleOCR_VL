from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tests" / "paddle_ocr" / "word_pdlocr.py"
SPEC = importlib.util.spec_from_file_location("word_pdlocr", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module at {MODULE_PATH}")
word_pdlocr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(word_pdlocr)


class WordPaddleOCRTests(unittest.TestCase):
    def test_raises_when_word_geometry_missing(self) -> None:
        result = [
            {
                "rec_texts": ["HELLO WORLD"],
                "rec_scores": [0.99],
                "rec_boxes": [[0, 0, 100, 20]],
            }
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "return_word_box=True did not provide word/char boxes",
        ):
            word_pdlocr.extract_mappings_from_result(result)

    def test_groups_chars_into_single_code_token(self) -> None:
        line_text = "7-60034-02664"
        chars = list(line_text)
        boxes = [[10 + i * 4, 10, 14 + i * 4, 20] for i in range(len(chars))]
        result = [
            {
                "rec_texts": [line_text],
                "rec_scores": [0.97],
                "rec_boxes": [[10, 10, 10 + len(chars) * 4, 20]],
                "text_word": [chars],
                "text_word_boxes": [boxes],
            }
        ]

        word_mapping, line_mapping, words = word_pdlocr.extract_mappings_from_result(result)

        self.assertEqual(len(word_mapping), 1)
        self.assertEqual(word_mapping[0]["text"], line_text)
        self.assertEqual(word_mapping[0]["box"], [10, 10, 14 + (len(chars) - 1) * 4, 20])
        self.assertEqual(word_mapping[0]["line_index"], 0)
        self.assertEqual(word_mapping[0]["line_text"], line_text)
        self.assertEqual(line_mapping[0]["text"], line_text)
        self.assertEqual(words, [line_text])

    def test_splits_multi_token_line_and_computes_boxes(self) -> None:
        line_text = "ABC 123 XY"
        chars = list("ABC123XY")
        boxes = [[5 + i * 5, 30, 9 + i * 5, 40] for i in range(len(chars))]
        result = [
            {
                "rec_texts": [line_text],
                "rec_scores": [0.95],
                "rec_boxes": [[5, 30, 9 + (len(chars) - 1) * 5, 40]],
                "text_word": [chars],
                "text_word_boxes": [boxes],
            }
        ]

        word_mapping, _, words = word_pdlocr.extract_mappings_from_result(result)
        self.assertEqual([w["text"] for w in word_mapping], ["ABC", "123", "XY"])
        self.assertEqual(words, ["ABC", "123", "XY"])
        self.assertEqual(word_mapping[0]["box"], [5, 30, 19, 40])   # A B C
        self.assertEqual(word_mapping[1]["box"], [20, 30, 34, 40])  # 1 2 3
        self.assertEqual(word_mapping[2]["box"], [35, 30, 44, 40])  # X Y

    def test_output_order_is_stable(self) -> None:
        result = [
            {
                "rec_texts": ["FIRST LINE", "SECOND 22"],
                "rec_scores": [0.99, 0.98],
                "rec_boxes": [[0, 0, 100, 20], [0, 30, 100, 50]],
                "text_word": [list("FIRSTLINE"), list("SECOND22")],
                "text_word_boxes": [
                    [[i * 3, 0, i * 3 + 2, 10] for i in range(9)],
                    [[i * 3, 30, i * 3 + 2, 40] for i in range(8)],
                ],
            }
        ]

        word_mapping, _, words = word_pdlocr.extract_mappings_from_result(result)
        self.assertEqual(words, ["FIRST", "LINE", "SECOND", "22"])
        self.assertEqual([w["text"] for w in word_mapping], ["FIRST", "LINE", "SECOND", "22"])
        self.assertEqual([w["line_index"] for w in word_mapping], [0, 0, 1, 1])


if __name__ == "__main__":
    unittest.main()

