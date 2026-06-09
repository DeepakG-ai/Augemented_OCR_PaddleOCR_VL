# Single-Pod RunPod Deployment Plan

**Updated: 2026-06-09**

This is the deployment plan for running the full Augmented OCR system on one
RunPod Ubuntu GPU pod without Docker Compose.

The pod runs these processes directly:

- FastAPI app and static frontend on port `8000`
- Postgres on `127.0.0.1:5432`
- MinIO on `127.0.0.1:9000` with console on `127.0.0.1:9001`
- MLflow tracking server on `127.0.0.1:5000`
- `llama-server` on `127.0.0.1:8056`
- Backend workers: `normalize`, `ocr`, `llm`, `postprocess`

Only the app/API port is public. The model, database, MinIO, and MLflow stay
inside the pod.

## 0. Important Answers

### Where does `.env` go?

The real production `.env` goes under `/workspace` on the pod:

```bash
/workspace/.env
```

Do not commit it. Do not put it inside the git repo. Do not include it in a zip.

The repo file `.env.example` is only a template.

Important: `/workspace` is only safe across pod deletion/recreation when the pod
has an explicit RunPod network volume attached. If the pod only has the default
volume disk, `/workspace` survives normal stop/start and pod edits, but it is
not portable and is deleted with the pod.

### Do we use Docker on RunPod?

No. The repo has `Dockerfile` and `docker-compose.yml` for local/dev use, but the
RunPod pod should run normal Linux processes from `/workspace/start.sh`.

Set the RunPod template start command to:

```bash
bash /workspace/start.sh
```

### Do we expose port `8056`?

No. `8056` is only for internal `llama-server` traffic.

The backend calls:

```env
LLM_URL=http://127.0.0.1:8056/v1/chat/completions
```

Public users only hit:

```text
https://<pod-id>-8000.proxy.runpod.net
```

### What about MinIO and MLflow ports?

Keep them private by default:

| Port | Public? | Purpose |
| --- | --- | --- |
| `8000/http` | Yes | FastAPI app + frontend |
| `5432` | No | Postgres |
| `8056` | No | llama-server |
| `9000` | No | MinIO API |
| `9001` | No | MinIO console |
| `5000` | No | MLflow UI/API |

If you need to inspect MinIO or MLflow, prefer an SSH tunnel instead of exposing
them publicly:

```bash
ssh -L 9001:127.0.0.1:9001 -L 5000:127.0.0.1:5000 root@<pod-ssh-host>
```

Then open these on your local machine:

```text
http://127.0.0.1:9001
http://127.0.0.1:5000
```

## 1. Target Architecture

```text
Internet
  |
  | HTTPS via RunPod proxy
  v
https://<pod-id>-8000.proxy.runpod.net
  |
  v
RunPod Ubuntu GPU Pod
  |
  |-- FastAPI + frontend     0.0.0.0:8000       public through RunPod proxy
  |-- Postgres               127.0.0.1:5432     private
  |-- MinIO API              127.0.0.1:9000     private
  |-- MinIO console          127.0.0.1:9001     private/debug only
  |-- MLflow                 127.0.0.1:5000     private/debug only
  |-- llama-server           127.0.0.1:8056     private
  |-- workers                local processes
  |
  `-- /workspace storage
```

`/workspace` is the only place this plan writes durable app state. For
production, attach a RunPod network volume at `/workspace` before deploying. If
IT gives you only a default volume disk, do not treat it as a backup or as data
that can survive pod termination.

## 2. Persistent File Layout

Create this layout on the pod:

```text
/workspace/
|-- app/                         # git clone of deploy_v1 branch
|-- .env                         # real production env, not committed
|-- start.sh                     # starts the full stack
|-- .venv/                       # Python venv
|-- bin/
|   |-- minio
|   `-- mc                       # optional MinIO client for backups/debug
|-- llama.cpp/                   # source checkout used to build llama-server
|-- llama-server/                # copied binary and shared libraries
|   |-- llama-server
|   |-- libggml*.so*
|   `-- libllama.so
|-- models/qwen3.5/
|   |-- Qwen3.5-9B-UD-Q4_K_XL.gguf
|   `-- mmproj-F16.gguf
|-- pgdata/                      # Postgres data
|-- minio-data/                  # MinIO object storage data
|-- mlflow/
|   |-- mlflow.db
|   `-- artifacts/
|-- backups/
`-- logs/
```

