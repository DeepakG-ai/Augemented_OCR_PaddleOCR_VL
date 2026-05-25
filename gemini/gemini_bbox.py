"""
gemini_bbox.py — Send each vendor's FULL PDF (native multi-page) to Gemini 3 Flash,
                  using the SAME prompt as the main codebase (backend/extractor.py),
                  ask for {vendor_confirmed, fields, boxes} as JSON, save to output/.

Gemini supports native multi-page PDF (inline base64, application/pdf), so the
whole document is sent in ONE request — no page splitting.

Quota: Gemini free tier ~20 requests/day. This script sends 3 requests
(one PDF each: Robert Scott, RJ Schinner, Restaurant Depot).

Usage:
    cd gemini
    python gemini_bbox.py
"""

import base64
import json
import re
from pathlib import Path
from typing import Any

import requests
import pypdfium2 as pdfium

# ── Paths ─────────────────────────────────────────────────────────────
INPUT_DIR  = Path(r"C:\Users\aigroup5\Downloads\PDF Samples\input")
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
ENV_FILE   = Path(__file__).parent.parent / ".env"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


# ── API key (from backend/.env, format: GEMINI_API_KEY = AIza...) ─────
def load_api_key() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("GEMINI_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("GEMINI_API_KEY not found in backend/.env")


# ── Resolve a 'gemini 3 flash' model id WITHOUT spending generate quota ─
def resolve_flash_model(api_key: str) -> str:
    r = requests.get(f"{GEMINI_BASE}/models", params={"key": api_key}, timeout=60)
    r.raise_for_status()
    models = r.json().get("models", [])
    candidates = [
        m["name"].split("/")[-1]
        for m in models
        if "generateContent" in m.get("supportedGenerationMethods", [])
        and "flash" in m["name"].lower()
    ]
    if not candidates:
        raise RuntimeError("No flash model with generateContent found")
    # Prefer a v3 flash, then 'latest', then anything
    for pref in (lambda n: "3" in n and "flash" in n,
                 lambda n: "latest" in n,
                 lambda n: True):
        hit = next((c for c in candidates if pref(c)), None)
        if hit:
            return hit
    return candidates[0]


# ── Vendor catalogue (fields = DB template, copied from bbox_vis.py) ───
VENDORS = [
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
    },
    {
        "name":             "AMERICAN PAPER AND TWINE",
        "file_prefix":      "American Paper and Twine 559096",
        "header_fields":    ["vendor", "po_number", "ship_to"],
        "line_item_fields": ["qty", "uom", "unit_cost", "item_number"],
    },
    {
        "name":             "AEGIS",
        "file_prefix":      "Aegis - 125962",
        "header_fields":    ["bill_to", "ship_to", "invoice_date", "invoice_number",
                             "vendor_name", "po_number", "order_date", "order_number",
                             "fob", "terms", "freight", "invoice_total"],
        "line_item_fields": ["item", "required_qty", "ship_qty", "uom", "unit_price", "line_total"],
    },
    {
        "name":             "CANADA METAL",
        "file_prefix":      "Canada Metal - FA595213",
        "header_fields":    ["vendor_name", "vendor_address", "invoice_number", "invoice_date",
                             "po_number", "invoice_total", "invoice_subtotal",
                             "tax_amount", "freigt_amount", "term"],
        "line_item_fields": ["item", "line_description", "quantity_ordered",
                             "quantity_recieved", "unit_price", "line_total", "uom"],
    },
    {
        "name":             "FERGUSON",
        "file_prefix":      "FERGUSON S563647",
        "header_fields":    ["from", "to", "ship_to", "po_number", "po_date"],
        "line_item_fields": ["qty", "item_code", "net_price", "u/m"],
    },
]


