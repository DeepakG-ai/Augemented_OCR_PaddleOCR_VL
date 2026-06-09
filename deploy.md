# Single-Pod RunPod Deployment Guide

**Updated: 2026-06-08**

This guide deploys the **entire** Augmented OCR system into **one RunPod GPU Pod** on an
**RTX 5090 (32 GB VRAM, Blackwell sm_120, CUDA 13, 16 vCPUs)**. Everything — Postgres, the object
store, the FastAPI app, all four pipeline workers, and `llama-server` — runs inside that single pod
and talks over `localhost`. The Qwen3-VL model is **never publicly exposed**.

> **Why this replaces the old split design.** The previous version of this doc ran the app stack on
> AWS and only the LLM on RunPod, joined by a private tunnel / authenticated gateway. That design is
> still valid and is the right answer once you outgrow one box (see
> [§13 Scale-up / alternative architectures](#13-scale-up--alternative-architectures)). But for the
> current single-customer / first-production push, one self-contained pod is simpler, cheaper, and
> removes the entire tunnel/gateway/AWS-security-group surface. **This single-pod path is now the
> primary, supported path.**

## Assumptions

- One RunPod **Pod** (not Serverless), RTX 5090 ×1, Secure Cloud.
- Model stays GGUF + mmproj via `llama.cpp`:
  - `Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf` (~6 GB)
  - `mmproj-F16.gguf` (~1.4 GB)
- A RunPod **network volume mounted at `/workspace`** holds model weights, the Postgres data dir,
  and the object store. The volume is **not a backup** — daily off-pod backups are mandatory (§9).
- OCR runs on **CPU** (`OCR_DEVICE=cpu`). The GPU is reserved for `llama-server`. (Rationale: §1,
  and the paddle Blackwell note in §7.)
- `llama-server` starts at `--parallel 4`; the app's `LLM_PAGE_BATCH_SIZE` **must match it** (§7).
- Authoring is on Windows (PowerShell). The **runtime pod is Linux** — all in-pod commands below are
  bash.

---

## Table of contents

1. [Architecture — one all-in-one pod](#1-architecture--one-all-in-one-pod)
2. [Why a Pod, not Serverless](#2-why-a-pod-not-serverless)
3. [Create the RunPod pod](#3-create-the-runpod-pod)
4. [Network volume layout](#4-network-volume-layout)
5. [Get the model files onto the volume](#5-get-the-model-files-onto-the-volume)
6. [Build and run the stack in one pod (start.sh)](#6-build-and-run-the-stack-in-one-pod-startsh)
7. [llama-server on RTX 5090 / Blackwell](#7-llama-server-on-rtx-5090--blackwell)
8. [Production .env for the single pod](#8-production-env-for-the-single-pod)
9. [Persistence and backups](#9-persistence-and-backups)
10. [Security for the all-in-one pod](#10-security-for-the-all-in-one-pod)
11. [Worker sizing for 16 vCPUs](#11-worker-sizing-for-16-vcpus)
12. [Batch rollout plan](#12-batch-rollout-plan)
13. [Scale-up / alternative architectures](#13-scale-up--alternative-architectures)
14. [Must fix / verify before deploy](#14-must-fix--verify-before-deploy)
15. [Pre-deploy tests and first-24h monitoring](#15-pre-deploy-tests-and-first-24h-monitoring)
16. [Appendix: post-deploy performance tuning](#16-appendix-post-deploy-performance-tuning)
17. [RunPod reference links](#17-runpod-reference-links)

---

## 1. Architecture — one all-in-one pod

Everything is inside one Linux container on the GPU pod. Only the FastAPI app port is published to
the internet, through RunPod's HTTPS proxy.

```text
Internet
  └─ HTTPS  https://<pod-id>-8000.proxy.runpod.net   (RunPod-provided TLS)
        │
        ▼
  ┌──────────────────────── RunPod Pod (RTX 5090, 32 GB, 16 vCPU) ────────────────────────┐
  │                                                                                        │
  │   FastAPI app  (uvicorn :8000)  ── serves frontend + REST + SSE                        │
  │        │  localhost only                                                               │
  │        ├──▶ Postgres        127.0.0.1:5432   (PGDATA on /workspace)                    │
  │        ├──▶ Object store     MinIO 127.0.0.1:9000  OR  local-disk fallback             │
  │        └──▶ enqueues jobs in the `jobs` table                                          │
  │                                                                                        │
  │   Workers (poll the jobs table, all on localhost):                                     │
  │        normalize ×2 ─▶ ocr ×2 (CPU paddle) ─┐                                          │
  │                        llm ×1 ──────────────┤─▶ postprocess ×1                         │
  │                          │                                                             │
  │                          └──▶ llama-server  127.0.0.1:8056   (GPU, Qwen3-VL GGUF)      │
  │                                                                                        │
  │   /workspace (network volume, survives stop/restart):                                  │
  │        models/   pgdata/   object-store/   backups/                                    │
  └────────────────────────────────────────────────────────────────────────────────────────┘
```

The pipeline itself is unchanged — `normalize → (ocr ‖ llm) → postprocess`, jobs claimed from the
`jobs` table, SSE progress on `GET /jobs/{job_id}/stream`. See `CLAUDE.md` for the full pipeline
description. The only thing that changes here is **where** the processes live: all in one box, all
on `localhost`.

---

## 2. Why a Pod, not Serverless

| RunPod option | Use here? | Reason |
| --- | --- | --- |
| **GPU Pod** | **Yes** | Full control of one GPU container: build `llama.cpp`, place GGUF + mmproj, run Postgres + app + workers, manage ports and logs. |
| Serverless vLLM endpoint | Future only | Good for autoscaling, but **not** a drop-in for the current GGUF + mmproj `llama.cpp` model, and the app calls a plain `LLM_URL` with no RunPod auth header. |
| Public Endpoints | No | Pre-baked models, not your tuned Qwen3-VL extraction service. |

The app is built around an OpenAI-compatible `POST /v1/chat/completions` to `llama-server`. A Pod
keeps the exact model format, prompt behavior, and bbox/JSON tuning you validated locally.
Serverless/vLLM stays a [future path](#13-scale-up--alternative-architectures) and must be
re-validated against real purchase orders before production.

> **Docker Compose is NOT available inside a RunPod Pod.** A Pod is a single container runtime. The
> repo's `docker-compose.yml` is for local/dev. Inside the pod, use a **start script** (or
> supervisord) to launch the processes — see §6.

---

## 3. Create the RunPod pod

- **Cloud type:** Secure Cloud (customer documents may contain sensitive business data).
- **GPU:** RTX 5090 ×1.
- **Base image:** a CUDA-13 / Blackwell-capable image. Two routes:
  - **Custom image (recommended):** build an image that already contains Python deps, a
    Blackwell-capable `llama.cpp` build, Postgres, and (optionally) MinIO. Most reproducible.
  - **Stock CUDA/PyTorch image + build on first boot:** start from a recent official RunPod
    CUDA/PyTorch image and run a one-time build inside `start.sh`. Simpler to start, slower cold
    boot, and you must pin the toolchain (see the Blackwell build note in §7).
- **Container disk:** size for OS + packages + the `llama.cpp` build cache (e.g. 30–50 GB). This is
  **ephemeral** — it is wiped on stop/restart/edit.
- **Network volume → mount at `/workspace`:** sized for model weights + Postgres data + object store
  + local backups. Start ~100 GB and grow as document volume grows. **This is the only persistent
  storage.**
- **Exposed ports — publish ONLY the app:**

| Port | Exposed publicly? | What |
| --- | --- | --- |
| `8000/http` | **Yes** (RunPod HTTPS proxy) | FastAPI app — the only public surface |
| `5432` | **No** | Postgres — localhost only |
| `8056` | **No** | `llama-server` — localhost only |
| `9000` / `9001` | **No** | MinIO API / console — localhost only (if MinIO is used) |
| `5000` | **No** | MLflow — not run in production (`MLFLOW_ENABLED=false`) |
| `8888/http` | Dev only | Jupyter, if you want it during bring-up; remove for production |

The app is reached at `https://<pod-id>-8000.proxy.runpod.net`. RunPod terminates TLS at the proxy,
so you do **not** need your own Nginx/Caddy for TLS. **Do not expose 5432 / 8056 / 9000 / 9001 /
5000.**

Use RunPod **secrets** for any token you need at template scope (e.g. a Hugging Face token to pull
weights), not plain template values:

```text
HF_TOKEN={{ RUNPOD_SECRET_huggingface_token }}
```

Application secrets (`SECRET_KEY`, `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, MinIO keys) live in the
pod's `/workspace/.env` (§8), created on the pod only.

---

## 4. Network volume layout

Everything that must survive a pod stop/restart lives under `/workspace`. Everything outside
`/workspace` (the container disk) is wiped on restart or when you edit the pod.

```text
/workspace/
├── app/                         # the application code (git checkout or unpacked release)
├── models/qwen3vl/
│   ├── Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf
│   └── mmproj-F16.gguf
├── pgdata/                      # PGDATA — Postgres data directory
├── object-store/               # app's LOCAL_OBJECT_STORE_DIR (or MinIO's data dir)
├── backups/                    # local staging for pg_dump + object-store snapshots (synced off-pod)
├── .env                        # production env (NOT committed, NOT in any zip)
├── start.sh                    # process launcher (§6)
└── logs/                       # pipeline + app logs
```

What survives vs. not:

- **Survives** stop/restart/edit: anything under `/workspace` (the network volume).
- **Does NOT survive:** the container disk — OS packages, anything `pip install`ed into the system
  Python, the `llama.cpp` build, `/tmp`. Either bake these into a custom image or rebuild them in
  `start.sh` on every boot.

> A network volume is durable while the volume exists, but it is **a single copy in one region**. It
> is **not a backup** of your database or documents. §9 is mandatory.

Provisioning order: create the network volume first, attach it to the pod at `/workspace`, then on
first boot create the subdirectories and initialize Postgres into `/workspace/pgdata`.

---

## 5. Get the model files onto the volume

Place the GGUF + mmproj under `/workspace/models/qwen3vl/`:

```bash
mkdir -p /workspace/models/qwen3vl
```

Pick whichever transfer fits your source:

```bash
# A) Hugging Face (set HF_TOKEN via RunPod secret if the repo is gated)
pip install -U "huggingface_hub[cli]"
hf download <org>/<repo> Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf \
  --local-dir /workspace/models/qwen3vl
hf download <org>/<repo> mmproj-F16.gguf \
  --local-dir /workspace/models/qwen3vl

# B) From your machine over SSH/SCP (authoring on Windows PowerShell):
#    scp .\Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf root@<pod-ssh-host>:/workspace/models/qwen3vl/

# C) From your own S3-compatible bucket (R2/B2/S3) if you archive model artifacts there.
```

Verify:

```bash
ls -lh /workspace/models/qwen3vl/
# expect ~6 GB GGUF + ~1.4 GB mmproj
```

Customer PDFs are stored by the app's object store on `/workspace`; you do not need to pre-load any
document data.

---

## 6. Build and run the stack in one pod (start.sh)

No Docker Compose inside the pod. Use a **start script** that brings the processes up in the right
order, or a process manager (`supervisord`) if you want automatic per-process restart. The script
below is the minimal, readable version; wrap each long-running process in `supervisord` later for
auto-restart in production.

**Startup order (important):**

1. **Postgres** with `PGDATA=/workspace/pgdata`. `initdb` only on first boot.
2. **Wait for Postgres ready** (`pg_isready`).
3. **App migrations** run themselves — `db.init()` creates/migrates the schema idempotently on app
   startup, so there is no separate migration command. (Workers also call `create_pool`; they assume
   the schema exists, so start the API/app first or let the API boot before workers do real work.)
4. **llama-server** bound to `127.0.0.1:8056`, `--parallel 4` (§7).
5. **The four workers**: `normalize`, `ocr`, `llm`, `postprocess` (exact commands from `CLAUDE.md`).
6. **uvicorn API** on `0.0.0.0:8000` (so the RunPod proxy can reach it; still only the proxy is
   public).

Sample `/workspace/start.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

export PGDATA=/workspace/pgdata
export PATH="/usr/lib/postgresql/16/bin:$PATH"   # adjust to the installed PG version
APP=/workspace/app
ENV_FILE=/workspace/.env

# Load env so child processes see it (config.py also load_dotenv()s, but be explicit)
set -a; source "$ENV_FILE"; set +a

# 1) Postgres — init once, then start
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "Initializing Postgres cluster at $PGDATA ..."
  initdb -D "$PGDATA" -U postgres
fi
pg_ctl -D "$PGDATA" -l /workspace/logs/postgres.log \
  -o "-c listen_addresses=127.0.0.1 -p 5432" start

# 2) Wait for ready
until pg_isready -h 127.0.0.1 -p 5432 -U postgres; do sleep 1; done

# Create the augocr role/db on first boot (idempotent-ish; ignore "already exists")
psql -h 127.0.0.1 -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='augocr'" | grep -q 1 \
  || psql -h 127.0.0.1 -U postgres -c "CREATE ROLE augocr LOGIN PASSWORD '${POSTGRES_PASSWORD}';"
psql -h 127.0.0.1 -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='augocr'" | grep -q 1 \
  || psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE augocr OWNER augocr;"

cd "$APP"

# 3) Migrations: handled automatically by db.init() when the app/workers start. No separate step.

# 4) llama-server (GPU). See §7 for the full flag set.
/workspace/llama.cpp/build/bin/llama-server \
  --model  /workspace/models/qwen3vl/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf \
  --mmproj /workspace/models/qwen3vl/mmproj-F16.gguf \
  --host 127.0.0.1 --port 8056 \
  --n-gpu-layers 999 --ctx-size 4096 --parallel 4 --cont-batching \
  >/workspace/logs/llama.log 2>&1 &

# Wait for llama-server to answer before starting the llm worker
until curl -sf http://127.0.0.1:8056/v1/models >/dev/null; do sleep 2; done

# 5) Workers (exact commands from CLAUDE.md). 16 vCPUs → see §11 for replica counts.
python -m backend.worker --stage normalize   --name normalize-1 >/workspace/logs/normalize-1.log 2>&1 &
python -m backend.worker --stage normalize   --name normalize-2 >/workspace/logs/normalize-2.log 2>&1 &
python -m backend.worker --stage ocr         --name ocr-1       >/workspace/logs/ocr-1.log       2>&1 &
python -m backend.worker --stage ocr         --name ocr-2       >/workspace/logs/ocr-2.log       2>&1 &
python -m backend.worker --stage llm         --name llm-1       >/workspace/logs/llm-1.log       2>&1 &
python -m backend.worker --stage postprocess --name post-1      >/workspace/logs/post-1.log      2>&1 &

# 6) API last (runs db.init() on startup; foreground so the pod stays alive)
exec python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Set the RunPod template's start command to `bash /workspace/start.sh`.

> **Production hardening:** run the same processes under **supervisord** so a crashed worker or
> `llama-server` restarts automatically (the workers already self-recover stuck *jobs* via
> `recover_stale_jobs`, but a dead *process* needs a process manager to come back). The script above
> backgrounds everything with `&`; that is fine for bring-up but does not restart on crash.

Notes:

- The app expects the venv/interpreter described in `CLAUDE.md`. If you build a venv at
  `/workspace/.venv`, call `/workspace/.venv/bin/python` instead of bare `python`. If deps are baked
  into the image's system Python, bare `python` is fine. Either way the deps in
  `backend/requirements.txt` (fastapi, uvicorn, asyncpg, httpx, pypdfium2, Pillow, **paddleocr +
  paddlepaddle** [CPU], rapidfuzz, json-repair, minio, …) must be installed.
- `paddlepaddle==3.2.2` (CPU) is in `requirements.txt`; do **not** swap in `paddlepaddle-gpu` here —
  see §7.

---

## 7. llama-server on RTX 5090 / Blackwell

Recommended first production command (model + mmproj under `/workspace`, bound to localhost):

```bash
llama-server \
  --model  /workspace/models/qwen3vl/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf \
  --mmproj /workspace/models/qwen3vl/mmproj-F16.gguf \
  --host 127.0.0.1 \
  --port 8056 \
  --n-gpu-layers 999 \
  --ctx-size 4096 \
  --parallel 4 \
  --cont-batching
```

Flag notes:

- `--host 127.0.0.1` — **localhost only**. The app and the `llm` worker are in the same pod; nothing
  else may reach it. Never bind `0.0.0.0` here.
- `--parallel 4` — concurrent request slots; this is the value `LLM_PAGE_BATCH_SIZE` must match
  (below). Test `--parallel 5` only after batch-4 is stable and `nvidia-smi` shows headroom.
- `--ctx-size 4096` — per the project's VRAM tuning notes; do not raise to 8192 on this GPU class
  without re-checking VRAM (see budget below).
- `--cont-batching` — continuous batching, needed to actually use the parallel slots.
- **Image-token flags:** if your `llama.cpp` build supports them, keep the project's validated image
  token budget (e.g. `--image-max-tokens 768 --image-min-tokens 192`). These names vary by build —
  only pass flags your binary actually accepts (`llama-server --help`). Do not invent flags.
- Keep any other locally validated flags your specific build requires (e.g. `--jinja` for the chat
  template, `--flash-attn auto`). Per the project's encoding-bottleneck findings, prefer
  `--flash-attn auto` over forcing it on.

### Cross-reference: match `LLM_PAGE_BATCH_SIZE` to `--parallel`

**This is the single most important throughput setting on one box.** The app defaults
`LLM_PAGE_BATCH_SIZE=1` (see `backend/config.py`), which **serializes** pages 2..N one at a time and
leaves the GPU's parallel slots idle. With `--parallel 4` you **must** set `LLM_PAGE_BATCH_SIZE=4` in
the pod `.env` (§8) so the `llm` worker fires 4 page requests concurrently and fills the GPU. If you
later move to `--parallel 5`, set `LLM_PAGE_BATCH_SIZE=5` to match. Roll this up gradually (§12).

### Blackwell (sm_120) / CUDA-13 build note — verify before you rely on it

This is the **highest-uncertainty** item in this doc. As of 2026-06, public reports are clear that
Blackwell sm_120 is a moving target and prebuilt CUDA binaries do **not** reliably cover it:

- NVIDIA's own migration guidance and community benchmarks recommend building `llama.cpp` for
  Blackwell (sm_120) and have reported **MMQ crashes / build failures specifically with CUDA 13.1**,
  and MXFP4 PTX instructions that **fail to compile for sm_120**. Several sources recommend the
  **CUDA 12.8** toolchain as the currently-most-reliable path for Blackwell, even on a CUDA-13 host
  driver.
- Therefore: **do not assume a stock prebuilt `llama.cpp` CUDA binary will run on the RTX 5090.**
  Plan to **build from source** targeting sm_120:

  ```bash
  git clone https://github.com/ggml-org/llama.cpp /workspace/llama.cpp
  cmake -S /workspace/llama.cpp -B /workspace/llama.cpp/build \
        -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120
  cmake --build /workspace/llama.cpp/build --config Release -j
  ```

  `-DCMAKE_CUDA_ARCHITECTURES=120` is the sm_120 (Blackwell) target. **Action item before deploy:**
  on the actual pod, confirm the build completes and `llama-server` loads the GGUF + mmproj on the
  GPU. If a CUDA 13 toolkit hits the MMQ/MXFP4 issues above, fall back to a CUDA 12.8 toolchain to
  build (the GPU still runs under the CUDA-13 driver). **Treat exact toolkit version and flags as
  TO-BE-VERIFIED on the pod — do not copy a version number from this doc as gospel.**

### VRAM budget (32 GB, --parallel 4–5)

Concrete, conservative estimate (assumptions stated):

| Item | Approx VRAM | Assumption |
| --- | --- | --- |
| Model weights (Q4_K_XL, fully offloaded `-ngl 999`) | ~6 GB | quant file size ≈ resident weights |
| mmproj (F16 vision projector) | ~1.4 GB | loaded once, shared |
| KV cache @ ctx 4096 × 4 slots | ~2–4 GB | 8B model, FP16 KV; grows ~linearly with `--parallel` and ctx |
| Vision/image encode activations (per concurrent page) | ~1–3 GB | a page image ≈ a few hundred to ~768 tokens; transient |
| CUDA context + fragmentation headroom | ~1–2 GB | driver/runtime overhead |
| **Total @ --parallel 4** | **~12–16 GB** | comfortably inside 32 GB |

That leaves **roughly half the 32 GB free**, which is why `--parallel 5` is a reasonable next step
and why `--ctx-size 4096` is not the binding constraint here. KV cache and image activations scale
with `--parallel`, so re-measure with `nvidia-smi -l 2` at each step (§12) rather than assuming
linear headroom forever.

---

## 8. Production .env for the single pod

Create `/workspace/.env` **on the pod only**. Variable names below are the real ones from
`backend/config.py`. Never commit it, never bake it into an image or zip.

```env
# --- Postgres (localhost, inside the pod) ---
POSTGRES_PASSWORD=<fresh-strong-password>
DATABASE_URL=postgresql://augocr:<fresh-strong-password>@127.0.0.1:5432/augocr

# --- LLM (localhost llama-server) ---
LLM_URL=http://127.0.0.1:8056/v1/chat/completions
LLM_MODEL=qwen3vl
LLM_PAGE_BATCH_SIZE=4            # MUST equal llama-server --parallel (§7). Default is 1 — too slow.
# LLM_TIMEOUT=300               # default 300s; raise only if large PDFs time out

# --- OCR: stay on CPU on Blackwell (§7) ---
OCR_DEVICE=cpu
# OCR_WORKERS=3                 # default 3; the 3 PaddleOCR threads + warmup are already tuned

# --- Object store: choose ONE ---
# Option A — MinIO on localhost (run minio in start.sh, data dir on the volume):
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=<fresh-minio-access-key>
MINIO_SECRET_KEY=<fresh-minio-secret-key>
MINIO_SECURE=false
# Option B — skip MinIO entirely; the app falls back to local disk:
# LOCAL_OBJECT_STORE_DIR=/workspace/object-store

# --- App / auth ---
SECRET_KEY=<64-hex-or-long-random-secret>
ADMIN_EMAIL=<your-admin-email>
ADMIN_PASSWORD=<fresh-strong-admin-password>   # must NOT start with "CHANGE_ME"

# --- Public surface ---
CORS_ALLOW_ORIGINS=https://<pod-id>-8000.proxy.runpod.net   # or your custom domain, comma-separated
RATE_LIMIT_PER_MINUTE=30
MAX_UPLOAD_MB=50
MAX_DOCUMENT_PAGES=100

# --- Observability OFF in production ---
MLFLOW_ENABLED=false
```

Secret-rotation rules (non-negotiable):

- Treat every dev/local credential as **burned**. Generate fresh values for `SECRET_KEY`,
  `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, and the MinIO keys before this pod serves real traffic.
- The `.env` is created on the pod and stays there. It must **not** appear in git, in `aug-ocr.zip`,
  in any image layer, or in any backup that leaves the pod.
- `CORS_ALLOW_ORIGINS` defaults (in `config.py`) are a list of `localhost` dev origins. In
  production set it to **exactly** the RunPod proxy URL (or your custom domain) and nothing else.

---

## 9. Persistence and backups

The `/workspace` network volume is durable but is **one copy in one region**. A deleted/expired
volume, a region incident, or a bad migration loses everything. Back up **off the pod**, daily.

**What to back up:** the Postgres database (logical dump) and the object store (rendered images,
artifacts, stored PDFs). The model files are re-downloadable, so they are optional.

**Daily `pg_dump` + object-store sync to external S3-compatible storage** (Cloudflare R2 / Backblaze
B2 / AWS S3). Stage in `/workspace/backups`, then push off-pod with `aws s3` / `rclone` /
`mc mirror`:

```bash
#!/usr/bin/env bash
# /workspace/backup.sh  — run daily via cron
set -euo pipefail
set -a; source /workspace/.env; set +a
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST="s3://augocr-backups"            # R2/B2/S3 bucket (configure the endpoint in your S3 client)

# 1) Postgres logical dump (custom format, compressed)
pg_dump "$DATABASE_URL" -Fc -f "/workspace/backups/db-$STAMP.dump"

# 2) Push DB dump off-pod
aws s3 cp "/workspace/backups/db-$STAMP.dump" "$DEST/db/db-$STAMP.dump"

# 3) Sync the object store off-pod (use your MinIO data dir OR the local fallback dir)
aws s3 sync /workspace/object-store "$DEST/object-store/"

# 4) Prune local staging older than 7 days
find /workspace/backups -name 'db-*.dump' -mtime +7 -delete
```

```bash
# Schedule it (in start.sh or the image): write a cron entry, then ensure cron is running
echo "30 3 * * * root /usr/bin/env bash /workspace/backup.sh >>/workspace/logs/backup.log 2>&1" \
  > /etc/cron.d/augocr-backup
```

**Restore drill (do this once before real customer data lands):** spin a scratch DB, run
`pg_restore -d <scratch> db-<stamp>.dump`, confirm row counts and that the app boots against it.

**Industry-standard alternative (use when you scale up):** decouple state from the GPU pod —
**managed Postgres** (Neon / Supabase / AWS RDS) + **S3 object storage**, with a **stateless** GPU
pod that holds only the model and runtime. Point `DATABASE_URL` at the managed Postgres and the
object-store config at S3. Then the pod can be recreated freely and backups/HA are the provider's
job. This is the natural next step and pairs with the split architecture in §13.

---

## 10. Security for the all-in-one pod

The **app is now the public surface** (RunPod's proxy provides TLS). Lock it down; keep everything
else on localhost.

- **Only port 8000 is exposed.** Postgres (5432), `llama-server` (8056), and MinIO (9000/9001) are
  **localhost-only** and must not be published. There is no public LLM endpoint to protect — the old
  tunnel/gateway/bearer-token concern is gone because the LLM never leaves the box.
- **TLS:** provided by the RunPod HTTPS proxy. Do not serve the app over plain HTTP to users.
- **CORS:** `CORS_ALLOW_ORIGINS` = the proxy URL / your domain only (§8). Admin UI is hidden
  client-side, so the server re-checks admin rights on every admin route — keep it that way.
- **Rate limits:** keep `RATE_LIMIT_PER_MINUTE` and the upload-size / page caps. Especially watch
  `/auth/login`, `/ingest/ui`, `/v1/extract`, and the SSE streams.
- **Secrets:** rotated and pod-only (§8). Bootstrap admin password must not start with `CHANGE_ME`;
  change it after first login if it was ever shared.
- **MLflow OFF:** `MLFLOW_ENABLED=false`. MLflow traces can contain prompt/response/extracted-value
  data; do not run it publicly. If you ever enable it, keep it localhost + behind auth.
- **Logs:** do not log raw extracted values or base64 page images. Rotate logs. Keep enough to debug
  failed jobs.
- **Auth model (unchanged):** single 8-hour access JWT in `localStorage`, no refresh token. Fine for
  an internal/admin OCR tool over HTTPS. If you add refresh tokens later, use HttpOnly+Secure+
  SameSite cookies, not `localStorage`.

---

## 11. Worker sizing for 16 vCPUs

One GPU, 16 vCPUs. The GPU is the scarce resource for `llm`; **CPU is the scarce resource for OCR**
(it runs on CPU here) and for page rendering. Suggested first sizing:

| Stage | Replicas | Reason |
| --- | --- | --- |
| `normalize` | 2 | renders pages, classifies digital/scanned, detects vendor — CPU + I/O heavy; keep PDFs flowing. |
| `ocr` | 2 | **CPU PaddleOCR is the heaviest CPU consumer.** Each worker uses the 3-thread paddle pool (`OCR_WORKERS=3`, with warmup). 2 × 3 ≈ 6 paddle threads — leaves headroom for normalize + Postgres + the app on 16 vCPUs. |
| `llm` | 1 | the single worker already issues `LLM_PAGE_BATCH_SIZE` concurrent page requests, which is what fills `--parallel` on the GPU. More `llm` workers just contend for one `llama-server`. |
| `postprocess` | 1 | usually fast (field-location mapping, spatial memory, ERP map, quota release). |

Balance rule: if the GPU is idle while `normalize`/`ocr` queues grow, add CPU-stage workers (or give
ocr more). If the GPU is saturated and the `llm` queue grows, CPU workers are not the problem —
adjust batch/`--parallel` (§12). Don't oversubscribe: 2×ocr (≈6 paddle threads) + 2×normalize +
app + Postgres should sit comfortably under 16 vCPUs; watch CPU saturation before adding more.

---

## 12. Batch rollout plan

Do not jump straight to batch 4 on real traffic.

1. Start `llama-server` with `--parallel 4`.
2. Set `LLM_PAGE_BATCH_SIZE=1`, run one known multi-page PDF, confirm extraction accuracy is correct.
3. Set `LLM_PAGE_BATCH_SIZE=2`, run several known PDFs, watch latency, VRAM, failures.
4. Set `LLM_PAGE_BATCH_SIZE=4` (now matching `--parallel 4`). Keep it **only** if there's no VRAM
   OOM, no timeout spike, and no accuracy regression.
5. Only then consider `--parallel 5` + `LLM_PAGE_BATCH_SIZE=5`.

Monitor the GPU:

```bash
nvidia-smi -l 2          # VRAM, utilization, power/thermals
curl -sf http://127.0.0.1:8056/v1/models   # llama-server alive
```

Watch the job queue:

```sql
SELECT job_type, status, count(*) FROM jobs GROUP BY 1,2 ORDER BY 1,2;
```

| Symptom | Likely cause | First action |
| --- | --- | --- |
| VRAM OOM | ctx/parallel too high for the page mix | drop to batch 2, or lower `--parallel`; recheck `nvidia-smi` |
| Batch 4 slower than batch 2 | GPU saturated, queueing inside `llama-server` | stay at batch 2 |
| GPU idle while jobs wait | CPU stages (normalize/ocr) bottlenecked | add normalize/ocr workers (§11) |
| `llm` 5xx spikes | `llama-server` unstable / crashed | check `/workspace/logs/llama.log`, restart it (supervisord), reduce `--parallel` |
| OCR job fails whole doc | one scanned page failed paddle (known rough edge) | inspect ocr worker log; OCR is currently all-or-nothing |

---

## 13. Scale-up / alternative architectures

These are **valid future paths**, kept brief. Use them when one box stops being enough.

**Split app/LLM (the old design).** Move the app stack (Postgres, object store, API, workers) to a
general server (e.g. AWS) and keep only `llama-server` on the RunPod GPU, joined by a **private
tunnel** (Tailscale/WireGuard) or an **authenticated gateway** (Nginx/Caddy + bearer/mTLS). This
re-introduces the LLM-endpoint protection work (the app currently posts to `LLM_URL` with no auth
header), but lets the app and GPU scale independently. This was the previous primary design and
remains correct at scale.

**Decoupled managed state.** Managed Postgres (Neon/Supabase/RDS) + S3 object storage + a stateless
GPU pod (see §9). Best operational story for HA and backups.

**Serverless / vLLM.** Attractive for bursty traffic and scale-to-zero, but **not** a drop-in:
current model is GGUF + mmproj on `llama.cpp`; the app sends no RunPod `Authorization` header; bbox
and JSON behavior were tuned against `llama-server`. Before moving you must validate a
vLLM-compatible multimodal model on real purchase orders, send `Authorization: Bearer <RUNPOD_API_KEY>`,
confirm the served model name matches `LLM_MODEL`, and raise the HTTP timeout for cold starts. The
OpenAI-compatible URL shape would be
`https://api.runpod.ai/v2/<endpoint-id>/openai/v1/chat/completions`.

---

## 14. Must fix / verify before deploy

| Item | Status | Notes |
| --- | --- | --- |
| **Template rule validation regression** | **RESOLVED — verified in code** | `backend/models.py` no longer applies a strict `[A-Za-z0-9_-]` pattern. `header_fields`/`line_item_fields` go through `validate_field_name_list` (length/reserved/dup/blank only), and `extraction_rules` has its own free-text validator (`_validate_rules`, lines 283–296) that allows normal rules like "Dates in DD/MM/YYYY format" and only rejects blanks/oversized. No action needed. |
| **Stored-XSS escape in review modal** | **In place — verified** | `frontend/review.js:1224` renders `${escapeHtml(fieldLabel)}` into `.rv-reason-field-name`. Keep it; don't regress. |
| **Rotate all secrets** | **TODO before deploy** | Fresh `SECRET_KEY`, `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, MinIO keys, and regenerate any DB API keys. Pod-only `.env`. |
| **Don't ship `aug-ocr.zip` with `.env`** | **TODO** | If you package a release zip, exclude `.env`, `backend/.env`, `__pycache__/`, `.venv/`, `.git/`, `logs/`, `outputs/`, `mlflow.db`, test outputs. The repo currently has an untracked `aug-ocr.zip` at root — do **not** upload it unless regenerated clean. |
| **Blackwell `llama.cpp` build runs on the pod** | **VERIFY on pod** | Confirm `llama-server` (built with `-DCMAKE_CUDA_ARCHITECTURES=120`) loads the GGUF + mmproj on the GPU. Be ready to fall back to a CUDA 12.8 build toolchain if CUDA 13.1 hits MMQ/MXFP4 build issues (§7). |
| **`OCR_DEVICE=cpu`** | **Keep** | Official paddlepaddle-gpu wheels don't ship sm_120; only unofficial community builds exist. CPU OCR avoids engine-init failure. Revisit only after a verified paddle Blackwell build. |
| **`LLM_PAGE_BATCH_SIZE` matches `--parallel`** | **TODO in `.env`** | Default is 1 (serializes pages). Set to 4 to match `--parallel 4` (§7). |

---

## 15. Pre-deploy tests and first-24h monitoring

Run the test suite on your authoring machine (Windows / PowerShell) with MLflow disabled. Tests mock
DB/store/LLM — no live services needed.

```powershell
$env:MLFLOW_ENABLED = "false"
.venv\Scripts\python.exe -m pytest tests\ -q
# Or the targeted high-value set:
.venv\Scripts\python.exe -m pytest tests\test_auth.py tests\test_validation_models.py `
  tests\test_review_api.py tests\test_reliability_hardening.py `
  tests\test_mapping_api_validation.py tests\test_config_env.py -q
```

Then a real end-to-end pipeline test **on the pod**:

1. `start.sh` is up; `curl http://127.0.0.1:8056/v1/models` returns the model.
2. Open `https://<pod-id>-8000.proxy.runpod.net`, log in as admin.
3. Create a client, a vendor, and a template.
4. Upload a known PDF; confirm the extraction result.
5. Open the review page; draw a box to fix a field; save the correction.
6. Confirm spatial memory / gold correction round-trips (re-upload a similar doc).
7. Confirm a second client cannot see the first client's vendor/extraction.
8. Confirm API-key extraction via `/v1/extract`.
9. Repeat the upload at `LLM_PAGE_BATCH_SIZE=1`, then `2`, then `4` (§12).

**First-24h monitoring** (no `docker compose` here — read the start.sh logs + DB + GPU directly):

```bash
# Processes alive?
ps aux | grep -E 'uvicorn|backend.worker|llama-server|postgres' | grep -v grep

# Tail the per-process logs written by start.sh
tail -f /workspace/logs/llama.log /workspace/logs/llm-1.log /workspace/logs/ocr-1.log

# GPU
nvidia-smi
curl -sf http://127.0.0.1:8056/v1/models

# Queue + DB connections
psql "$DATABASE_URL" -c "SELECT job_type, status, count(*) FROM jobs GROUP BY 1,2 ORDER BY 1,2;"
psql "$DATABASE_URL" -c "SELECT count(*) FROM pg_stat_activity;"
```

Signals: `llm` queue grows while GPU saturated → lower batch/parallel or add GPU later; `normalize`/
`ocr` queue grows while GPU idle → add CPU workers; `pg_stat_activity` near the connection limit →
the pool is too large for too many worker processes (see appendix).

---

## 16. Appendix: post-deploy performance tuning

Optional throughput tweaks for one box (these are **tuning, not launch blockers**):

1. **`LLM_PAGE_BATCH_SIZE` = `--parallel`** — already covered (§7). The biggest single win; the
   default of 1 wastes the GPU.
2. **DB indexes for the hot job/usage queries.** The schema today has `jobs_active_idx`
   (`extraction_id, job_type` partial) and `llm_usage` indexes on `(doc_id, ts)` and
   `(vendor_id, ts)` (`backend/db.py`). For the worker claim loop and usage reporting under load,
   consider adding:
   - `jobs (job_type, status, priority, created_at)` — matches the `claim_job` ordering.
   - `jobs (extraction_id, created_at DESC)` — job history per extraction.
   - `llm_usage (user_id, ts)` — per-user usage rollups.
   Add as idempotent `CREATE INDEX IF NOT EXISTS` migrations in `_init_db` if profiling shows these
   queries hot.
3. **Raise the asyncpg pool.** `db.create_pool` uses `min_size=2, max_size=10`
   (`backend/db.py`). With the API + ~6 worker processes each holding a pool, raising `max_size`
   toward ~20–30 reduces contention — **but** every process multiplies connections, so size Postgres
   `max_connections` accordingly and watch `pg_stat_activity` (§15). On one box, prefer a modest bump
   plus fewer idle workers over a large pool everywhere.
4. **`get_extraction` loads heavy JSONB on hot paths**, and the **SSE stream re-reads the extraction
   each tick** (~1/s per open stream). With many concurrent review/SSE viewers this is real DB load.
   If it shows up in profiling, narrow those reads (select only the columns SSE needs) rather than
   pulling full `result`/`page_results`/`ocr_data` JSONB every second. Note as a known hot path; not
   a day-one blocker.

---

## 17. RunPod reference links

- RunPod Pods overview: <https://docs.runpod.io/pods/overview>
- RunPod Pod management: <https://docs.runpod.io/pods/manage-pods>
- RunPod connection options: <https://docs.runpod.io/pods/connect-to-a-pod>
- RunPod Pod storage types: <https://docs.runpod.io/pods/storage/types>
- RunPod network volumes: <https://docs.runpod.io/pods/storage/create-network-volumes>
- RunPod global networking: <https://docs.runpod.io/pods/networking>
- RunPod Pod environment variables: <https://docs.runpod.io/pods/templates/environment-variables>
- RunPod secrets: <https://docs.runpod.io/pods/templates/secrets>
- RunPod RTX 5090 GPU page: <https://www.runpod.io/gpu-models/rtx-5090>
- RunPod Serverless overview (future path): <https://docs.runpod.io/serverless/endpoints/overview>
- RunPod vLLM OpenAI compatibility (future path): <https://docs.runpod.io/serverless/vllm/openai-compatibility>
```
