#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/workspace/app}"
ENV_FILE="${ENV_FILE:-/workspace/.env}"
# Postgres is now external (AWS) — see DATABASE_URL in /workspace/.env.
# No local Postgres and no workspace backup are started by this script.
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
  echo "Create it with: python3 -m venv $VENV_DIR && source $VENV_DIR/bin/activate && pip install -r $APP_DIR/backend/requirements.txt"
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
# Persist CUDA JIT-compiled kernels on the persistent volume so each pod restart
# doesn't recompile from scratch (which takes 40-70s per worker on first GPU use).
export CUDA_CACHE_PATH="${CUDA_CACHE_PATH:-/workspace/paddleocr/cuda_cache}"
mkdir -p "$PADDLE_PDX_CACHE_HOME" "$MODELSCOPE_CACHE" "$PADDLE_HOME" "$CUDA_CACHE_PATH"
export MODEL_GGUF="${MODEL_GGUF:-/workspace/models/qwen3.5/Qwen3.5-9B-UD-Q4_K_XL.gguf}"
export MMPROJ_GGUF="${MMPROJ_GGUF:-/workspace/models/qwen3.5/mmproj-F16.gguf}"
export LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
export LLAMA_PORT="${LLAMA_PORT:-8056}"
export LLAMA_N_GPU_LAYERS="${LLAMA_N_GPU_LAYERS:-99}"
export LLAMA_CTX_SIZE="${LLAMA_CTX_SIZE:-8192}"
export LLAMA_BATCH_SIZE="${LLAMA_BATCH_SIZE:-4096}"
export LLAMA_PARALLEL="${LLAMA_PARALLEL:-1}"
export LLAMA_IMAGE_MIN_TOKENS="${LLAMA_IMAGE_MIN_TOKENS:-1024}"
export LLAMA_IMAGE_MAX_TOKENS="${LLAMA_IMAGE_MAX_TOKENS:-2048}"
export LLM_PAGE_BATCH_SIZE="${LLM_PAGE_BATCH_SIZE:-$LLAMA_PARALLEL}"

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

if ! command -v supervisord >/dev/null 2>&1; then
  echo "ERROR: supervisord is not installed. Run: apt-get update && apt-get install -y supervisor"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: 'curl' not found. Run: apt-get update && apt-get install -y curl"
  exit 1
fi

if ! "$VENV_DIR/bin/python" -c "import uvicorn, mlflow" >/dev/null 2>&1; then
  echo "ERROR: venv at $VENV_DIR is missing required packages (uvicorn/mlflow)."
  echo "Run: $VENV_DIR/bin/pip install -r $APP_DIR/backend/requirements.txt"
  exit 1
fi

echo "Using external Postgres (DATABASE_URL from .env) — no local Postgres started."

export MINIO_ROOT_USER="$MINIO_ACCESS_KEY"
export MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY"
export MINIO_BIN="${MINIO_BIN:-/workspace/bin/minio}"

if [ ! -x "$MINIO_BIN" ]; then
  echo "ERROR: MinIO binary not found or not executable: $MINIO_BIN"
  echo "Install it with: wget https://dl.min.io/server/minio/release/linux-amd64/minio -O /workspace/bin/minio && chmod +x /workspace/bin/minio"
  exit 1
fi

# --- Clean restart -----------------------------------------------------------
# Running start.sh again should be a RESTART, not a duplicate. Stop any existing
# supervisord, then reap orphaned services so their ports (minio 9000, llama
# 8056, api 8000) are free for the fresh start. (A force-killed supervisord can
# leave children running and holding their ports.)
SUPERVISORD_PID_FILE="${SUPERVISORD_PID_FILE:-/workspace/supervisord.pid}"
OLD_PID="$(cat "$SUPERVISORD_PID_FILE" 2>/dev/null || true)"
if [ -z "${OLD_PID:-}" ]; then
  OLD_PID="$(pgrep -f 'supervisord -c .*supervisord.conf' | head -1 || true)"
fi
if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
  echo "Stopping existing supervisord (pid $OLD_PID)..."
  kill -TERM "$OLD_PID" 2>/dev/null || true
  for _i in $(seq 1 15); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 1; done
  kill -KILL "$OLD_PID" 2>/dev/null || true
fi

echo "Reaping any orphaned services..."
for _pat in "/workspace/bin/minio server" "llama-server -m" "uvicorn backend.main" "backend.worker --stage" "mlflow server"; do
  pkill -f "$_pat" 2>/dev/null || true
done
rm -f "$SUPERVISORD_PID_FILE" /workspace/supervisor.sock
sleep 2

echo "Starting services with supervisord..."
exec supervisord -c "$APP_DIR/supervisord.conf"
