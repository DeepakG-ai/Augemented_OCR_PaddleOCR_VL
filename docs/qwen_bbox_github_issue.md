# Qwen3-VL Bounding Box Alignment Issue (llama.cpp)

---

## Problem Statement

When using **Qwen3-VL** with the **llama.cpp** server, bounding box coordinates appear offset or
shifted when overlaid on the original image in the Review UI. The boxes point approximately to
the right area of the page but are never pixel-perfect — typically a few pixels to ~20px off in
any direction.

---

## History of Fixes

### Phase 1 — First attempt (~75% accuracy)

**Symptom**: Boxes were significantly off. Some fields showed no box at all (5/10 fields mapped).

**Root cause (believed at the time)**: llama.cpp pads images to the nearest multiple of 32 before
passing to the ViT. The 0–1000 coordinate grid was assumed to map to that aligned image, not the
original.

**Fix applied** (`bbox_agent.py`, then `worker.py`):

```python
# Step 1: aligned dimensions
FACTOR = 32
w_bar = round(original_width  / FACTOR) * FACTOR   # e.g., 1275 → 1280
h_bar = round(original_height / FACTOR) * FACTOR   # e.g., 1650 → 1664

# Step 2: 0-1000 grid → aligned pixels → 0-1 normalized
x0_px = (raw_box[0] / 1000.0) * w_bar
nx0   = x0_px / original_width
```

This improved accuracy to ~80% but never reached pixel-perfect alignment.

**Also fixed at this point**: Removed OCR word-snapping from `qwen_layout_apply.py` (was
silently dropping 5/10 fields by failing to find matching words).

---

### Phase 2 — Research on 2026-05-15 (Claude Code investigation)

#### What was actually found in the code

The full coordinate pipeline was traced end-to-end:

```
processor.py      → resize image to 32-aligned dims (_p1w, _p1h), store in MinIO + DB
worker.py (LLM)   → receive raw_box [0-1000] from Qwen, apply w_bar/h_bar, save normalized_box
worker.py (POST)  → load qwen_layout_boxes, call qwen_layout_apply
qwen_layout_apply → denormalize normalized_box × page_width/height → pixel_box
review.js         → pixel_box × (dispW / img.naturalWidth) → CSS position
```

**Key finding**: `processor.py` already pre-aligns images to multiples of 32 via
`_resize_to_vlm_budget()` before sending to the model. This means `_p1w` is always a multiple
of 32, so:

```python
w_bar = round(_p1w / 32) * 32  ==  _p1w   # no change, always
```

The entire `w_bar`/`h_bar` intermediate step was a **mathematical no-op**:

```
raw_box[0] / 1000 × w_bar ÷ _p1w  =  raw_box[0] / 1000 × _p1w ÷ _p1w  =  raw_box[0] / 1000
```

The code was doing `raw → pixel → normalized` (in worker.py), then immediately
`normalized → pixel` again (in qwen_layout_apply). The user correctly identified this as
"duplicating 2 times." It produced the right answer but through a redundant roundtrip.

---

#### Critical discovery: Qwen3-VL vs Qwen2.5-VL coordinate systems are different

The `w_bar`/`h_bar` fix was designed for **Qwen2.5-VL**, which used **absolute pixel
coordinates** relative to the resized image. You had to know `resized_w` to decode them.

**Qwen3-VL changed to purely relative 0–1000 coordinates.**

Source — llama.cpp issue #16842, contributor `theo77186` (Contributor badge):

> "Qwen3-VL bounding box coordinates are relative (per thousand unit)... So regardless of
> the image's original size, **both x and y will always be from 0 to 1000**.
> Coordinate System: Qwen3-VL's default coordinate system has been **changed from the
> absolute coordinates used in Qwen2.5-VL to relative coordinates ranging from 0 to 1000.
> You don't need to calculate the resized_w.**"

Source — contributor `tarruda` (same issue), showing the correct decode:

```javascript
const [label, x1, y1, x2, y2] = bbox;
const size = 1000;
const X1 = x1 / size * imgWidth;   // just this — no w_bar
const Y1 = y1 / size * imgHeight;
const X2 = x2 / size * imgWidth;
const Y2 = y2 / size * imgHeight;
```

