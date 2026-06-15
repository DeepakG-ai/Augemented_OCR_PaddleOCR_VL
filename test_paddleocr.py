#!/usr/bin/env python3
"""
test_paddleocr.py -- Isolated PaddleOCR benchmark / diagnostic.

Reproduces, OUTSIDE the FastAPI app, exactly what backend/ocr_runner.py does so
we can see where the time goes. The production logs show:

    PaddleOCR warmup complete: 2 thread engine(s) ready in ~99000ms

That 99s is *engine initialization*, not per-page inference (which is 3-4s).
This script splits init into its sub-stages so we can pinpoint the time:

    1. import paddleocr           (Python + C++ .so load)
    2. PaddleOCR(...) constructor (model file resolve / load / MKLDNN setup)
    3. first  .predict()          (lazy model load + cuDNN autotune / MKLDNN JIT)
    4. warm  .predict()           (true steady-state per-page cost)

The engine is built ONCE and reused across every PDF/page, exactly like the
production worker — so the cold cost (steps 2-3) is paid once and every page
after that is the warm steady-state number you actually care about.

Usage:
    # Ubuntu / RunPod (defaults to every *.pdf next to this script)
    python3 test_paddleocr.py
    python3 test_paddleocr.py --device gpu
    python3 test_paddleocr.py /path/a.pdf /path/b.pdf --device cpu --threads 4

    # Windows
    python test_paddleocr.py --device cpu

Flags:
    --device cpu|gpu     default: env OCR_DEVICE or cpu
    --threads N          cpu_threads, default 4 (matches OCR_CPU_THREADS)
    --no-mkldnn          disable MKLDNN to test if MKLDNN JIT is the bottleneck
    --pages N            render at most N pages per PDF (default: all)
    --concurrent N       reproduce the production warmup: build N engines in a
                         thread pool concurrently and time wall-clock.
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
from pathlib import Path

# Render settings mirrored from backend/processor.py + pdf_extractor.py
MAX_LONG_SIDE = 1536
MAX_PIXELS = 1536 * 1120
JPEG_QUALITY = 92
DPI_FLOOR = 96
DPI_DEFAULT = 128
_FPDF_LCD_TEXT = 0x02


def _banner(msg: str) -> None:
    print(f"\n{'=' * 72}\n{msg}\n{'=' * 72}", flush=True)


def _t(label: str, t0: float) -> float:
    dt = (time.perf_counter() - t0) * 1000
    print(f"  [{dt:9.0f} ms]  {label}", flush=True)
    return dt


def default_pdfs() -> list[Path]:
    """Every *.pdf sitting next to this script (the uploaded test set)."""
    here = Path(__file__).resolve().parent
    return sorted(here.glob("*.pdf"))


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

    pdf.close()
    return pages


def build_engine(args):
    from paddleocr import PaddleOCR
    enable_mkldnn = (args.device == "cpu") and not args.no_mkldnn
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


def _word_count(res) -> int:
    return sum(len(r["rec_texts"]) for r in res)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*", help="PDF paths (default: every *.pdf next to this script)")
    ap.add_argument("--device", default=os.environ.get("OCR_DEVICE", "cpu"))
    ap.add_argument("--threads", type=int, default=int(os.environ.get("OCR_CPU_THREADS", "4")))
    ap.add_argument("--no-mkldnn", action="store_true")
    ap.add_argument("--pages", type=int, default=None)
    ap.add_argument("--concurrent", type=int, default=0,
                    help="Build N engines concurrently in a thread pool (matches ocr_runner "
                         "OCR_WORKERS warmup) and time wall-clock.")
    args = ap.parse_args()

    # MUST be set before numpy / paddle import, same as ocr_runner.py.
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(var, str(args.threads))
    os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

    pdf_paths = [Path(p) for p in args.pdfs] if args.pdfs else default_pdfs()
    if not pdf_paths:
        print("ERROR: no PDFs given and none found next to this script.", file=sys.stderr)
        return 2
    missing = [p for p in pdf_paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"ERROR: PDF not found: {p}", file=sys.stderr)
        return 2

    enable_mkldnn = (args.device == "cpu") and not args.no_mkldnn

    _banner("ENVIRONMENT")
    print(f"  python       : {sys.version.split()[0]}")
    print(f"  platform     : {sys.platform}")
    print(f"  device       : {args.device}")
    print(f"  cpu_threads  : {args.threads}")
    print(f"  enable_mkldnn: {enable_mkldnn}")
    print(f"  pdfs         : {len(pdf_paths)}")
    for p in pdf_paths:
        print(f"                 - {p.name}  ({p.stat().st_size:,} bytes)")
    try:
        import paddle  # noqa
        print(f"  paddle       : {paddle.__version__}")
    except Exception as e:
        print(f"  paddle       : <import failed: {e}>")
    try:
        import paddleocr  # noqa
        print(f"  paddleocr    : {getattr(paddleocr, '__version__', '?')}")
    except Exception as e:
        print(f"  paddleocr    : <import failed: {e}>")

    # ── STAGE 0 — render every PDF up front ──────────────────────────────────
    _banner("STAGE 0 — RENDER PDFs (pypdfium2)")
    docs: list[dict] = []
    for p in pdf_paths:
        t0 = time.perf_counter()
        pages = render_pdf(p, args.pages)
        ms = (time.perf_counter() - t0) * 1000
        docs.append({"path": p, "pages": pages, "render_ms": ms})
        dims = ", ".join(f"{pg['w']}x{pg['h']}" for pg in pages)
        print(f"  [{ms:9.0f} ms]  {p.name}: {len(pages)} page(s)  [{dims}]", flush=True)

    # ── STAGE 1 — import ─────────────────────────────────────────────────────
    _banner("STAGE 1 — import paddleocr")
    t0 = time.perf_counter()
    from paddleocr import PaddleOCR  # noqa: F401
    import_ms = _t("from paddleocr import PaddleOCR", t0)

    # ── Optional: reproduce the production concurrent warmup (the 99s line) ───
    if args.concurrent:
        import threading
        import numpy as np
        import cv2
        from concurrent.futures import ThreadPoolExecutor

        raw0 = base64.b64decode(docs[0]["pages"][0]["image_b64"], validate=True)
        warm_img = cv2.imdecode(np.frombuffer(raw0, np.uint8), cv2.IMREAD_COLOR)

        _banner(f"WARMUP REPRO — build {args.concurrent} engine(s) CONCURRENTLY (matches ocr_runner)")

        def _warm_one(idx: int):
            name = threading.current_thread().name
            te = time.perf_counter()
            eng = build_engine(args)
            ctor = (time.perf_counter() - te) * 1000
            tp = time.perf_counter()
            eng.predict(warm_img)
            pred = (time.perf_counter() - tp) * 1000
            print(f"  [thread {name}] ctor={ctor:.0f}ms  first_predict={pred:.0f}ms  total={ctor + pred:.0f}ms", flush=True)
            return ctor + pred

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
            list(ex.map(_warm_one, range(args.concurrent)))
        _t(f"ALL {args.concurrent} engine(s) ready (wall-clock)", t0)
        print("\n  >> This is the production 'N thread engine(s) ready in Xms' number.")
        return 0

    # ── STAGE 2 — construct the engine ONCE (reused for every page) ──────────
    _banner("STAGE 2 — PaddleOCR(...) constructor  [once, reused like production]")
    t0 = time.perf_counter()
    engine = build_engine(args)
    ctor_ms = _t("constructor returned", t0)

    import numpy as np
    import cv2

    def decode(p: dict):
        raw = base64.b64decode(p["image_b64"], validate=True)
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)

    # ── STAGE 3+4 — predict every page; the very first call is the cold one ──
    _banner("STAGE 3+4 — .predict() every page (first call = cold; rest = warm)")
    first_ms = None
    for d in docs:
        print(f"\n  --- {d['path'].name} ---", flush=True)
        d["page_ms"] = []
        for p in d["pages"]:
            img = decode(p)
            t0 = time.perf_counter()
            res = engine.predict(img)
            ms = (time.perf_counter() - t0) * 1000
            tag = "  <- COLD (first ever predict: lazy load + autotune/JIT)" if first_ms is None else ""
            if first_ms is None:
                first_ms = ms
            d["page_ms"].append(ms)
            print(f"  [{ms:9.0f} ms]  page {p['page_number']}/{len(d['pages'])} "
                  f"-> {_word_count(res)} words{tag}", flush=True)

    # warm = every page except the single global-first (cold) call
    all_ms = [ms for d in docs for ms in d["page_ms"]]
    warm_ms = all_ms[1:] if len(all_ms) > 1 else all_ms

    # ── REPORT ───────────────────────────────────────────────────────────────
    _banner("REPORT")
    print(f"  device={args.device}  threads={args.threads}  mkldnn={enable_mkldnn}\n")
    print(f"  {'document':<36} {'pages':>5} {'render':>9} {'ocr_total':>10} {'ocr/page':>9}")
    print(f"  {'-'*36} {'-'*5} {'-'*9} {'-'*10} {'-'*9}")
    tot_pages = tot_render = tot_ocr = 0.0
    for d in docs:
        ocr_total = sum(d["page_ms"])
        n = len(d["pages"])
        tot_pages += n
        tot_render += d["render_ms"]
        tot_ocr += ocr_total
        print(f"  {d['path'].name:<36} {n:>5} {d['render_ms']:>8.0f}m {ocr_total:>9.0f}m "
              f"{ocr_total / n:>8.0f}m")
    print(f"  {'-'*36} {'-'*5} {'-'*9} {'-'*10} {'-'*9}")
    print(f"  {'TOTAL':<36} {int(tot_pages):>5} {tot_render:>8.0f}m {tot_ocr:>9.0f}m "
          f"{tot_ocr / tot_pages:>8.0f}m")

    print("\n  ENGINE INITIALIZATION (paid once per worker process):")
    print(f"    import paddleocr        : {import_ms:9.0f} ms")
    print(f"    constructor             : {ctor_ms:9.0f} ms   <- model resolve/load (network volume)")
    print(f"    first predict (COLD)    : {first_ms:9.0f} ms   <- cuDNN autotune / MKLDNN JIT + lazy load")
    print(f"    => cold start total     : {import_ms + ctor_ms + first_ms:9.0f} ms")
    if warm_ms:
        print("\n  STEADY-STATE per-page (this is your real throughput):")
        print(f"    warm predict (avg)      : {sum(warm_ms) / len(warm_ms):9.0f} ms")
        print(f"    warm predict (min)      : {min(warm_ms):9.0f} ms")
        print(f"    warm predict (max)      : {max(warm_ms):9.0f} ms")

    print("\n  INTERPRETATION:")
    print("    - constructor large       -> model file load / disk I/O (network volume) / source-check.")
    print("    - first predict large     -> GPU: cuDNN autotune+context init; CPU: MKLDNN JIT (try --no-mkldnn).")
    print("    - warm predict            -> true per-page cost; compare to the ~3-4s Windows baseline.")
    print("    - cold start is paid ONCE per process; reuse the engine and warm pages are cheap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