# ── Prompt builders — VERBATIM copy of backend/extractor.py ────────────
# (gold_examples=None path, so the correction-hints section is omitted.)
def build_system_prompt(
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    format_type: str,
    gold_examples: list[dict] | None = None,
    include_boxes: bool = False,
    vendor_name: str | None = None,
) -> str:
    context_section = ""
    if instructions and instructions.strip():
        context_section = f"""
<document_context>
{instructions.strip()}
</document_context>"""

    rules_section = ""
    if rules:
        numbered = "\n".join(f"  {i}. {rule}" for i, rule in enumerate(rules, 1))
        rules_section = f"""
<extraction_rules>
{numbered}
</extraction_rules>"""

    gold_section = ""  # gold_examples is None for this tool

    if include_boxes:
        return_keys = """Return three top-level keys:
- `vendor_confirmed`: true if the document belongs to the detected vendor, false otherwise
- `fields`: extracted values
- `boxes`: bounding box of the LABEL text for each field"""

        bbox_rules = """
<bbox_rules>
- For each header field, return the bounding box of the LABEL text (e.g., word "PO Number:"), NOT the value next to it.
- For each line item column, return the bounding box of the COLUMN HEADER text in the table header row.
- Each box value MUST be a plain JSON array: [x1, y1, x2, y2] — four integers in a 0-1000 normalized grid relative to the full page image. Do NOT nest it in a dict or use any key like "bbox_2d".
- If a label or column header is not visible on this page, set its box to null.
</bbox_rules>"""

        vendor_section = ""
        if vendor_name:
            vendor_section = f"""
<vendor_verification>
System detected this document belongs to: "{vendor_name}"
Check the document header, letterhead, or company name in the image.
Return vendor_confirmed: true if correct, false if the document belongs to a different company.
</vendor_verification>"""
    else:
        return_keys = """Return one top-level key:
- `fields`: extracted values"""
        bbox_rules = ""
        vendor_section = ""

    return f"""You are a highly accurate document data extraction assistant.
This request is processed one page at a time.

{return_keys}
{context_section}
{rules_section}
{gold_section}
{vendor_section}
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


def build_user_message(
    header_fields: list[str],
    line_item_fields: list[str],
    page_num: int,
    total_pages: int,
    include_boxes: bool = False,
) -> str:
    if header_fields or line_item_fields:
        fields_template: dict[str, Any] = {}
        for f in header_fields:
            fields_template[f] = None
        if line_item_fields:
            fields_template["line_items"] = [{col: None for col in line_item_fields}]

        if include_boxes:
            all_keys = list(header_fields) + list(line_item_fields)
            boxes_template = {k: None for k in all_keys}
            full_template: dict[str, Any] = {
                "vendor_confirmed": None,
                "fields": fields_template,
                "boxes": boxes_template,
            }
        else:
            full_template = {"fields": fields_template}

        header_section = ""
        if header_fields:
            header_list = "\n".join(f"  - {f}" for f in header_fields)
            header_section = f"""
<header_fields>
{header_list}
</header_fields>"""

        line_section = ""
        if line_item_fields:
            line_list = "\n".join(f"  - {f}" for f in line_item_fields)
            line_section = f"""
<line_item_columns>
{line_list}
</line_item_columns>"""

        return f"""Extract the header fields AND all visible line item rows from this purchase order page (page {page_num} of {total_pages}).

If any field is empty or not visible, return null.
{header_section}
{line_section}

Return JSON in exactly this shape:
{json.dumps(full_template, indent=2)}

<rules>
- Empty or missing cells → null.
- Extract every visible line item row.
</rules>

STRICTLY return ONLY valid JSON matching EXACTLY the structure above."""

    return f"""Extract ALL data from this invoice/purchase order document (page {page_num} of {total_pages}).

