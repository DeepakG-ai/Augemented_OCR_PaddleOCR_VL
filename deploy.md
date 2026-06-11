# RunPod Manual Deployment Plan

**Updated: 2026-06-10**

This branch is for the manual RunPod Ubuntu/PyTorch pod setup.

We are not using Docker, Docker Compose, or a Windows `.exe` for this path.
RunPod starts an Ubuntu container, then we run normal Linux processes from
`/workspace`.

## 1. What Runs

The Augmented OCR app is not one `python main.py` process. It is a web app plus
background workers:

```text
FastAPI + frontend        public port 8000
Postgres                  private 127.0.0.1:5432
MinIO                     private 127.0.0.1:9000 / 9001
MLflow                    private 127.0.0.1:5000
llama-server              private 127.0.0.1:8056
normalize worker          background process
OCR worker                background process
LLM worker                background process
postprocess worker        background process
```

`start.sh` starts Postgres first, then hands the rest to `supervisord.conf`.

## 2. Files We Need In The Repo

For the manual RunPod path, the important repo files are:

```text
backend/
frontend/
start.sh
supervisord.conf
.env.example
deploy.md
```

Docker files are not used in this path.

## 3. Target Layout On RunPod

Use this layout:

```text
/workspace/
|-- app/                         # cloned repo
|-- .env                         # real secret env file, never commit
|-- venvs/
|   `-- augocr/                  # Python virtual environment, if creating new
|-- bin/
|   `-- minio                    # MinIO server binary
|-- llama.cpp/                   # llama.cpp source/build folder
|-- llama-server/
|   |-- llama-server
|   |-- libggml*.so*
|   `-- libllama.so
|-- models/qwen3.5/
|   |-- Qwen3.5-9B-UD-Q4_K_XL.gguf
|   `-- mmproj-F16.gguf
|-- backups/
|   `-- postgres/              # pg_dump backups (augocr_latest.dump, augocr_previous.dump)
|-- minio-data/
|-- mlflow/
|   `-- artifacts/
`-- logs/
```

Use `/workspace` because it survives normal pod stop/start. For production,
attach a RunPod network volume at `/workspace`.

## 4. Install OS Packages

Run once in the RunPod terminal:

```bash
apt-get update
apt-get install -y \
  git curl wget ca-certificates build-essential cmake pkg-config \
  python3 python3-venv python3-pip \
  postgresql postgresql-contrib \
  supervisor net-tools
```

Check:

```bash
nvidia-smi
nvcc --version
cmake --version
python3 --version
```

If `nvidia-smi` fails, the GPU is not visible. If `nvcc --version` fails, the
pod image may not be able to build `llama.cpp`; use a CUDA development image or
copy a compatible prebuilt `llama-server`.

## 5. Pull The Repo

If the repo already exists:

```bash
cd /workspace/app
git pull origin deployment_v1
```

If it does not exist:

```bash
cd /workspace
git clone -b deployment_v1 https://github.com/DeepakG-ai/Augemented_OCR_PaddleOCR_VL.git app
```

## 6. Use Or Create Python Virtual Environment

If you already have a venv, do not create another one. `start.sh` auto-detects
these locations in order:

```text
/workspace/app/.venv
/workspace/.venv
/workspace/venvs/augocr
```

Check for an existing venv:

```bash
ls -la /workspace/app
ls -la /workspace
ls -la /workspace/venvs 2>/dev/null || true
```

If your venv is somewhere else, set it in `/workspace/.env`:

```env
VENV_DIR=/path/to/your/existing/venv
```

Then install/update requirements into that existing venv:

```bash
source /path/to/your/existing/venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r /workspace/app/backend/requirements.txt
```

Only create a new venv if none exists:

```bash
python3 -m venv /workspace/venvs/augocr
source /workspace/venvs/augocr/bin/activate
python -m pip install --upgrade pip
python -m pip install -r /workspace/app/backend/requirements.txt
```

Verify whichever venv you are using:

```bash
"$VENV_DIR/bin/python" -c "import fastapi, asyncpg, minio, mlflow, paddleocr; print('ok')"
```

## 7. Install MinIO

```bash
mkdir -p /workspace/bin /workspace/minio-data
wget https://dl.min.io/server/minio/release/linux-amd64/minio -O /workspace/bin/minio
chmod +x /workspace/bin/minio
```

Verify:

```bash
/workspace/bin/minio --version
```

## 8. Download Model Files

```bash
mkdir -p /workspace/models/qwen3.5
cd /workspace/models/qwen3.5

wget -c "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-UD-Q4_K_XL.gguf"
wget -c "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-F16.gguf"

