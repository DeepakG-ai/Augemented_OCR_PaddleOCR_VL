#!/usr/bin/env bash
# =============================================================================
#  apply.sh  —  wire up the single-port (8000) gateway
# =============================================================================
#  What it does (idempotent):
#    1. Moves the aug-ocr API off port 8000 -> 8100 (+ adds --root-path /aug-ocr)
#       in /workspace/app/supervisord.conf, and restarts it via supervisorctl.
#    2. Adds `include /workspace/gateway/nginx-gateway.conf;` inside the http{}
#       block of /etc/nginx/nginx.conf (backs the file up first).
#    3. Runs `nginx -t` and, only if valid, reloads nginx.
#
#  Re-run safe. Run AFTER every pod restart (nginx.conf lives in the image, not
#  in /workspace, so the include is lost on restart — see README.md).
# =============================================================================
set -euo pipefail

GATEWAY_CONF="/workspace/gateway/nginx-gateway.conf"
NGINX_CONF="/etc/nginx/nginx.conf"
SUPERVISORD_CONF="/workspace/app/supervisord.conf"

echo "==> 1/3  Move aug-ocr 8000 -> 8100 (+ root-path) in supervisord.conf"
if grep -q -- '--port 8000 --no-access-log' "$SUPERVISORD_CONF"; then
    cp -a "$SUPERVISORD_CONF" "$SUPERVISORD_CONF.bak.$(date +%s)"
    sed -i 's#--port 8000 --no-access-log#--port 8100 --root-path /aug-ocr --no-access-log#' "$SUPERVISORD_CONF"
    echo "    patched. restarting api worker..."
    supervisorctl -c "$SUPERVISORD_CONF" update >/dev/null 2>&1 || true
    supervisorctl -c "$SUPERVISORD_CONF" restart api || echo "    (restart api manually if supervisorctl is unavailable)"
else
    echo "    already moved (no '--port 8000' found) — skipping."
fi

echo "==> 2/3  Add include to nginx http{} block"
if grep -qF "$GATEWAY_CONF" "$NGINX_CONF"; then
    echo "    include already present — skipping."
else
    cp -a "$NGINX_CONF" "$NGINX_CONF.bak.$(date +%s)"
    # Insert the include just before the final closing brace (end of http{}).
    awk -v inc="    include $GATEWAY_CONF;" '
        { lines[NR]=$0 }
        END {
            last=NR
            while (last>0 && lines[last] !~ /[^[:space:]]/) last--   # skip trailing blanks
            for (i=1;i<=NR;i++) { if (i==last) print inc; print lines[i] }
        }' "$NGINX_CONF" > "$NGINX_CONF.tmp" && mv "$NGINX_CONF.tmp" "$NGINX_CONF"
    echo "    inserted."
fi

echo "==> 3/3  Validate + reload nginx"
if nginx -t; then
    nginx -s reload && echo "    nginx reloaded OK."
else
    echo "    !! nginx config invalid — NOT reloading. Restore from the .bak file."
    exit 1
fi

echo "==> 4/4  Ensure supervisord is running and up-to-date"
if ! pgrep -f 'supervisord -c .*supervisord.conf' >/dev/null; then
    echo "    supervisord is not running. Starting it in the background..."
    nohup bash /workspace/app/start.sh >/workspace/logs/supervisord.start.log 2>&1 &
    # Wait for supervisord socket to become available
    for i in {1..30}; do
        if [ -S /workspace/supervisor.sock ]; then
            echo "    supervisord started successfully."
            break
        fi
        sleep 1
    done
    if [ ! -S /workspace/supervisor.sock ]; then
        echo "    WARNING: supervisord socket did not appear after 30 seconds. Check /workspace/logs/supervisord.start.log"
    fi
else
    echo "    supervisord is already running. Updating configurations..."
    supervisorctl -c "$SUPERVISORD_CONF" update
fi

echo
echo "Done. Only port 8000 needs to stay exposed in RunPod now."
echo "Test:  https://<podid>-8000.proxy.runpod.net/aug-ocr/"
