"""
geometry.py — unified per-page text geometry service.

Produces a single {page_number, source, char_count, word_count, words[]} shape
regardless of whether the source is pypdfium2 (digital pages) or PaddleOCR
(scanned pages). Word boxes always live in the final rendered image pixel
space (post Qwen3-VL resize) — the same coords the frontend and PaddleOCR
consume.
"""
from __future__ import annotations

import logging

import pypdfium2 as pdfium

if __package__:
    from . import pdf_extractor
else:
    import pdf_extractor  # type: ignore[no-redef]

logger = logging.getLogger("geometry")

SOURCE_DIGITAL = "pypdfium"
SOURCE_SCANNED = "paddleocr"


def _rescale_words(words: list[dict], scale_x: float, scale_y: float) -> list[dict]:
    out: list[dict] = []
    for w in words:
        x0, y0, x1, y1 = w["box"]
        out.append(
            {
                "text": w["text"],
                "box": [
                    int(round(x0 * scale_x)),
                    int(round(y0 * scale_y)),
                    int(round(x1 * scale_x)),
                    int(round(y1 * scale_y)),
                ],
                "score": w.get("score", 1.0),
            }
        )
    return out


def _digital_words_for_page(
    pdf: pdfium.PdfDocument,
    page_index: int,
    final_width: int,
    final_height: int,
) -> tuple[list[dict], int, bool]:
    """Extract digital words for a single page, rescaled to the final image space.

    Returns (words, char_count, is_digital).
    """
    page = pdf[page_index]
    textpage = page.get_textpage()
    is_digital, char_count = pdf_extractor.page_is_digital(textpage)
    if not is_digital:
        return [], char_count, False

    w_pts = page.get_width()
    h_pts = page.get_height()
    scale = pdf_extractor.compute_scale(w_pts, h_pts)
    raw = pdf_extractor.extract_words(textpage, h_pts, scale)

    native_w = max(1, int(w_pts * scale))
    native_h = max(1, int(h_pts * scale))
    sx = final_width / native_w if final_width > 0 else 1.0
    sy = final_height / native_h if final_height > 0 else 1.0
    return _rescale_words(raw, sx, sy), char_count, True


def compute_pdf_geometry(
    pdf_bytes: bytes,
    page_sizes: list[dict],
) -> list[dict]:
    """Compute per-page unified geometry for a PDF.

    page_sizes: list of {page_number, width, height} — the final rendered image
    dimensions for each page, as returned by processor.pdf_to_images.

    Returns a list aligned with page_sizes, each element:
        {page_number, source, char_count, word_count, words}
    where source == 'pypdfium' for digital pages (words filled in) or
    'paddleocr' for pages needing OCR (words empty until OCR runs).
    """
    pdf = pdfium.PdfDocument(pdf_bytes)
    results: list[dict] = []
    try:
        for i, meta in enumerate(page_sizes):
            if i >= len(pdf):
                break
            try:
                words, char_count, is_digital = _digital_words_for_page(
                    pdf, i, meta.get("width", 0) or 0, meta.get("height", 0) or 0
                )
            except Exception as exc:
                logger.warning(
                    "Digital word extraction failed for page %d: %s — marking as scanned",
                    meta.get("page_number", i + 1),
                    exc,
                )
                words, char_count, is_digital = [], 0, False
            results.append(
                {
                    "page_number": meta["page_number"],
                    "source": SOURCE_DIGITAL if is_digital else SOURCE_SCANNED,
                    "char_count": char_count,
                    "word_count": len(words),
                    "words": words,
                }
            )
    finally:
        pdf.close()
    return results


def merge_scanned_into_geometry(
    base_geometry: list[dict],
    ocr_pages: list[dict],
) -> list[dict]:
    """Merge OCR results (scanned pages) into the base unified geometry.

    base_geometry already contains digital word data for pypdfium pages and
    empty entries for paddleocr pages. This function fills in words for
    the paddleocr entries using the freshly computed OCR output.
    """
    ocr_by_page = {p["page_number"]: p for p in ocr_pages}
    out: list[dict] = []
    for entry in base_geometry:
        if entry.get("source") == SOURCE_SCANNED:
            ocr_entry = ocr_by_page.get(entry["page_number"])
            if ocr_entry:
                words = ocr_entry.get("words") or []
                merged = {
                    **entry,
                    "words": words,
                    "word_count": len(words),
                }
                out.append(merged)
                continue
        out.append(entry)
    return out


def image_page_geometry(page_number: int = 1) -> dict:
    """Return an empty scanned-source geometry entry for a non-PDF image.

    Non-PDF images always need OCR, so we return a placeholder that marks
    the page as scanned with zero words until OCR fills them in.
    """
    return {
        "page_number": page_number,
        "source": SOURCE_SCANNED,
        "char_count": 0,
        "word_count": 0,
        "words": [],
    }
