"""
ocr_runner.py -- Run PaddleOCR on rendered page images.

Takes base64-encoded page images (from processor.py) and returns
bounding boxes + text for every detected text region, formatted
for text_matcher.py.

Output per page:
    {
        "page_number": 1,
        "words": [
            {"text": "FRESH PRODUCTS, INC.", "box": [128,214,338,229], "score": 0.95},
            ...
        ]
    }
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

logger = logging.getLogger("ocr_runner")

try:
    from .phoenix_tracing import trace_ocr_page
except ImportError:
    from phoenix_tracing import trace_ocr_page

_executor = ThreadPoolExecutor(max_workers=3)

import threading
_ocr_local = threading.local()

def _get_ocr_engine():
    """Lazy-initialize PaddleOCR engine (thread-local)."""
    if not hasattr(_ocr_local, "engine"):
        logger.info("Initializing PaddleOCR engine...")
        t0 = time.perf_counter()
        from paddleocr import PaddleOCR
        _ocr_local.engine = PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
            enable_mkldnn=False,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("PaddleOCR engine ready for thread %s in %.0fms", threading.current_thread().name, elapsed)
    return _ocr_local.engine


def _b64_to_temp_path(image_b64: str, page_number: int) -> str:
    """Decode base64 image and write to a temp file (PaddleOCR needs a file path)."""
    import tempfile
    img_bytes = base64.b64decode(image_b64)
    # Determine extension from JPEG header
    ext = ".jpg" if img_bytes[:2] == b'\xff\xd8' else ".png"
    fd, path = tempfile.mkstemp(suffix=f"_page{page_number}{ext}")
    with os.fdopen(fd, "wb") as f:
        f.write(img_bytes)
    return path


def _run_ocr_on_page(image_b64: str, page_number: int) -> dict:
    """
    Run PaddleOCR on a single page image.

    Args:
        image_b64: Base64-encoded JPEG/PNG image
        page_number: 1-indexed page number

    Returns:
        {"page_number": 1, "words": [{"text": "...", "box": [x0,y0,x1,y1], "score": 0.95}]}
    """
    with trace_ocr_page(page_number) as ocr_page_ctx:
        t0 = time.perf_counter()
        tmp_path = None
        try:
            ocr = _get_ocr_engine()
            tmp_path = _b64_to_temp_path(image_b64, page_number)

            result = ocr.predict(tmp_path)

            words = []
            for res in result:
                rec_texts = res["rec_texts"]
                rec_scores = res["rec_scores"]
                rec_boxes = res["rec_boxes"]

                for text, score, box in zip(rec_texts, rec_scores, rec_boxes):
                    text_str = str(text).strip()
                    if not text_str:
                        continue
                    # box is numpy array [x0, y0, x1, y1]
                    box_list = [int(round(float(v))) for v in box.tolist()]
                    words.append({
                        "text": text_str,
                        "box": box_list,
                        "score": round(float(score), 4),
                    })

            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "PaddleOCR page %d: %d words detected in %.0fms",
                page_number, len(words), elapsed,
            )

            ocr_page_ctx["words_detected"] = len(words)

            return {
                "page_number": page_number,
                "words": words,
            }

        except Exception as exc:
            logger.error("PaddleOCR failed on page %d: %s", page_number, exc)
            return {"page_number": page_number, "words": []}

        finally:
            # Clean up temp file
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


async def run_ocr_on_pages(pages: list[dict]) -> list[dict]:
    if not pages:
        return []

    t0 = time.perf_counter()
    logger.info("Starting PaddleOCR on %d page(s)...", len(pages))

    loop = asyncio.get_running_loop()

    # Process pages in parallel using the 3-worker thread pool
    tasks = []
    for page in pages:
        task = loop.run_in_executor(
            _executor,
            _run_ocr_on_page,
            page["image_b64"],
            page["page_number"]
        )
        tasks.append(task)
        
    ocr_pages = await asyncio.gather(*tasks)

    elapsed = (time.perf_counter() - t0) * 1000
    total_words = sum(len(p["words"]) for p in ocr_pages)
    logger.info(
        "PaddleOCR complete: %d page(s), %d total words, %.0fms",
        len(ocr_pages), total_words, elapsed,
    )

    return ocr_pages
