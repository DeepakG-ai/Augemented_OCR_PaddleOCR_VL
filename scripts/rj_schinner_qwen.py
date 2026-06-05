"""
rj_schinner_qwen.py — Multi-page RJ Schinner Purchase Order extraction via Qwen3-VL (llama-server).
- Page 1: extracts document header fields + line items
- Continuation pages: extracts line items only
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
log = logging.getLogger("rj_schinner_po")

# ── Config ────────────────────────────────────────────────────────────
LLAMA_URL = "http://localhost:8056/v1/chat/completions"

# Add all your page image paths here in order
IMAGE_PATHS = [
    r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-30 101253.png",  # Page 1
    r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-30 101303.png",  # Page 2+
    # add more pages here...
]

# ── JSON Templates ────────────────────────────────────────────────────
_LINE_ITEM_TEMPLATE = {
    "order_qty":  None,
    "ship_qty":   None,
    "item":       None,
    "pack":       None,
    "unit_price": None,
}

_PAGE_TEMPLATE = {
    "po_number":   None,
    "order_date":  None,
    "vendor":      None,
    "bill_to":     None,
    "line_items":  [_LINE_ITEM_TEMPLATE],
}

# ── System Prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "Extract ALL data from this invoice/purchase order document.\n\n"
    "CRITICAL: Count the number of rows in the line items table FIRST, "
    "then extract that exact number of items.\n\n"
    "Return in this JSON format:\n"
    "- Header fields: po_number, order_date, vendor, bill_to\n"
    "- Line items: order_qty, ship_qty, item, pack, unit_price\n\n"
    "Strictly return in JSON format. "
    "Do NOT include markdown fences, explanations, or any extra text. "
    "Return ONLY valid JSON.\n\n"
    "ACCURACY REQUIREMENTS:\n"
    "- Before extraction: Count total rows in the table visually.\n"
    "- After extraction: Verify your line_items array has that many items.\n"
    "- Double-check you didn't skip rows at page breaks or table headers.\n"
    "- Extract ONLY what is explicitly visible in the document image.\n"
    "- Never guess or fabricate values."
)

# ── User Prompt (same for every page) ────────────────────────────────
def get_user_prompt() -> str:
    return f"""Extract the header fields AND all visible line item rows from this purchase order page.
If any field is empty or not visible, return null in the JSON object.

Return ONLY valid JSON matching EXACTLY this structure:
{json.dumps(_PAGE_TEMPLATE, indent=2)}

Rules:
- po_number: the Purchase Order number (e.g. P1416144).
- order_date: the order date shown on the document.
- vendor: the vendor name and address.
- bill_to: the bill-to company name and address.
- order_qty: the ORDER QTY column value (number).
- ship_qty: the SHIP QTY column value (number).
- item: the ITEM # column value (e.g. 3PB-F-20-BX).
- pack: the PACK column value (e.g. 12/bx, 4/cs).
- unit_price: the UNIT PRICE column value (number).
- Empty or missing cells → null.
- order_qty, ship_qty, unit_price must be numbers, not strings.
- Extract every visible line item row."""


# ── LLM Call ─────────────────────────────────────────────────────────
_THINK_RE    = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_START = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
_FENCE_END   = re.compile(r"\n?```\s*$",          re.MULTILINE)


def clean_response(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    raw = _FENCE_START.sub("", raw)
    raw = _FENCE_END.sub("", raw)
    return raw.strip()


def call_llm(image_b64: str, page_number: int, total_pages: int) -> dict:
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

    log.info("Calling LLM for page %d/%d ...", page_number, total_pages)
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(LLAMA_URL, json=payload)
        resp.raise_for_status()

    raw     = resp.json()["choices"][0]["message"]["content"].strip()
    log.debug("Raw output page %d (first 300 chars):\n%s", page_number, raw[:300])
    cleaned = clean_response(raw)
    return json.loads(cleaned)


# ── Result Merger ─────────────────────────────────────────────────────
def merge_pages(page_results: list[dict]) -> dict:
    """
    Header fields → from page 1.
    line_items    → concatenated from all pages, deduplicated by (item, order_qty, unit_price).
    """
    if not page_results:
        return {}

    merged = {k: v for k, v in page_results[0].items() if k != "line_items"}

    all_items: list[dict] = []
    seen: set[tuple] = set()

    for page in page_results:
        for item in (page.get("line_items") or []):
            key = (
                str(item.get("item", "")).strip().lower(),
                str(item.get("order_qty", "")).strip(),
                str(item.get("unit_price", "")).strip(),
            )
            if key not in seen:
                seen.add(key)
                all_items.append(item)

    merged["line_items"] = all_items
    return merged


# ── Main ──────────────────────────────────────────────────────────────
def main():
    total_pages  = len(IMAGE_PATHS)
    page_results = []

    for idx, path in enumerate(IMAGE_PATHS):
        page_number = idx + 1
        image_path  = Path(path)

        if not image_path.exists():
            log.error("Image not found: %s", path)
            sys.exit(1)

        log.info("Loading page %d: %s", page_number, image_path.name)
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        try:
            result = call_llm(b64, page_number, total_pages)
            log.info("Page %d done — %d line item(s)", page_number, len(result.get("line_items") or []))
            page_results.append(result)

        except json.JSONDecodeError as exc:
            log.error("Page %d JSON parse FAILED at pos %d: %s", page_number, exc.pos, exc.msg)
            sys.exit(1)
        except httpx.HTTPStatusError as exc:
            log.error("HTTP error %s: %s", exc.response.status_code, exc.response.text)
            sys.exit(1)
        except httpx.RequestError as exc:
            log.error("Request failed (llama-server running?): %s", exc)
            sys.exit(1)

    final = merge_pages(page_results)

    print("\n" + "=" * 60)
    print("FINAL MERGED RESULT")
    print("=" * 60)
    print(json.dumps(final, indent=2, ensure_ascii=False))

    output_path = Path("rj_schinner_extracted_po.json")
    output_path.write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved to %s", output_path.resolve())
    log.info("Done — %d page(s), %d total line item(s)", total_pages, len(final.get("line_items", [])))


if __name__ == "__main__":
    main()


# llama-server ^
#   --model "Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf" ^
#   --mmproj "mmproj-F16.gguf" ^
#   --host 0.0.0.0 --port 8056 ^
#   --n-gpu-layers 999 ^
#   --ctx-size 8192 ^
#   --threads 8

"""llama-server ^
  --model "Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf" ^
  --mmproj "mmproj-F16.gguf" ^
  --host 0.0.0.0 ^
  --port 8056 ^
  --n-gpu-layers 999 ^
  --ctx-size 16384 ^
  --threads 8 ^
  --reasoning-budget 1024"""