Why not `/opt`? Your local WSL build uses `/opt/llama-server` and `/opt/models`.
That is fine locally, but on RunPod the intended persistent place is
`/workspace`. Use `/workspace/llama-server` and `/workspace/models/qwen3.5` so
the build and model survive normal restarts. For deletion/recreation safety,
confirm that `/workspace` is backed by a network volume.

## 3. Create The RunPod Pod

Recommended pod settings:

- Cloud type: Secure Cloud
- GPU: RTX 5090 x1
- Base image: Ubuntu/CUDA image with NVIDIA drivers visible in the pod
- Storage: attach a RunPod network volume mounted at `/workspace` for production
- Exposed HTTP ports: `8000` only

Do not expose:

```text
5432
8056
9000
9001
5000
```

After the pod starts, open a terminal and verify GPU access:

```bash
nvidia-smi
```

## 4. One-Time OS Package Install

Run this on the pod. Package names can vary by image, but this is the normal
Ubuntu shape:

```bash
apt-get update
apt-get install -y \
  git curl ca-certificates build-essential cmake pkg-config \
  python3 python3-venv python3-pip \
  postgresql postgresql-contrib \
  net-tools
```

Verify the important tools:

```bash
python3 --version
cmake --version
psql --version
nvidia-smi
```

If `cmake` is too old for your CUDA/llama.cpp build, install a newer CMake or
use a newer CUDA development image.

## 5. Clone The App Branch

Clone the deployment branch into `/workspace/app`:

```bash
cd /workspace
git clone -b deploy_v1 https://github.com/DeepakG-ai/Augemented_OCR_PaddleOCR_VL.git app
cd /workspace/app
```

If `/workspace/app` already exists:

```bash
cd /workspace/app
git pull
```

## 6. Build The Python Environment

Create the venv on `/workspace`:

```bash
python3 -m venv /workspace/.venv
source /workspace/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r /workspace/app/backend/requirements.txt
```

Verify:

```bash
/workspace/.venv/bin/python -c "import fastapi, asyncpg, minio, mlflow, paddleocr; print('ok')"
```

OCR stays on CPU:

```env
OCR_DEVICE=cpu
```

Do not switch to `paddlepaddle-gpu` unless you have separately verified a
Blackwell-compatible Paddle build.

## 7. Build llama.cpp On RunPod

Your local Word document used this build:

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
git checkout b9294
cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j $(nproc) --target llama-server
```

That `b9294` pin is useful as a record of your local build, but do not use it as
the first RunPod build for RTX 5090. Blackwell (`sm_120`) needs a CUDA 12.8+
toolchain and a new enough `llama.cpp`; a mid-2024 commit may not understand the
GPU target or newer multimodal flags. On RunPod, build current `main` first and
record the exact commit that works.

Build on RunPod under `/workspace`:

```bash
cd /workspace
git clone https://github.com/ggml-org/llama.cpp.git llama.cpp
cd /workspace/llama.cpp
git pull --ff-only
git rev-parse HEAD

cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=120 \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --config Release -j "$(nproc)" --target llama-server
```

If the build fails because the CUDA toolkit or CMake image is not ready for
`sm_120`, verify the pod image has CUDA 12.8+ development tools and a recent
CMake. Only pin a `llama.cpp` commit after you have verified it works on the
actual RunPod RTX 5090.

If you need to retry from a clean build directory:

```bash
rm -rf build
cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=120 \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --config Release -j "$(nproc)" --target llama-server
```

Copy the runtime binary and shared libraries:

```bash
mkdir -p /workspace/llama-server
cp /workspace/llama.cpp/build/bin/llama-server /workspace/llama-server/
cp /workspace/llama.cpp/build/bin/libggml*.so* /workspace/llama-server/
cp /workspace/llama.cpp/build/bin/libllama.so /workspace/llama-server/
chmod +x /workspace/llama-server/llama-server
```

Verify:

```bash
LD_LIBRARY_PATH=/workspace/llama-server \
  /workspace/llama-server/llama-server --version

ldd /workspace/llama-server/llama-server | grep -E "cuda|ggml|llama|not found"
```

If `ldd` shows `not found`, the needed `.so` file is missing from
`/workspace/llama-server` or the CUDA runtime is missing from the pod image.

### Copying Your WSL Build Instead

Your local path:

```text
\\wsl.localhost\Ubuntu\opt\llama-server
```

exists only on your Windows/WSL machine. RunPod cannot use it directly.

You can tar and copy it, but this is less reliable than building on RunPod
because CUDA libraries and GPU architecture may differ:

```bash
# In WSL
cd /opt
tar -czf llama-server.tar.gz llama-server
```

Then upload to RunPod and extract:

```bash
cd /workspace
tar -xzf llama-server.tar.gz
mv llama-server /workspace/llama-server
```

Recommended path: build on RunPod once, store the result in
`/workspace/llama-server`.

## 8. Put Model Files On The Pod

Create the model folder:

```bash
mkdir -p /workspace/models/qwen3.5
cd /workspace/models/qwen3.5

wget "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-UD-Q4_K_XL.gguf"
wget "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-F16.gguf"
```

Verify:

```bash
ls -lh /workspace/models/qwen3.5/
```

## 9. Install MinIO

Install the MinIO server binary:

```bash
mkdir -p /workspace/bin /workspace/minio-data

curl -L https://dl.min.io/server/minio/release/linux-amd64/minio \
  -o /workspace/bin/minio

chmod +x /workspace/bin/minio
```

Optional but useful: install the MinIO client for backup/debug:

```bash
curl -L https://dl.min.io/client/mc/release/linux-amd64/mc \
  -o /workspace/bin/mc

chmod +x /workspace/bin/mc
```

MinIO will be started by `/workspace/start.sh`, not manually.

## 10. Create `/workspace/.env`

Create the real env file on the pod:

```bash
nano /workspace/.env
```

Use this shape:

```env
# Postgres
POSTGRES_DB=augocr
POSTGRES_USER=augocr
POSTGRES_PASSWORD=<fresh-strong-postgres-password>
DATABASE_URL=postgresql://augocr:<fresh-strong-postgres-password>@127.0.0.1:5432/augocr

# MinIO
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=<fresh-minio-access-key>
MINIO_SECRET_KEY=<fresh-minio-secret-key>
MINIO_SECURE=false
MINIO_DOCUMENTS_BUCKET=augocr-documents
MINIO_ARTIFACTS_BUCKET=augocr-artifacts
MINIO_EXPORTS_BUCKET=augocr-exports

# MLflow
MLFLOW_ENABLED=true
MLFLOW_TRACKING_URI=http://127.0.0.1:5000
MLFLOW_EXPERIMENT_NAME=augmented_ocr

# llama-server command settings used by start.sh
MODEL_GGUF=/workspace/models/qwen3.5/Qwen3.5-9B-UD-Q4_K_XL.gguf
MMPROJ_GGUF=/workspace/models/qwen3.5/mmproj-F16.gguf
LLAMA_SERVER_DIR=/workspace/llama-server
LLAMA_HOST=127.0.0.1
LLAMA_PORT=8056
LLAMA_N_GPU_LAYERS=99
LLAMA_CTX_SIZE=8192
LLAMA_BATCH_SIZE=4096
LLAMA_PARALLEL=1
LLAMA_IMAGE_MIN_TOKENS=1024
LLAMA_IMAGE_MAX_TOKENS=2048

