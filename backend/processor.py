"""
processor.py — PDF→images via Google PDFium (pypdfium2)

- Commercial Safe   : Apache/BSD licensed (no PyMuPDF AGPL)
- True Concurrency  : PDFium releases GIL — real parallel rendering
- Chrome Quality    : draw_annots + draw_forms + FPDF_LCD_TEXT (0x02)
- Dual VLM Budget   : long-side + pixel-area cap (matches Qwen3-VL _resize_pil)
- Memory Safe       : bitmap.close() + pdf.close() in finally blocks
- Corrupt-PDF Safe  : zero-dimension guard prevents ZeroDivisionError
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from concurrent.futures import ThreadPoolExecutor

import pypdfium2 as pdfium
from PIL import Image

from .config import (
    MAX_LONG_SIDE_PX as MAX_LONG_SIDE,
    MAX_PIXELS,
    JPEG_QUALITY,
    DPI_FLOOR,
    DPI_DEFAULT,
    PDF_WORKERS,
)

logger = logging.getLogger(__name__)

# PDFium render flag: subpixel ClearType text (same as Chrome)
_FPDF_LCD_TEXT = 0x02

_executor = ThreadPoolExecutor(max_workers=PDF_WORKERS)


def count_pdf_pages(file_bytes: bytes) -> int:
    """Return PDF page count without rendering. Raises ValueError on corrupt input."""
    pdf = None
    try:
        pdf = pdfium.PdfDocument(file_bytes)
        return len(pdf)
    except Exception as exc:
        raise ValueError(f"Could not open PDF to count pages: {exc}") from exc
    finally:
        if pdf is not None:
            pdf.close()


# ── Qwen3-VL dual-constraint resize ───────────────────────────────────────────

def _resize_to_vlm_budget(img: Image.Image) -> Image.Image:
    """
    Matches Qwen3-VL's internal _resize_pil logic exactly.
    Two constraints — tighter one wins:
      1. Long side must not exceed MAX_LONG_SIDE
      2. Total pixel area must not exceed MAX_PIXELS
    Uses LANCZOS for sharpest downscale quality.
    """
    w, h = img.size
    scale_side = min(1.0, MAX_LONG_SIDE / max(w, h))
    scale_area = (MAX_PIXELS / (w * h)) ** 0.5 if (w * h) > MAX_PIXELS else 1.0
    scale = min(scale_side, scale_area)

    if scale < 1.0:
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
    else:
        nw, nh = w, h

    # Qwen3-VL patch size alignment: ensure dimensions are multiples of 32
    # This prevents the VLM from internally padding/resizing and throwing off bbox coordinates
    # (Fixes the issue where bounding boxes point slightly above the field)
    nw = max(32, round(nw / 32) * 32)
    nh = max(32, round(nh / 32) * 32)

    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        logger.debug("VLM resize %.3f → %dx%d (aligned to 32)", scale, nw, nh)

    return img


# ── PDF rendering ─────────────────────────────────────────────────────────────

def _render_pdf_sync(file_bytes: bytes, dpi: int = DPI_DEFAULT, max_pages: int | None = None) -> list[dict]:
    """
    Render every page of a PDF to JPEG base64 using Google's PDFium engine.

    Auto-DPI: calculates per-page DPI so the long side hits MAX_LONG_SIDE px.
    Falls back to DPI_FLOOR so tiny pages (receipts, labels) remain readable.

    Args:
        file_bytes: Raw PDF bytes.
        dpi: Target DPI (subject to per-page adaptive scaling).
        max_pages: If set, render at most this many pages (from page 1).
    """
    results: list[dict] = []

    pdf = None
    try:
        pdf = pdfium.PdfDocument(file_bytes)
    except Exception as exc:
        raise ValueError(f"PDF could not be opened (corrupt or unsupported format): {exc}") from exc

    total_pages = len(pdf)
    render_count = min(total_pages, max_pages) if max_pages else total_pages

    logger.info("PDFium render — %d page(s) (rendering %d), target=%dpx, dpi_floor=%d",
                total_pages, render_count, MAX_LONG_SIDE, DPI_FLOOR)

    try:
        for i in range(render_count):
            page = pdf[i]
            page_num = i + 1

            try:
                w_pts = page.get_width()
                h_pts = page.get_height()
                max_pts = max(w_pts, h_pts)

                # Guard: corrupted PDFs can report 0x0 page dimensions
                if max_pts <= 0:
                    logger.warning(
                        "Page %d has invalid dimensions (%.1fx%.1f) — skipping.",
                        page_num, w_pts, h_pts
                    )
                    continue

                # Adaptive DPI — scale so long side hits MAX_LONG_SIDE exactly
                target_dpi = min(dpi, int(MAX_LONG_SIDE / (max_pts / 72.0)))
                target_dpi = max(target_dpi, DPI_FLOOR)

                bitmap = page.render(
                    scale=target_dpi / 72.0,
                    fill_color=(255, 255, 255, 255),  # white background, no transparency
                    draw_annots=True,                  # render annotations (includes forms in pypdfium2)
                    extra_flags=_FPDF_LCD_TEXT,        # ClearType subpixel text (Chrome flag)
                )

                try:
                    img = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()  # free C++ bitmap heap immediately after PIL copy

                orig_w, orig_h = img.size

                # Apply Qwen3-VL dual-budget resize (long-side + pixel-area)
                img = _resize_to_vlm_budget(img)

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=JPEG_QUALITY)  # no optimize=True — local pipeline
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")

                logger.debug(
                    "Page %d/%d — dpi=%d size=%dx%d b64_len=%d",
                    page_num, total_pages, target_dpi, img.width, img.height, len(b64)
                )

                results.append({
                    "page_number": page_num,
                    "image_b64":   b64,
                    "mime_type":   "image/jpeg",
                    "width":       img.width,
                    "height":      img.height,
                    "orig_width":  orig_w,
                    "orig_height": orig_h,
                    "doc_total_pages": total_pages,
                })

            except Exception as page_err:
                # Skip corrupt/unrenderable pages — don't crash the whole document
                logger.warning("Skipping page %d — render error: %s", page_num, page_err)

    finally:
        if pdf is not None:
            pdf.close()  # always release PDFium document resources

    logger.info("PDF render complete — %d/%d pages OK", len(results), total_pages)
    return results


# ── Single image normalisation ────────────────────────────────────────────────

def _resize_image_sync(file_bytes: bytes) -> list[dict]:
    """
    Normalise a single uploaded image:
      - Convert to RGB (strips alpha, handles palette / grayscale modes)
      - Apply Qwen3-VL dual-budget resize (long-side + pixel-area cap)
      - Encode as JPEG base64
    """
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    w, h = img.size

    img = _resize_to_vlm_budget(img)
    logger.debug("Image normalised %dx%d → %dx%d", w, h, img.width, img.height)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)  # no optimize=True — local pipeline
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return [{
        "page_number": 1,
        "image_b64":   b64,
        "mime_type":   "image/jpeg",
        "width":       img.width,
        "height":      img.height,
        "orig_width":  w,
        "orig_height": h,
        "doc_total_pages": 1,
    }]


# ── Async public API ──────────────────────────────────────────────────────────

async def pdf_to_images(
    file_bytes: bytes,
    dpi: int = DPI_DEFAULT,
    max_pages: int | None = None,
) -> list[dict]:
    """
    Async wrapper — offloads PDFium rendering to ThreadPoolExecutor.
    Auto-DPI caps each page at MAX_LONG_SIDE for optimal Qwen3-VL tile count.
    Returns list of dicts: {page_number, image_b64, mime_type, width, height}

    Args:
        file_bytes: Raw PDF bytes.
        dpi: Target DPI.
        max_pages: If set, render at most this many pages.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _render_pdf_sync, file_bytes, dpi, max_pages
    )


async def image_file_to_b64(
    file_bytes: bytes,
    filename: str = "",
) -> list[dict]:
    """
    Async wrapper — normalises a single image upload.
    filename retained for future extension-based routing (e.g. TIFF handling).
    Returns a single-element list matching the same schema as pdf_to_images.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _resize_image_sync, file_bytes)