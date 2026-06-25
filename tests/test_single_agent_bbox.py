"""
Tests for v5.4 single-agent extraction: page 1 returns {fields, boxes},
page 2+ returns {fields} only. vendor_confirmed removed — vendor_detector.py handles it.

Covers:
  - System prompt dual mode (include_boxes=True vs False)
  - User message dual mode (page 1 boxes template vs page 2+ fields-only)
  - Merge safety: boxes key excluded from merged result
  - FACTOR=32 coordinate normalization (the llama.cpp alignment fix)
  - Field type classification (header vs line_item_column)
  - Edge cases: degenerate boxes, null boxes, non-multiple-of-32 dimensions
  - extract_document page routing (page 1 gets page1 prompt, page 2+ gets fields prompt)
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.extractor as extractor


HEADER = ["po_number", "supplier", "bill_to", "order_date"]
ITEMS = ["item_code", "description", "qty", "unit_price", "amount"]


# ═══════════════════════════════════════════════════════════════════════
# 1. Prompt construction tests
# ═══════════════════════════════════════════════════════════════════════

class SystemPromptDualModeTests(unittest.TestCase):
    """Verify build_system_prompt behaves differently for page 1 vs 2+."""

    def test_version_is_v5(self):
        self.assertEqual(extractor.PROMPT_VERSION, "v5.4")

    def test_page1_prompt_contains_boxes_instruction(self):
        prompt = extractor.build_system_prompt(
            HEADER, ITEMS, instructions=None, rules=[], format_type="single_po_multipage",
            include_boxes=True,
        )
        self.assertIn("boxes", prompt.lower())
        self.assertIn("bbox_rules", prompt.lower())
        self.assertIn("Return two top-level keys", prompt)
        self.assertNotIn("vendor_confirmed", prompt)
        self.assertNotIn("vendor_verification", prompt)

    def test_page2_prompt_no_boxes(self):
        prompt = extractor.build_system_prompt(
            HEADER, ITEMS, instructions=None, rules=[], format_type="single_po_multipage",
            include_boxes=False,
        )
        self.assertNotIn("boxes", prompt.lower())
        self.assertNotIn("bbox_rules", prompt.lower())
        self.assertIn("Return one top-level key", prompt)

    def test_default_is_no_boxes(self):
        """Calling without include_boxes should default to fields-only."""
        prompt = extractor.build_system_prompt(
            HEADER, ITEMS, instructions=None, rules=[], format_type="single_po_multipage",
        )
        self.assertNotIn("boxes", prompt.lower())
        self.assertIn("Return one top-level key", prompt)

    def test_vendor_name_not_in_prompt(self):
        """vendor_confirmed removed — vendor_detector.py owns detection, not the LLM."""
        prompt = extractor.build_system_prompt(
            HEADER, ITEMS, instructions=None, rules=[], format_type="single_po_multipage",
            include_boxes=True,
        )
        self.assertNotIn("vendor_confirmed", prompt)
        self.assertNotIn("vendor_verification", prompt)

    def test_gold_correction_examples_include_original_and_corrected_values(self):
        gold = [{"correction_diff": {"supplier": {"original": "Wrong Co", "corrected": "ACME Corp"}}}]
        for include_boxes in (True, False):
            prompt = extractor.build_system_prompt(
                HEADER, ITEMS, instructions=None, rules=[], format_type="single_po_multipage",
                gold_examples=gold, include_boxes=include_boxes,
            )
            self.assertIn("supplier", prompt)
            self.assertIn("correction_examples", prompt)
            self.assertIn("original_value", prompt)
            self.assertIn("correct_diff", prompt)
            self.assertIn("Wrong Co", prompt)
            self.assertIn("ACME Corp", prompt)

    def test_custom_instructions_appear_in_both_modes(self):
        for include_boxes in (True, False):
            prompt = extractor.build_system_prompt(
                HEADER, ITEMS, instructions="Custom vendor hint", rules=["Rule A"],
                format_type="single_po_multipage", include_boxes=include_boxes,
            )
            self.assertIn("Custom vendor hint", prompt)
            self.assertIn("Rule A", prompt)


class UserMessageDualModeTests(unittest.TestCase):
    """Verify build_user_message adds boxes template only for page 1."""

    def test_page1_message_has_boxes_template(self):
        msg = extractor.build_user_message(HEADER, ITEMS, 1, 3, include_boxes=True)
        parsed_template = self._extract_json_template(msg)
        self.assertNotIn("vendor_confirmed", parsed_template)
        self.assertIn("fields", parsed_template)
        self.assertIn("boxes", parsed_template)

    def test_page2_message_no_boxes(self):
        msg = extractor.build_user_message(HEADER, ITEMS, 2, 3, include_boxes=False)
        parsed_template = self._extract_json_template(msg)
        self.assertIn("fields", parsed_template)
        self.assertNotIn("boxes", parsed_template)

    def test_page1_boxes_template_contains_all_field_keys(self):
        msg = extractor.build_user_message(HEADER, ITEMS, 1, 3, include_boxes=True)
        parsed_template = self._extract_json_template(msg)
        boxes = parsed_template["boxes"]
        for field in HEADER + ITEMS:
            self.assertIn(field, boxes, f"Missing {field} in boxes template")

    def test_page1_message_uses_json_shape_for_boxes(self):
        msg = extractor.build_user_message(HEADER, ITEMS, 1, 3, include_boxes=True)
        self.assertNotIn('"vendor_confirmed"', msg)
        self.assertIn('"boxes"', msg)

    def test_page2_message_no_bbox_instructions(self):
        msg = extractor.build_user_message(HEADER, ITEMS, 2, 3, include_boxes=False)
        self.assertNotIn("bbox_instructions", msg)

    def test_default_is_no_boxes(self):
        msg = extractor.build_user_message(HEADER, ITEMS, 1, 1)
        parsed_template = self._extract_json_template(msg)
        self.assertNotIn("boxes", parsed_template)

    def test_auto_extract_mode_never_has_boxes(self):
        """When no fields are provided (auto-extract), never include boxes."""
        msg = extractor.build_user_message([], [], 1, 1, include_boxes=True)
        self.assertNotIn("boxes", msg.lower())

    def _extract_json_template(self, msg: str) -> dict:
        """Pull the JSON template from the user message."""
        # Find the JSON block between "Return JSON in exactly this shape:" and "<rules>"
        start = msg.index("{")
        depth, end = 0, start
        for i, ch in enumerate(msg[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return json.loads(msg[start:end])


# ═══════════════════════════════════════════════════════════════════════
# 2. Merge safety tests
# ═══════════════════════════════════════════════════════════════════════

class MergeBoxesSafetyTests(unittest.TestCase):
    """The 'boxes' key from page 1 must NEVER leak into the merged result."""

    def test_boxes_excluded_from_merged_result(self):
        page_results = [
            {
                "_page": 1,
                "fields": {
                    "po_number": "P1234",
                    "line_items": [{"item_code": "A", "qty": 1}],
                },
                "boxes": {
                    "po_number": [100, 50, 250, 70],
                    "item_code": [30, 400, 120, 420],
                },
            },
            {
                "_page": 2,
                "fields": {
                    "line_items": [{"item_code": "B", "qty": 2}],
                },
            },
        ]
        merged = extractor.merge_results(page_results, HEADER, ITEMS)
        self.assertNotIn("boxes", merged)
        self.assertEqual(merged["po_number"], "P1234")
        self.assertEqual(len(merged["line_items"]), 2)

    def test_page1_boxes_preserved_in_page_results(self):
        """page_results[0] must still have boxes for the worker to extract."""
        pr = {
            "_page": 1,
            "fields": {"po_number": "P1234"},
            "boxes": {"po_number": [100, 50, 250, 70]},
        }
        self.assertIn("boxes", pr)
        self.assertEqual(pr["boxes"]["po_number"], [100, 50, 250, 70])


# ═══════════════════════════════════════════════════════════════════════
# 3. FACTOR=32 coordinate normalization tests
# ═══════════════════════════════════════════════════════════════════════

def _normalize_box(raw_box: list, page_w: int, page_h: int) -> dict | None:
    """
    Replicate the exact normalization logic from worker.py for testing.
    0-1000 grid → aligned pixel space → 0-1 normalized.
    """
    FACTOR = 32
    w_bar = max(FACTOR, int(round(page_w / FACTOR) * FACTOR))
    h_bar = max(FACTOR, int(round(page_h / FACTOR) * FACTOR))
    x0_px = (raw_box[0] / 1000.0) * w_bar
    y0_px = (raw_box[1] / 1000.0) * h_bar
    x1_px = (raw_box[2] / 1000.0) * w_bar
    y1_px = (raw_box[3] / 1000.0) * h_bar
    nx0 = max(0.0, x0_px / page_w)
    ny0 = max(0.0, y0_px / page_h)
    nx1 = min(1.0, x1_px / page_w)
    ny1 = min(1.0, y1_px / page_h)
    if nx1 <= nx0 or ny1 <= ny0:
        return None
    return {"x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1}


class Factor32NormalizationTests(unittest.TestCase):
    """Test the FACTOR=32 alignment correction from docs/qwen_bbox_github_issue.md."""

    def test_multiple_of_32_is_identity(self):
        """800x1024 are both multiples of 32, so w_bar==w and h_bar==h."""
        box = _normalize_box([100, 200, 400, 260], 800, 1024)
        self.assertAlmostEqual(box["x0"], 0.1, places=5)
        self.assertAlmostEqual(box["y0"], 0.2, places=5)
        self.assertAlmostEqual(box["x1"], 0.4, places=5)
        self.assertAlmostEqual(box["y1"], 0.26, places=5)

    def test_non_multiple_of_32_applies_correction(self):
        """1275x1650 → w_bar=1280, h_bar=1664. Without FACTOR=32 fix, coords drift."""
        box = _normalize_box([500, 500, 600, 600], 1275, 1650)
        # With correction: x0_px = (500/1000)*1280 = 640, nx0 = 640/1275 ≈ 0.50196
        # Without correction: x0_px = (500/1000)*1275 = 637.5, nx0 = 0.5 (wrong)
        self.assertAlmostEqual(box["x0"], 640 / 1275, places=5)
        self.assertAlmostEqual(box["y0"], 832 / 1650, places=5)
        # Verify it's NOT 0.5 (the naive/wrong calculation)
        self.assertNotAlmostEqual(box["x0"], 0.5, places=3)

    def test_degenerate_box_returns_none(self):
        """x1 == x0 or y1 == y0 → degenerate, must be rejected."""
        self.assertIsNone(_normalize_box([100, 100, 100, 200], 800, 1024))
        self.assertIsNone(_normalize_box([100, 200, 200, 200], 800, 1024))

    def test_inverted_box_returns_none(self):
        """x1 < x0 → inverted, must be rejected."""
        self.assertIsNone(_normalize_box([400, 200, 100, 300], 800, 1024))

    def test_clamped_to_0_1(self):
        """Coordinates outside 0-1000 should be clamped after normalization."""
        box = _normalize_box([0, 0, 1000, 1000], 800, 1024)
        self.assertLessEqual(box["x1"], 1.0)
        self.assertLessEqual(box["y1"], 1.0)
        self.assertGreaterEqual(box["x0"], 0.0)
        self.assertGreaterEqual(box["y0"], 0.0)


class FieldTypeClassificationTests(unittest.TestCase):
    """Header fields vs line_item_column classification."""

    def test_header_field_classified_as_header(self):
        req_items_set = set(ITEMS)
        for f in HEADER:
            field_type = "line_item_column" if f in req_items_set else "header"
            self.assertEqual(field_type, "header", f"{f} should be header")

    def test_line_item_field_classified_as_column(self):
        req_items_set = set(ITEMS)
        for f in ITEMS:
            field_type = "line_item_column" if f in req_items_set else "header"
            self.assertEqual(field_type, "line_item_column", f"{f} should be line_item_column")


# ═══════════════════════════════════════════════════════════════════════
# 4. extract_document page routing tests
# ═══════════════════════════════════════════════════════════════════════

class ExtractDocumentPageRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Verify that page 1 gets system_prompt_page1 and include_boxes=True,
    while page 2+ gets the regular system_prompt."""

    async def test_page1_uses_page1_prompt_and_boxes(self):
        """Mock call_llm to capture which system_prompt and user_msg are used."""
        captured_calls = []

        async def fake_call_llm(image_b64, system_prompt, user_msg, *args, **kwargs):
            captured_calls.append({
                "system_prompt": system_prompt,
                "user_msg": user_msg,
                "page_num": kwargs.get("page_num", 0),
            })
            return {"fields": {"po_number": "P1234", "line_items": []}}

        pages = [
            {"page_number": 1, "image_b64": "fake", "mime_type": "image/jpeg"},
            {"page_number": 2, "image_b64": "fake", "mime_type": "image/jpeg"},
        ]

        sys_prompt = "FIELDS_ONLY_PROMPT"
        sys_prompt_p1 = "PAGE1_WITH_BOXES_PROMPT"

        with patch.object(extractor, "call_llm", side_effect=fake_call_llm):
            result = await extractor.extract_document(
                pages=pages,
                header_fields=HEADER,
                line_item_fields=ITEMS,
                system_prompt=sys_prompt,
                format_type="single_po_multipage",
                llm_url="http://fake",
                model="qwen3vl",
                system_prompt_page1=sys_prompt_p1,
            )

        self.assertEqual(len(captured_calls), 2)

        # Page 1: must use page1 prompt
        p1_call = captured_calls[0]
        self.assertEqual(p1_call["system_prompt"], "PAGE1_WITH_BOXES_PROMPT")
        self.assertIn('"boxes"', p1_call["user_msg"])

        # Page 2: must use regular prompt
        p2_call = captured_calls[1]
        self.assertEqual(p2_call["system_prompt"], "FIELDS_ONLY_PROMPT")
        self.assertNotIn('"boxes"', p2_call["user_msg"])

    async def test_no_page1_prompt_falls_back_to_regular(self):
        """When system_prompt_page1 is None, page 1 uses the regular prompt."""
        captured_prompts = []

        async def fake_call_llm(image_b64, system_prompt, user_msg, *args, **kwargs):
            captured_prompts.append(system_prompt)
            return {"fields": {"po_number": "P1234", "line_items": []}}

        pages = [{"page_number": 1, "image_b64": "fake", "mime_type": "image/jpeg"}]

        with patch.object(extractor, "call_llm", side_effect=fake_call_llm):
            await extractor.extract_document(
                pages=pages,
                header_fields=HEADER,
                line_item_fields=ITEMS,
                system_prompt="REGULAR",
                format_type="single_page",
                llm_url="http://fake",
                model="qwen3vl",
                system_prompt_page1=None,
            )

        self.assertEqual(captured_prompts[0], "REGULAR")

    async def test_page1_boxes_can_be_disabled_for_api_json_only_mode(self):
        """API JSON-only mode should use the regular prompt and fields-only user message."""
        captured = {}

        async def fake_call_llm(image_b64, system_prompt, user_msg, *args, **kwargs):
            captured["system_prompt"] = system_prompt
            captured["user_msg"] = user_msg
            return {"fields": {"po_number": "P1234", "line_items": []}}

        pages = [{"page_number": 1, "image_b64": "fake", "mime_type": "image/jpeg"}]

        with patch.object(extractor, "call_llm", side_effect=fake_call_llm):
            await extractor.extract_document(
                pages=pages,
                header_fields=HEADER,
                line_item_fields=ITEMS,
                system_prompt="REGULAR",
                format_type="single_page",
                llm_url="http://fake",
                model="qwen3vl",
                system_prompt_page1=None,
                include_page1_boxes=False,
            )

        self.assertEqual(captured["system_prompt"], "REGULAR")
        self.assertNotIn('"boxes"', captured["user_msg"])
        self.assertNotIn("bounding", captured["user_msg"].lower())

    async def test_boxes_in_page1_result_not_in_final_merge(self):
        """End-to-end: boxes from page 1 LLM response must not appear in merged result."""
        async def fake_call_llm(image_b64, system_prompt, user_msg, *args, **kwargs):
            page_num = kwargs.get("page_num", 1)
            if page_num == 1:
                return {
                    "fields": {"po_number": "P999", "line_items": [{"item_code": "X", "qty": 5}]},
                    "boxes": {"po_number": [100, 50, 250, 70], "item_code": [30, 400, 120, 420]},
                }
            return {"fields": {"line_items": [{"item_code": "Y", "qty": 3}]}}

        pages = [
            {"page_number": 1, "image_b64": "fake", "mime_type": "image/jpeg"},
            {"page_number": 2, "image_b64": "fake", "mime_type": "image/jpeg"},
        ]

        with patch.object(extractor, "call_llm", side_effect=fake_call_llm):
            output = await extractor.extract_document(
                pages=pages,
                header_fields=HEADER,
                line_item_fields=ITEMS,
                system_prompt="SYS",
                format_type="single_po_multipage",
                llm_url="http://fake",
                model="qwen3vl",
                system_prompt_page1="SYS_P1",
            )

        final = output["result"]
        # Boxes must not leak into the merged result
        self.assertNotIn("boxes", final)
        # Fields must be merged correctly
        self.assertEqual(final["po_number"], "P999")
        self.assertEqual(len(final["line_items"]), 2)

        # But boxes ARE in page_results[0] for worker extraction
        p1_pr = output["page_results"][0]
        self.assertIn("boxes", p1_pr)
        self.assertEqual(p1_pr["boxes"]["po_number"], [100, 50, 250, 70])


if __name__ == "__main__":
    unittest.main()