# App LLM client
LLM_URL=http://127.0.0.1:8056/v1/chat/completions
LLM_MODEL=qwen3vl
LLM_PAGE_BATCH_SIZE=1
LLM_TIMEOUT=300

# OCR
OCR_DEVICE=cpu
OCR_WORKERS=3

# App/auth
SECRET_KEY=<fresh-long-random-secret>
ADMIN_EMAIL=<your-admin-email>
ADMIN_PASSWORD=<fresh-strong-admin-password>

# Public app origin. Do not put port 8056 here.
CORS_ALLOW_ORIGINS=https://<pod-id>-8000.proxy.runpod.net
RATE_LIMIT_PER_MINUTE=30
MAX_UPLOAD_MB=50
MAX_DOCUMENT_PAGES=100

# Logs
PIPELINE_LOG_DIR=/workspace/logs/pipeline
LOG_LEVEL=INFO
```

Rules:

- `LLM_PAGE_BATCH_SIZE` must match `LLAMA_PARALLEL`.
- If you run `LLAMA_PARALLEL=1`, use `LLM_PAGE_BATCH_SIZE=1`.
- If you later run `LLAMA_PARALLEL=4`, use `LLM_PAGE_BATCH_SIZE=4`.
- `CORS_ALLOW_ORIGINS` is the browser/app origin, not the LLM port.
- If `POSTGRES_PASSWORD` contains URL special characters such as `@`, `:`, `/`,
  `?`, `#`, or `%`, URL-encode it inside `DATABASE_URL`. The simplest first
  deploy path is to generate a long password using letters and numbers only.
- Rotate all secrets before real customer data.

## 11. Create `/workspace/start.sh`

Create the startup script:

```bash
nano /workspace/start.sh
chmod +x /workspace/start.sh
```

Use this script:

This starter script assumes the RunPod container runs as `root`, which is the
normal RunPod pod terminal behavior. It uses `runuser` for Postgres because
`initdb` must not run as root.

