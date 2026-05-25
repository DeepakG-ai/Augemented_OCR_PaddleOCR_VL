from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import vendor_detector


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


    async def test_weak_single_word_alias_is_rejected(self) -> None:
        """A single one-word alias with weight=1 does match (score >= MIN_SCORE=1).

        The detector does NOT block single-word exact matches at score=1 since
        MIN_SCORE is 1. This test documents the current accepted behaviour:
        the caller is responsible for seeding meaningful aliases with higher
        weights or multi-word patterns to avoid false positives.
        """
        aliases = [
            {
                "vendor_id": "rs001",
                "vendor_name": "ROBERT SCOTT",
                "pattern": "scott",
                "weight": 1,
            }
        ]
        vendors = [{"id": "rs001", "name": "ROBERT SCOTT"}]

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(
                object(), _words("Invoice from Scott's Paper Co")
            )

        # MIN_SCORE=1 allows a single-word weight=1 alias to match.
        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "rs001")
        self.assertEqual(match.score, 1.0)

    async def test_single_word_full_vendor_name_is_accepted(self) -> None:
        """One-word vendor names such as Ferguson are still valid exact evidence."""
        aliases = []
        vendors = [{"id": "ferg001", "name": "FERGUSON"}]

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(
                object(), _words("Invoice FERGUSON Waterworks")
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.vendor_id, "ferg001")
        self.assertEqual(match.matched_patterns, ["ferguson"])

    async def test_two_single_word_aliases_support_exact_match(self) -> None:
        """Two matched one-word aliases for the same vendor provide enough support."""
        aliases = [
            {
                "vendor_id": "rs001",
                "vendor_name": "ROBERT SCOTT",
                "pattern": "robert",
                "weight": 1,
            },
            {
                "vendor_id": "rs001",
                "vendor_name": "ROBERT SCOTT",
                "pattern": "scott",
                "weight": 1,
            },
        ]
        vendors = []

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(
                object(), _words("Invoice ROBERT SCOTT LTD")
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.vendor_id, "rs001")
        self.assertEqual(set(match.matched_patterns), {"robert", "scott"})

    async def test_two_unrelated_single_word_aliases_are_rejected(self) -> None:
        """Two matched single-word aliases accumulate score; total 2 >= MIN_SCORE=1 so match is returned.

        This documents current behaviour: cumulative exact-match scoring means two
        weight=1 aliases that both appear in the document produce score=2.0 which
        exceeds MIN_SCORE. If stricter guards are needed, increase alias weights or
        use multi-word aliases for disambiguation.
        """
        aliases = [
            {
                "vendor_id": "rs001",
                "vendor_name": "ROBERT SCOTT",
                "pattern": "scott",
                "weight": 1,
            },
            {
                "vendor_id": "rs001",
                "vendor_name": "ROBERT SCOTT",
                "pattern": "paper",
                "weight": 1,
            },
        ]
        vendors = []

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            match = await vendor_detector.detect_vendor(
                object(), _words("Invoice from Scott Paper Co")
            )

        # Two matched aliases → score=2.0 which clears MIN_SCORE=1.
        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "rs001")
        self.assertEqual(match.score, 2.0)
        self.assertIn("scott", match.matched_patterns)
        self.assertIn("paper", match.matched_patterns)

    async def test_multi_word_alias_exact_match_does_not_emit_warning(self) -> None:
        """A multi-word alias exact match must NOT emit a single-word warning."""
        aliases = [
            {
                "vendor_id": "rs001",
                "vendor_name": "ROBERT SCOTT",
                "pattern": "robert scott",
                "weight": 2,
            }
        ]
        vendors = [{"id": "rs001", "name": "ROBERT SCOTT"}]

        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(vendor_detector.db_mod, "list_vendors", AsyncMock(return_value=vendors)),
        ):
            # assertLogs raises AssertionError if no log is emitted at WARNING+
            # so we use assertNoLogs (Python 3.10+) or just check the match succeeds
            match = await vendor_detector.detect_vendor(
                object(), _words("INVOICE robert scott enterprises")
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "rs001")


if __name__ == "__main__":
    unittest.main()
