"""
draw_boxes.py — Overlay the Gemini bounding boxes (already saved in output/*.json)
                onto rendered PDF page 1. No API calls — reuses saved responses.

Usage:
    cd gemini
    python draw_boxes.py
"""

import json
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from gemini_bbox import VENDORS, INPUT_DIR, OUTPUT_DIR

COLOR_HEADER    = "#00FF88"   # green — header field labels
COLOR_LINE_ITEM = "#FFD700"   # gold  — line-item column headers
COLOR_NULL      = "#FF4444"   # red   — null / not found

RENDER_SCALE = 3.0   # pypdfium2 render scale (~216 DPI) for a crisp overlay


def normalize_box_format(boxes: dict) -> tuple[dict, bool]:
    """Gemini inconsistently emits its native [ymin,xmin,ymax,xmax] for some
    documents instead of the prompted [x1,y1,x2,y2]. It is consistent WITHIN a
    document, so detect per-document: real text-label boxes are wider than tall,
    so if the vast majority are taller-than-wide the axes are swapped.
    """
    vals = [v for v in boxes.values() if isinstance(v, list) and len(v) == 4]
    if not vals:
        return boxes, False
    tall = sum(1 for v in vals if (v[3] - v[1]) > (v[2] - v[0]))
    if tall < 0.8 * len(vals):          # mostly wide → already [x1,y1,x2,y2]
        return boxes, False
    fixed = {}
    for k, v in boxes.items():
        if isinstance(v, list) and len(v) == 4:
            fixed[k] = [v[1], v[0], v[3], v[2]]   # ymin,xmin,ymax,xmax → x1,y1,x2,y2
        else:
            fixed[k] = v
    return fixed, True


def render_page1(pdf_path: Path) -> Image.Image:
    pdf  = pdfium.PdfDocument(str(pdf_path))
    page = pdf[0]
    bmp  = page.render(scale=RENDER_SCALE)
    return bmp.to_pil().convert("RGB")


def draw(img: Image.Image, boxes: dict, header_fields: set,
         line_item_fields: set) -> tuple[Image.Image, int, int]:
    img  = img.copy()
    d    = ImageDraw.Draw(img)
    iw, ih = img.size
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    found = missing = 0
    for key, box in boxes.items():
        is_line = key in line_item_fields
        if not isinstance(box, list) or len(box) != 4:
            missing += 1
            continue
        x0 = max(0, min(iw, box[0] / 1000.0 * iw))
        y0 = max(0, min(ih, box[1] / 1000.0 * ih))
        x1 = max(0, min(iw, box[2] / 1000.0 * iw))
        y1 = max(0, min(ih, box[3] / 1000.0 * ih))
        if x1 <= x0 or y1 <= y0:
            missing += 1
            continue
        color  = COLOR_LINE_ITEM if is_line else COLOR_HEADER
        d.rectangle([x0, y0, x1, y1], outline=color, width=2)
        label   = key.replace("_", " ")
        label_y = y0 + 2 if (y1 - y0) > 20 else max(0, y0 - 16)
        try:
            bb = font.getbbox(label)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            lw, lh = len(label) * 8, 14
        d.rectangle([x0, label_y, x0 + lw + 4, label_y + lh + 4], fill=(0, 0, 0))
        d.text((x0 + 2, label_y + 1), label, fill=color, font=font)
        found += 1
    return img, found, missing


def legend(img: Image.Image, vendor: str, found: int, missing: int) -> Image.Image:
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        font = ImageFont.load_default()
    rows = [
        (COLOR_HEADER,    "header field label box"),
        (COLOR_LINE_ITEM, "line-item column header box"),
        ("white",         f"vendor: {vendor}"),
        ("white",         f"found: {found}  missing: {missing}"),
    ]
    x, y = 6, img.height - len(rows) * 20 - 6
    for color, text in rows:
        d.rectangle([x - 3, y - 2, x + 320, y + 17], fill=(0, 0, 0))
        d.text((x, y), text, fill=color, font=font)
        y += 20
    return img


def main():
    all_pdfs = {p.name.lower(): p for p in INPUT_DIR.glob("*")
                if p.suffix.lower() == ".pdf"}

    for vendor in VENDORS:
        name   = vendor["name"]
        slug   = name.replace(" ", "_").lower()
        prefix = vendor["file_prefix"].lower()
        resp   = OUTPUT_DIR / f"{slug}_response.json"

        if not resp.exists():
            print(f"[{name}]  no saved response ({resp.name}) — skipping")
            continue
        pdf_path = next((p for n, p in all_pdfs.items() if n.startswith(prefix)), None)
        if pdf_path is None:
            print(f"[{name}]  PDF not found — skipping")
            continue

        data  = json.loads(resp.read_text(encoding="utf-8"))
        boxes = data.get("boxes") or {}
        boxes, swapped = normalize_box_format(boxes)
        img   = render_page1(pdf_path)
        img, found, missing = draw(
            img, boxes, set(vendor["header_fields"]), set(vendor["line_item_fields"]))
        img = legend(img, name, found, missing)

        out = OUTPUT_DIR / f"{slug}_bbox.png"
        img.save(out, format="PNG")
        tag = "  [axes swapped: Gemini native ymin,xmin,ymax,xmax]" if swapped else ""
        print(f"[{name}]  {found} drawn, {missing} skipped -> {out.name}{tag}")

    print("Done.")


if __name__ == "__main__":
    main()
