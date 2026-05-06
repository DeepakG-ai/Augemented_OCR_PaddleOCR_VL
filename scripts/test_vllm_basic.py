from __future__ import annotations

import base64
import json
import urllib.request


def run_demo() -> None:
    image_path = r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-24 172803.png"
    vllm_url = "http://localhost:8008/v1/chat/completions"

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": "PaddlePaddle/PaddleOCR-VL-1.5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "OCR:"},
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }

    req = urllib.request.Request(
        vllm_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=90)
    result = json.loads(resp.read())
    print(result["choices"][0]["message"]["content"])


if __name__ == "__main__":
    run_demo()
