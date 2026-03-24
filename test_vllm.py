"""Test vLLM with single-field prompts (how the actual pipeline works)."""
import base64, json, urllib.request

IMAGE_PATH = r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-24 172803.png"
VLLM_URL = "http://localhost:8001/v1/chat/completions"

with open(IMAGE_PATH, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

FIELDS = [
    ("vendor_name", "Find the company or vendor name at the top of this document."),
    ("order_date", "Find the date of the order or invoice."),
    ("order_number", "Find the order number or invoice number."),
    ("bill_to_address", "Find the billing address or 'Bill To' address."),
]

SYSTEM = (
    "You are a document data extraction assistant. "
    "Return ONLY valid JSON. No explanation. "
    "If the field is not found, return null."
)

for field, prompt in FIELDS:
    payload = json.dumps({
        "model": "PaddlePaddle/PaddleOCR-VL-1.5",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": f'{prompt}\n\nReturn ONLY: {{"{field}": "<value>"}}'}
                ]
            }
        ],
        "temperature": 0.0,
        "max_tokens": 128
    }).encode()

    req = urllib.request.Request(VLLM_URL, data=payload, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())

    content = result["choices"][0]["message"]["content"].strip()
    finish = result["choices"][0]["finish_reason"]
    print(f"\n[{field}] (finish={finish})")
    print(f"  Raw: {content[:200]}")

    # Try JSON parse
    try:
        clean = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(clean)
        print(f"  Parsed: {json.dumps(parsed)}")
    except:
        print(f"  (not valid JSON)")

print("\nDone.")
