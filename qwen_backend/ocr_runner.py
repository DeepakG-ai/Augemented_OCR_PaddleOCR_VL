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
import sys
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

logger = logging.getLogger("ocr_runner")

# Ensure this logger has at least one handler that flushes immediately,
# so log lines survive PaddleOCR/MKLDNN C++ stderr interleaving in Docker.
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _h.setLevel(logging.DEBUG)
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False #True before duplicate logs

try:
    from .phoenix_tracing import trace_ocr_page
except ImportError:
    from phoenix_tracing import trace_ocr_page

_executor = ThreadPoolExecutor(max_workers=3)

import threading
import numpy as np
import cv2

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
            enable_mkldnn=True,
            cpu_threads=4,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("PaddleOCR engine ready for thread %s in %.0fms", threading.current_thread().name, elapsed)
        sys.stderr.flush()
    return _ocr_local.engine


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
        try:
            ocr = _get_ocr_engine()
            
            # Decode base64 directly to in-memory numpy array (no disk write needed)
            img_bytes = base64.b64decode(image_b64)
            img_array = np.frombuffer(img_bytes, dtype=np.uint8)
            img_cv = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

            result = ocr.predict(img_cv)

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
            sys.stderr.flush()

            ocr_page_ctx["words_detected"] = len(words)

            return {
                "page_number": page_number,
                "words": words,
            }

        except Exception as exc:
            logger.error("PaddleOCR failed on page %d: %s", page_number, exc)
            return {"page_number": page_number, "words": []}


async def run_ocr_on_pages(pages: list[dict]) -> list[dict]:
    if not pages:
        return []

    t0 = time.perf_counter()
    logger.info("Starting PaddleOCR on %d page(s)...", len(pages))
    sys.stderr.flush()

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
    sys.stderr.flush()

    return ocr_pages
