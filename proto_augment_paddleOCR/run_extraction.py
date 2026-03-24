"""
Proto Augment PaddleOCR — Raw VLM Output Tester
================================================
Sends PDF page images to PaddleOCR-VL with NO prompts.
Shows exactly what the model returns by default.
"""

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import requests
from PIL import Image

VLLM_MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"


def pdf_to_images(pdf_path, dpi=200):
    import fitz
    doc = fitz.open(pdf_path)
    images = []
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append(img)
    doc.close()
    return images


def image_to_base64(img, quality=90):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def call_vllm_raw(vllm_url, image_b64, user_text="What is shown in this image?", max_tokens=256):
    """Send image to vLLM with minimal/no prompt. Returns raw response."""
    payload = {
        "model": VLLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": user_text,
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }

    start = time.time()
    try:
        resp = requests.post(f"{vllm_url}/v1/chat/completions", json=payload, timeout=120)
        elapsed = time.time() - start
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {})
        return {"ok": True, "raw": raw, "ms": round(elapsed * 1000), "tokens": tokens}
    except Exception as e:
        return {"ok": False, "raw": str(e), "ms": round((time.time() - start) * 1000), "tokens": {}}


def main():
    parser = argparse.ArgumentParser(description="Raw PaddleOCR-VL tester")
    parser.add_argument("file", help="Path to PDF or image")
    parser.add_argument("--url", default="http://localhost:8001", help="vLLM URL")
    parser.add_argument("--max-tokens", type=int, default=256, help="Max tokens")
    parser.add_argument("--pages", default=None, help="Pages to test, e.g. 1 or 1,3 (default: all)")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[ERROR] File not found: {args.file}")
        sys.exit(1)

    ext = path.suffix.lower()
    if ext == ".pdf":
        print(f"[INFO] Converting PDF to images (200 DPI)...")
        images = pdf_to_images(str(path))
    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"):
        images = [Image.open(str(path)).convert("RGB")]
    else:
        print(f"[ERROR] Unsupported file type: {ext}")
        sys.exit(1)

    # Filter pages if specified
    if args.pages:
        page_nums = [int(p) - 1 for p in args.pages.split(",")]
        images = [images[i] for i in page_nums if i < len(images)]

    print(f"[INFO] File: {path.name}")
    print(f"[INFO] Pages: {len(images)}")
    print(f"[INFO] Model: {VLLM_MODEL}")
    print(f"[INFO] URL: {args.url}")
    print(f"[INFO] Max tokens: {args.max_tokens}")
    print(f"[INFO] Mode: RAW (no prompts, just image)")
    print()

    all_results = []

    for idx, img in enumerate(images):
        page_num = idx + 1
        print(f"--- PAGE {page_num} / {len(images)} ({img.width}x{img.height}) ---")

        b64 = image_to_base64(img)

        result = call_vllm_raw(
            vllm_url=args.url,
            image_b64=b64,
            user_text="What is shown in this image?",
            max_tokens=args.max_tokens,
        )

        record = {
            "page": page_num,
            "status": "OK" if result["ok"] else "FAILED",
            "elapsed_ms": result["ms"],
            "tokens": result["tokens"],
            "raw_response": result["raw"],
        }
        all_results.append(record)

        if result["ok"]:
            print(f"[STATUS] OK | {result['ms']}ms | tokens: {result['tokens']}")
        else:
            print(f"[STATUS] FAILED | {result['ms']}ms")

        print()

    # Save results to JSON
    out_file = path.parent / f"{path.stem}_raw_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    print(f"[DONE] {len(images)} pages processed")
    print(f"[SAVED] {out_file}")


if __name__ == "__main__":
    main()
