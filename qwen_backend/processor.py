"""
processor.py — PDF→images via PyMuPDF (fitz), image base64 helpers.
All sync fitz work is offloaded to a threadpool executor.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor

import fitz  # PyMuPDF

# Shared executor — avoids creating a new pool per call
_executor = ThreadPoolExecutor(max_workers=2)


# ── Sync inner function (runs in threadpool) ─────────────────────────

def _render_pdf_sync(file_bytes: bytes, dpi: int) -> list[dict]:
    """
    Render every page of a PDF to PNG base64.
    Returns list of {page_number, image_b64, width, height}.
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    results: list[dict] = []
    try:
        for i, page in enumerate(doc):
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode("ascii")
            results.append({
                "page_number": i + 1,
                "image_b64": b64,
                "width": pix.width,
                "height": pix.height,
            })
    finally:
        doc.close()
    return results


# ── Async wrappers ───────────────────────────────────────────────────

async def pdf_to_images(file_bytes: bytes, dpi: int = 150) -> list[dict]:
    """
    Async wrapper — offloads sync fitz rendering to threadpool so the
    event loop is never blocked.  DPI 150 = good enough for text extraction.
    Returns list of {page_number, image_b64, width, height}.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _render_pdf_sync, file_bytes, dpi)


async def image_file_to_b64(file_bytes: bytes) -> list[dict]:
    """
    For JPEG/PNG uploads — wrap a single image as page 1.
    No rendering needed; just base64-encode the raw bytes.
    """
    b64 = base64.b64encode(file_bytes).decode("ascii")
    # We don't know dimensions without parsing; set 0 (frontend can read from img tag)
    return [{
        "page_number": 1,
        "image_b64": b64,
        "width": 0,
        "height": 0,
    }]
