#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/workspace/app}"
ENV_FILE="${ENV_FILE:-/workspace/.env}"
PGDATA="${PGDATA:-/workspace/pgdata}"
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

mkdir -p "$LOG_DIR" "$PGDATA" /workspace/minio-data /workspace/mlflow/artifacts

export APP_DIR
export VENV_DIR
export PGDATA
export LOG_DIR
export PATH="$VENV_DIR/bin:/usr/lib/postgresql/16/bin:/usr/lib/postgresql/15/bin:/usr/lib/postgresql/14/bin:$PATH"
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

for bin in runuser initdb pg_ctl pg_isready psql createdb curl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found. Run: apt-get update && apt-get install -y postgresql postgresql-contrib curl"
    exit 1
  fi
done

if ! "$VENV_DIR/bin/python" -c "import uvicorn, mlflow" >/dev/null 2>&1; then
  echo "ERROR: venv at $VENV_DIR is missing required packages (uvicorn/mlflow)."
  echo "Run: $VENV_DIR/bin/pip install -r $APP_DIR/backend/requirements.txt"
  exit 1
fi

echo "Starting Postgres..."
chown -R postgres:postgres "$PGDATA" "$LOG_DIR"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  runuser -u postgres -- initdb -D "$PGDATA" -U postgres --auth-local=trust --auth-host=scram-sha-256
fi

if pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; then
  echo "Postgres is already running."
else
  if [ -f "$PGDATA/postmaster.pid" ]; then
    POSTMASTER_PID="$(head -n 1 "$PGDATA/postmaster.pid" || true)"
    # In a recreated container PIDs restart from 1, so the recorded PID usually
    # belongs to an unrelated live process — kill -0 alone would wrongly keep
    # the pid file. Only keep it when that PID is actually postgres.
    if [ -z "$POSTMASTER_PID" ] || [ "$(cat "/proc/$POSTMASTER_PID/comm" 2>/dev/null)" != "postgres" ]; then
      echo "Removing stale Postgres pid file."
      rm -f "$PGDATA/postmaster.pid"
    fi
  fi

  runuser -u postgres -- pg_ctl -D "$PGDATA" \
    -l "$LOG_DIR/postgres.log" \
    -o "-c listen_addresses=127.0.0.1 -p 5432" \
    start
fi

for i in $(seq 1 30); do
  if pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: Postgres did not become ready within 30s. Last lines of postgres.log:"
    tail -n 40 "$LOG_DIR/postgres.log" || true
    exit 1
  fi
  sleep 1
done

SQL_PW="$(printf '%s' "$POSTGRES_PASSWORD" | sed "s/'/''/g")"

ROLE="$(runuser -u postgres -- psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='augocr'")"
if [ "$ROLE" != "1" ]; then
  runuser -u postgres -- psql -U postgres -c "CREATE ROLE augocr LOGIN PASSWORD '$SQL_PW';"
else
  runuser -u postgres -- psql -U postgres -c "ALTER ROLE augocr WITH PASSWORD '$SQL_PW';"
fi

DB="$(runuser -u postgres -- psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='augocr'")"
if [ "$DB" != "1" ]; then
  runuser -u postgres -- createdb -U postgres -O augocr augocr
fi

export MINIO_ROOT_USER="$MINIO_ACCESS_KEY"
export MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY"
export MINIO_BIN="${MINIO_BIN:-/workspace/bin/minio}"

if [ ! -x "$MINIO_BIN" ]; then
  echo "ERROR: MinIO binary not found or not executable: $MINIO_BIN"
  echo "Install it with: wget https://dl.min.io/server/minio/release/linux-amd64/minio -O /workspace/bin/minio && chmod +x /workspace/bin/minio"
  exit 1
fi

echo "Starting services with supervisord..."
exec supervisord -c "$APP_DIR/supervisord.conf"
