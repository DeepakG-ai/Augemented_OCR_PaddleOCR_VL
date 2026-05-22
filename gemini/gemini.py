"""
gemini.py — Processes 3 specified PDF files using the official google-genai SDK,
            disabling thinking ("thinking will be false"), and saving the outputs as:
            - gemini_<filename>.png (page 1 image with bounding boxes drawn, like folder examples)
            - gemini_<filename>.json (extracted fields and boxes from Gemini)
"""

import base64
import json
import os
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
import pypdfium2 as pdfium

# Import the official google-genai SDK
from google import genai
from google.genai import types

# ── Paths ─────────────────────────────────────────────────────────────
INPUT_DIR  = Path(r"C:\Users\aigroup5\Downloads\PDF Samples\input")
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
ENV_FILE   = Path(__file__).parent.parent / ".env"

COLOR_HEADER    = "#00FF88"   # green — header field labels
COLOR_LINE_ITEM = "#FFD700"   # gold  — line-item column headers
COLOR_NULL      = "#FF4444"   # red   — null / not found

RENDER_SCALE = 3.0   # pypdfium2 render scale (~216 DPI) for a crisp image


# ── API key (from root/.env, format: GEMINI_API_KEY = AIza...) ────────
def load_api_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GEMINI_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("GEMINI_API_KEY not found in environment or .env file")


# ── Vendor configurations for the 3 target PDFs ───────────────────────
VENDORS = [
    {
        "name":             "AMERICAN PAPER AND TWINE",
        "file_prefix":      "American Paper and Twine 559096",
        "header_fields":    ["vendor", "po_number", "ship_to"],
        "line_item_fields": ["qty", "uom", "unit_cost", "item_number"],
    },
    {
        "name":             "ROBERT SCOTT",
        "file_prefix":      "ROBERT SCOTT 547589",
        "header_fields":    ["supplier", "deliver_to", "document_date", "purchase_order_no"],
        "line_item_fields": ["no", "variant", "qty", "uom", "unit_cost"],
    },
    {
        "name":             "RJ SCHINNER",
        "file_prefix":      "RJ SCHINNER 563773",
        "header_fields":    ["ship_to", "bill_to", "po_number", "order_date", "required_date"],
        "line_item_fields": ["order_qty", "item", "pack", "unit_price"],
    },
    {
        "name":             "RD AMERICA LLC",   # Restaurant Depot — name printed on the doc
        "file_prefix":      "RESTAURANT DEPOT 557269",
        "header_fields":    ["bill_to", "ship_to", "supplier", "po_number", "order_date"],
        "line_item_fields": ["our_item_code", "pack", "our_units", "list_cost", "master_cases"],
    }
]


# ── Prompt builders ───────────────────────────────────────────────────
def build_system_prompt(header_fields: list[str], line_item_fields: list[str]) -> str:
    return_keys = """Return two top-level keys:
- `fields`: extracted values
- `boxes`: bounding box of the LABEL text for each field"""

    bbox_rules = """
<bbox_rules>
- For each header field, return the bounding box of the LABEL text (e.g., word "PO Number:"), NOT the value next to it.
- For each line item column, return the bounding box of the COLUMN HEADER text in the table header row.
- Each box value MUST be a plain JSON array: [x1, y1, x2, y2] — four integers in a 0-1000 normalized grid relative to the full page image. Do NOT nest it in a dict or use any key like "bbox_2d".
- If a label or column header is not visible on this page, set its box to null.
</bbox_rules>"""

    return f"""You are a highly accurate document data extraction assistant.
This request is processed one page at a time.

{return_keys}
{bbox_rules}

<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

<output_rules>
- Extract ONLY what is explicitly visible in the document image.
- Never guess or fabricate data.
- STRICTLY return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- Use null for missing fields, never omit them.
- For line_items, return an array even if only one item exists.
- Dates should be in the format they appear in the document.
</output_rules>"""