```bash
#!/usr/bin/env bash
set -euo pipefail

APP=/workspace/app
ENV_FILE=/workspace/.env
PY=/workspace/.venv/bin/python
MLFLOW=/workspace/.venv/bin/mlflow
PGDATA=/workspace/pgdata
LOG_DIR=/workspace/logs

mkdir -p "$LOG_DIR" "$PGDATA" /workspace/minio-data /workspace/mlflow/artifacts

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

export PATH="/usr/lib/postgresql/16/bin:/usr/lib/postgresql/15/bin:/usr/lib/postgresql/14/bin:$PATH"

wait_for_http() {
  local url="$1"
  local name="$2"
  echo "Waiting for $name at $url ..."
  until curl -fsS "$url" >/dev/null 2>&1; do
    sleep 2
  done
}

wait_for_pg() {
  echo "Waiting for Postgres ..."
  until pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; do
    sleep 1
  done
}

echo "Starting Postgres ..."
mkdir -p "$PGDATA"
chown -R postgres:postgres "$PGDATA" "$LOG_DIR"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  runuser -u postgres -- initdb -D "$PGDATA" -U postgres --auth-local=trust --auth-host=scram-sha-256
fi

runuser -u postgres -- pg_ctl -D "$PGDATA" \
  -l "$LOG_DIR/postgres.log" \
  -o "-c listen_addresses=127.0.0.1 -p 5432" \
  start

wait_for_pg

APP_DB_PASSWORD_SQL="$(printf "%s" "$POSTGRES_PASSWORD" | sed "s/'/''/g")"

ROLE_EXISTS="$(runuser -u postgres -- psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='augocr'")"
if [ "$ROLE_EXISTS" != "1" ]; then
  runuser -u postgres -- psql -U postgres \
    -c "CREATE ROLE augocr LOGIN PASSWORD '$APP_DB_PASSWORD_SQL';"
else
  runuser -u postgres -- psql -U postgres \
    -c "ALTER ROLE augocr WITH PASSWORD '$APP_DB_PASSWORD_SQL';"
fi

DB_EXISTS="$(runuser -u postgres -- psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='augocr'")"
if [ "$DB_EXISTS" != "1" ]; then
  runuser -u postgres -- createdb -U postgres -O augocr augocr
fi

echo "Starting MinIO ..."
MINIO_ROOT_USER="$MINIO_ACCESS_KEY" \
MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY" \
  /workspace/bin/minio server /workspace/minio-data \
  --address 127.0.0.1:9000 \
  --console-address 127.0.0.1:9001 \
  >"$LOG_DIR/minio.log" 2>&1 &
MINIO_PID=$!

wait_for_http "http://127.0.0.1:9000/minio/health/live" "MinIO"

echo "Starting MLflow ..."
"$MLFLOW" server \
  --host 127.0.0.1 \
  --port 5000 \
  --backend-store-uri sqlite:////workspace/mlflow/mlflow.db \
  --default-artifact-root /workspace/mlflow/artifacts \
  >"$LOG_DIR/mlflow.log" 2>&1 &
MLFLOW_PID=$!

wait_for_http "http://127.0.0.1:5000/" "MLflow"

echo "Starting llama-server ..."
LLAMA_BIN="${LLAMA_SERVER_DIR:-/workspace/llama-server}/llama-server"
LLAMA_LIB_DIR="${LLAMA_SERVER_DIR:-/workspace/llama-server}"

LD_LIBRARY_PATH="$LLAMA_LIB_DIR" "$LLAMA_BIN" \
  -m "$MODEL_GGUF" \
  --mmproj "$MMPROJ_GGUF" \
  --host "${LLAMA_HOST:-127.0.0.1}" \
  --port "${LLAMA_PORT:-8056}" \
  -ngl "${LLAMA_N_GPU_LAYERS:-99}" \
  -c "${LLAMA_CTX_SIZE:-8192}" \
  -b "${LLAMA_BATCH_SIZE:-4096}" \
  --parallel "${LLAMA_PARALLEL:-1}" \
  --cont-batching \
  --image-min-tokens "${LLAMA_IMAGE_MIN_TOKENS:-1024}" \
  --image-max-tokens "${LLAMA_IMAGE_MAX_TOKENS:-2048}" \
  --cache-ram 0 \
  --reasoning off \
  --jinja \
  >"$LOG_DIR/llama.log" 2>&1 &
LLAMA_PID=$!

wait_for_http "http://127.0.0.1:${LLAMA_PORT:-8056}/v1/models" "llama-server"

cd "$APP"

echo "Starting API ..."
"$PY" -m uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log \
  >"$LOG_DIR/api.log" 2>&1 &
API_PID=$!

wait_for_http "http://127.0.0.1:8000/health" "API"

echo "Starting workers ..."
"$PY" -m backend.worker --stage normalize   --name normalize-1 >"$LOG_DIR/normalize-1.log" 2>&1 &
NORM1_PID=$!
"$PY" -m backend.worker --stage normalize   --name normalize-2 >"$LOG_DIR/normalize-2.log" 2>&1 &
NORM2_PID=$!
"$PY" -m backend.worker --stage ocr         --name ocr-1       >"$LOG_DIR/ocr-1.log" 2>&1 &
OCR1_PID=$!
"$PY" -m backend.worker --stage ocr         --name ocr-2       >"$LOG_DIR/ocr-2.log" 2>&1 &
OCR2_PID=$!
"$PY" -m backend.worker --stage llm         --name llm-1       >"$LOG_DIR/llm-1.log" 2>&1 &
LLM1_PID=$!
"$PY" -m backend.worker --stage postprocess --name post-1      >"$LOG_DIR/post-1.log" 2>&1 &
POST1_PID=$!

echo "Augmented OCR stack is running."
echo "Public app: http://0.0.0.0:8000 through RunPod proxy"
echo "Logs: $LOG_DIR"

wait -n \
  "$MINIO_PID" "$MLFLOW_PID" "$LLAMA_PID" "$API_PID" \
  "$NORM1_PID" "$NORM2_PID" "$OCR1_PID" "$OCR2_PID" "$LLM1_PID" "$POST1_PID"

echo "One process exited. Check logs in $LOG_DIR."
exit 1
```

