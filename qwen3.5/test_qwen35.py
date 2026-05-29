"""
qwen3.5/test_qwen35.py — diagnostic script using PRODUCTION code directly.

Usage:
    cd C:\\Users\\aigroup5\\PycharmProjects\\Augemented_OCR_PaddleOCR_VL
    .venv\\Scripts\\python.exe qwen3.5\\test_qwen35.py
"""
from __future__ import annotations

import asyncio
import argparse
import base64
import json
import sys
import textwrap
import time
from pathlib import Path

import httpx

# ── Add project root so backend imports work ──────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Production modules — no duplication
from backend.processor import pdf_to_images
from backend.extractor import build_system_prompt, build_user_message
from backend.config import (
    LLM_URL, LLM_MODEL,
    LLM_TEMPERATURE, LLM_TOP_P, LLM_PRESENCE_PENALTY, LLM_MAX_TOKENS_FIELDS,
)

OUT_DIR = Path(__file__).parent / "output"
OUT_DIR.mkdir(exist_ok=True)

VENDORS = [
    {
        "name": "RJ Schinner",
        "pdf":  Path(r"C:\Users\aigroup5\Downloads\PDF Samples\RJ SchINNER\RJ SCHINNER 563773.pdf"),
        "header_fields": [
            "bill_to", "ship_to", "order_date",
            "vendor_name", "order_number", "vendor_adress",
        ],
        "line_item_fields": [
            "item", "pack", "order_qty", "unit_price",
        ],
        "instructions": None,
        "rules": [],
        "strict_line_items": True,
        "total_pages": 3,
    },
    {
        "name": "Canada Metal",
        "pdf":  Path(r"C:\Users\aigroup5\Downloads\Canada Metal - INVCM-1005.pdf"),
        "header_fields": [
            "vendor_name", "vendor_address", "invoice_number", "invoice_date",
            "po_number", "invoice_total", "invoice_subtotal",
            "tax_amount", "frieght_amount",
        ],
        "line_item_fields": [
            "item", "line_description", "quantity_ordered",
            "quantity_received", "unit_price", "line_total", "uom",
        ],
        "instructions": None,
        "rules": [],
    },
    {
        "name": "Aegis",
        "pdf":  Path(r"C:\Users\aigroup5\Downloads\Aegis - INV-AG1008 2.pdf"),
        "header_fields": [
            "bill_to", "ship_to", "invoice_date", "invoice_number",
            "vendor_name", "po_number", "order_date", "order_number",
            "fob", "terms", "freight", "invoice_total",
        ],
        "line_item_fields": [
            "item", "required_qty", "ship_qty", "uom", "unit_price", "line_total",
        ],
        "instructions": None,
        "rules": [],
    },
]


async def call_llm_raw(image_b64: str, system_prompt: str, user_message: str) -> dict:
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": user_message},
                ],
            },
        ],
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "presence_penalty": LLM_PRESENCE_PENALTY,
        "max_tokens": LLM_MAX_TOKENS_FIELDS,
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(LLM_URL, json=payload)
    return resp.json()


def hr(title="", w=80):
    if title:
        pad = (w - len(title) - 2) // 2
        print("=" * pad + f" {title} " + "=" * (w - pad - len(title) - 2))
    else:
        print("=" * w)


def dump_response(resp: dict, label: str):
    hr(f"RESPONSE -- {label}")
    choices = resp.get("choices") or []
    msg = choices[0].get("message", {}) if choices else {}
    usage = resp.get("usage", {})
    print(f"finish_reason : {choices[0].get('finish_reason') if choices else 'N/A'}")
    print(f"tokens        : prompt={usage.get('prompt_tokens')}  "
          f"completion={usage.get('completion_tokens')}  "
          f"total={usage.get('total_tokens')}")
    content   = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    print(f"\n--- message.content ({len(content)} chars) ---")
    print(content[:4000] if content else "[EMPTY]")
    print(f"\n--- message.reasoning_content ({len(reasoning)} chars) ---")
    print(textwrap.shorten(reasoning, 600, placeholder=" ...[truncated]") if reasoning else "[EMPTY]")
    hr()
    print()


