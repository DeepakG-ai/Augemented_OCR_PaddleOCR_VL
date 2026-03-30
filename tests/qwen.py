import base64
import httpx
import json

IMAGE_PATH = r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-26 160717.png"
LLAMA_URL  = "http://localhost:8001/v1/chat/completions"

with open(IMAGE_PATH, "rb") as f:
    b64 = base64.b64encode(f.read()).decode("utf-8")

payload = {
    "model": "qwen3vl",
    "messages": [
        {
            "role": "system",
            "content": "You are a document data extraction assistant. Extract only what is explicitly visible in the document. Return ONLY valid JSON with no explanation, no markdown, no extra text."
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}
                },
                {
                    "type": "text",
                    "text": """You are seeing the continuation of first page which has 8 columns has header no, variant , description , supplier code( null), qty, uom, unit cost, amount. Extract the following fields from this document and return ONLY a valid JSON object. If a field is not found, set it to null.
Return ONLY this JSON structure, nothing else"""
                }
            ]
        }
    ],
    "temperature": 0.0,
    "max_tokens": 1024
}

print("Sending image to model...")

with httpx.Client(timeout=120.0) as client:
    response = client.post(LLAMA_URL, json=payload)
    response.raise_for_status()

raw = response.json()["choices"][0]["message"]["content"].strip()

# Strip markdown fences if model adds them
raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

try:
    parsed = json.loads(raw)
    print("\n=== EXTRACTED DATA ===")
    print(json.dumps(parsed, indent=2))
except json.JSONDecodeError:
    print("\n=== RAW OUTPUT (parse failed) ===")
    print(raw)

"""llama-server ^
  --model Qwen3-VL-8B-Instruct-UD-Q4KXL.gguf ^
  --mmproj mmproj-F16.gguf ^
  --host 0.0.0.0 --port 8001 ^
  --n-gpu-layers 999 ^
  --ctx-size 8192 ^
  --threads 8 ^
  --parallel 2 ^
  --flash-attn"""