Set the RunPod template start command to:

```bash
bash /workspace/start.sh
```

For a first manual run:

```bash
bash /workspace/start.sh
```

## 12. Verify The Stack

In a second pod terminal:

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

Watch logs:

```bash
tail -f /workspace/logs/api.log
tail -f /workspace/logs/llama.log
tail -f /workspace/logs/llm-1.log
tail -f /workspace/logs/minio.log
tail -f /workspace/logs/mlflow.log
```

Open the app:

```text
https://<pod-id>-8000.proxy.runpod.net
```

## 13. Run llama-server Manually For Debugging

Use this only for testing. Stop the running stack first if `8056` is already in
use.

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

Health check:

```bash
curl http://127.0.0.1:8056/v1/models
```

The local command from your WSL system used:

```bash
--host 0.0.0.0 --port 8001
```

For RunPod production, use:

```bash
--host 127.0.0.1 --port 8056
```

That keeps the LLM private and lets the backend call it locally.

## 14. MinIO Notes

The backend creates/checks buckets when it starts through `get_store()`.

Expected env:

```env
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=<fresh-minio-access-key>
MINIO_SECRET_KEY=<fresh-minio-secret-key>
MINIO_SECURE=false
```

To inspect MinIO without exposing it publicly:

```bash
ssh -L 9001:127.0.0.1:9001 root@<pod-ssh-host>
```

Then open:

```text
http://127.0.0.1:9001
```

Login with `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`.

## 15. MLflow Notes

MLflow is enabled in this plan:

```env
MLFLOW_ENABLED=true
MLFLOW_TRACKING_URI=http://127.0.0.1:5000
```

The app traces only the LLM worker path. MLflow may contain prompts, responses,
token usage, and extracted values, so do not expose `5000` publicly.

To inspect MLflow without exposing it publicly:

```bash
ssh -L 5000:127.0.0.1:5000 root@<pod-ssh-host>
```

Then open:

```text
http://127.0.0.1:5000
```

## 16. First End-To-End Test

After the stack is up:

