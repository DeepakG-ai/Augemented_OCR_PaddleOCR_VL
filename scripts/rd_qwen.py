"""
rd_qwen.py — Restaurant Depot PO extraction via Qwen3-VL (llama-server).
Each page is an independent PO — one prompt, per-page results.
"""
import base64
import json
import logging
import re
import sys
from pathlib import Path

import httpx

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rd_qwen")

# ── Config ────────────────────────────────────────────────────────────
LLAMA_URL = "http://localhost:8056/v1/chat/completions"

IMAGE_PATHS = [
    r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-30 124620.png",
    r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-30 124637.png",
]

# ── JSON Template ─────────────────────────────────────────────────────
HEADER_FIELDS = ["po_number", "order_date", "vendor", "bill_to", "ship_to"]
LINE_ITEM_FIELDS = ["item_code", "upc_code", "pack_size", "description",
                     "vendor_item", "ordered_cases", "our_units",
                     "list_cost", "po_cost", "extended_po_cost"]

_TEMPLATE: dict = {}
for _f in HEADER_FIELDS:
    _TEMPLATE[_f] = None
_TEMPLATE["line_items"] = [{col: None for col in LINE_ITEM_FIELDS}]

# ── System Prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a highly accurate document data extraction assistant.
Extract ONLY what is explicitly visible in the document image.
Never guess or fabricate data. If a field is not visible, set it to null.

<document_format>
Each page of this document contains an independent purchase order.
Extract each page as a separate, complete record.
</document_format>

<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

<output_rules>
- Return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- Use null for missing fields, never omit them.
- For line_items, return an array even if only one item exists.
- Numbers should be numeric (not strings) when possible.
- Dates should be in the format they appear in the document.
</output_rules>"""

# ── User Prompt (same for every page) ─────────────────────────────────
def get_user_prompt() -> str:
    header_list = "\n".join(f"  - {f}" for f in HEADER_FIELDS)
    line_list = "\n".join(f"  - {f}" for f in LINE_ITEM_FIELDS)

    return f"""Extract the header fields AND all visible line item rows from this purchase order page.
If any field is empty or not visible, return null in the JSON object.

<header_fields>
{header_list}
</header_fields>

<line_item_columns>
{line_list}
</line_item_columns>

<json_template>
{json.dumps(_TEMPLATE, indent=2)}
</json_template>

<rules>
- Empty or missing cells → null.
- Numbers (qty, unit_cost, amount, unit_price, etc.) must be numbers, not strings.
- Extract every visible line item row.
</rules>

Return ONLY valid JSON matching EXACTLY the structure above."""


# ── LLM Call ─────────────────────────────────────────────────────────
_THINK_RE    = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_START = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
_FENCE_END   = re.compile(r"\n?```\s*$",          re.MULTILINE)


def clean_response(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    raw = _FENCE_START.sub("", raw)
    raw = _FENCE_END.sub("", raw)
    return raw.strip()


def call_llm(image_b64: str) -> dict:
    payload = {
        "model": "qwen3vl",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": get_user_prompt(),
                    },
                ],
            },
        ],
        "temperature": 0.6,
        "max_tokens":  6000,
    }

    log.info("Calling LLM ...")
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(LLAMA_URL, json=payload)
        resp.raise_for_status()

    raw     = resp.json()["choices"][0]["message"]["content"].strip()
    log.debug("Raw output (first 300 chars):\n%s", raw[:300])
    cleaned = clean_response(raw)
    return json.loads(cleaned)


# ── Main ──────────────────────────────────────────────────────────────
def main():
    all_results = []

    for idx, path in enumerate(IMAGE_PATHS):
        image_path = Path(path)

        if not image_path.exists():
            log.error("Image not found: %s", path)
            sys.exit(1)

        log.info("Loading page %d: %s", idx + 1, image_path.name)
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        try:
            result = call_llm(b64)
            items = result.get("line_items") or []
            log.info("Page %d done — PO: %s — %d line item(s)",
                     idx + 1, result.get("po_number", "?"), len(items))
            all_results.append(result)

        except json.JSONDecodeError as exc:
            log.error("Page %d JSON parse FAILED at pos %d: %s", idx + 1, exc.pos, exc.msg)
            sys.exit(1)
        except httpx.HTTPStatusError as exc:
            log.error("HTTP error %s: %s", exc.response.status_code, exc.response.text)
            sys.exit(1)
        except httpx.RequestError as exc:
            log.error("Request failed (llama-server running?): %s", exc)
            sys.exit(1)

    # Each page is a separate PO — output as array
    print("\n" + "=" * 60)
    print(f"EXTRACTED {len(all_results)} PURCHASE ORDER(S)")
    print("=" * 60)
    print(json.dumps(all_results, indent=2, ensure_ascii=False))

    output_path = Path("rd_extracted_pos.json")
    output_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved to %s", output_path.resolve())


if __name__ == "__main__":
    main()