def build_user_message(header_fields: list[str], line_item_fields: list[str]) -> str:
    fields_template: dict[str, Any] = {}
    for f in header_fields:
        fields_template[f] = None
    if line_item_fields:
        fields_template["line_items"] = [{col: None for col in line_item_fields}]

    all_keys = list(header_fields) + list(line_item_fields)
    boxes_template = {k: None for k in all_keys}
    full_template = {
        "fields": fields_template,
        "boxes": boxes_template,
    }

    header_section = "\n".join(f"  - {f}" for f in header_fields)
    line_section = "\n".join(f"  - {f}" for f in line_item_fields)

    return f"""Extract the header fields AND all visible line item rows from this purchase order page.

If any field is empty or not visible, return null.
<header_fields>
{header_section}
</header_fields>

<line_item_columns>
{line_section}
</line_item_columns>

Return JSON in exactly this shape:
{json.dumps(full_template, indent=2)}

<rules>
- Empty or missing cells → null.
- Extract every visible line item row.
</rules>

STRICTLY return ONLY valid JSON matching EXACTLY the structure above."""


# ── Render page 1 to PIL image and PNG bytes ──────────────────────────
def render_page1_pdfium(pdf_path: Path) -> tuple[Image.Image, bytes]:
    pdf  = pdfium.PdfDocument(str(pdf_path))
    page = pdf[0]
    
    # Render for saving and display
    pil_img = page.render(scale=RENDER_SCALE).to_pil().convert("RGB")
    
    # Save to bytes to send to Gemini
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    
    return pil_img, png_bytes


# ── Bounding box coordinates normalization / axis fix ──────────────────
def normalize_box_format(boxes: dict) -> tuple[dict, bool]:
    """Gemini inconsistently emits its native [ymin, xmin, ymax, xmax] instead of [x1, y1, x2, y2].
    Detect if axes are swapped (text labels are typically wider than tall).
    """
    vals = [v for v in boxes.values() if isinstance(v, list) and len(v) == 4]
    if not vals:
        return boxes, False
    tall = sum(1 for v in vals if (v[3] - v[1]) > (v[2] - v[0]))
    if tall < 0.8 * len(vals):  # mostly wide → already [x1, y1, x2, y2]
        return boxes, False
    fixed = {}
    for k, v in boxes.items():
        if isinstance(v, list) and len(v) == 4:
            fixed[k] = [v[1], v[0], v[3], v[2]]   # ymin,xmin,ymax,xmax → x1,y1,x2,y2
        else:
            fixed[k] = v
    return fixed, True


# ── Draw bounding boxes and text labels ───────────────────────────────
def draw_boxes_on_image(img: Image.Image, boxes: dict, header_fields: set,
                        line_item_fields: set) -> tuple[Image.Image, int, int]:
    img = img.copy()
    d = ImageDraw.Draw(img)
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
        
        # Grid is 0-1000 normalized relative to the full page image
        x0 = max(0, min(iw, box[0] / 1000.0 * iw))
        y0 = max(0, min(ih, box[1] / 1000.0 * ih))
        x1 = max(0, min(iw, box[2] / 1000.0 * iw))
        y1 = max(0, min(ih, box[3] / 1000.0 * ih))
        
        if x1 <= x0 or y1 <= y0:
            missing += 1
            continue
            
        color = COLOR_LINE_ITEM if is_line else COLOR_HEADER
        d.rectangle([x0, y0, x1, y1], outline=color, width=2)
        
        label = key.replace("_", " ")
        label_y = y0 + 2 if (y1 - y0) > 20 else max(0, y0 - 16)
        try:
            bb = font.getbbox(label)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            lw, lh = len(label) * 8, 14
        
        # Draw background label box
        d.rectangle([x0, label_y, x0 + lw + 4, label_y + lh + 4], fill=(0, 0, 0))
        d.text((x0 + 2, label_y + 1), label, fill=color, font=font)
        found += 1
        
    return img, found, missing


# ── Legend overlay on image ───────────────────────────────────────────
def draw_legend(img: Image.Image, vendor: str, found: int, missing: int) -> Image.Image:
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


