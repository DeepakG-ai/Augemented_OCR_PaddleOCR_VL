import base64, json, urllib.request, textwrap

IMAGE_PATH = r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-24 172803.png"
VLLM_URL = "http://localhost:8001/v1/chat/completions"

with open(IMAGE_PATH, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

prompt = "OCR:"  # or 'Table Recognition:', 'Chart Recognition:', etc.

payload = {
    "model": "PaddlePaddle/PaddleOCR-VL-1.5",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }
    ],
    "temperature": 0.0,
    "max_tokens": 512,
}

req = urllib.request.Request(
    VLLM_URL,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
resp = urllib.request.urlopen(req, timeout=90)
result = json.loads(resp.read())

print(result["choices"][0]["message"]["content"])