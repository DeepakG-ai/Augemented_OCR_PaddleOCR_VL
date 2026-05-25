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

import backend.extractor as extractor


def _b64(data: bytes = b"fake-image") -> str:
    return base64.b64encode(data).decode("ascii")


class LlmUsageRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_llm_records_usage_per_page(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"fields": {"supplier": "Bluechip"}})}}],
            "usage": {
                "prompt_tokens": 1643,
                "completion_tokens": 461,
                "total_tokens": 2104,
            },
        }

        pool = object()
        with patch("backend.extractor.httpx.AsyncClient") as MockClient, \
             patch.object(extractor.db_mod, "record_llm_usage", new=AsyncMock(return_value={"id": 1})) as mock_record:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(return_value=resp)

            result = await extractor.call_llm(
                _b64(),
                "system",
                "user",
                "http://localhost:8001/v1/chat/completions",
                "qwen3vl",
                page_num=2,
                total_pages=5,
                pipeline_context={
                    "document_id": 42,
                    "extraction_id": 99,
                    "vendor_id": "ROBERT_SCOTT",
                    "job_id": 7,
                },
                pool=pool,
            )

        self.assertEqual(result["fields"]["supplier"], "Bluechip")
        mock_record.assert_awaited_once()
        args, kwargs = mock_record.await_args
        self.assertIs(args[0], pool)
        self.assertEqual(kwargs["doc_id"], 42)
        self.assertEqual(kwargs["extraction_id"], 99)
        self.assertEqual(kwargs["vendor_id"], "ROBERT_SCOTT")
        self.assertEqual(kwargs["page_num"], 2)
        self.assertEqual(kwargs["total_pages"], 5)
        self.assertEqual(kwargs["call_type"], "extraction")
        self.assertEqual(kwargs["prompt_tokens"], 1643)
        self.assertEqual(kwargs["completion_tokens"], 461)
        self.assertEqual(kwargs["total_tokens"], 2104)
        self.assertEqual(kwargs["llm_url"], "http://localhost:8001/v1/chat/completions")

    async def test_call_llm_does_not_record_usage_on_json_failure(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "not-valid-json-at-all"}}],
            "usage": {
                "prompt_tokens": 1643,
                "completion_tokens": 461,
                "total_tokens": 2104,
            },
        }

        pool = object()
        with patch("backend.extractor.httpx.AsyncClient") as MockClient, \
             patch.object(extractor.db_mod, "record_llm_usage", new=AsyncMock(return_value={"id": 1})) as mock_record:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(return_value=resp)

            with self.assertRaises(ValueError) as ctx:
                await extractor.call_llm(
                    _b64(),
                    "system",
                    "user",
                    "http://localhost:8001/v1/chat/completions",
                    "qwen3vl",
                    page_num=2,
                    total_pages=5,
                    pipeline_context={
                        "document_id": 42,
                        "extraction_id": 99,
                        "vendor_id": "ROBERT_SCOTT",
                        "job_id": 7,
                    },
                    pool=pool,
                )
            
            self.assertIn("LLM returned invalid JSON", str(ctx.exception))
            mock_record.assert_not_awaited()


class LlmResponseGuardTests(unittest.IsolatedAsyncioTestCase):
    """call_llm must not crash when the LLM returns a malformed response."""

    def _mock_client(self, response_body: dict):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = response_body
        return resp

    async def _call(self, response_body: dict):
        resp = self._mock_client(response_body)
        with patch("backend.extractor.httpx.AsyncClient") as MockClient, \
             patch.object(extractor.db_mod, "record_llm_usage", new=AsyncMock(return_value={"id": 1})):
            MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value.post = AsyncMock(return_value=resp)
            return await extractor.call_llm(
                _b64(), "sys", "usr",
                "http://localhost:8001/v1/chat/completions", "qwen3vl",
                page_num=1, total_pages=1, pool=None,
            )

    async def test_missing_choices_returns_error_sentinel(self):
        """LLM response with no 'choices' key must return error sentinel, not {}."""
        result = await self._call({"error": "model overloaded"})
        self.assertEqual(result.get("_error"), "empty_choices")

    async def test_empty_choices_list_returns_error_sentinel(self):
        """LLM response with choices=[] must return error sentinel, not {}."""
        result = await self._call({"choices": []})
        self.assertEqual(result.get("_error"), "empty_choices")

    async def test_choices_item_not_a_dict_returns_error_sentinel(self):
        """choices[0] being a non-dict (e.g. integer) must return error sentinel."""
        result = await self._call({"choices": [42]})
        self.assertEqual(result.get("_error"), "empty_choices")

    async def test_choices_item_missing_message_returns_error_sentinel(self):
        """choices[0] present but lacking 'message' key must return error sentinel."""
        result = await self._call({"choices": [{"finish_reason": "stop"}]})
        self.assertEqual(result.get("_error"), "empty_choices")


if __name__ == "__main__":
    unittest.main()
