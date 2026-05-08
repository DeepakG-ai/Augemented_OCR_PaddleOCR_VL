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


if __name__ == "__main__":
    unittest.main()
