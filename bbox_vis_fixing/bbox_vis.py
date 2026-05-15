"""
bbox_vis.py — Send each vendor's first PDF page to Qwen3-VL,
              get label bounding boxes, draw them on the image, save to output/.

Usage:
    cd bbox_vis_fixing
    python bbox_vis.py

Outputs: output/<vendor>_<filename>_bbox.png  +  output/<vendor>_<filename>_response.json
"""

import base64
import io
import json
import os
import sys
from pathlib import Path

import pypdfium2 as pdfium
import requests
from PIL import Image, ImageDraw, ImageFont

# ── Paths ─────────────────────────────────────────────────────────────
INPUT_DIR  = Path(r"C:\Users\aigroup5\Downloads\PDF Samples\input")
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── LLM config ────────────────────────────────────────────────────────
LLM_URL   = os.getenv("LLM_URL",   "http://localhost:8001/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3vl")

# ── Image preprocessing (matches processor.py exactly) ────────────────
MAX_LONG_SIDE = 960
MAX_PIXELS    = 960 * 720   # 691 200
FACTOR        = 32          # 2 × patch_size — pre-align so llama.cpp won't re-pad

def _resize_to_vlm_budget(img: Image.Image) -> Image.Image:
    w, h = img.size
    scale = min(
        1.0,
        MAX_LONG_SIDE / max(w, h),
        (MAX_PIXELS / (w * h)) ** 0.5 if (w * h) > MAX_PIXELS else 1.0,
    )
    nw = max(FACTOR, round(max(1, int(w * scale)) / FACTOR) * FACTOR)
    nh = max(FACTOR, round(max(1, int(h * scale)) / FACTOR) * FACTOR)
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    return img

def render_page1(pdf_path: Path) -> tuple[Image.Image, str]:
    """Render first page → (PIL Image at VLM budget, base64 jpeg)."""
    pdf  = pdfium.PdfDocument(str(pdf_path))
    page = pdf[0]
    w_pts, h_pts = page.get_width(), page.get_height()
    max_pts = max(w_pts, h_pts, 1)
    dpi  = max(96, min(120, int(MAX_LONG_SIDE / (max_pts / 72.0))))
    bm   = page.render(scale=dpi / 72.0, fill_color=(255, 255, 255, 255))
    img  = bm.to_pil().convert("RGB")
    bm.close(); pdf.close()
    img = _resize_to_vlm_budget(img)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return img, base64.b64encode(buf.getvalue()).decode()


# ── Vendor catalogue — fields from DB, PDFs by filename ───────────────
# Each vendor: one PDF (first match in INPUT_DIR whose name starts with the prefix)
VENDORS = [
    {
        "name":           "RJ SCHINNER",
        "file_prefix":    "RJ SCHINNER 563773",
        "header_fields":  ["ship_to", "bill_to", "po_number", "order_date", "required_date"],
        "line_item_fields": ["order_qty", "item", "pack", "unit_price"],
    },
    {
        "name":           "ROBERT SCOTT",
        "file_prefix":    "ROBERT SCOTT 547589",
        "header_fields":  ["supplier", "deliver_to", "document_date", "purchase_order_no"],
        "line_item_fields": ["no", "variant", "qty", "uom", "unit_cost"],
    },
    {
        "name":           "AMERICAN PAPER AND TWINE",
        "file_prefix":    "American Paper and Twine 559096",
        "header_fields":  ["vendor", "po_number", "ship_to"],
        "line_item_fields": ["qty", "uom", "unit_cost", "item_number"],
    },
    {
        "name":             "RD AMERICA LLC",   # actual company name printed on the document
        "file_prefix":      "RESTAURANT DEPOT 557269",
        "header_fields":    ["bill_to", "ship_to", "supplier", "po_number", "order_date"],
        "line_item_fields": ["our_item_code", "pack", "our_units", "list_cost", "master_cases"],
    },
    {
        "name":           "AEGIS",
        "file_prefix":    "Aegis - 125962",
        "header_fields":  ["bill_to", "ship_to", "invoice_date", "invoice_number",
                           "vendor_name", "po_number", "order_date", "order_number",
                           "fob", "terms", "freight", "invoice_total"],
        "line_item_fields": ["item", "required_qty", "ship_qty", "uom", "unit_price", "line_total"],
    },
    {
        "name":           "CANADA METAL",
        "file_prefix":    "Canada Metal - FA595213",
        "header_fields":  ["vendor_name", "vendor_address", "invoice_number", "invoice_date",
                           "po_number", "invoice_total", "invoice_subtotal",
                           "tax_amount", "freigt_amount", "term"],
        "line_item_fields": ["item", "line_description", "quantity_ordered",
                             "quantity_recieved", "unit_price", "line_total", "uom"],
    },
    {
        "name":           "FERGUSON",
        "file_prefix":    "FERGUSON S563647",
        "header_fields":  ["from", "to", "ship_to", "po_number", "po_date"],
        "line_item_fields": ["qty", "item_code", "net_price", "u/m"],
    },
]