STRICTLY return ONLY valid JSON. No markdown fences, no explanation, no extra text."""


# ── Render page 1 to PNG — single-page reference frame for clean boxes ─
# Native multi-page PDF gives correct FIELDS but degenerate BOX geometry
# (Gemini can't anchor the 0-1000 grid to one page). Sending page 1 alone
# as an image makes the box grid map cleanly to that single page.
def render_page1_png(pdf_bytes: bytes, scale: float = 3.0) -> bytes:
    import io
    pdf  = pdfium.PdfDocument(pdf_bytes)
    page = pdf[0]
    pil  = page.render(scale=scale).to_pil().convert("RGB")
    buf  = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


# ── Gemini call — page-1 image inline ─────────────────────────────────
def call_gemini(api_key: str, model: str, png_bytes: bytes,
                system_prompt: str, user_message: str) -> tuple[dict | None, str]:
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(png_bytes).decode(),
                }},
                {"text": user_message},
            ],
        }],
        "generationConfig": {
            "temperature": 0.6,
            "responseMimeType": "application/json",
        },
    }
    try:
        r = requests.post(url, params={"key": api_key}, json=payload, timeout=300)
        r.raise_for_status()
    except Exception as e:
        body = getattr(e, "response", None)
        detail = body.text if body is not None else str(e)
        return None, f"HTTP error: {detail}"

    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return None, json.dumps(data, indent=2)

    content = text
    if content.startswith("```"):
        content = "\n".join(content.split("\n")[1:])
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3].rstrip()

    try:
        return json.loads(content), text
    except json.JSONDecodeError:
        pairs = re.findall(
            r'"([^"]+)"\s*:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]', text)
        if pairs:
            return {"boxes": {k: [int(a), int(b), int(c), int(d)]
                              for k, a, b, c, d in pairs}}, text
        return None, text


# ── Main ──────────────────────────────────────────────────────────────
def main():
    import sys
    only = [a.lower() for a in sys.argv[1:]]   # e.g. python gemini_bbox.py american canada

    api_key = load_api_key()
    print("Resolving Gemini flash model (ListModels — does not use generate quota) ...")
    model = resolve_flash_model(api_key)
    print(f"Model: {model}")
    if only:
        print(f"Filter: {only}")
    print()

    all_pdfs = {p.name.lower(): p for p in INPUT_DIR.glob("*")
                if p.suffix.lower() == ".pdf"}

    for vendor in VENDORS:
        if only and not any(o in vendor["name"].lower() for o in only):
            continue
        name   = vendor["name"]
        prefix = vendor["file_prefix"].lower()
        hf     = vendor["header_fields"]
        lf     = vendor["line_item_fields"]

        pdf_path = next((p for n, p in all_pdfs.items() if n.startswith(prefix)), None)
        if pdf_path is None:
            print(f"[{name}]  PDF not found (prefix: {vendor['file_prefix']}) — skipping")
            continue

        pdf_bytes  = pdf_path.read_bytes()
        total_pages = len(pdfium.PdfDocument(pdf_bytes))
        png_bytes  = render_page1_png(pdf_bytes)
        print(f"[{name}]  {pdf_path.name}  ({total_pages} pages — sending PAGE 1 image for clean boxes)")

        sys_prompt = build_system_prompt(
            hf, lf, instructions=None, rules=[],
            format_type="single_po_multipage",
            gold_examples=None, include_boxes=True, vendor_name=name,
        )
        user_msg = build_user_message(
            hf, lf, page_num=1, total_pages=1, include_boxes=True)

        print(f"  prompt: {len(hf)} header fields, {len(lf)} line-item columns")
        print(f"  calling Gemini ({model}) — page-1 image ...")
        result, raw = call_gemini(api_key, model, png_bytes, sys_prompt, user_msg)

        slug = name.replace(" ", "_").lower()
        (OUTPUT_DIR / f"{slug}_raw.txt").write_text(raw, encoding="utf-8")

        if result is None:
            print(f"  FAILED — see {slug}_raw.txt\n")
            continue

        (OUTPUT_DIR / f"{slug}_response.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        boxes = result.get("boxes") or {}
        non_null = sum(1 for v in boxes.values() if isinstance(v, list) and len(v) == 4)
        print(f"  vendor_confirmed: {result.get('vendor_confirmed')}")
        print(f"  boxes: {non_null} non-null / {len(boxes)} total")
        print(f"  saved: {slug}_response.json\n")

    print("Done.")


if __name__ == "__main__":
    main()
