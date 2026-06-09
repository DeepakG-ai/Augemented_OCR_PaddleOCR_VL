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

# ── 1. Build llama-server on first boot ────────────────────────────────────
if [ ! -f /workspace/llama-server/llama-server ]; then
  echo "Building llama-server for RTX 5090 (sm_120) — first boot only..."
  cd /workspace
  git clone https://github.com/ggml-org/llama.cpp.git llama-build
  cd /workspace/llama-build

  cmake -B build \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_BUILD_TYPE=Release

  cmake --build build --config Release -j"$(nproc)" --target llama-server

  mkdir -p /workspace/llama-server
  cp build/bin/llama-server   /workspace/llama-server/
  cp build/bin/libggml*.so*   /workspace/llama-server/
  cp build/bin/libllama.so    /workspace/llama-server/
  chmod +x /workspace/llama-server/llama-server

  rm -rf /workspace/llama-build
  echo "llama-server build complete."
fi

# ── 2. Postgres — init once, then start ────────────────────────────────────
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
fi

DB="$(runuser -u postgres -- psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='augocr'")"
if [ "$DB" != "1" ]; then
  runuser -u postgres -- createdb -U postgres -O augocr augocr
fi

# ── 3. Export MinIO credentials for supervisord ─────────────────────────────
export MINIO_ROOT_USER="$MINIO_ACCESS_KEY"
export MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY"

# ── 4. Hand off to supervisord ──────────────────────────────────────────────
echo "Starting all services via supervisord..."
exec supervisord -c /app/supervisord.conf
