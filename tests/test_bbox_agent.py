"""
Tests for backend/bbox_agent.py

Covers: LLM response parsing, 0-1000 → 0..1 normalisation with FACTOR=32 alignment,
field_type assignment, HTTP error handling, JSON parse error handling.
"""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.bbox_agent as bbox_agent


def _b64(data: bytes = b"fake-image") -> str:
    return base64.b64encode(data).decode("ascii")


def _make_response(boxes: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"boxes": boxes})}}]
    }
    return resp


class BboxAgentParsingTests(unittest.IsolatedAsyncioTestCase):

    async def _call(self, boxes: dict, header=None, items=None) -> dict:
        header = header or ["supplier", "bill_to"]
        items = items or []
        mock_resp = _make_response(boxes)
        with patch("backend.bbox_agent.httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(return_value=mock_resp)
            return await bbox_agent.learn_layout_for_vendor(
                page1_image_b64=_b64(),
                page1_width=800,
                page1_height=1024,  # multiple of 32 → alignment factor is a no-op
                header_field_keys=header,
                line_item_column_keys=items,
                llm_url="http://localhost:8001/v1/chat/completions",
                model="qwen3vl",
            )

    async def test_normalises_0_1000_to_0_1(self):
        # page 800x1024 — both multiples of 32, so w_bar==800 and h_bar==1024
        # meaning the alignment factor is a no-op and coords map cleanly
        result = await self._call({"supplier": [100, 200, 400, 260]})
        self.assertIn("supplier", result)
        box = result["supplier"]["normalized_box"]
        self.assertAlmostEqual(box["x0"], 0.1,  places=5)
        self.assertAlmostEqual(box["y0"], 0.2,  places=5)
        self.assertAlmostEqual(box["x1"], 0.4,  places=5)
        self.assertAlmostEqual(box["y1"], 0.26, places=5)

    async def test_field_type_header_vs_line_item(self):
        result = await self._call(
            {"supplier": [10, 10, 100, 30], "qty": [200, 500, 300, 520]},
            header=["supplier"],
            items=["qty"],
        )
        self.assertEqual(result["supplier"]["field_type"], "header")
        self.assertEqual(result["qty"]["field_type"], "line_item_column")

    async def test_null_field_omitted(self):
        result = await self._call({"supplier": [10, 10, 100, 30], "bill_to": None})
        self.assertIn("supplier", result)
        self.assertNotIn("bill_to", result)

    async def test_extra_key_from_llm_ignored(self):
        result = await self._call(
            {"supplier": [10, 10, 100, 30], "invented_field": [0, 0, 50, 50]},
            header=["supplier"],
        )
        self.assertNotIn("invented_field", result)

    async def test_both_header_and_items_returned(self):
        result = await self._call(
            {"supplier": [10, 10, 200, 40], "item_code": [50, 500, 150, 530]},
            header=["supplier"],
            items=["item_code"],
        )
        self.assertIn("supplier", result)
        self.assertIn("item_code", result)

    async def test_degenerate_box_skipped(self):
        # x1 == x0 → degenerate, must be omitted
        result = await self._call({"supplier": [100, 100, 100, 200]})
        self.assertNotIn("supplier", result)

    async def test_empty_request_returns_empty(self):
        result = await self._call({}, header=[], items=[])
        self.assertEqual(result, {})

    async def test_http_error_returns_empty(self):
        with patch("backend.bbox_agent.httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
            result = await bbox_agent.learn_layout_for_vendor(
                page1_image_b64=_b64(),
                page1_width=800, page1_height=1024,
                header_field_keys=["supplier"],
                line_item_column_keys=[],

                llm_url="http://localhost:8001/v1/chat/completions",
                model="qwen3vl",
            )
        self.assertEqual(result, {})

    async def test_json_parse_error_returns_empty(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": "not json at all"}}]}
        with patch("backend.bbox_agent.httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(return_value=resp)
            result = await bbox_agent.learn_layout_for_vendor(
                page1_image_b64=_b64(),
                page1_width=800, page1_height=1024,
                header_field_keys=["supplier"],
                line_item_column_keys=[],

                llm_url="http://localhost:8001/v1/chat/completions",
                model="qwen3vl",
            )
        self.assertEqual(result, {})

    async def test_markdown_fence_stripped(self):
        fenced = "```json\n" + json.dumps({"boxes": {"supplier": [10, 20, 200, 60]}}) + "\n```"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": fenced}}]}
        with patch("backend.bbox_agent.httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(return_value=resp)
            result = await bbox_agent.learn_layout_for_vendor(
                page1_image_b64=_b64(),
                page1_width=800, page1_height=1024,
                header_field_keys=["supplier"],
                line_item_column_keys=[],

                llm_url="http://localhost:8001/v1/chat/completions",
                model="qwen3vl",
            )
        self.assertIn("supplier", result)


if __name__ == "__main__":
    unittest.main()