ls -lh /workspace/models/qwen3.5
```

Expected paths:

```text
/workspace/models/qwen3.5/Qwen3.5-9B-UD-Q4_K_XL.gguf
/workspace/models/qwen3.5/mmproj-F16.gguf
```

## 9. Build llama.cpp / llama-server

For RTX 5090 only, CUDA architecture `120` is enough. If this build may later
run on A100, RTX 4090, L40S, H100, or RTX 5090, use the multi-arch value below.

```bash
cd /workspace
rm -rf /workspace/llama.cpp
git clone --depth 1 https://github.com/ggml-org/llama.cpp.git llama.cpp
cd /workspace/llama.cpp

cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="80;86;89;90;120" \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --config Release -j"$(nproc)" --target llama-server
```

Copy the runtime files:

```bash
mkdir -p /workspace/llama-server
cp /workspace/llama.cpp/build/bin/llama-server /workspace/llama-server/
find /workspace/llama.cpp/build/bin -maxdepth 1 -name "*.so*" -exec cp {} /workspace/llama-server/ \;
chmod +x /workspace/llama-server/llama-server
```

Verify:

```bash
LD_LIBRARY_PATH=/workspace/llama-server /workspace/llama-server/llama-server --version
ldd /workspace/llama-server/llama-server | grep -E "not found|cuda|ggml|llama"
```

If `not found` appears, a shared library is missing from
`/workspace/llama-server` or the CUDA runtime is missing.

## 10. Create `/workspace/.env`

Create the real env file:

```bash
cp /workspace/app/.env.example /workspace/.env
nano /workspace/.env
```

Minimum important values:

```env
POSTGRES_DB=augocr
POSTGRES_USER=augocr
POSTGRES_PASSWORD=replace_with_strong_postgres_password
DATABASE_URL=postgresql://augocr:replace_with_strong_postgres_password@127.0.0.1:5432/augocr

MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=replace_with_minio_access_key
MINIO_SECRET_KEY=replace_with_strong_minio_secret_key
MINIO_SECURE=false

MLFLOW_ENABLED=true
MLFLOW_TRACKING_URI=http://127.0.0.1:5000

MODEL_GGUF=/workspace/models/qwen3.5/Qwen3.5-9B-UD-Q4_K_XL.gguf
MMPROJ_GGUF=/workspace/models/qwen3.5/mmproj-F16.gguf
LLAMA_BIN=/workspace/llama-server/llama-server
LLAMA_LIB_DIR=/workspace/llama-server
LLAMA_HOST=127.0.0.1
LLAMA_PORT=8056
LLAMA_N_GPU_LAYERS=99
LLAMA_CTX_SIZE=8192
LLAMA_BATCH_SIZE=4096
LLAMA_PARALLEL=1
LLAMA_IMAGE_MIN_TOKENS=1024
LLAMA_IMAGE_MAX_TOKENS=2048

LLM_URL=http://127.0.0.1:8056/v1/chat/completions
LLM_MODEL=qwen3vl
LLM_PAGE_BATCH_SIZE=1
LLM_TIMEOUT=300

OCR_DEVICE=cpu
OCR_WORKERS=3

SECRET_KEY=replace_with_64_hex_chars
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=replace_with_strong_admin_password

CORS_ALLOW_ORIGINS=https://<pod-id>-8000.proxy.runpod.net
PIPELINE_LOG_DIR=/workspace/logs/pipeline
LOG_LEVEL=INFO
```

Rules:

- Do not commit `/workspace/.env`.
- Do not put `8056` in `CORS_ALLOW_ORIGINS`.
- `LLM_PAGE_BATCH_SIZE` must match `LLAMA_PARALLEL`.
- Keep MinIO, MLflow, Postgres, and llama-server private.

## 11. Start The Whole App

After venv, MinIO, model files, and `llama-server` are ready:

```bash
cd /workspace/app
chmod +x start.sh
bash ./start.sh
```

`start.sh` will:

```text
load /workspace/.env
check the venv, model files, llama-server
start Postgres on local filesystem (/var/lib/postgresql/data)
create/update DB user and DB
if fresh initdb + backup exists → restore from /workspace/backups/postgres/augocr_latest.dump
start supervisord
```

`supervisord.conf` will start:

```text
MinIO
MLflow
llama-server
FastAPI on 8000
pg-backup (pg_dump every 5 min → /workspace/backups/postgres/)
normalize workers
OCR workers
LLM worker
postprocess worker
```

Keep that terminal running. If the terminal closes, the stack can stop.

## 12. Verify

Open another pod terminal and run:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U postgres
curl -fsS http://127.0.0.1:9000/minio/health/live
curl -fsS http://127.0.0.1:5000/
curl -fsS http://127.0.0.1:8056/v1/models
curl -fsS http://127.0.0.1:8000/health
```

