#!/usr/bin/env bash
set -euo pipefail

# Augmented OCR — RunPod single-pod launcher (external Supabase Postgres).
# Postgres is NOT run locally: the app connects to Supabase via DATABASE_URL.
# This script validates the environment, then hands off to supervisord which
# runs MinIO, MLflow, llama-server, the API, and the workers.

APP_DIR="${APP_DIR:-/workspace/app}"
ENV_FILE="${ENV_FILE:-/workspace/.env}"
LOG_DIR="${LOG_DIR:-/workspace/logs}"

if [ -z "${VENV_DIR:-}" ]; then
  for candidate in "$APP_DIR/.venv" /workspace/.venv /workspace/venvs/augocr; do
    if [ -x "$candidate/bin/python" ]; then
      VENV_DIR="$candidate"
      break
    fi
  done
fi

VENV_DIR="${VENV_DIR:-/workspace/venvs/augocr}"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Create it before starting."
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "ERROR: Python venv not found at $VENV_DIR"
  echo "Create it with: python3 -m venv $VENV_DIR && $VENV_DIR/bin/pip install -r $APP_DIR/backend/requirements.txt"
  exit 1
fi

if [ ! -f "$APP_DIR/supervisord.conf" ]; then
  echo "ERROR: $APP_DIR/supervisord.conf not found."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

mkdir -p "$LOG_DIR" /workspace/minio-data /workspace/mlflow/artifacts
chmod 0777 "$LOG_DIR" 2>/dev/null || true

export APP_DIR
export VENV_DIR
export LOG_DIR
export PATH="$VENV_DIR/bin:$PATH"
export LLAMA_BIN="${LLAMA_BIN:-/workspace/llama-server/llama-server}"
export LLAMA_LIB_DIR="${LLAMA_LIB_DIR:-/workspace/llama-server}"
# Keep PaddleOCR model caches on the persistent volume so a pod restart
# doesn't re-download models on the first OCR job.
export PADDLE_PDX_CACHE_HOME="${PADDLE_PDX_CACHE_HOME:-/workspace/paddleocr/pdx_cache}"
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-/workspace/paddleocr/modelscope_cache}"
export PADDLE_HOME="${PADDLE_HOME:-/workspace/paddleocr/paddle_home}"
mkdir -p "$PADDLE_PDX_CACHE_HOME" "$MODELSCOPE_CACHE" "$PADDLE_HOME"
export MODEL_GGUF="${MODEL_GGUF:-/workspace/models/qwen3.5/Qwen3.5-9B-UD-Q4_K_XL.gguf}"
export MMPROJ_GGUF="${MMPROJ_GGUF:-/workspace/models/qwen3.5/mmproj-F16.gguf}"
export LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
export LLAMA_PORT="${LLAMA_PORT:-8056}"
export LLAMA_N_GPU_LAYERS="${LLAMA_N_GPU_LAYERS:-99}"
export LLAMA_BATCH_SIZE="${LLAMA_BATCH_SIZE:-4096}"
export LLAMA_PARALLEL="${LLAMA_PARALLEL:-1}"
export LLAMA_IMAGE_MIN_TOKENS="${LLAMA_IMAGE_MIN_TOKENS:-1024}"
export LLAMA_IMAGE_MAX_TOKENS="${LLAMA_IMAGE_MAX_TOKENS:-2048}"

# ── llama-server context sizing for continuous batching ─────────────────────
# llama-server runs with --cont-batching and splits the TOTAL context (-c)
# evenly across the --parallel slots:  per-slot = LLAMA_CTX_SIZE / LLAMA_PARALLEL.
# One page request (image + prompt + generated fields) needs about one
# known-good single-slot context (8192) or it truncates. So the total context
# must scale with the slot count — otherwise every slot is starved the moment
# LLAMA_PARALLEL > 1 (e.g. parallel=4 with -c 8192 gives each slot only 2048).
# We guarantee each slot gets at least LLAMA_CTX_PER_SLOT, auto-raising -c if a
# too-small LLAMA_CTX_SIZE was provided in the env file.
export LLAMA_CTX_PER_SLOT="${LLAMA_CTX_PER_SLOT:-8192}"
_required_ctx=$(( LLAMA_PARALLEL * LLAMA_CTX_PER_SLOT ))
export LLAMA_CTX_SIZE="${LLAMA_CTX_SIZE:-$_required_ctx}"
if [ "$LLAMA_CTX_SIZE" -lt "$_required_ctx" ]; then
  echo "NOTE: LLAMA_CTX_SIZE=$LLAMA_CTX_SIZE is too small for LLAMA_PARALLEL=$LLAMA_PARALLEL" \
       "(each slot needs $LLAMA_CTX_PER_SLOT, total >= $_required_ctx). Raising -c to $_required_ctx."
  export LLAMA_CTX_SIZE="$_required_ctx"