# ── System prompt — exact copy of extractor.py build_system_prompt ────
def build_system_prompt(vendor_name: str, best_effort_boxes: bool = False) -> str:
    return (
        "Return me the bounding box of each LABEL, not the values. "
        "Coordinates must be in a [0-1000] normalized grid relative to the full page image. "
        "The label may be written vertically or horizontally — understand the context. "
        "Return the bounding box in strict JSON format."
    )


# ── User message — minimal: just list the labels, ask for JSON ────────
def build_user_message(header_fields: list, line_item_fields: list) -> str:
    all_keys   = list(header_fields) + list(line_item_fields)
    boxes_tmpl = {k: [0, 0, 0, 0] for k in all_keys}

    return f"""Return the bounding box of each of these labels in the image.

Labels:
{chr(10).join(f"- {k}" for k in all_keys)}

Return strict JSON in exactly this shape (each value = [x1, y1, x2, y2] in 0-1000):
{json.dumps({"boxes": boxes_tmpl}, indent=2)}

Return ONLY the JSON. No markdown, no explanation."""


# ── Call Qwen ─────────────────────────────────────────────────────────
def call_qwen(image_b64: str, system_prompt: str, user_message: str) -> tuple[dict | None, str]:
    """Returns (parsed dict or None, raw content string)."""
    payload = {
        "model":       LLM_MODEL,
        "max_tokens":  4096,
        "temperature": 0.6,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": user_message},
            ]},
        ],
    }
    try:
        resp = requests.post(LLM_URL, json=payload, timeout=180)
        resp.raise_for_status()
    except Exception as e:
        print(f"    HTTP error: {e}")
        return None, str(e)

    raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown fences if the model added them despite instructions
    content = raw
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])          # drop ```json line
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3].rstrip()

    try:
        return json.loads(content), raw
    except json.JSONDecodeError:
        salvaged = _salvage_json(content)
        if salvaged is not None:
            print(f"    JSON truncated/malformed — salvaged {len(salvaged.get('boxes', {}))} boxes")
            return salvaged, raw
        print(f"    JSON parse error (unrecoverable)")
        return None, raw


def _salvage_json(text: str) -> dict | None:
    """Recover boxes from a truncated/trailing-comma response.

    The model sometimes stops mid-output (e.g. `"item": [41,`). Strip any
    incomplete trailing field, close open brackets, drop trailing commas.
    """
    import re

    # Keep only complete `"key": [n, n, n, n]` entries inside boxes.
    pairs = re.findall(r'"([^"]+)"\s*:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]', text)
    if not pairs:
        return None
    boxes = {k: [int(a), int(b), int(c), int(d)] for k, a, b, c, d in pairs}
    return {"boxes": boxes}


# ── Draw boxes ────────────────────────────────────────────────────────
COLOR_HEADER    = "#00FF88"   # green  — header field labels
COLOR_LINE_ITEM = "#FFD700"   # gold   — line item column headers
COLOR_NULL      = "#FF4444"   # red    — field returned null (not found)

