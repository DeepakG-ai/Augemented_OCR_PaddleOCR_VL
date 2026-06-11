#!/usr/bin/env bash
# Tail every service log as one combined, labelled stream.
# Each line is prefixed with [service] so you can read one feed instead of
# juggling separate files. Ctrl+C to stop.
#
# Usage:
#   bash /workspace/app/logs.sh            # all services
#   bash /workspace/app/logs.sh api llama  # only matching services

LOG_DIR="${LOG_DIR:-/workspace/logs}"

if [ "$#" -gt 0 ]; then
  files=()
  for name in "$@"; do
    for f in "$LOG_DIR/$name"*.log; do
      [ -f "$f" ] && files+=("$f")
    done
  done
else
  files=("$LOG_DIR"/*.log)
fi

if [ "${#files[@]}" -eq 0 ]; then
  echo "No matching log files in $LOG_DIR"
  exit 1
fi

# tail -F follows rotation; --quiet hides the ==> file <== banners; we add our
# own [service] prefix via awk so every line shows which service it came from.
tail -n 20 -F "${files[@]}" 2>/dev/null \
  | awk '
    /^==> / { svc = $2; sub(/.*\//, "", svc); sub(/\.log <==$/, "", svc); next }
    { printf "[%s] %s\n", svc, $0 }
  '
