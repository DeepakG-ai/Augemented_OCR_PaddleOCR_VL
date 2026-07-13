"""
ocr_pipeline.py — Parallel PaddleOCR over PDF pages

Imports pdf_to_images from processor.py (unchanged).
Adds 4-worker OCR layer on top.

Usage:
    results = await run_pdf_ocr(pdf_bytes, out_dir=Path("output"))
    for page in results:
        print(page["page_number"], page["rec_texts"])
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from PIL import Image

# ── import from your existing processor.py ───────────────────────────────────
from processor import pdf_to_images, DPI_DEFAULT, JPEG_QUALITY

logger = logging.getLogger("ocr_pipeline")

# ── Config ────────────────────────────────────────────────────────────────────

OCR_WORKERS = int(os.getenv("OCR_WORKERS", "4"))

os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

# ── Thread-local PaddleOCR instances ─────────────────────────────────────────
# Paddle predictor is NOT thread-safe → each thread owns its own instance

_ocr_local = threading.local()

def _get_ocr():
    """Return this thread's PaddleOCR instance, creating it once if needed."""
    if not hasattr(_ocr_local, "ocr"):
        from paddleocr import PaddleOCR
        _ocr_local.ocr = PaddleOCR(
            text_detection_model_name    = "PP-OCRv5_mobile_det",
            text_recognition_model_name  = "PP-OCRv5_mobile_rec",
            use_doc_orientation_classify = False,
            use_doc_unwarping            = False,
            use_textline_orientation     = False,
            device        = "cpu",
            enable_mkldnn = False,
        )
        logger.debug("PaddleOCR created for thread %s", threading.current_thread().name)
    return _ocr_local.ocr

# ── Batch split ───────────────────────────────────────────────────────────────

def _split_batches(items: list, n_workers: int) -> list[list]:
    """
    Distribute items evenly across n_workers.

    Example — 5 pages, 4 workers:
      divmod(5,4) → k=1, remainder=1
      worker0 → [p1, p2]   (k+1)
      worker1 → [p3]       (k)
      worker2 → [p4]       (k)
      worker3 → [p5]       (k)
    """
    k, remainder = divmod(len(items), n_workers)
    batches, idx = [], 0
    for i in range(n_workers):
        size = k + (1 if i < remainder else 0)
        if size:
            batches.append(items[idx: idx + size])
            idx += size
    return batches

# ── OCR single page ───────────────────────────────────────────────────────────

def _ocr_page(page_dict: dict, out_dir: Path | None = None) -> dict:
    """Run OCR on one page. page_dict comes from processor.pdf_to_images."""
    ocr = _get_ocr()

    # decode base64 → save temp jpg (PaddleOCR needs a file path or ndarray)
    img_bytes = base64.b64decode(page_dict["image_b64"])
    img = Image.open(BytesIO(img_bytes)).convert("RGB")

    tmp_path = Path(f"/tmp/ocr_page_{page_dict['page_number']}.jpg")
    img.save(str(tmp_path), format="JPEG", quality=JPEG_QUALITY)

    result = ocr.predict(str(tmp_path))

    texts, boxes, mapping = [], [], []

    for res in result:
        if out_dir:
            res.save_to_json(str(out_dir))   # paddle raw JSON  → disk
            res.save_to_img(str(out_dir))    # annotated image  → disk

        for text, score, poly, box in zip(
            res["rec_texts"],
            res["rec_scores"],
            res["dt_polys"],
            res["rec_boxes"],
        ):
            texts.append(text)
            boxes.append(poly.tolist())
            mapping.append({
                "text" : text,
                "score": round(float(score), 4),
                "poly" : poly.tolist(),   # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                "box"  : box.tolist(),    # [x_min, y_min, x_max, y_max]
            })

    tmp_path.unlink(missing_ok=True)

    return {
        **page_dict,          # page_number, width, height, mime_type, image_b64
        "rec_texts": texts,
        "bboxes"   : boxes,
        "mapping"  : mapping,
    }