Check processes:

```bash
ps aux | grep -E "uvicorn|backend.worker|llama-server|minio|mlflow|postgres" | grep -v grep
```

Watch logs. The stack writes 6 grouped log files:

```text
database.log    Postgres + automated backups
llama.log       the LLM server (llama-server)
pipeline.log    all extraction workers (normalize, ocr, llm, postprocess)
api.log         FastAPI web server
services.log    MinIO + MLflow
supervisord.log the process manager itself
```

Tail one feed, or all of them combined with the helper:

```bash
tail -f /workspace/logs/pipeline.log      # extraction activity
tail -f /workspace/logs/llama.log         # LLM
bash /workspace/app/logs.sh               # all services, one labelled stream
bash /workspace/app/logs.sh pipeline llama  # only these two
```

Open:

```text
https://<pod-id>-8000.proxy.runpod.net
```

## 13. Stop Or Restart

If `start.sh` is still running in the foreground, press `Ctrl+C`.

If using supervisor:

```bash
supervisorctl -s unix:///workspace/supervisor.sock status
supervisorctl -s unix:///workspace/supervisor.sock stop all
supervisorctl -s unix:///workspace/supervisor.sock start all
```

Stop Postgres:

```bash
runuser -u postgres -- pg_ctl -D "${PGDATA:-/var/lib/postgresql/data}" stop
```

## 14. Postgres Backup and Pod Migration

Postgres data lives on the local container filesystem (`/var/lib/postgresql/data`).
It is **lost** when a pod is migrated or recreated. The `pg-backup` supervisord
program protects against this by dumping to `/workspace/backups/postgres/` every
5 minutes (configurable via `PG_BACKUP_INTERVAL`).

Two backup files are kept:

```text
/workspace/backups/postgres/augocr_latest.dump     ← most recent successful dump
/workspace/backups/postgres/augocr_previous.dump   ← one rotation back
```

**On pod migration:** `start.sh` detects a fresh `initdb` (empty PGDATA) and
automatically restores from `augocr_latest.dump` before supervisord starts.
No manual steps required — just run `bash /workspace/app/start.sh` as normal.

**Worst-case data loss:** up to `PG_BACKUP_INTERVAL` seconds (default 5 minutes).

To check backup status:

```bash
ls -lh /workspace/backups/postgres/
tail -f /workspace/logs/database.log
```

To manually trigger a backup:

```bash
pg_dump -U postgres -Fc augocr > /workspace/backups/postgres/augocr_manual.dump
```

## 16. Manual llama-server Debug

Use only when the full stack is stopped or port `8056` is free:

```bash
LD_LIBRARY_PATH=/workspace/llama-server /workspace/llama-server/llama-server \
  -m /workspace/models/qwen3.5/Qwen3.5-9B-UD-Q4_K_XL.gguf \
  --mmproj /workspace/models/qwen3.5/mmproj-F16.gguf \
  --host 127.0.0.1 \
  --port 8056 \
  -ngl 99 \
  -c 8192 \
  -b 4096 \
  --parallel 1 \
  --cont-batching \
  --image-min-tokens 1024 \
  --image-max-tokens 2048 \
  --cache-ram 0 \
  --reasoning off \
  --jinja
```

## 17. Troubleshooting

### llama-server build fails

Check:

```bash
nvcc --version
cmake --version
nvidia-smi
```

If CUDA does not support `120`, and you are not on RTX 5090, remove `120` from
`CMAKE_CUDA_ARCHITECTURES`. If you are on RTX 5090, use a CUDA 12.8+
development image.

### API fails at startup

Check MinIO first:

```bash
curl -fsS http://127.0.0.1:9000/minio/health/live
tail -n 100 /workspace/logs/services.log
tail -n 100 /workspace/logs/api.log
```

### Postgres says already running or pid exists

`start.sh` checks whether Postgres is already running and removes stale pid only
when the pid no longer exists.

### App cannot reach LLM

Check:

```bash
curl -fsS http://127.0.0.1:8056/v1/models
tail -n 100 /workspace/logs/llama.log
tail -n 100 /workspace/logs/pipeline.log
```

## 18. Security

- Public port: `8000` only.
- Private ports: `5432`, `8056`, `9000`, `9001`, `5000`.
- Never expose llama-server publicly.
- Never commit `.env`.
- Treat MLflow traces as sensitive because they may contain prompts/results.
- Use SSH tunnels if you need MinIO console or MLflow UI.
