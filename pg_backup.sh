#!/usr/bin/env bash
set -euo pipefail

PG_BACKUP_DIR="${PG_BACKUP_DIR:-/workspace/backups/postgres}"
PG_BACKUP_INTERVAL="${PG_BACKUP_INTERVAL:-300}"
POSTGRES_DB="${POSTGRES_DB:-augocr}"

mkdir -p "$PG_BACKUP_DIR"

echo "[pg-backup] Starting. interval=${PG_BACKUP_INTERVAL}s db=${POSTGRES_DB} dir=${PG_BACKUP_DIR}"

# Initial delay — give Postgres, the schema migration, and the app time to
# fully start before attempting the first dump.
sleep 60

while true; do
  TIMESTAMP="$(date '+%Y-%m-%dT%H:%M:%S')"
  TMP_FILE="$PG_BACKUP_DIR/augocr_latest.dump.tmp"
  LATEST="$PG_BACKUP_DIR/augocr_latest.dump"
  PREVIOUS="$PG_BACKUP_DIR/augocr_previous.dump"

  if pg_dump -U postgres -Fc "$POSTGRES_DB" > "$TMP_FILE"; then
    # Rotate before replacing so a crash mid-mv never loses both copies.
    [ -f "$LATEST" ] && mv "$LATEST" "$PREVIOUS"
    mv "$TMP_FILE" "$LATEST"
    SIZE="$(du -sh "$LATEST" | cut -f1)"
    echo "[$TIMESTAMP] backup OK — $SIZE written to $LATEST"
  else
    echo "[$TIMESTAMP] ERROR: pg_dump failed — previous backup preserved"
    rm -f "$TMP_FILE"
  fi

  sleep "$PG_BACKUP_INTERVAL"
done