def draw_boxes(
    img: Image.Image,
    boxes: dict,
    header_fields: set,
    line_item_fields: set,
) -> Image.Image:
    img  = img.copy()
    draw = ImageDraw.Draw(img)
    iw, ih = img.size

    # Try to get a small font; fall back to built-in
    try:
        font       = ImageFont.truetype("arial.ttf",   11)
        font_small = ImageFont.truetype("arial.ttf",   9)
    except Exception:
        font       = ImageFont.load_default()
        font_small = font

    found, missing = 0, 0

    for field_key, raw_box in boxes.items():
        is_line = field_key in line_item_fields

        if raw_box is None:
            # Mark as missing — draw a small ✗ indicator at top-left region
            missing += 1
            label = field_key.replace("_", " ")
            x_off = 4 + (missing % 8) * (iw // 8)
            y_off = 4 + (missing // 8) * 14
            draw.text((x_off, y_off), f"✗ {label}", fill=COLOR_NULL, font=font_small)
            continue

        if not isinstance(raw_box, list) or len(raw_box) != 4:
            continue

        # Qwen3-VL relative 0-1000 → pixel
        x0 = max(0, min(iw, raw_box[0] / 1000.0 * iw))
        y0 = max(0, min(ih, raw_box[1] / 1000.0 * ih))
        x1 = max(0, min(iw, raw_box[2] / 1000.0 * iw))
        y1 = max(0, min(ih, raw_box[3] / 1000.0 * ih))

        if x1 <= x0 or y1 <= y0:
            continue

        color = COLOR_LINE_ITEM if is_line else COLOR_HEADER
        draw.rectangle([x0, y0, x1, y1], outline=color, width=2)

        # Label — inside box at top-left, or just above if too short
        label = field_key.replace("_", " ")
        label_y = y0 + 2 if (y1 - y0) > 16 else max(0, y0 - 13)
        # Semi-transparent background for readability
        try:
            bb = font.getbbox(label)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            lw, lh = len(label) * 6, 10
        draw.rectangle([x0, label_y, x0 + lw + 4, label_y + lh + 2], fill=(0, 0, 0, 180))
        draw.text((x0 + 2, label_y + 1), label, fill=color, font=font)

        found += 1

    return img, found, missing


# ── Legend ────────────────────────────────────────────────────────────
def draw_legend(img: Image.Image, vendor_name: str, found: int, missing: int) -> Image.Image:
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        font = ImageFont.load_default()

    lines = [
        (COLOR_HEADER,    f"■ header field label box ({COLOR_HEADER})"),
        (COLOR_LINE_ITEM, f"■ column header box ({COLOR_LINE_ITEM})"),
        (COLOR_NULL,      f"✗ not found on page ({COLOR_NULL})"),
        ("white",         f"vendor: {vendor_name}"),
        ("white",         f"found: {found}  missing: {missing}"),
    ]
    x, y = 4, img.height - (len(lines) * 16) - 4
    for color, text in lines:
        draw.rectangle([x - 2, y - 1, x + 260, y + 13], fill=(0, 0, 0, 160))
        draw.text((x, y), text, fill=color, font=font)
        y += 15
    return img


# ── Main ──────────────────────────────────────────────────────────────
def main():
    # Optional: pass vendor name fragments as args to run only those vendors
    # e.g.  python bbox_vis.py rj schinner  restaurant
    only = [a.lower() for a in sys.argv[1:]]

    print(f"Input:  {INPUT_DIR}")
    print(f"Output: {OUTPUT_DIR}")
    if only:
        print(f"Filter: {only}\n")
    else:
        print()

    all_pdfs = {p.name.lower(): p for p in INPUT_DIR.glob("*") if p.suffix.lower() == ".pdf"}

    for vendor in VENDORS:
        if only and not any(o in vendor["name"].lower() for o in only):
            continue
        name    = vendor["name"]
        prefix  = vendor["file_prefix"].lower()
        hf      = vendor["header_fields"]
        lf      = vendor["line_item_fields"]

        # Locate the PDF
        pdf_path = next((p for n, p in all_pdfs.items() if n.startswith(prefix)), None)
        if pdf_path is None:
            print(f"[{name}]  PDF not found (prefix: {vendor['file_prefix']}) — skipping")
            continue

        print(f"[{name}]  {pdf_path.name}")

        # ── Step 1: render page 1
        print(f"  rendering page 1 ...")
        try:
            img, b64 = render_page1(pdf_path)
        except Exception as e:
            print(f"  render failed: {e}")
            continue
        print(f"  image: {img.width}×{img.height}px")

        # ── Step 2: build prompts (same as extractor.py)
        sys_prompt = build_system_prompt(name, best_effort_boxes=vendor.get("best_effort_boxes", False))
        user_msg   = build_user_message(hf, lf)
        print(f"  prompt: {len(hf)} header fields, {len(lf)} line-item columns")

        # ── Step 3: call Qwen3-VL
        print(f"  calling Qwen3-VL ...")
        result, raw = call_qwen(b64, sys_prompt, user_msg)

        # Save raw response always
        slug        = name.replace(" ", "_").lower()
        raw_path    = OUTPUT_DIR / f"{slug}_response.json"
        raw_path.write_text(raw, encoding="utf-8")

        if result is None:
            print(f"  LLM returned no parseable JSON — see {raw_path.name}")
            img.save(OUTPUT_DIR / f"{slug}_FAILED.png")
            continue

        boxes = result.get("boxes") or {}
        if not isinstance(boxes, dict):
            boxes = {}

        vendor_ok = result.get("vendor_confirmed")
        print(f"  vendor_confirmed: {vendor_ok}")
        print(f"  boxes received:   {len(boxes)}")

        # ── Step 4: draw boxes
        annotated, found, missing = draw_boxes(img, boxes, set(hf), set(lf))
        annotated = draw_legend(annotated, name, found, missing)

        print(f"  drawn: {found} found, {missing} null/missing")

        # ── Step 5: save
        out_path = OUTPUT_DIR / f"{slug}_bbox.png"
        annotated.save(out_path, format="PNG")
        print(f"  saved: {out_path.name}\n")

    print("Done.")


if __name__ == "__main__":
    main()
