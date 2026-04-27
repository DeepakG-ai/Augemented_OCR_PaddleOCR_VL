from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qwen_backend import vendor_detector


def _words(text: str) -> list[dict]:
    return [{"text": token} for token in text.split()]


class VendorDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_alias_match_runs_before_fuzzy_match(self) -> None:
        aliases = [
            {
                "vendor_id": "98726",
                "vendor_name": "CANADA METAL V1",
                "pattern": "Canada Metal",
                "weight": 3,
            }
        ]
        vendors = [{"id": "98726", "name": "CANADA METAL V1"}]

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(object(), _words("Invoice from Canada Metal"))

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.vendor_id, "98726")
        self.assertEqual(match.score, 3.0)
        self.assertEqual(match.matched_patterns, ["canada metal"])

    async def test_fuzzy_match_detects_versioned_vendor_name(self) -> None:
        vendors = [{"id": "98726", "name": "CANADA METAL V1"}]

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=[]),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(
                object(),
                _words("Canada Metal FA595213 APV 184468"),
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.vendor_id, "98726")
        self.assertGreaterEqual(match.score, vendor_detector.FUZZY_MIN_SCORE)
        self.assertTrue(match.matched_patterns[0].startswith("fuzzy:canada metal v1~canada metal"))

    async def test_close_fuzzy_matches_are_rejected_as_ambiguous(self) -> None:
        vendors = [
            {"id": "V1", "name": "CANADA METAL V1"},
            {"id": "V2", "name": "CANADA METAL V2"},
        ]

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=[]),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(object(), _words("Canada Metal"))

        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
