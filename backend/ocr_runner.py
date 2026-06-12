"""
ocr_runner.py -- Run PaddleOCR on rendered page images.

Takes base64-encoded page images (from processor.py) and returns
bounding boxes + text for every detected text region, formatted
for the review geometry pipeline.

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

from .config import OCR_CPU_THREADS, OCR_WORKERS

# Bound the native math-library thread pools (OpenMP / MKL / OpenBLAS) BEFORE
# numpy and PaddleOCR's MKLDNN backend initialize them. Each OCR worker process
# runs up to OCR_WORKERS engine threads concurrently; with these unset, every
# engine call lets OpenMP fan out across *all* CPU cores, so N concurrent calls
# oversubscribe the box to N x cores threads thrashing the scheduler. Pinning the
# libs to OCR_CPU_THREADS is what makes PaddleOCR(cpu_threads=...) actually
# effective. setdefault keeps any explicit deploy override (e.g. OMP_NUM_THREADS
# in .env) authoritative.
for _thread_env in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_thread_env, str(OCR_CPU_THREADS))

os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

logger = logging.getLogger("ocr_runner")

# Ensure this logger has at least one handler that flushes immediately,
# so log lines survive PaddleOCR/MKLDNN C++ stderr interleaving in Docker.
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    logger.propagate = True


_executor = ThreadPoolExecutor(max_workers=OCR_WORKERS)

class OCRUnavailable(Exception):
    """Exception raised when PaddleOCR fails to initialize or run."""
    pass


import threading
import numpy as np
import cv2

_ocr_local = threading.local()


def _get_ocr_engine():
    """Lazy-initialize PaddleOCR engine (thread-local)."""
    if not hasattr(_ocr_local, "engine"):
        logger.info("Initializing PaddleOCR engine...")
        t0 = time.perf_counter()
        try:
            from .config import OCR_DEVICE
            from paddleocr import PaddleOCR
            _ocr_local.engine = PaddleOCR(
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name="PP-OCRv5_mobile_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device=OCR_DEVICE,
                enable_mkldnn=(OCR_DEVICE == "cpu"),
                cpu_threads=OCR_CPU_THREADS,
                return_word_box=True,
            )
        except Exception as exc:
            logger.exception("PaddleOCR model initialization failed")
            raise OCRUnavailable(f"OCR model initialization failed: {exc}") from exc
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

    t0 = time.perf_counter()
    try:
        # Decode base64 directly to in-memory numpy array (no disk write needed)
        img_bytes = base64.b64decode(image_b64, validate=True)
        img_array = np.frombuffer(img_bytes, dtype=np.uint8)
        img_cv = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if img_cv is None:
            raise ValueError("OpenCV could not decode OCR page image")

        ocr = _get_ocr_engine()
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

        return {
            "page_number": page_number,
            "words": words,
        }

    except Exception as exc:
        logger.error("PaddleOCR failed on page %d: %s", page_number, exc)
        return {
            "page_number": page_number,
            "words": [],
            "_ocr_error": str(exc),
            "_ocr_error_type": type(exc).__name__,
        }



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

    # Check for failures and raise OCRUnavailable to avoid silent blank page failures
    failed_pages = [p for p in ocr_pages if p.get("_ocr_error")]
    if failed_pages:
        details = "; ".join(f"page {p['page_number']}: {p['_ocr_error']}" for p in failed_pages)
        raise OCRUnavailable(f"PaddleOCR failed: {details}")

    elapsed = (time.perf_counter() - t0) * 1000
    total_words = sum(len(p["words"]) for p in ocr_pages)
    logger.info(
        "PaddleOCR complete: %d page(s), %d total words, %.0fms",
        len(ocr_pages), total_words, elapsed,
    )
    sys.stderr.flush()

    return ocr_pages