# ── Main ──────────────────────────────────────────────────────────────
def main():
    try:
        api_key = load_api_key()
    except Exception as e:
        print(f"Error: {e}")
        return

    print("Initializing Google GenAI SDK Client...")
    client = genai.Client(api_key=api_key)

    # Use gemini-3-flash-preview explicitly
    model = "gemini-3-flash-preview"
    print(f"Using model: {model}\n")

    # Find all PDFs in the input directory
    all_pdfs = {p.name.lower(): p for p in INPUT_DIR.glob("*")
                if p.suffix.lower() == ".pdf"}

    import sys
    only = [a.lower() for a in sys.argv[1:]]

    for vendor in VENDORS:
        if only and not any(o in vendor["name"].lower() for o in only):
            continue
        name = vendor["name"]
        prefix = vendor["file_prefix"].lower()
        hf = vendor["header_fields"]
        lf = vendor["line_item_fields"]

        # Find the matching PDF file
        pdf_path = next((p for n, p in all_pdfs.items() if n.startswith(prefix)), None)
        if pdf_path is None:
            print(f"[{name}] PDF not found (prefix: {vendor['file_prefix']}) — skipping")
            continue

        print(f"[{name}] Processing {pdf_path.name}...")

        # Render page 1 to PIL image and PNG bytes
        pil_img, png_bytes = render_page1_pdfium(pdf_path)

        # Build Prompts
        sys_prompt = build_system_prompt(hf, lf)
        user_msg = build_user_message(hf, lf)

        # Output file paths
        pdf_stem = pdf_path.stem
        png_out_path = OUTPUT_DIR / f"gemini_{pdf_stem}.png"
        json_out_path = OUTPUT_DIR / f"gemini_{pdf_stem}.json"

        # Call Gemini API via official google-genai SDK using the exact structure requested
        print("  Calling Gemini API (thinking level = MINIMAL)...")
        
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                    types.Part.from_text(text=user_msg)
                ],
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    temperature=0.2,
                    top_p=0.95,
                    top_k=20,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_level="MINIMAL"
                    )
                ),
            )
            raw_text = response.text.strip()
        except Exception as e:
            # Graceful fallback if thinking_config is not supported by the model or version
            print(f"  Warning: failed with thinking_level=MINIMAL: {e}")
            print("  Retrying without thinking_config...")
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                    types.Part.from_text(text=user_msg)
                ],
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    temperature=0.2,
                    top_p=0.95,
                    top_k=20,
                    response_mime_type="application/json"
                ),
            )
            raw_text = response.text.strip()

        # Clean raw text response if it contains markdown code block markers
        cleaned_text = raw_text
        if cleaned_text.startswith("```"):
            cleaned_text = "\n".join(cleaned_text.split("\n")[1:])
            if cleaned_text.rstrip().endswith("```"):
                cleaned_text = cleaned_text.rstrip()[:-3].rstrip()

        # Print the response text to see exactly what Gemini returns
        print(f"  Raw Gemini Response for {name}:")
        print("-" * 50)
        print(cleaned_text)
        print("-" * 50)

        # Parse JSON and draw bounding boxes on the PNG
        try:
            parsed_json = json.loads(cleaned_text)
            
            # Extract and normalize boxes
            boxes = parsed_json.get("boxes") or {}
            normalized_boxes, swapped = normalize_box_format(boxes)
            parsed_json["boxes"] = normalized_boxes
            
            # Save the JSON file
            json_out_path.write_text(json.dumps(parsed_json, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  Saved JSON: {json_out_path.name}")
            
            # Draw boxes on the image
            drawn_img, found, missing = draw_boxes_on_image(
                pil_img, normalized_boxes, set(hf), set(lf)
            )
            drawn_img = draw_legend(drawn_img, name, found, missing)
            
            # Save drawing to PNG
            drawn_img.save(png_out_path, format="PNG")
            print(f"  Saved PNG (with bounding boxes overlaid): {png_out_path.name}")
            if swapped:
                print("  Note: coordinates detected as ymin,xmin,ymax,xmax and auto-swapped.")
                
        except json.JSONDecodeError:
            print(f"  Warning: response was not valid JSON. Saving raw text to: {json_out_path.name}")
            json_out_path.write_text(raw_text, encoding="utf-8")
            
            # Save raw rendered page 1 without boxes
            pil_img.save(png_out_path, format="PNG")
            print(f"  Saved raw PNG (no bounding boxes): {png_out_path.name}")

        print()

    print("Finished processing all target PDFs.")


if __name__ == "__main__":
    main()
