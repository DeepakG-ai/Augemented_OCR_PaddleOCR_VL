#!/usr/bin/env python3
"""
test_paddleocr.py -- Isolated PaddleOCR benchmark / diagnostic.

Reproduces, OUTSIDE the FastAPI app, exactly what backend/ocr_runner.py does so
we can see where the time goes. The production logs show:

    PaddleOCR warmup complete: 2 thread engine(s) ready in ~99000ms

That 99s is *engine initialization*, not per-page inference (which is 3-4s).
This script splits init into its sub-stages so we can pinpoint the 99s:

    1. import paddleocr           (Python + C++ .so load)
    2. PaddleOCR(...) constructor (model file resolve / load / MKLDNN setup)
    3. first  .predict()          (lazy model load + MKLDNN graph JIT compile)
    4. second .predict()          (true steady-state per-page cost)

Usage:
    # Windows
    python pdl_test\test_paddleocr.py
    python pdl_test\test_paddleocr.py "C:\\path\\to\\file.pdf"

    # Ubuntu / RunPod
    python3 pdl_test/test_paddleocr.py
    python3 pdl_test/test_paddleocr.py /workspace/sample.pdf --device cpu --threads 4

Flags:
    --device cpu|gpu     default: cpu (matches OCR_DEVICE)
    --threads N          cpu_threads, default 4 (matches OCR_CPU_THREADS)
    --no-mkldnn          disable MKLDNN to test if MKLDNN JIT is the bottleneck
    --pages N            render at most N pages (default: all)
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
from pathlib import Path

# Default input: the sample the user is testing with on Windows.
DEFAULT_PDF = r"C:\Users\aigroup5\Downloads\PDF Samples\American Paper Twine\American Paper and Twine 557737.pdf"

# Render settings mirrored from backend/processor.py + pdf_extractor.py
MAX_LONG_SIDE = 1536
MAX_PIXELS = 1536 * 1120
JPEG_QUALITY = 92
DPI_FLOOR = 96
DPI_DEFAULT = 128
_FPDF_LCD_TEXT = 0x02


def _banner(msg: str) -> None:
    print(f"\n{'=' * 70}\n{msg}\n{'=' * 70}", flush=True)


def _t(label: str, t0: float) -> float:
    dt = (time.perf_counter() - t0) * 1000
    print(f"  [{dt:9.0f} ms]  {label}", flush=True)
    return dt


def render_pdf(pdf_path: Path, max_pages: int | None) -> list[dict]:
    """Render PDF pages to base64 JPEG, mirroring backend/processor.py."""
    import pypdfium2 as pdfium
    from PIL import Image

    file_bytes = pdf_path.read_bytes()
    pdf = pdfium.PdfDocument(file_bytes)
    total = len(pdf)
    count = min(total, max_pages) if max_pages else total
    pages: list[dict] = []

    for i in range(count):
        page = pdf[i]
        w_pts, h_pts = page.get_width(), page.get_height()
        max_pts = max(w_pts, h_pts)
        target_dpi = max(min(DPI_DEFAULT, int(MAX_LONG_SIDE / (max_pts / 72.0))), DPI_FLOOR)

        bitmap = page.render(
            scale=target_dpi / 72.0,
            fill_color=(255, 255, 255, 255),
            draw_annots=True,
            extra_flags=_FPDF_LCD_TEXT,
        )
        try:
            img = bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()

        # Qwen3-VL dual-budget resize (long-side + pixel-area), 32-aligned.
        w, h = img.size
        scale_side = min(1.0, MAX_LONG_SIDE / max(w, h))
        scale_area = (MAX_PIXELS / (w * h)) ** 0.5 if (w * h) > MAX_PIXELS else 1.0
        scale = min(scale_side, scale_area)
        nw, nh = (max(1, int(w * scale)), max(1, int(h * scale))) if scale < 1.0 else (w, h)
        nw = max(32, round(nw / 32) * 32)
        nh = max(32, round(nh / 32) * 32)
        if (nw, nh) != (w, h):
            img = img.resize((nw, nh), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        pages.append({"page_number": i + 1, "image_b64": b64, "w": img.width, "h": img.height})
        print(f"  rendered page {i + 1}/{count}  {img.width}x{img.height}  dpi={target_dpi}", flush=True)

    pdf.close()
    return pages


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", nargs="?", default=DEFAULT_PDF)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-mkldnn", action="store_true")
    ap.add_argument("--pages", type=int, default=None)
    ap.add_argument("--concurrent", type=int, default=0,
                    help="Reproduce the production warmup: build N engines concurrently "
                         "in a thread pool (matches ocr_runner OCR_WORKERS) and time wall-clock.")
    args = ap.parse_args()

    # MUST be set before numpy / paddle import, same as ocr_runner.py.
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(var, str(args.threads))
    os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    enable_mkldnn = (args.device == "cpu") and not args.no_mkldnn

    _banner("ENVIRONMENT")
    print(f"  python      : {sys.version.split()[0]}")
    print(f"  platform    : {sys.platform}")
    print(f"  pdf         : {pdf_path}")
    print(f"  device      : {args.device}")
    print(f"  cpu_threads : {args.threads}")
    print(f"  enable_mkldnn: {enable_mkldnn}")
    try:
        import paddle  # noqa
        print(f"  paddle      : {paddle.__version__}")
    except Exception as e:
        print(f"  paddle      : <import failed: {e}>")
    try:
        import paddleocr  # noqa
        print(f"  paddleocr   : {getattr(paddleocr, '__version__', '?')}")
    except Exception as e:
        print(f"  paddleocr   : <import failed: {e}>")

    _banner("STAGE 0 — RENDER PDF (pypdfium2)")
    t0 = time.perf_counter()
    pages = render_pdf(pdf_path, args.pages)
    _t(f"rendered {len(pages)} page(s)", t0)

    _banner("STAGE 1 — import paddleocr")
    t0 = time.perf_counter()
    from paddleocr import PaddleOCR
    _t("from paddleocr import PaddleOCR", t0)

    def build_engine():
        return PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=args.device,
            enable_mkldnn=enable_mkldnn,
            cpu_threads=args.threads,
            return_word_box=True,
        )

    # ── Optional: reproduce the production concurrent warmup (the 99s line) ──
    if args.concurrent:
        import threading
        import numpy as np
        import cv2
        from concurrent.futures import ThreadPoolExecutor

        raw0 = base64.b64decode(pages[0]["image_b64"], validate=True)
        warm_img = cv2.imdecode(np.frombuffer(raw0, np.uint8), cv2.IMREAD_COLOR)

        _banner(f"WARMUP REPRO — build {args.concurrent} engine(s) CONCURRENTLY (matches ocr_runner)")

        def _warm_one(idx: int):
            name = threading.current_thread().name
            te = time.perf_counter()
            eng = build_engine()
            ctor = (time.perf_counter() - te) * 1000
            tp = time.perf_counter()
            eng.predict(warm_img)
            pred = (time.perf_counter() - tp) * 1000
            print(f"  [thread {name}] ctor={ctor:.0f}ms  first_predict={pred:.0f}ms  total={ctor + pred:.0f}ms", flush=True)
            return ctor + pred

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
            list(ex.map(_warm_one, range(args.concurrent)))
        wall = _t(f"ALL {args.concurrent} engine(s) ready (wall-clock)", t0)
        print(f"\n  >> This is the production 'N thread engine(s) ready in {wall:.0f}ms' number.")
        print("  >> If this >> single-engine total below, the bottleneck is CONCURRENT init")
        print("     (MKLDNN/oneDNN JIT contention or OMP/MKL thread oversubscription).")
        return 0

    _banner("STAGE 2 — PaddleOCR(...) constructor")
    t0 = time.perf_counter()
    engine = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device=args.device,
        enable_mkldnn=enable_mkldnn,
        cpu_threads=args.threads,
        return_word_box=True,
    )
    ctor_ms = _t("constructor returned", t0)

    import numpy as np
    import cv2

    def decode(p: dict):
        raw = base64.b64decode(p["image_b64"], validate=True)
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)

    _banner("STAGE 3 — first .predict()  (lazy load + MKLDNN JIT)")
    img0 = decode(pages[0])
    t0 = time.perf_counter()
    res0 = engine.predict(img0)
    first_ms = _t(f"first predict (page 1) -> {sum(len(r['rec_texts']) for r in res0)} words", t0)

    per_page_ms = []
    _banner("STAGE 4 — warm .predict() per page (steady-state)")
    for p in pages:
        img = decode(p)
        t0 = time.perf_counter()
        res = engine.predict(img)
        ms = _t(f"page {p['page_number']} -> {sum(len(r['rec_texts']) for r in res)} words", t0)
        per_page_ms.append(ms)

    _banner("SUMMARY")
    print(f"  import paddleocr      : {'?':>9}")
    print(f"  constructor           : {ctor_ms:9.0f} ms")
    print(f"  first predict (cold)  : {first_ms:9.0f} ms   <- includes MKLDNN JIT + lazy model load")
    print(f"  warm predict (avg)    : {sum(per_page_ms) / len(per_page_ms):9.0f} ms")
    print(f"  warm predict (min)    : {min(per_page_ms):9.0f} ms")
    print()
    print("  INTERPRETATION:")
    print("    - If 'constructor' dominates  -> model file load / source-check / disk I/O.")
    print("    - If 'first predict' dominates -> MKLDNN graph JIT compile (try --no-mkldnn).")
    print("    - 'warm predict' is your true per-page cost; compare to the 3-4s baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