# ── OCR batch (runs in one thread) ───────────────────────────────────────────

def _ocr_batch(pages: list[dict], out_dir: Path | None) -> list[dict]:
    return [_ocr_page(p, out_dir) for p in pages]

# ── Parallel OCR orchestrator ─────────────────────────────────────────────────

def run_ocr_parallel(
    pages    : list[dict],
    out_dir  : Path | None = None,
    n_workers: int = OCR_WORKERS,
) -> list[dict]:
    """
    Run OCR across all pages using n_workers threads.
    Results are returned in original page order.

    Args:
        pages     : list of dicts from processor.pdf_to_images()
        out_dir   : optional — saves paddle JSON + annotated images here
        n_workers : thread count (default 4)

    Returns:
        Same list with added keys: rec_texts, bboxes, mapping
    """
    if not pages:
        return []

    actual_workers = min(n_workers, len(pages))
    batches        = _split_batches(pages, actual_workers)

    logger.info(
        "OCR — %d pages | %d workers | batch sizes %s",
        len(pages), actual_workers, [len(b) for b in batches],
    )

    all_results: list[dict] = [None] * len(pages)

    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        future_map = {
            executor.submit(_ocr_batch, batch, out_dir): batch
            for batch in batches
        }
        for future in as_completed(future_map):
            batch = future_map[future]
            try:
                for page_result in future.result():
                    all_results[page_result["page_number"] - 1] = page_result
            except Exception as e:
                logger.error("Batch failed: %s", e)
                for page in batch:
                    all_results[page["page_number"] - 1] = {
                        **page,
                        "rec_texts": [], "bboxes": [], "mapping": [],
                        "error": str(e),
                    }

    return all_results

# ── Async public entry point ──────────────────────────────────────────────────

_ocr_executor = ThreadPoolExecutor(max_workers=OCR_WORKERS)

async def run_pdf_ocr(
    file_bytes: bytes,
    out_dir   : Path | None = None,
    dpi       : int = DPI_DEFAULT,
    n_workers : int = OCR_WORKERS,
) -> list[dict]:
    """
    Full async pipeline: PDF bytes → rendered pages → parallel OCR.

    Args:
        file_bytes : raw PDF bytes
        out_dir    : optional folder for paddle JSON + annotated images
        dpi        : render DPI passed to processor.pdf_to_images
        n_workers  : OCR thread count (default 4)

    Returns:
        list[dict] per page — keys:
            page_number, width, height, image_b64,
            rec_texts, bboxes, mapping

    Example:
        results = await run_pdf_ocr(pdf_bytes, out_dir=Path("output"))
        for page in results:
            print(page["page_number"], page["rec_texts"])
    """
    # Step 1 — render PDF → images (uses processor.py unchanged)
    pages = await pdf_to_images(file_bytes, dpi)

    # Step 2 — OCR all pages in parallel
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ocr_executor,
        lambda: run_ocr_parallel(pages, out_dir, n_workers),
    )


# ── Optional: write output files ─────────────────────────────────────────────

def save_ocr_outputs(pages: list[dict], out_dir: Path) -> None:
    """
    Write raw_text.txt, bboxes.json, mapping.json per page.

    Files named:  page_1_raw_text.txt, page_1_mapping.json, etc.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        n = page["page_number"]
        (out_dir / f"page_{n}_raw_text.txt").write_text(
            "\n".join(page.get("rec_texts", [])), encoding="utf-8"
        )
        (out_dir / f"page_{n}_bboxes.json").write_text(
            json.dumps(page.get("bboxes", []), indent=2), encoding="utf-8"
        )
        (out_dir / f"page_{n}_mapping.json").write_text(
            json.dumps(page.get("mapping", []), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    logger.info("Saved OCR outputs for %d pages → %s", len(pages), out_dir)
