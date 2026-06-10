#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=/workspace/.env

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Create it before starting."
  exit 1
fi

set -a; source "$ENV_FILE"; set +a

mkdir -p /workspace/logs /workspace/pgdata /workspace/minio-data \
         /workspace/mlflow/artifacts

export PATH="/usr/lib/postgresql/16/bin:/usr/lib/postgresql/15/bin:/usr/lib/postgresql/14/bin:$PATH"
export LLAMA_BIN="${LLAMA_BIN:-/opt/llama-server/llama-server}"
export LLAMA_LIB_DIR="${LLAMA_LIB_DIR:-/opt/llama-server}"

# 1. Fallback llama-server build if the image is missing it
if [ ! -x "$LLAMA_BIN" ]; then
  echo "Building llama-server for RTX 5090 (sm_120) fallback..."
  cd /workspace
  rm -rf /workspace/llama-build
  git clone https://github.com/ggml-org/llama.cpp.git llama-build
  cd /workspace/llama-build

  cmake -B build \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_BUILD_TYPE=Release

  cmake --build build --config Release -j"$(nproc)" --target llama-server

  mkdir -p "$LLAMA_LIB_DIR"
  cp build/bin/llama-server "$LLAMA_BIN"
  find build/bin -maxdepth 1 -name "*.so*" -exec cp {} "$LLAMA_LIB_DIR"/ \;
  chmod +x "$LLAMA_BIN"

  rm -rf /workspace/llama-build
  echo "llama-server build complete."
fi

# 2. Postgres: init once, then start
chown -R postgres:postgres /workspace/pgdata /workspace/logs

if [ ! -s /workspace/pgdata/PG_VERSION ]; then
  echo "Initialising Postgres..."
  runuser -u postgres -- initdb -D /workspace/pgdata -U postgres \
    --auth-local=trust --auth-host=scram-sha-256
fi

runuser -u postgres -- pg_ctl -D /workspace/pgdata \
  -l /workspace/logs/postgres.log \
  -o "-c listen_addresses=127.0.0.1 -p 5432" start

until pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; do sleep 1; done

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

# 3. Export runtime settings for supervisord
export MINIO_ROOT_USER="$MINIO_ACCESS_KEY"
export MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY"

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

if [ ! -f "$MODEL_GGUF" ]; then
  echo "ERROR: MODEL_GGUF not found: $MODEL_GGUF"
  exit 1
fi

if [ ! -f "$MMPROJ_GGUF" ]; then
  echo "ERROR: MMPROJ_GGUF not found: $MMPROJ_GGUF"
  exit 1
fi

# 4. Hand off to supervisord
echo "Starting all services via supervisord..."
exec supervisord -c /app/supervisord.conf