async def run_vendor(v: dict):
    name = v["name"]
    slug = name.replace(" ", "_").lower()
    hr(f"TEST: {name}")

    # 1. Render using production processor.pdf_to_images
    print(f"[1] Rendering {v['pdf'].name} page 1 ...")
    if not v["pdf"].exists():
        print(f"    ERROR: not found: {v['pdf']}"); return
    pages = await pdf_to_images(v["pdf"].read_bytes(), max_pages=1)
    page  = pages[0]
    image_b64 = page["image_b64"]
    w, h = page["width"], page["height"]
    tokens = (w // 32) * (h // 32)
    print(f"    image: {w}x{h}  image_tokens={tokens}")
    (OUT_DIR / f"{slug}_page1.jpg").write_bytes(base64.b64decode(image_b64))

    # 2. Build prompts using production extractor functions
    print("[2] Building prompts ...")
    sys_prompt = build_system_prompt(
        header_fields=v["header_fields"],
        line_item_fields=v["line_item_fields"],
        instructions=v["instructions"],
        rules=v["rules"],
        format_type="single_po_multipage",
        gold_examples=None,
        include_boxes=True,
    )
    user_msg = build_user_message(
        header_fields=v["header_fields"],
        line_item_fields=v["line_item_fields"],
        page_num=1, total_pages=v.get("total_pages", 1),
        include_boxes=True,
    )
    (OUT_DIR / f"{slug}_system_prompt.txt").write_text(sys_prompt, encoding="utf-8")
    (OUT_DIR / f"{slug}_user_message.txt").write_text(user_msg, encoding="utf-8")

    if v.get("strict_line_items"):
        strict_block = """
<strict_line_items_required>
- You MUST return the `fields.line_items` key.
- `fields.line_items` MUST be an array of row objects using exactly the requested line-item columns.
- If any line item row is visible on this page, do NOT omit `line_items` and do NOT return an empty array.
- Boxes are only for labels/column headers; they do not replace row extraction.
</strict_line_items_required>"""
        sys_prompt = sys_prompt + strict_block
        user_msg = user_msg + """

STRICT LINE ITEM REQUIREMENT:
Return `fields.line_items` as an array. Extract every visible table row on this page.
Do not omit the `line_items` key."""

        (OUT_DIR / f"{slug}_system_prompt.txt").write_text(sys_prompt, encoding="utf-8")
        (OUT_DIR / f"{slug}_user_message.txt").write_text(user_msg, encoding="utf-8")

    print(f"    system_prompt: {len(sys_prompt)} chars")
    print(f"    user_message:  {len(user_msg)} chars")

    # 3. Call LLM
    print(f"[3] POST {LLM_URL}  model={LLM_MODEL}  "
          f"temp={LLM_TEMPERATURE} top_p={LLM_TOP_P} "
          f"presence_penalty={LLM_PRESENCE_PENALTY} max_tokens={LLM_MAX_TOKENS_FIELDS}")
    t0 = time.perf_counter()
    resp = await call_llm_raw(image_b64, sys_prompt, user_msg)
    print(f"    done in {time.perf_counter()-t0:.1f}s")
    (OUT_DIR / f"{slug}_response.json").write_text(
        json.dumps(resp, indent=2, ensure_ascii=False), encoding="utf-8")

    dump_response(resp, name)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vendors", nargs="*", help="Optional vendor names to run, e.g. RJ Schinner")
    args = parser.parse_args()
    wanted = {name.casefold() for name in args.vendors}

    print(f"LLM: {LLM_URL}  model={LLM_MODEL}\n")
    for v in VENDORS:
        if wanted and v["name"].casefold() not in wanted:
            continue
        try:
            await run_vendor(v)
        except Exception as exc:
            print(f"\nFATAL ERROR in {v['name']}: {exc}")
            import traceback; traceback.print_exc()
        print()
    print(f"Output: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
