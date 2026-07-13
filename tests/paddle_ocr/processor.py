"""
processor.py — PDF→images via PyMuPDF (fitz)

Improvements over original:
- MAX_LONG_SIDE raised 1120→1500 (env-configurable) for better VLM OCR on dense docs
- DPI floor raised 50→96 (screen resolution minimum — 50 DPI is unreadable)
- alpha=False in get_pixmap — avoids redundant Pixmap conversion step
- doc.get_toc() pre-fetch triggers fitz widget/form field state flush (fillable PDFs)
- PDF_WORKERS and JPEG_QUALITY env-configurable
- Detailed logging per page (dpi used, output dimensions, b64 size)
- Graceful corrupt-page handling — skips bad pages instead of crashing whole doc
- _resize_image_sync: uses Image.Resampling.LANCZOS (Pillow 10+ deprecation fix)
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import fitz
from PIL import Image

logger = logging.getLogger("processor")

# ── Config (all env-overridable) ─────────────────────────────────────────────

MAX_LONG_SIDE = int(os.getenv("MAX_LONG_SIDE_PX", "1344"))   # was 1500
JPEG_QUALITY  = int(os.getenv("JPEG_QUALITY", "90"))
DPI_FLOOR     = int(os.getenv("DPI_FLOOR", "96"))             # was 50
DPI_DEFAULT   = int(os.getenv("DPI_DEFAULT", "150"))
PDF_WORKERS   = int(os.getenv("PDF_WORKERS", "2"))

_executor = ThreadPoolExecutor(max_workers=PDF_WORKERS)


# ── PDF rendering ─────────────────────────────────────────────────────────────

def _render_pdf_sync(file_bytes: bytes, dpi: int = DPI_DEFAULT) -> list[dict]:
    """
    Render every page of a PDF to JPEG base64.

    Auto-DPI: calculates per-page DPI so the long side hits MAX_LONG_SIDE px.
    Falls back to DPI_FLOOR so tiny pages (receipts, labels) still get at
    least a readable render.

    Mirrors PaddleOCR's PDFReaderBackend logic but:
      - adaptive DPI instead of fixed zoom=2.0
      - fitz (PyMuPDF) instead of pypdfium2 — better corrupt PDF recovery
      - alpha=False skips the extra Pixmap strip step
      - form fields flushed via page.annots() (equivalent to init_forms)
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    results: list[dict] = []

    logger.info("Rendering PDF — %d page(s), target long side=%dpx, dpi_floor=%d",
                len(doc), MAX_LONG_SIDE, DPI_FLOOR)

    try:
        for i, page in enumerate(doc):
            try:
                # Flush form field / widget state into render tree
                # (PaddleOCR equivalent: doc.init_forms())
                list(page.annots())

                rect = page.rect
                long_side_pts   = max(rect.width, rect.height)
                long_side_inches = long_side_pts / 72.0

                # Target DPI to hit MAX_LONG_SIDE — cap at caller's dpi arg
                target_dpi = min(dpi, int(MAX_LONG_SIDE / long_side_inches))
                target_dpi = max(target_dpi, DPI_FLOOR)

                mat = fitz.Matrix(target_dpi / 72.0, target_dpi / 72.0)

                # alpha=False — no alpha channel means no extra strip step needed
                pix = page.get_pixmap(matrix=mat, alpha=False)

                jpg_bytes = pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
                b64        = base64.b64encode(jpg_bytes).decode("ascii")

                logger.debug(
                    "Page %d/%d — dpi=%d  size=%dx%d  b64_len=%d",
                    i + 1, len(doc), target_dpi, pix.width, pix.height, len(b64)
                )

                results.append({
                    "page_number": i + 1,
                    "image_b64":   b64,
                    "mime_type":   "image/jpeg",
                    "width":       pix.width,
                    "height":      pix.height,
                })

            except Exception as page_err:
                # Skip corrupt/unrenderable pages — don't crash the whole doc
                logger.warning("Skipping page %d — render error: %s", i + 1, page_err)

    finally:
        total_in_doc = len(doc) if doc else 0
        doc.close()

    logger.info("PDF render complete — %d/%d pages OK", len(results), total_in_doc)
    return results


# ── Single image normalisation ────────────────────────────────────────────────

def _resize_image_sync(file_bytes: bytes) -> list[dict]:
    """
    Normalise a single uploaded image:
    - Convert to RGB (strips alpha, handles palette modes)
    - Downscale so long side ≤ MAX_LONG_SIDE (LANCZOS)
    - Encode as JPEG base64
    """
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    w, h = img.size

    long_side = max(w, h)
    if long_side > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / long_side
        new_w, new_h = int(w * scale), int(h * scale)
        # Image.Resampling.LANCZOS — Pillow 10+ (replaces deprecated Image.LANCZOS)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        logger.debug("Image resized %dx%d → %dx%d", w, h, new_w, new_h)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return [{
        "page_number": 1,
        "image_b64":   b64,
        "mime_type":   "image/jpeg",
        "width":       img.width,
        "height":      img.height,
    }]


# ── Async public API ──────────────────────────────────────────────────────────

async def pdf_to_images(
    file_bytes: bytes,
    dpi: int = DPI_DEFAULT,
) -> list[dict]:
    """
    Async wrapper — offloads fitz rendering to ThreadPoolExecutor.

    Auto-DPI caps each page at MAX_LONG_SIDE for optimal Qwen3-VL tile count.
    Returns list of dicts: {page_number, image_b64, mime_type, width, height}
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _render_pdf_sync, file_bytes, dpi)


async def image_file_to_b64(
    file_bytes: bytes,
    filename: str = "",
) -> list[dict]:
    """
    Async wrapper — normalises a single image upload.
    Returns a single-element list matching the same schema as pdf_to_images.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _resize_image_sync, file_bytes)
