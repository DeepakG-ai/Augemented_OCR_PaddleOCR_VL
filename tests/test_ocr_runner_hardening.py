from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import ocr_runner


class OcrRunnerHardeningTests(unittest.TestCase):
    def test_invalid_base64_does_not_initialize_paddleocr(self) -> None:
        with patch.object(ocr_runner, "_get_ocr_engine") as mock_engine:
            result = ocr_runner._run_ocr_on_page("not base64 !!", 1)

        self.assertEqual(result["page_number"], 1)
        self.assertEqual(result["words"], [])
        self.assertIn("_ocr_error", result)
        mock_engine.assert_not_called()

    def test_undecodable_image_does_not_initialize_paddleocr(self) -> None:
        image_b64 = base64.b64encode(b"not an image").decode("ascii")
        with patch.object(ocr_runner, "_get_ocr_engine") as mock_engine:
            result = ocr_runner._run_ocr_on_page(image_b64, 2)

        self.assertEqual(result["page_number"], 2)
        self.assertEqual(result["words"], [])
        self.assertIn("_ocr_error", result)
        mock_engine.assert_not_called()

    def test_ocr_engine_failure_is_not_reported_as_blank_page(self) -> None:
        # 1x1 transparent PNG.
        image_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        import numpy as np
        dummy_img = np.zeros((10, 10, 3), dtype=np.uint8)
        with patch("cv2.imdecode", return_value=dummy_img), \
             patch.object(ocr_runner, "_get_ocr_engine", side_effect=RuntimeError("model missing")):
            result = ocr_runner._run_ocr_on_page(image_b64, 3)

        self.assertEqual(result["page_number"], 3)
        self.assertEqual(result["words"], [])
        self.assertIn("model missing", result["_ocr_error"])


if __name__ == "__main__":
    unittest.main()
