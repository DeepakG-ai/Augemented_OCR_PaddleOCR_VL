"""
qwen_raw.py — Send images to Qwen3-VL and print EVERYTHING it returns.
No cleaning, no parsing. Just raw output.
"""
import base64
import json
import sys
import time
from pathlib import Path

import httpx

LLAMA_URL = "http://localhost:8056/v1/chat/completions"

IMAGE_PATHS = [
    r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-27 101218.png",
    r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-27 101241.png",
]

# ── JSON Templates ────────────────────────────────────────────────────
_LINE_ITEM_TEMPLATE = {
    "no":            None,
    "variant":       None,
    "description":   None,
    "supplier_code": None,
    "qty":           None,
    "uom":           None,
    "unit_cost":     None,
    "amount":        None,
}

_PAGE1_TEMPLATE = {
    "po_number":               None,
    "document_date":           None,
    "account_no":              None,
    "requested_shipment_date": None,
    "supplier_name":           None,
    "supplier_address":        None,
    "deliver_to_name":         None,
    "deliver_to_address":      None,
    "line_items":              [_LINE_ITEM_TEMPLATE],
}

_CONTINUATION_TEMPLATE = {
    "line_items": [_LINE_ITEM_TEMPLATE],
}

# ── System Prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert invoice and purchase order extraction system.

Your PRIMARY MISSION: Extract EVERY SINGLE LINE ITEM from the table correctly.

CRITICAL RULES:
1. Extract ALL table rows - if you see 50 rows, extract all 50
2. NEVER truncate or use "..." - extract complete data
3. If table spans multiple pages, extract ALL pages
4. Maintain exact field values as shown in document
5. Never invent or hallucinate data

ACCURACY REQUIREMENTS:
- Before extraction: Count total rows in table visually
- After extraction: Verify your line_items array has that many items
- Double-check you didn't skip rows at page breaks or table headers

OUTPUT FORMAT:
Return ONLY valid JSON, no markdown, no explanations."""


# ── Per-page user prompt ──────────────────────────────────────────────
def get_user_prompt(page_number: int, total_pages: int) -> str:
    if page_number == 1:
        return f"""This is Page 1 of {total_pages} of a Purchase Order.

Extract the header fields AND all visible line item rows from this page. if any field is empty return null in json object
variant will single word like  MANGO, COTTON. description is more than 1 word start with p-wave. Don't hallicinate
Return ONLY valid JSON matching EXACTLY this structure:
{json.dumps(_PAGE1_TEMPLATE, indent=2)}

Rules:
- Empty or missing cells → null.
- qty, unit_cost, amount must be numbers, not strings.
- Extract every visible line item row."""

    else:
        return f"""This is page {page_number} of {total_pages} of a Purchase Order. There is no document header on this page.

Extract ALL visible line item rows from the table. variant will single word like  MANGO, COTTON. description is more than 1 word. Don't hallicinate

Return ONLY valid JSON matching EXACTLY this structure:
{json.dumps(_CONTINUATION_TEMPLATE, indent=2)}

Rules:
- Empty or missing cells → null.
- qty, unit_cost, amount must be numbers, not strings.
- Extract every visible line item row, even partial rows."""


# ── Send & print raw ─────────────────────────────────────────────────
def send_image(image_b64: str, page_num: int, total: int):
    user_prompt = get_user_prompt(page_num, total)

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
                        "text": user_prompt,
                    },
                ],
            },
        ],
        "temperature": 0.6,
        "max_tokens": 6000,
    }

    print(f"\n{'═' * 70}")
    print(f"  PAGE {page_num}/{total} — SENDING TO LLM")
    print(f"  Image size: {len(image_b64):,} base64 chars")
    print(f"{'═' * 70}")
    print(f"\n┌─ SYSTEM PROMPT ─────────────────────────────────────────────")
    print(SYSTEM_PROMPT)
    print(f"└─────────────────────────────────────────────────────────────")
    print(f"\n┌─ USER PROMPT ───────────────────────────────────────────────")
    print(user_prompt)
    print(f"└─────────────────────────────────────────────────────────────")

    payload["stream"] = True

    print(f"\n{'─' * 70}")
    print(f"  PAGE {page_num} — RAW STREAMING RESPONSE:")
    print(f"{'─' * 70}", flush=True)

    t0 = time.perf_counter()
    raw_chunks = []
    buf = b""

    with httpx.Client(timeout=300.0) as client:
        with client.stream("POST", LLAMA_URL, json=payload) as response:
            response.raise_for_status()
            for chunk in response.iter_raw():
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(data_str)
                        delta = chunk_data["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            sys.stdout.write(content)
                            sys.stdout.flush()
                            raw_chunks.append(content)
                    except json.JSONDecodeError:
                        pass

    print()  # newline after stream ends
    elapsed = time.perf_counter() - t0
    raw = "".join(raw_chunks)
    
    print(f"{'─' * 70}")

    # Usage stats (streaming doesn't always provide usage without specific flags, so we'll estimate tokens)
    print(f"\n  ⏱  Latency:    {elapsed:.2f}s")
    print(f"  📊 Completion: ~{len(raw_chunks)} chunks received")
    print(f"{'═' * 70}\n")


def main():
    total = len(IMAGE_PATHS)
    for idx, path in enumerate(IMAGE_PATHS):
        page_num = idx + 1
        p = Path(path)
        if not p.exists():
            print(f"ERROR: File not found: {path}")
            sys.exit(1)

        print(f"\nLoading {p.name}...")
        b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
        send_image(b64, page_num, total)


if __name__ == "__main__":
    main()