Source — **confirmed working Python reference** (issue #16880 author, issue is now CLOSED):

```python
# Image sent to llama-server at ORIGINAL size — no 32-alignment pre-processing at all.
# This still works perfectly. Confirms coordinates are truly proportional to whatever
# image size you send.
image = Image.open(filename)   # e.g., 800×600, not pre-aligned

for bbox in bboxes:
    x0, y0, x1, y1 = bbox["bbox_2d"]
    size = 1000
    x0 = x0 / size * image.width    # proportional to the image you sent — nothing else
    y0 = y0 / size * image.height
    x1 = x1 / size * image.width
    y1 = y1 / size * image.height
```

**Issue #16880 status: CLOSED.** The author confirmed: "Qwen3-VL's bounding box coordinates
are relative to a 1000×1000 grid. I transformed the bounding box coordinates back to the
original image size" — and the code above works correctly without any alignment correction.

---

#### Code fix applied (2026-05-15)

`backend/worker.py` — removed the `w_bar`/`h_bar` intermediate step:

**Before (wrong reasoning, accidentally correct result):**
```python
# Based on Qwen2.5-VL absolute-pixel logic — does NOT apply to Qwen3-VL.
# Only "worked" because processor.py pre-aligns images, making w_bar == _p1w always,
# so the multiplication and division cancel each other out.
FACTOR = 32
w_bar = max(FACTOR, int(round(_p1w / FACTOR) * FACTOR))   # == _p1w (always, since pre-aligned)
h_bar = max(FACTOR, int(round(_p1h / FACTOR) * FACTOR))   # == _p1h (always)
x0_px = (raw_box[0] / 1000.0) * w_bar   # = raw_box[0] / 1000 * _p1w
nx0   = max(0.0, x0_px / _p1w)          # = raw_box[0] / 1000   ← the w_bar cancelled!
# then in qwen_layout_apply:
# pixel = nx0 * page_width = raw_box[0] / 1000 * page_width  ← same as the direct formula
```

**After (correct for Qwen3-VL, matches the confirmed reference implementation):**
```python
nx0 = max(0.0, raw_box[0] / 1000.0)
ny0 = max(0.0, raw_box[1] / 1000.0)
nx1 = min(1.0, raw_box[2] / 1000.0)
ny1 = min(1.0, raw_box[3] / 1000.0)
# qwen_layout_apply then: pixel = nx * page_dim  →  raw_box[i] / 1000 * page_dim
```

**Will this change bbox positions?** No — numerically identical to the old code. Code clarity
fix only.

**Does the 32-pixel pre-alignment in `processor.py` still matter?**
Yes, but not for coordinate accuracy. It prevents llama.cpp from silently re-padding the
image to a different size than what we have stored in the DB for `page_width`/`page_height`.
If llama.cpp internally resized to a different dimension, our stored dimensions would be
wrong and all coordinate math would be off. Pre-aligning ensures the image we store matches
the image the model sees.

---

#### Why ~20% drift still remains — and cannot be fixed in Python

The remaining inaccuracy has two causes that live entirely inside the llama.cpp server:

**1. Non-square image aspect-ratio distortion (llama.cpp issue #16880)**

> "Eval bug: Qwen3-VL provides incorrect bounding boxes on sizes that are not 1000×1000px /
> non-square."
> "For the square image we need to resize the image to be 1.25x so that the bounding box
> output aligns correctly. For non-square images the bounding boxes have incorrect aspect ratio."

PDF pages are portrait-oriented (e.g., 960×1280). llama.cpp's internal ViT preprocessing
applies scaling that is not perfectly proportional for non-square aspect ratios. The resulting
coordinate drift is systematic and proportional to how far the aspect ratio is from 1:1.

**2. 8B Q4 model inherent localization imprecision (llama.cpp issue #17131)**

> "Qwen3-VL 8B shows poor localisation. This occurs even with FP16 versions, ruling out
> quantization as the cause. Suspect non-vision layers are removed during GGUF conversion."

The 8B model at Q4 quantization does not pinpoint label boundaries precisely. Boxes often
include a few pixels of the adjacent value text or miss the edge of the label.

**Update (2026-05-15)**: Issue #16880 is now **CLOSED**. The author confirmed the fix:
`coord / 1000 * image_dim` works correctly for any image size. The "non-square aspect ratio"
wording in the original report was misleading — the actual bug was that the reporter was
NOT using the simple proportional formula. Once they used `x / 1000 * width` and
`y / 1000 * height` independently, boxes aligned correctly for all aspect ratios.

**What this means for our pipeline**: The coordinate math is correct. The remaining visual
drift is purely the 8B Q4 model's localization imprecision (issue #17131), not a coordinate
system problem.

**Workarounds for remaining model-level imprecision:**
- Use a larger model (30B). Significantly more accurate localization but higher hardware cost.
- Switch to HuggingFace Transformers inference. Eliminates llama.cpp entirely but needs GPU
  memory for full Python inference — not viable in the current setup.
- Accept the imprecision. At 8B Q4, boxes land in the correct field area and the label is
  identifiable in the UI.

---

## Current state of the pipeline (as of 2026-05-15)

| Component | What it does | Status |
|---|---|---|
| `processor.py` `_resize_to_vlm_budget()` | Pre-aligns image to 32px multiples before sending to model | Correct — prevents server re-padding |
| `worker.py` LLM stage | Saves `raw_box[i] / 1000.0` as normalized_box | Fixed — redundant w_bar step removed |
| `qwen_layout_apply.py` | `normalized × page_dim` → pixel box | Correct — pure pass-through |
| `review.js` `rvRenderMappingRects()` | `pixel_box × (dispW / natW)` → CSS position | Correct |

**Accuracy**: ~80% — boxes land in the correct field area but may be off by a few pixels on
any edge due to the non-square llama.cpp issue and model precision.

---

## References

| Link | What it says |
|---|---|
| [llama.cpp #16842](https://github.com/ggml-org/llama.cpp/issues/16842) | w_bar/h_bar padding proposal; `theo77186` clarifies Qwen3-VL uses relative 0-1000 (no resized_w needed) |
| [llama.cpp #16878](https://github.com/ggml-org/llama.cpp/pull/16878) | PR: refactor preprocessing, support max/min pixels |
| [llama.cpp #16880](https://github.com/ggml-org/llama.cpp/issues/16880) | CLOSED. Confirmed fix: `coord / 1000 * image_dim`. No aspect-ratio correction needed. |
| [llama.cpp #17131](https://github.com/ggml-org/llama.cpp/issues/17131) | Qwen3-VL 8B poor localisation even in FP16 |
| [llama.cpp #13694](https://github.com/ggml-org/llama.cpp/issues/13694) | Qwen2.5-VL-7B inaccurate bbox — confirmed separate from Qwen3-VL issue |
| [Qwen3-VL #1486](https://github.com/QwenLM/Qwen3-VL/issues/1486) | Open question on whether double conversion is needed (answer: no, relative coords) |
| [tarruda gist](https://gist.github.com/tarruda/09dcbc44c2be0cbc96a4b9809942d503) | Reference Qwen3-VL bbox decode: `x / 1000 * W` |
| [HF Qwen3-VL-8B discussion #17](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct/discussions/17) | Scaling and offset issues reported even in FP16 — architectural, not quantization |
