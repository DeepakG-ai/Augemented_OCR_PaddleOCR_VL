from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.extractor as extractor


def _make_pages(n: int) -> list[dict]:
    return [{"page_number": i, "image_b64": str(i)} for i in range(1, n + 1)]


def _ok(page_num: int) -> dict:
    return {"vendor_name": f"V{page_num}", "line_items": [{"item": page_num}]}


_COMMON = dict(
    header_fields=["vendor_name"],
    line_item_fields=["item"],
    system_prompt="sys",
    format_type="single_po_multipage",
    llm_url="http://llm.local",
    model="qwen",
)


class ExtractorConcurrencyTests(unittest.IsolatedAsyncioTestCase):

    # ── original test preserved ──────────────────────────────────────────────

    async def test_extract_document_processes_pages_in_sequential_batches_of_one(self) -> None:
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
            **kwargs,
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

        self.assertEqual(max_active, 1)
        self.assertEqual(on_page_done_calls, [1, 2, 3, 4])
        self.assertEqual([pr["_page"] for pr in output["page_results"]], [1, 2, 3, 4])
        self.assertEqual(
            output["result"]["line_items"],
            [{"item": 1, "_page": 1}, {"item": 2, "_page": 2},
             {"item": 3, "_page": 3}, {"item": 4, "_page": 4}]
        )
        self.assertFalse(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 4)

    # ── new tests ────────────────────────────────────────────────────────────

    async def test_phase_b_pages_run_concurrently(self) -> None:
        """Pages 2-N within a batch all start before any of them finish."""
        active = 0
        max_active_p1 = 0
        max_active_phase_b = 0

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            nonlocal active, max_active_p1, max_active_phase_b
            active += 1
            if page_num == 1:
                max_active_p1 = max(max_active_p1, active)
            else:
                max_active_phase_b = max(max_active_phase_b, active)
            await asyncio.sleep(0.05)
            active -= 1
            return _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 3):
            await extractor.extract_document(pages=_make_pages(4), **_COMMON)

        self.assertEqual(max_active_p1, 1)     # page 1 ran alone
        self.assertEqual(max_active_phase_b, 3)  # pages 2,3,4 ran together

    async def test_page1_always_completes_before_phase_b_starts(self) -> None:
        """No page > 1 may start before page 1 finishes."""
        page1_done = False
        phase_b_before_p1 = False

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            nonlocal page1_done, phase_b_before_p1
            if page_num > 1 and not page1_done:
                phase_b_before_p1 = True
            await asyncio.sleep(0.01)
            if page_num == 1:
                page1_done = True
            return _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 4):
            await extractor.extract_document(pages=_make_pages(5), **_COMMON)

        self.assertFalse(phase_b_before_p1)

    async def test_page1_failure_stops_extraction_entirely(self) -> None:
        """If page 1 fails, Phase B is never entered and result is marked cancelled."""
        calls: list[int] = []

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            calls.append(page_num)
            return {"_error": "timeout"} if page_num == 1 else _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)):
            output = await extractor.extract_document(pages=_make_pages(4), **_COMMON)

        self.assertEqual(calls, [1])
        self.assertTrue(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 0)

    async def test_phase_b_batch_failure_stops_remaining_batches(self) -> None:
        """A failed page in batch [2,3] prevents batch [4,5] and [6] from running.

        With LLM_PAGE_BATCH_SIZE=2 on 6 pages:
          Phase A:       page 1
          Phase B batch1: pages 2,3  ← page 3 fails → stop
          Phase B batch2: pages 4,5  ← never reached
          Phase B batch3: page 6     ← never reached
        """
        calls: list[int] = []

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            calls.append(page_num)
            return {"_error": "timeout"} if page_num == 3 else _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 2):
            output = await extractor.extract_document(pages=_make_pages(6), **_COMMON)

        self.assertEqual(sorted(calls), [1, 2, 3])   # 4,5,6 never called
        self.assertTrue(output["cancelled"])
        for page in (4, 5, 6):
            self.assertNotIn(page, calls)

    async def test_retry_skips_already_completed_page1(self) -> None:
        """When page 1 already succeeded (retry), Phase A is empty and only pages 2-3 run."""
        calls: list[int] = []

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            calls.append(page_num)
            return _ok(page_num)

        existing = [{"_page": 1, "_total_pages": 3, "vendor_name": "Acme", "line_items": []}]

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)):
            output = await extractor.extract_document(
                pages=_make_pages(3),
                existing_page_results=existing,
                **_COMMON,
            )

        self.assertEqual(sorted(calls), [2, 3])   # page 1 not re-called
        self.assertFalse(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 3)

    async def test_cancellation_before_page1_makes_zero_llm_calls(self) -> None:
        """A pre-set cancel_event prevents any LLM call."""
        calls: list[int] = []

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            calls.append(page_num)
            return _ok(page_num)

        cancel_event = asyncio.Event()
        cancel_event.set()

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)):
            output = await extractor.extract_document(
                pages=_make_pages(4),
                cancel_event=cancel_event,
                **_COMMON,
            )

        self.assertEqual(calls, [])
        self.assertTrue(output["cancelled"])

    async def test_cancellation_set_during_page1_blocks_phase_b(self) -> None:
        """Setting cancel_event inside page-1's LLM call stops Phase B before it starts."""
        calls: list[int] = []
        cancel_event = asyncio.Event()

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            calls.append(page_num)
            if page_num == 1:
                cancel_event.set()   # signal during page-1
            await asyncio.sleep(0.01)
            return _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 3):
            output = await extractor.extract_document(
                pages=_make_pages(4),
                cancel_event=cancel_event,
                **_COMMON,
            )

        self.assertEqual(calls, [1])   # only page 1 ran
        self.assertTrue(output["cancelled"])
        # Page 1 result is still recorded (Phase A completed before cancellation was noticed)
        self.assertTrue(any(pr.get("_page") == 1 for pr in output["page_results"]))

    async def test_single_page_document_phase_b_is_no_op(self) -> None:
        """One-page document: page 1 runs, nothing else."""
        calls: list[int] = []

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            calls.append(page_num)
            return _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)):
            output = await extractor.extract_document(pages=_make_pages(1), **_COMMON)

        self.assertEqual(calls, [1])
        self.assertFalse(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 1)

    async def test_batch_size_larger_than_remaining_pages(self) -> None:
        """Oversized batch (size 10) on 4 pages — all three Phase-B pages run in one batch."""
        active_phase_b = 0
        max_active_phase_b = 0

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            nonlocal active_phase_b, max_active_phase_b
            if page_num > 1:
                active_phase_b += 1
                max_active_phase_b = max(max_active_phase_b, active_phase_b)
            await asyncio.sleep(0.05)
            if page_num > 1:
                active_phase_b -= 1
            return _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 10):
            output = await extractor.extract_document(pages=_make_pages(4), **_COMMON)

        self.assertEqual(max_active_phase_b, 3)   # pages 2,3,4 in one batch
        self.assertFalse(output["cancelled"])
        self.assertEqual(output["last_completed_page"], 4)

    async def test_on_page_done_callbacks_always_in_page_order(self) -> None:
        """Callbacks fire in page-number order even when a slow page finishes last.

        asyncio.gather preserves input order, so the slow page-3 result lands
        in position 1 of batch_results (after page-2), not at the end.
        """
        callback_order: list[int] = []

        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            await asyncio.sleep(0.1 if page_num == 3 else 0.01)
            return _ok(page_num)

        async def on_page_done(page_num: int, total: int, result: dict | None) -> None:
            callback_order.append(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 3):
            await extractor.extract_document(
                pages=_make_pages(4),
                on_page_done=on_page_done,
                **_COMMON,
            )

        self.assertEqual(callback_order, [1, 2, 3, 4])

    async def test_merge_order_preserved_when_fast_page_finishes_before_slow(self) -> None:
        """Merged line_items are always in page order even if page 4 finishes before page 2.

        page 2 sleeps 0.15 s (slow); pages 3 and 4 sleep 0.01 s (fast).
        Without the post-sort on line 688 of extractor.py, items would appear as
        [item_1, item_3, item_4, item_2].  With the sort, order is [1, 2, 3, 4].
        """
        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            # page 2 is slowest; 3 and 4 finish first
            await asyncio.sleep(0.15 if page_num == 2 else 0.01)
            return _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 3):
            output = await extractor.extract_document(pages=_make_pages(4), **_COMMON)

        self.assertFalse(output["cancelled"])
        line_items = output["result"]["line_items"]
        # Items must be in page order: item 1, 2, 3, 4 — not 1, 3, 4, 2
        self.assertEqual([li["item"] for li in line_items], [1, 2, 3, 4])
        self.assertEqual([li["_page"] for li in line_items], [1, 2, 3, 4])

    async def test_last_completed_page_stops_at_gap_inside_parallel_batch(self) -> None:
        """last_completed_page reflects the last unbroken streak, not the highest success.

        With LLM_PAGE_BATCH_SIZE=3 all of pages 2,3,4 run concurrently.
        Page 3 fails; pages 2 and 4 succeed.
        page_results = [1✓, 2✓, 3✗, 4✓]  → last_completed_page should be 2 (gap at 3).
        """
        async def fake_call_llm(image_b64, sys_prompt, user_msg, llm_url, model,
                                *, page_num=0, **kwargs):
            return {"_error": "bad"} if page_num == 3 else _ok(page_num)

        with patch.object(extractor, "call_llm", new=AsyncMock(side_effect=fake_call_llm)), \
             patch.object(extractor, "LLM_PAGE_BATCH_SIZE", 3):
            output = await extractor.extract_document(pages=_make_pages(4), **_COMMON)

        self.assertEqual(output["last_completed_page"], 2)
        self.assertTrue(output["cancelled"])


if __name__ == "__main__":
    unittest.main()
