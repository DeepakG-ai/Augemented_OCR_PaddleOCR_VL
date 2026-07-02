# Single-port gateway (`:8000`)

Goal: expose **only** RunPod port **8000**. nginx (already running on this pod)
listens on 8000 and routes by URL path prefix to each app, which keep running on
their own *internal-only* ports.

## Routing map

| Public URL (only `:8000` exposed)              | Internal backend     | App dir                                          |
|------------------------------------------------|----------------------|--------------------------------------------------|
| `…-8000.proxy.runpod.net/aug-ocr/`             | `127.0.0.1:8100` *(was 8000)* | `/workspace/app`                        |
| `…-8000.proxy.runpod.net/sale-sentiment/`       | `127.0.0.1:8024`     | `/workspace/sales_sentiment/Sentiment-Analysis`  |
| `…-8000.proxy.runpod.net/resume-matcher/`      | `127.0.0.1:8025`     | `/workspace/Resume_Matcher`                      |
| `…-8000.proxy.runpod.net/tone-analysis/`       | `127.0.0.1:8026`     | `/workspace/Tone-Analysis`                       |
| `…-8000.proxy.runpod.net/chatbot/`             | `127.0.0.1:8048`     | `/workspace/chatbot`                             |
| `…-8000.proxy.runpod.net/datacleaning/`        | `127.0.0.1:8084`     | `/workspace/datacleaning`                        |

After applying, you can **un-expose 8024, 8025, 8026, 8048, 8084** in the RunPod
pod settings and keep only **8000**.

## How it works

- nginx `proxy_pass …/` with a trailing slash **strips** the path prefix, so each
  app still sees `/`-rooted requests and needs no route changes.
- `sub_filter` rewrites absolute asset links (`/static/…`, `/api/…`, `href/src/action="/…"`)
  in HTML/CSS/JS responses to include the prefix — so the web frontends load.
- `X-Forwarded-Prefix` + each app's `root_path` keep `/docs` and redirects correct.

## Apply

```bash
bash /workspace/gateway/apply.sh
```

This patches `supervisord.conf` (aug-ocr 8000→8100 + `--root-path /aug-ocr`),
adds the `include` to `/etc/nginx/nginx.conf`, validates, and reloads nginx.
It is idempotent and backs up both files before editing.

## ⚠️ Persistence across pod restarts

`/etc/nginx/nginx.conf` lives in the **container image**, not in `/workspace`, so
the `include` is **lost when the pod restarts**. Re-run `apply.sh` after each
restart (e.g. add it to your pod start command, after the apps are launched).

## Recommended per-app `root_path` (optional but cleaner for `/docs`)

The gateway works via `sub_filter` even without these, but setting `root_path`
makes each app's own generated links/redirects/Swagger correct:

| App            | How it launches                | Edit                                                        |
|----------------|--------------------------------|-------------------------------------------------------------|
| aug-ocr        | supervisord uvicorn            | handled by `apply.sh` (`--root-path /aug-ocr`)              |
| sale-sentiment  | `uvicorn app.main:app`         | add `--root-path /sale-sentiment` to its launch command      |
| resume-matcher | `uvicorn api_server.main:app`  | add `--root-path /resume-matcher` to its launch command     |
| tone-analysis  | `python3 main.py`              | `FastAPI(..., root_path="/tone-analysis")` in `main.py`      |
| chatbot        | `python launcher.py`           | `uvicorn.run(..., root_path="/chatbot")` in `launcher.py`    |
| datacleaning   | `python main.py`               | `FastAPI(..., root_path="/datacleaning")` in `main.py`       |

## Caveats

- `sub_filter` only rewrites links present in HTML/CSS/JS text. URLs **built at
  runtime in JavaScript** (string concatenation, `fetch(API_BASE + …)`) may still
  hit the wrong path — fix those apps to use a relative base or `root_path`.
- WebSockets are handled (the shared `snippets/nginx-proxy.conf` upgrades the
  connection).

## Rollback

```bash
# restore the most recent backups
cp /etc/nginx/nginx.conf.bak.* /etc/nginx/nginx.conf      # pick the right one
cp /workspace/app/supervisord.conf.bak.* /workspace/app/supervisord.conf
nginx -t && nginx -s reload
supervisorctl -c /workspace/app/supervisord.conf restart api
```
