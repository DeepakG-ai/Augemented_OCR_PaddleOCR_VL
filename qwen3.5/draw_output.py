"""Draw extracted boxes on the page images and open an HTML viewer."""
import json
import base64
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).parent / "output"

COLOR_HEADER    = "#00FF88"
COLOR_LINE_ITEM = "#FFD700"

VENDORS = [
    {
        "slug": "canada_metal",
        "name": "Canada Metal",
        "header_fields": ["vendor_name","vendor_address","invoice_number","invoice_date",
                          "po_number","invoice_total","invoice_subtotal","tax_amount","frieght_amount"],
        "line_item_fields": ["item","line_description","quantity_ordered","quantity_received",
                             "unit_price","line_total","uom"],
    },
    {
        "slug": "aegis",
        "name": "Aegis",
        "header_fields": ["bill_to","ship_to","invoice_date","invoice_number","vendor_name",
                          "po_number","order_date","order_number","fob","terms","freight","invoice_total"],
        "line_item_fields": ["item","required_qty","ship_qty","uom","unit_price","line_total"],
    },
]

def draw_boxes(slug, name, header_fields, line_item_fields):
    img_path  = OUT_DIR / f"{slug}_page1.jpg"
    json_path = OUT_DIR / f"{slug}_response.json"
    out_path  = OUT_DIR / f"{slug}_boxes.png"

    img  = Image.open(img_path).convert("RGB")
    raw_resp = json.loads(json_path.read_text(encoding="utf-8"))
    content_str = raw_resp["choices"][0]["message"]["content"]
    data = json.loads(content_str)
    boxes = data.get("boxes") or {}

    iw, ih = img.size
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
        font_sm = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        font = font_sm = ImageFont.load_default()

    hf_set = set(header_fields)
    lf_set = set(line_item_fields)
    found = missing = 0

    for key, box in boxes.items():
        if not isinstance(box, list) or len(box) != 4:
            missing += 1
            continue
        x0 = box[0] / 1000 * iw
        y0 = box[1] / 1000 * ih
        x1 = box[2] / 1000 * iw
        y1 = box[3] / 1000 * ih
        if x1 <= x0 or y1 <= y0:
            missing += 1
            continue
        color = COLOR_LINE_ITEM if key in lf_set else COLOR_HEADER
        draw.rectangle([x0, y0, x1, y1], outline=color, width=2)
        label = key.replace("_", " ")
        lbl_y = y0 - 14 if y0 > 14 else y1 + 2
        draw.rectangle([x0, lbl_y, x0 + len(label)*7 + 4, lbl_y + 13], fill=(0,0,0))
        draw.text((x0+2, lbl_y+1), label, fill=color, font=font_sm)
        found += 1

    # legend
    legend = [
        (COLOR_HEADER,    "header field label"),
        (COLOR_LINE_ITEM, "line-item column header"),
        ("white",         f"{name}  |  found={found}  missing={missing}"),
    ]
    lx, ly = 6, ih - len(legend)*20 - 8
    for c, t in legend:
        draw.rectangle([lx-3, ly-2, lx+340, ly+15], fill=(0,0,0))
        draw.text((lx, ly), t, fill=c, font=font_sm)
        ly += 20

    img.save(out_path, format="PNG")
    print(f"Saved: {out_path.name}  ({found} boxes drawn)")
    return out_path


# Build HTML viewer
pages = []
for v in VENDORS:
    box_img = draw_boxes(v["slug"], v["name"], v["header_fields"], v["line_item_fields"])
    json_path = OUT_DIR / f"{v['slug']}_response.json"
    raw_resp = json.loads(json_path.read_text(encoding="utf-8"))
    data = json.loads(raw_resp["choices"][0]["message"]["content"])
    fields = data.get("fields", {})
    line_items = fields.pop("line_items", [])
    pages.append({
        "name": v["name"],
        "img_path": box_img,
        "fields": fields,
        "line_items": line_items,
    })

def img_b64(path):
    return base64.b64encode(path.read_bytes()).decode()

html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Qwen3.5 Extraction Output</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d0d0d; color: #e0e0e0; font-family: 'JetBrains Mono', monospace, monospace; padding: 20px; }
h1 { color: #00ff88; font-size: 18px; margin-bottom: 20px; letter-spacing: 2px; }
h2 { color: #00ff88; font-size: 14px; letter-spacing: 1px; margin-bottom: 12px; border-bottom: 1px solid #333; padding-bottom: 6px; }
.vendor { display: flex; gap: 20px; margin-bottom: 40px; }
.left { flex: 0 0 auto; }
.left img { max-width: 520px; border: 1px solid #333; display: block; }
.right { flex: 1; overflow: auto; }
.section { margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 11px; }
th { background: #1a1a1a; color: #888; text-align: left; padding: 4px 8px; border-bottom: 1px solid #333; }
td { padding: 4px 8px; border-bottom: 1px solid #1a1a1a; vertical-align: top; color: #ccc; }
td:first-child { color: #aaa; width: 180px; }
td.val { color: #00d4ff; }
tr:hover td { background: #111; }
.null { color: #444; font-style: italic; }
.badge { display:inline-block; padding:2px 8px; border-radius:3px; font-size:10px; font-weight:bold; }
.badge-ok { background:#003322; color:#00ff88; border:1px solid #00ff88; }
</style>
</head>
<body>
<h1>&#9654; QWEN3.5 9B EXTRACTION OUTPUT</h1>
"""

for p in pages:
    fields_rows = ""
    for k, v in p["fields"].items():
        val = str(v).replace("\n", " ") if v is not None else None
        cls = "null" if val is None else "val"
        display = val if val is not None else "null"
        fields_rows += f"<tr><td>{k}</td><td class='{cls}'>{display}</td></tr>"

    li_headers = list(p["line_items"][0].keys()) if p["line_items"] else []
    li_head_html = "".join(f"<th>{h}</th>" for h in li_headers)
    li_rows_html = ""
    for row in p["line_items"]:
        cells = ""
        for h in li_headers:
            v = row.get(h)
            val = str(v).replace("\n"," ") if v is not None else "null"
            cls = "null" if v is None else "val"
            cells += f"<td class='{cls}'>{val}</td>"
        li_rows_html += f"<tr>{cells}</tr>"

    b64 = img_b64(p["img_path"])
    html += f"""
<h2>{p["name"]} &nbsp; <span class="badge badge-ok">DONE</span></h2>
<div class="vendor">
  <div class="left">
    <img src="data:image/png;base64,{b64}" />
  </div>
  <div class="right">
    <div class="section">
      <h2>Header Fields</h2>
      <table><tbody>{fields_rows}</tbody></table>
    </div>
    <div class="section">
      <h2>Line Items ({len(p["line_items"])})</h2>
      <table><thead><tr>{li_head_html}</tr></thead><tbody>{li_rows_html}</tbody></table>
    </div>
  </div>
</div>
"""

html += "</body></html>"

html_path = OUT_DIR / "viewer.html"
html_path.write_text(html, encoding="utf-8")
print(f"\nViewer: {html_path}")
