"""
pdf_extractor.py — pypdfium2-based digital PDF word extraction helpers.

Importable module used by geometry.py. Detects whether a page has usable
embedded text, and if so extracts word-level boxes at a given render scale.
The boxes produced here live in the same pixel space as the rendered image
described by compute_scale() below.
"""
from __future__ import annotations


DIGITAL_CHAR_THRESHOLD = 50
DETECTION_SAMPLE_SIZE = 500
MAX_LONG_SIDE = 1344
DPI_FLOOR = 96
DPI_DEFAULT = 150


def compute_scale(w_pts: float, h_pts: float) -> float:
    """Return the scale factor used to render a PDF page at pypdfium2's target DPI."""
    max_pts = max(w_pts, h_pts)
    if max_pts <= 0:
        return DPI_FLOOR / 72.0
    target_dpi = min(DPI_DEFAULT, int(MAX_LONG_SIDE / (max_pts / 72.0)))
    target_dpi = max(target_dpi, DPI_FLOOR)
    return target_dpi / 72.0


def page_is_digital(textpage) -> tuple[bool, int]:
    """Classify a textpage as digital if it has enough printable characters.

    Returns (is_digital, char_count). char_count is the raw pypdfium2 count.
    """
    total = textpage.count_chars()
    if total < DIGITAL_CHAR_THRESHOLD:
        return False, total
    printable = 0
    for i in range(min(total, DETECTION_SAMPLE_SIZE)):
        ch = textpage.get_text_range(index=i, count=1)
        if ch.strip() and ch.isprintable():
            printable += 1
            if printable >= DIGITAL_CHAR_THRESHOLD:
                return True, total
    return False, total


def extract_words(textpage, page_h_pts: float, scale: float) -> list[dict]:
    """Extract word-level boxes from a pypdfium2 textpage at the given render scale.

    Boxes are in image pixel space (scale * PDF points) with Y-axis flipped
    to match top-left origin. Returns [{text, box:[x0,y0,x1,y1], score}].
    """
    words: list[dict] = []
    cur_chars: list[str] = []
    cur_boxes: list[tuple[float, float, float, float]] = []

    def _flush() -> None:
        if not cur_chars:
            return
        left = min(b[0] for b in cur_boxes)
        bottom = min(b[1] for b in cur_boxes)
        right = max(b[2] for b in cur_boxes)
        top = max(b[3] for b in cur_boxes)
        words.append(
            {
                "text": "".join(cur_chars),
                "box": [
                    int(left * scale),
                    int((page_h_pts - top) * scale),
                    int(right * scale),
                    int((page_h_pts - bottom) * scale),
                ],
                "score": 1.0,
            }
        )

    for i in range(textpage.count_chars()):
        ch = textpage.get_text_range(index=i, count=1)
        box = textpage.get_charbox(i, loose=False)
        is_delim = ch in (" ", "\t", "\n", "\r", "\x0c", "\x0b", "") or (box[2] - box[0]) < 0.5

        if is_delim:
            _flush()
            cur_chars, cur_boxes = [], []
        else:
            cur_chars.append(ch)
            cur_boxes.append(box)

    _flush()
    return words