1. Open `https://<pod-id>-8000.proxy.runpod.net`.
2. Login using `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
3. Create a client.
4. Create or verify a vendor/template.
5. Upload a known PDF.
6. Confirm extraction completes.
7. Open review and save a correction.
8. Re-upload a similar document to confirm memory/correction behavior.
9. Confirm MinIO has stored objects.
10. Confirm MLflow has LLM traces.

Useful checks:

```bash
psql "$DATABASE_URL" -c "SELECT job_type, status, count(*) FROM jobs GROUP BY 1,2 ORDER BY 1,2;"
/workspace/bin/mc alias set local http://127.0.0.1:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"
/workspace/bin/mc ls local
curl -fsS http://127.0.0.1:5000/
nvidia-smi
```

## 17. Worker Sizing

Initial sizing for one RTX 5090 pod with 16 vCPUs:

| Stage | Replicas | Why |
| --- | --- | --- |
| `normalize` | 2 | CPU and I/O stage |
| `ocr` | 2 | CPU PaddleOCR is heavy |
| `llm` | 1 | One worker can fill `llama-server` using page batching |
| `postprocess` | 1 | Usually light |

With the current llama command:

```env
LLAMA_PARALLEL=1
LLM_PAGE_BATCH_SIZE=1
```

If you later test parallelism:

```env
LLAMA_PARALLEL=2
LLM_PAGE_BATCH_SIZE=2
```

Then:

```env
LLAMA_PARALLEL=4
LLM_PAGE_BATCH_SIZE=4
```

Watch VRAM and latency with:

```bash
nvidia-smi -l 2
tail -f /workspace/logs/llama.log /workspace/logs/llm-1.log
```

## 18. Backups

Whether `/workspace` is a network volume or the default volume disk, it is not
a backup. Back up off the pod. A network volume survives pod deletion, but it is
still one storage copy in one provider/region.

Back up Postgres:

```bash
mkdir -p /workspace/backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
pg_dump "$DATABASE_URL" -Fc -f "/workspace/backups/db-$STAMP.dump"
```

Back up MinIO with the MinIO client:

```bash
/workspace/bin/mc alias set local http://127.0.0.1:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"
/workspace/bin/mc mirror local/augocr-documents /workspace/backups/minio-documents
/workspace/bin/mc mirror local/augocr-artifacts /workspace/backups/minio-artifacts
```

Then copy `/workspace/backups` to S3, R2, B2, or another off-pod location.

Do not rely only on `/workspace`.

## 19. Security Rules

- Public port: `8000` only.
- Private ports: `5432`, `8056`, `9000`, `9001`, `5000`.
- Never expose `llama-server` publicly.
- Never put `.env` into git or a release zip.
- Rotate `SECRET_KEY`, `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, and MinIO keys
  before production use.
- Use the exact RunPod proxy URL in `CORS_ALLOW_ORIGINS`.
- Keep MLflow and MinIO behind SSH tunnels when you need their UIs.
- Treat MLflow traces as sensitive data.

## 20. Troubleshooting

### llama-server cannot find shared libraries

```bash
ldd /workspace/llama-server/llama-server | grep "not found"
```

Fix by copying missing `libggml*.so*` or `libllama.so` into
`/workspace/llama-server`, or by using the correct CUDA base image.

### llama-server out of VRAM

Reduce:

```env
LLAMA_CTX_SIZE=4096
LLAMA_BATCH_SIZE=2048
LLAMA_PARALLEL=1
LLM_PAGE_BATCH_SIZE=1
```

### Port already in use

```bash
netstat -tulpn | grep -E "8000|8056|9000|9001|5000|5432"
```

Stop the old process before starting again.

### MinIO starts but app falls back to local storage

Check:

```bash
tail -n 100 /workspace/logs/minio.log
curl -fsS http://127.0.0.1:9000/minio/health/live
```

Verify `/workspace/.env` has the same access key/secret used by `start.sh`.

### MLflow is slow or unavailable

The app has a tracing circuit breaker, so extraction should continue even if
MLflow is down. Check:

```bash
tail -n 100 /workspace/logs/mlflow.log
curl -fsS http://127.0.0.1:5000/
```

### API cannot connect to Postgres

Check:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U postgres
psql "$DATABASE_URL" -c "SELECT 1;"
tail -n 100 /workspace/logs/postgres.log
tail -n 100 /workspace/logs/api.log
```

## 21. What To Commit

Commit these:

```text
backend/
frontend/
Dockerfile
docker-compose.yml
.env.example
deploy.md
```

Do not commit:

```text
.env
backend/.env
/workspace/.env
.venv/
logs/
mlflow.db
aug-ocr.zip
model files
MinIO data
Postgres data
```

## 22. RunPod References

- RunPod Pods overview: <https://docs.runpod.io/pods/overview>
- RunPod pod networking: <https://docs.runpod.io/pods/networking>
- RunPod storage: <https://docs.runpod.io/pods/storage/types>
- RunPod network volumes: <https://docs.runpod.io/pods/storage/create-network-volumes>
- RunPod secrets: <https://docs.runpod.io/pods/templates/secrets>
- RunPod SSH: <https://docs.runpod.io/pods/connect-to-a-pod>
