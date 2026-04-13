from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qwen_backend.extractor as extractor


class ExtractorConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_document_processes_pages_in_parallel_batches_of_two(self) -> None:
        active = 0
        max_active = 0
        on_page_done_calls: list[int] = []

        async def fake_call_llm(
            image_b64: str,
            system_prompt: str,
            user_message: str,
            llm_url: str,
            model: str,
            mime_type: str = "image/jpeg",
            page_num: int = 0,
            total_pages: int = 0,
        ) -> dict:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {
                "vendor_name": f"Page {page_num}",
                "line_items": [{"item": page_num}],
            }

        async def on_page_done(page_num: int, total_pages: int, page_result: dict | None) -> None:
            self.assertEqual(total_pages, 4)
            self.assertIsNotNone(page_result)
            on_page_done_calls.append(page_num)

        pages = [
            {"page_number": 1, "image_b64": "a"},
            {"page_number": 2, "image_b64": "b"},
            {"page_number": 3, "image_b64": "c"},
            {"page_number": 4, "image_b64": "d"},
        ]

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)):
            output = await extractor.extract_document(
                pages=pages,
                header_fields=["vendor_name"],
                line_item_fields=["item"],
                system_prompt="system",
                format_type="single_po_multipage",
                llm_url="http://llm.local",
                model="qwen3vl",
                on_page_done=on_page_done,
            )

        self.assertEqual(max_active, 2)
        self.assertEqual(on_page_done_calls, [1, 2, 3, 4])
        self.assertEqual([pr["_page"] for pr in output["page_results"]], [1, 2, 3, 4])
        self.assertEqual(output["result"]["line_items"], [{"item": 1}, {"item": 2}, {"item": 3}, {"item": 4}])
        self.assertFalse(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 4)


if __name__ == "__main__":
    unittest.main()
