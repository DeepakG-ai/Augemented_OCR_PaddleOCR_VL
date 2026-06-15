# CLAUDE.md

Augmented OCR — invoice/PO extraction pipeline. FastAPI backend + supervisord-managed
workers, running as a single RunPod pod (RTX 5090).

## Architecture

Pipeline (each stage = its own worker process, jobs claimed from a Postgres queue):

    normalize → ocr → llm → postprocess

| Service        | Port | Notes |
|----------------|------|-------|
| api (uvicorn)  | 8000 | `backend.main:app`, static frontend at `/` |
| llama-server   | 8056 | Qwen3.5-9B GGUF (vision), served as model alias `qwen3.5` |
| minio          | 9000 / 9001 | object store for PDFs + page artifacts |
| mlflow         | 5000 | tracing (sqlite backend) |
| Workers        | —    | normalize-1/2, ocr-1, llm-1, postprocess-1 |

## Operate

    bash /workspace/app/start.sh          # sources /workspace/.env, (re)starts supervisord
    supervisorctl -c /workspace/app/supervisord.conf status
    supervisorctl -c /workspace/app/supervisord.conf restart <name>

`start.sh` kills any existing supervisord first (prevents duplicate-process explosions).
Config lives in `/workspace/.env` (not in git); `.env.example` documents the keys.

## Database — EXTERNAL

Single Postgres on AWS (`DATABASE_URL` in `/workspace/.env`). There is **no** local
Postgres and **no** backup anymore — the AWS DB is the only copy.
⚠️ The pod (Europe) and DB (AWS Mumbai) are ~143ms apart → ~9s/doc of pure DB latency.
This is the dominant per-document cost; co-locating the DB is the biggest possible win.

## PaddleOCR — how timing works (important)

- Runs on **CPU** (`paddlepaddle` is the CPU-only wheel; `compiled_with_cuda=False`).
  `OCR_DEVICE=gpu` is silently ignored — the RTX 5090 is used only by llama-server.
- **Engine build is slow ONCE (~20–46s) per worker process.** It is NOT threads, GPU
  probe, or disk I/O — it's Paddle compiling its inference graph. Can't be sped up on CPU.
- **After build, every page is ~1s (always fast)** — the long-lived worker keeps the
  warm engine in RAM. There is no per-page slowness.
- The ~20–46s is paid **at startup** via `warmup_ocr_engines()` for the `ocr` AND
  `normalize` stages (normalize OCRs page 1 for vendor detection). So **no user request
  ever hits a cold start** — it's absorbed during boot. Cost: workers need ~45s after a
  restart before accepting their first job.
- **Single OCR engine** (`OCR_WORKERS=1`, one `ocr` worker). A second concurrent engine
  oversubscribed the CPU during init (that was the old "99s warmup") with no benefit.

Benchmark/diagnostic: `python test_paddleocr.py --device cpu` (uses the PDFs next to it).

## Environment gotchas

- `/workspace` is a **RunPod network volume** (MooseFS/FUSE), ~10× slower than local disk
  and wiped-safe across pod stops. Local disk (`/var/lib/...`, `/root`) is lost on stop.
- Models: PaddleOCR weights cached in `/workspace/paddleocr/pdx_cache`; llama GGUF in
  `/workspace/models/qwen3.5`.
- Deployment branch: `runpod_deploy_v1`.
