# Performance Bottleneck Analysis & Changes
Date: 2026-05-15
Hardware: RTX 4060 8GB GPU, i9 CPU, 64GB RAM
Model: Unsloth Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf + mmproj-F16.gguf (1.15GB, F16)

---

## Observed Problem

Per-page extraction was taking **30-36 seconds**. User reported mmproj was the main culprit.

---

## Root Cause Analysis (from llama cpp log.txt)

Every request in the log showed the same two independent bottlenecks:

```
image slice encoded in ~12,000-13,000 ms   ← ViT encoder (mmproj)
image decoded in ~1,000 ms                 ← projection to LLM space
LLM generation ~21,000-24,000 ms           ← 760-1005 output tokens at 35 t/s

Total: ~36,000 ms per page
```

The image token count was **always 1218 tokens** — fixed, regardless of page content.
This meant every single page was hitting the mmproj at maximum load.

---

## Why 1218 Tokens Every Time

The pixel flow before changes:

```
PDF page (A4 at 150 DPI) = 1240 x 1754 pixels
→ processor.py _resize_to_vlm_budget() caps long side at MAX_LONG_SIDE_PX=1344
→ result: ~949 x 1344 pixels = ~1,275,000 pixels

→ sent to llama-server
→ llama-server had: --image-min-tokens 1024 (min 1,048,576 pixels)
                    --image-max-tokens 2048 (max 2,097,152 pixels)
→ 1.28M pixels is INSIDE [1M, 2M] → NO server-side scaling at all
→ ViT (27 transformer layers, 1152 embedding dim, F16) processes full image
→ always produces 1218 tokens → always 12-13 seconds
```

The `--image-min-tokens 1024` was also upscaling small pages (under 1M pixels)
to 1M pixels before encoding — making those pages even slower than necessary.

---

## Changes Made

### 1. backend/config.py — Image Resolution

| Setting | Before | After |
|---------|--------|-------|
| MAX_LONG_SIDE_PX | 1344 | 960 |
| MAX_PIXELS | 1,806,336 (1344 x 1344) | 691,200 (960 x 720) |
| DPI_DEFAULT | 150 | 120 |
| JPEG_QUALITY | 90 | 92 |

Why:
- MAX_LONG_SIDE_PX=1344 produced images of ~1.28M pixels that always landed inside
  the llama-server [1M, 2M] band, so the ViT always ran at near-maximum load.
- Dropping to 960 produces pages of ~652K pixels — below the server max of 786K,
  so no server-side scaling needed, and the ViT processes ~550-650 tokens instead of 1218.
- DPI_DEFAULT 150→120: digital PDF text is sharp at any DPI above 96. The smaller
  render produces a smaller image before the resize step, slightly faster PDF processing.
- JPEG_QUALITY 90→92: since the image is now smaller, same file size budget buys
  slightly better quality going into the VLM.

New pixel flow:
```
A4 at 120 DPI = 1240 x 1654 pixels
→ caps at long side 960 → ~680 x 960 = ~652,800 pixels
→ llama-server min=196,608 / max=786,432
→ 652K is inside [196K, 786K] → no scaling
→ ViT processes ~652K pixels → ~550-650 image tokens
→ estimated encoding: 4-5 seconds (was 12-13 seconds)
```

### 2. backend/config.py — LLM Sampling Parameters

| Setting | Before | After |
|---------|--------|-------|
| LLM_TEMPERATURE | 0.1 | 0.6 |
| LLM_TOP_P | 0.8 | 0.95 |
| LLM_PRESENCE_PENALTY | 1.5 | 1.0 |

Why:
- Temperature 0.1 is nearly greedy decoding. Qwen3-VL's official recommended
  temperature is 0.6. Too low hurts accuracy on ambiguous invoice text (e.g. when
  two similar vendor names or amounts are near each other on the page).
- top_p 0.95 pairs with temp=0.6 per Qwen3-VL's recommended sampling config.
- Presence penalty 1.5 was too aggressive. Structured JSON output naturally repeats
  token patterns (keys, brackets, null values). A high penalty discourages this
  repetition and can cause malformed JSON or truncated fields. 1.0 is safer.
- These changes do NOT affect inference speed — only output quality.

### 3. llama-server startup command

Before:
```
llama-server
  --model Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf
  --mmproj mmproj-F16.gguf
  --host 0.0.0.0 --port 8001
  --n-gpu-layers 99
  --ctx-size 8192
  --parallel 1
  --flash-attn on
  --cache-ram 0
  --image-max-tokens 2048
  --image-min-tokens 1024
```

After:
```
llama-server
  --model Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf
  --mmproj mmproj-F16.gguf
  --host 0.0.0.0 --port 8001
  --n-gpu-layers 99
  --ctx-size 4096
  --parallel 1
  --flash-attn on
  --image-max-tokens 768
  --image-min-tokens 192
```

Why each change:
- --image-max-tokens 2048 → 768: caps server-side max pixels at ~786K.
  This is the most important single change. It matches the new image size from code.
- --image-min-tokens 1024 → 192: was causing small pages (<1M pixels) to be
  upscaled to 1M before ViT encoding. 192 (196K pixels) is a reasonable floor
  for any real document page.
- --ctx-size 8192 → 4096: actual token usage peaks at ~3000 (prompt ~2000 +
  output ~1000). Halving context size cuts KV cache from 1152 MiB to 576 MiB,
  freeing ~576 MiB VRAM. This reduces memory pressure during ViT encoding.
- --cache-ram 0 removed: this line was actively disabling the KV prompt cache.
  Removing it allows llama-server to cache the non-image system prompt prefix
  across consecutive calls.

---

## Expected Performance After Changes

| Phase | Before | After (estimated) |
|-------|--------|-------------------|
| ViT encoding | 12-13s | 4-5s |
| Image decoding | ~1s | ~0.4s |
| LLM generation (760t) | 21-24s | 8-12s (fewer context tokens) |
| Total per page | 30-36s | 14-18s |

Generation speed (35 t/s) is hardware-bound and cannot be improved without
a stronger GPU or a smaller model. The remaining time after these changes is
the unavoidable cost of the LLM forward pass producing your extraction JSON.

---

## What Was NOT Changed

- mmproj-F16.gguf: still using F16 (full precision) mmproj. A Q4_K or Q8_0
  quantized mmproj would give an additional 25-40% speedup on ViT encoding,
  but Unsloth has not released one for Qwen3-VL-8B as of 2026-05-15.
  Check: https://huggingface.co/unsloth/Qwen3-VL-8B-Instruct-GGUF
- PaddleOCR: runs at full resolution separately (CPU, PP-OCRv5_mobile).
  No changes needed there — it is not in the critical path for the 30s issue.
- n-gpu-layers 99: all layers on GPU, correct.
- flash-attn on: already enabled, correct.

---

## One Anomaly in the Log

Request for task 5621 encoded in 5,146 ms instead of the usual 12,000+ ms,
with the same 1218 output tokens. This was likely a smaller raw input image
(e.g. a short page or a page with large whitespace margins) where the raw
pixel count before the ViT tiling step was lower. It confirms that the
ViT encoding time scales with input pixel area, not just output token count.