fi

# Concurrency to llama-server is driven by (llm workers) × LLM_PAGE_BATCH_SIZE.
# With a single llm worker this defaults to the slot count so one multi-page PDF
# can fill all slots; with multiple llm workers set LLM_PAGE_BATCH_SIZE=1 in .env
# and let cont-batching interleave pages from different users across the slots.
export LLM_PAGE_BATCH_SIZE="${LLM_PAGE_BATCH_SIZE:-$LLAMA_PARALLEL}"

echo "llama-server: --parallel $LLAMA_PARALLEL -c $LLAMA_CTX_SIZE" \
     "(per-slot $(( LLAMA_CTX_SIZE / LLAMA_PARALLEL )), cont-batching) | LLM_PAGE_BATCH_SIZE=$LLM_PAGE_BATCH_SIZE"

if [ ! -x "$LLAMA_BIN" ]; then
  echo "ERROR: llama-server not found or not executable: $LLAMA_BIN"
  echo "Build llama.cpp first, then copy llama-server and shared libraries into /workspace/llama-server."
  exit 1
fi

if [ ! -f "$MODEL_GGUF" ]; then
  echo "ERROR: MODEL_GGUF not found: $MODEL_GGUF"
  exit 1
fi

if [ ! -f "$MMPROJ_GGUF" ]; then
  echo "ERROR: MMPROJ_GGUF not found: $MMPROJ_GGUF"
  exit 1
fi

# ── Auto-install missing system packages (apt / RunPod containers) ───────────
_apt_updated=0
_apt_ensure() {
  if ! command -v "$1" >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1; then
      echo "ERROR: '$1' not found and apt-get is unavailable. Install it manually."
      exit 1
    fi
    if [ "$_apt_updated" -eq 0 ]; then
      echo "Updating apt package index..."
      apt-get update -qq
      _apt_updated=1
    fi
    echo "Auto-installing: ${*:2}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${@:2}"
  fi
}

_apt_ensure supervisord  supervisor
_apt_ensure curl         curl
_apt_ensure pg_lsclusters postgresql-contrib

if ! "$VENV_DIR/bin/python" -c "import uvicorn, mlflow, asyncpg" >/dev/null 2>&1; then
  echo "ERROR: venv at $VENV_DIR is missing required packages (uvicorn/mlflow/asyncpg)."
  echo "Run: $VENV_DIR/bin/pip install -r $APP_DIR/backend/requirements.txt"
  exit 1
fi

# ── Database (Supabase / external Postgres) ──────────────────────────────────
# The app talks to Supabase over the network via DATABASE_URL. There is no local
# Postgres to start, no data dir, and no backups to manage here — Supabase owns
# all of that. We just fail fast with a clear message if it isn't reachable.
if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL is not set in $ENV_FILE."
  echo "Use your Supabase Session-pooler string (port 5432) with ?sslmode=require, e.g.:"
  echo "  postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
  exit 1
fi

echo "Checking database connectivity (Supabase)..."
if ! "$VENV_DIR/bin/python" - <<'PYEOF'
import asyncio, os, sys
import asyncpg

async def main():
    try:
        conn = await asyncio.wait_for(asyncpg.connect(os.environ["DATABASE_URL"]), timeout=15)
        version = await conn.fetchval("SELECT version()")
        await conn.close()
        print("  connected:", version.split(" on ")[0])
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

asyncio.run(main())
PYEOF
then
  echo "ERROR: could not connect to DATABASE_URL."
  echo "  - Use the Supabase Session pooler (port 5432), NOT the transaction pooler (6543)."
  echo "  - Make sure the password is correct and the URL ends with ?sslmode=require."
  exit 1
fi

export MINIO_ROOT_USER="$MINIO_ACCESS_KEY"
export MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY"
export MINIO_BIN="${MINIO_BIN:-/workspace/bin/minio}"

if [ ! -x "$MINIO_BIN" ]; then
  echo "MinIO not found at $MINIO_BIN — downloading..."
  mkdir -p "$(dirname "$MINIO_BIN")"
  curl -fsSL "https://dl.min.io/server/minio/release/linux-amd64/minio" -o "$MINIO_BIN"
  chmod +x "$MINIO_BIN"
  echo "MinIO downloaded: $MINIO_BIN"
fi

echo "Starting services with supervisord..."
exec supervisord -c "$APP_DIR/supervisord.conf"
