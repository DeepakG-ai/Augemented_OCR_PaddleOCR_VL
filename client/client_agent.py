"""
client/client_agent.py — AugmentedOCR desktop agent.

Runs as a standalone exe on the user's Windows machine.
Communicates with the OCR server (AWS Docker) via its REST API.

On startup it prints a small banner and prompts for email + password in the
terminal.  The password is hidden while typing.  Credentials are kept in
memory only — never written to disk.

Auth
----
Calls POST /auth/login with email + password to get a JWT (8 h TTL).
A background thread re-logs in proactively every 7 hours so the token never
expires mid-operation.  Any 401 response triggers an immediate re-login.

Folder config
-------------
The four folder paths (input / output / success / failed) are read from
GET /api/config — the same values the user sets in the Settings page on the
server.  A background thread re-fetches config every 60 s so folder changes
made in the UI take effect without restarting the exe.

Heartbeat
---------
POST /api/client/heartbeat is called every 30 s.  The server uses this to
show ACTIVE / INACTIVE on the Settings page.

PDF lifecycle
-------------
  1. Watchdog detects a new PDF in input_folder.
  2. Queues it locally until an enabled SaaS schedule's next_run time is due.
  3. Waits for the file to finish writing (size stable for 1 s).
  4. POST /ingest/rest  ->  { job_id, extraction_id }
  5. GET  /jobs/{job_id}/stream  (SSE)  ->  progress ... -> terminal status
  6. Writes <stem>.json to output_folder (always, even on failure).
  7. Moves PDF to success_folder (done) or failed_folder (failed / partial / cancelled / unverified / timeout).

Local config file  ->  client_agent.json  (next to this exe):
  {
    "server_url": "http://your-aws-server:8000"
  }

Env-var overrides: AUGOCR_SERVER_URL, AUGOCR_EMAIL, AUGOCR_PASSWORD
CLI overrides:     --server, --email, --password, --config, --log-dir
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import requests
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

# ── Logging — same column format as backend/logging_config.py ────────────────

_FMT = "%(asctime)s.%(msecs)03d  %(levelname)-5s  %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("client_agent")


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    rotate = logging.handlers.RotatingFileHandler(
        str(log_dir / "client_agent.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotate.setFormatter(fmt)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(console)
    root.addHandler(rotate)
    root.setLevel(logging.INFO)


def _kv(**pairs: Any) -> str:
    parts = []
    for k, v in pairs.items():
        if v is None:
            continue
        s = str(v)
        if " " in s and not (s.startswith('"') and s.endswith('"')):
            s = f'"{s}"'
        parts.append(f"{k}={s}")
    return ("  | " + " ".join(parts)) if parts else ""


# ── Token manager — login, in-memory storage, auto-refresh ───────────────────

_TOKEN_REFRESH_SECS = 7 * 3600  # re-login 1 h before the 8 h JWT expires


class _TokenManager:
    def __init__(self, server_url: str, email: str, password: str) -> None:
        self._url = server_url.rstrip("/") + "/auth/login"
        self._email = email
        self._password = password
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def login(self) -> bool:
        try:
            resp = requests.post(
                self._url,
                json={"email": self._email, "password": self._password},
                timeout=15,
            )
            if resp.status_code == 200:
                tok = resp.json().get("access_token")
                if tok:
                    with self._lock:
                        self._token = tok
                        self._expires_at = time.monotonic() + _TOKEN_REFRESH_SECS
                    logger.info("login ok%s", _kv(email=self._email))
                    return True
            logger.error(
                "login failed%s",
                _kv(status=resp.status_code, detail=resp.text[:120]),
            )
        except Exception as exc:
            logger.error("login error%s", _kv(exc=str(exc)[:120]))
        return False

    def get(self) -> str | None:
        with self._lock:
            if time.monotonic() < self._expires_at:
                return self._token
        return None

    def invalidate(self) -> None:
        with self._lock:
            self._expires_at = 0.0

    def needs_refresh(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._expires_at


# ── Authenticated API call with one automatic re-login on 401 ─────────────────

def _call(
    token_mgr: _TokenManager,
    method: str,
    url: str,
    **kwargs: Any,
) -> requests.Response | None:
    # Pop caller-supplied headers once so kwargs stays clean across both attempts.
    extra_headers = dict(kwargs.pop("headers", {}))
    for attempt in range(2):
        tok = token_mgr.get()
        if tok is None:
            if not token_mgr.login():
                return None
            tok = token_mgr.get()

        headers = dict(extra_headers)  # fresh copy each attempt
        headers["Authorization"] = f"Bearer {tok}"
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                logger.warning("401 received — re-logging in")
                token_mgr.invalidate()
                token_mgr.login()
                continue
            return resp
        except requests.exceptions.RequestException as exc:
            logger.error("request error%s", _kv(url=url, exc=str(exc)[:120]))
            return None
    return None


# ── File utilities ────────────────────────────────────────────────────────────

def _wait_stable(path: Path, stable_secs: float = 1.0, timeout_secs: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_secs
    last_size = -1
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(0.25)
            continue
        if size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_secs:
                return True
        else:
            last_size = size
            stable_since = None
        time.sleep(0.25)
    return False


def _move(src: Path, dest_folder: str, label: str) -> None:
    if not dest_folder:
        logger.warning("move skipped: %s_folder not configured%s", label, _kv(file=src.name))
        return
    try:
        dest = Path(dest_folder)
        dest.mkdir(parents=True, exist_ok=True)
        dest_path = _unique_dest(dest, src.name)
        shutil.move(str(src), str(dest_path))
        logger.info("moved to %s%s", label, _kv(file=dest_path.name, folder=dest_folder))
    except Exception as exc:
        logger.error("move failed%s", _kv(file=src.name, label=label, exc=str(exc)[:120]))


def _unique_dest(dest_folder: Path, filename: str) -> Path:
    candidate = dest_folder / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        candidate = dest_folder / f"{stem}__{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find free destination name for {filename}")


def _pdfs_in(folder: Path) -> List[Path]:
    pdfs = {str(p).casefold(): p for p in folder.glob("*.pdf")}
    pdfs.update({str(p).casefold(): p for p in folder.glob("*.PDF")})
    return sorted(pdfs.values(), key=lambda p: str(p).casefold())


def _format_extraction_for_output(extraction: Any) -> Any:
    """Convert internal result JSON to the customer-facing output shape."""
    if isinstance(extraction, list):
        return [_format_extraction_for_output(item) for item in extraction]
    if not isinstance(extraction, dict):
        return extraction

    source_items = extraction.get("line_items")
    if not isinstance(source_items, list):
        source_items = extraction.get("items") if isinstance(extraction.get("items"), list) else []

    out: Dict[str, Any] = {}
    for key, value in extraction.items():
        if key in {"line_items", "items"} or str(key).startswith("_"):
            continue
        out[key] = value

    out["items"] = [
        {
            key: value
            for key, value in item.items()
            if not str(key).startswith("_")
        } if isinstance(item, dict) else item
        for item in source_items
    ]
    return out


def _write_output(
    output_folder: str,
    pdf_name: str,
    *,
    status: str,
    extraction: Any = None,
    error: str | None = None,
) -> None:
    """Always write a result JSON to output_folder regardless of success or failure."""
    payload: Dict[str, Any] = {
        "status": status,
        "file": pdf_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extraction is not None:
        payload["extraction"] = _format_extraction_for_output(extraction)
    if error:
        payload["error"] = error

    if not output_folder:
        logger.warning("output_folder not configured — result not written%s", _kv(file=pdf_name))
        return
    try:
        dest = Path(output_folder)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / (Path(pdf_name).stem + ".json")
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        logger.info("output written%s", _kv(file=out_path.name, status=status))
    except Exception as exc:
        logger.error("output write failed%s", _kv(file=pdf_name, exc=str(exc)[:120]))


# ── SSE line parser ───────────────────────────────────────────────────────────

def _iter_sse(response: requests.Response):
    for raw in response.iter_lines():
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw.startswith("data: "):
            try:
                yield json.loads(raw[6:])
            except json.JSONDecodeError:
                pass


# ── Schedule gate — holds uploads until a schedule time is due ────────────────

_SCHED_POLL_SECS = 10


class _ScheduleState:
    """Thread-safe queue for local PDFs controlled by SaaS schedule times."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: List[Path] = []
        self._pending_keys: Set[str] = set()
        self._seen: Set[str] = set()
        self._schedules: List[dict] = []
        self._executed_slots: Set[Tuple[int, datetime]] = set()
        self._batch_running = False
        self._has_enabled_schedules = False

    @property
    def has_enabled_schedules(self) -> bool:
        with self._lock:
            return self._has_enabled_schedules

    def queue(self, path: Path) -> bool:
        key = str(path).casefold()
        with self._lock:
            if key in self._pending_keys:
                return False
            self._pending.append(path)
            self._pending_keys.add(key)
            return True

    def mark_seen(self, path: Path) -> bool:
        """Mark a path as seen. Returns True if it was NOT already seen."""
        key = str(path).casefold()
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def remove_seen(self, path: Path) -> None:
        """Remove a path from seen so it can be processed again in the future."""
        key = str(path).casefold()
        with self._lock:
            self._seen.discard(key)

    def update_from_server(self, schedules: List[dict]) -> Tuple[int, int]:
        """Refresh enabled schedule definitions. Returns (enabled, tracked)."""
        enabled = [s for s in schedules if s.get("enabled")]
        parsed_schedules = []
        for sched in enabled:
            try:
                sid = int(sched["id"])
                hour = int(sched["utc_hour"])
                minute = int(sched["utc_minute"])
            except (ValueError, TypeError, KeyError):
                continue

            last_ran_str = sched.get("last_ran_at")
            last_ran = None
            if last_ran_str:
                try:
                    last_ran = datetime.fromisoformat(last_ran_str.replace("Z", "+00:00"))
                    if last_ran.tzinfo is None:
                        last_ran = last_ran.replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            parsed_schedules.append({
                "id": sid,
                "utc_hour": hour,
                "utc_minute": minute,
                "last_ran_at": last_ran,
            })

        with self._lock:
            self._has_enabled_schedules = bool(enabled)
            self._schedules = parsed_schedules

            # Housekeep self._executed_slots to avoid growing indefinitely.
            # Keep only slots within the last 2 days.
            two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
            self._executed_slots = {
                item for item in self._executed_slots
                if item[1] >= two_days_ago
            }

            return len(enabled), len(parsed_schedules)

    def claim_due_batch(self) -> Tuple[str, List[Path], List[int]]:
        """Claim one due schedule batch.

        State is one of: none, empty, busy, start. Due schedules are consumed
        immediately, so a later schedule does not start while a prior batch runs.
        """
        now = datetime.now(timezone.utc)
        due_sids = []

        with self._lock:
            for sched in self._schedules:
                sid = sched["id"]
                hour = sched["utc_hour"]
                minute = sched["utc_minute"]
                last_ran = sched["last_ran_at"]

                # Calculate the most recent scheduled time for today
                today_sched = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now >= today_sched:
                    last_scheduled_time = today_sched
                else:
                    last_scheduled_time = today_sched - timedelta(days=1)

                # Check if this slot was already executed in this session
                if (sid, last_scheduled_time) in self._executed_slots:
                    continue

                # Check if this slot was already run on the server (according to DB last_ran_at)
                if last_ran is not None and last_ran >= last_scheduled_time:
                    continue

                # It is due!
                due_sids.append(sid)
                self._executed_slots.add((sid, last_scheduled_time))

            if not due_sids:
                return "none", [], []

            if self._batch_running:
                return "busy", [], due_sids

            items = list(self._pending)
            self._pending.clear()
            self._pending_keys.clear()
            if not items:
                return "empty", [], due_sids

            self._batch_running = True
            return "start", items, due_sids

    def finish_batch(self) -> None:
        with self._lock:
            self._batch_running = False


_UPLOAD_RETRY_DELAYS = (3, 10, 30)
_SSE_TIMEOUT_SECS = 600  # 10 min covers the full pipeline


def _process_pdf(
    base_url: str,
    token_mgr: _TokenManager,
    folders: Dict[str, str],
    pdf_path: Path,
) -> None:
    t0 = time.perf_counter()

    if not _wait_stable(pdf_path):
        logger.warning("skipped: file not stable%s", _kv(file=pdf_path.name))
        _write_output(folders["output"], pdf_path.name, status="failed",
                      error="file did not stabilise before upload")
        _move(pdf_path, folders["failed"], "failed")
        return

    # ── 1. Upload ─────────────────────────────────────────────────────────────
    # File uploads are handled directly here (not via _call) so the file handle
    # is kept inside a with-block and a 401 retry can re-open the file fresh.
    job_id: int | None = None

    def _do_upload(tok: str) -> requests.Response:
        with pdf_path.open("rb") as fh:
            return requests.post(
                base_url + "/ingest/rest",
                headers={"Authorization": f"Bearer {tok}"},
                files={"file": (pdf_path.name, fh, "application/pdf")},
                timeout=60,
            )

    for attempt, delay in enumerate((*_UPLOAD_RETRY_DELAYS, None), start=1):
        tok = token_mgr.get()
        if tok is None:
            if not token_mgr.login():
                _write_output(folders["output"], pdf_path.name, status="failed",
                              error="authentication failed before upload")
                _move(pdf_path, folders["failed"], "failed")
                return
            tok = token_mgr.get()

        try:
            resp = _do_upload(tok)
            # Re-login once on 401 and retry with a fresh file open.
            if resp.status_code == 401:
                logger.warning("401 on upload — re-logging in")
                token_mgr.invalidate()
                if not token_mgr.login():
                    _write_output(folders["output"], pdf_path.name, status="failed",
                                  error="re-authentication failed during upload")
                    _move(pdf_path, folders["failed"], "failed")
                    return
                tok = token_mgr.get()
                resp = _do_upload(tok)
        except requests.exceptions.RequestException as exc:
            if delay is None:
                logger.error("upload failed: all retries exhausted%s", _kv(file=pdf_path.name))
                _write_output(folders["output"], pdf_path.name, status="failed",
                              error="upload failed after all retries")
                _move(pdf_path, folders["failed"], "failed")
                return
            logger.warning("upload error, retrying%s", _kv(file=pdf_path.name, attempt=attempt,
                                                            exc=str(exc)[:80]))
            time.sleep(delay)
            continue

        if resp.status_code in (200, 201, 202):
            data = resp.json()
            job_id = data.get("job_id")
            logger.info(
                "upload ok%s",
                _kv(file=pdf_path.name, job_id=job_id,
                    extraction_id=data.get("extraction_id")),
            )
            break

        if resp.status_code == 409:
            msg = "vendor not registered — add it in the server UI first"
        elif resp.status_code == 402:
            msg = "subscription quota exceeded"
        elif resp.status_code == 401:
            msg = "authentication failed — check credentials"
        else:
            msg = f"server rejected upload (HTTP {resp.status_code})"

        logger.error("upload rejected: %s%s", msg, _kv(file=pdf_path.name))
        _write_output(folders["output"], pdf_path.name, status="failed", error=msg)
        _move(pdf_path, folders["failed"], "failed")
        return

    if job_id is None:
        logger.error("upload response missing job_id%s", _kv(file=pdf_path.name))
        _write_output(folders["output"], pdf_path.name, status="failed",
                      error="server did not return a job_id")
        _move(pdf_path, folders["failed"], "failed")
        return

    # ── 2. Stream pipeline progress ───────────────────────────────────────────
    stream_url = f"{base_url}/jobs/{job_id}/stream"
    tok = token_mgr.get()
    if tok is None:
        token_mgr.login()
        tok = token_mgr.get()

    try:
        with requests.get(
            stream_url,
            headers={"Authorization": f"Bearer {tok}"},
            stream=True,
            timeout=_SSE_TIMEOUT_SECS,
        ) as stream_resp:
            if stream_resp.status_code != 200:
                msg = f"SSE stream returned HTTP {stream_resp.status_code}"
                logger.error("%s%s", msg, _kv(file=pdf_path.name, job_id=job_id))
                _write_output(folders["output"], pdf_path.name, status="failed", error=msg)
                _move(pdf_path, folders["failed"], "failed")
                return

            for event in _iter_sse(stream_resp):
                evt = event.get("event")
                extraction = event.get("extraction") or {}
                result = extraction.get("result")
                elapsed_ms = (time.perf_counter() - t0) * 1000

                if evt == "progress":
                    logger.info(
                        "progress%s",
                        _kv(file=pdf_path.name, job_id=job_id,
                            status=extraction.get("status"),
                            progress=str(extraction.get("progress", ""))[:60]),
                    )
                    continue

                if evt == "done":
                    logger.info(
                        "done%s",
                        _kv(file=pdf_path.name, job_id=job_id, ms=f"{elapsed_ms:.0f}ms"),
                    )
                    _write_output(folders["output"], pdf_path.name,
                                  status="done", extraction=result)
                    _move(pdf_path, folders["success"], "success")
                    return

                # "failed" or "partial" — both go to failed_folder
                logger.warning(
                    "pipeline %s%s",
                    evt,
                    _kv(file=pdf_path.name, job_id=job_id, ms=f"{elapsed_ms:.0f}ms"),
                )
                error_msg = (extraction.get("error") or extraction.get("progress") or evt)
                _write_output(folders["output"], pdf_path.name,
                              status="failed", extraction=result,
                              error=str(error_msg)[:300])
                _move(pdf_path, folders["failed"], "failed")
                return

            # If the loop finished without returning, the stream terminated prematurely
            msg = "SSE stream ended prematurely without a final status event"
            logger.error("%s%s", msg, _kv(file=pdf_path.name, job_id=job_id))
            _write_output(folders["output"], pdf_path.name, status="failed", error=msg)
            _move(pdf_path, folders["failed"], "failed")
            return

    except requests.exceptions.Timeout:
        msg = f"SSE stream timed out after {_SSE_TIMEOUT_SECS}s"
        logger.error("%s%s", msg, _kv(file=pdf_path.name, job_id=job_id))
        _write_output(folders["output"], pdf_path.name, status="failed", error=msg)
        _move(pdf_path, folders["failed"], "failed")
    except Exception as exc:
        msg = f"SSE stream error: {exc}"
        logger.error("%s%s", str(exc)[:120], _kv(file=pdf_path.name, job_id=job_id))
        _write_output(folders["output"], pdf_path.name, status="failed", error=msg)
        _move(pdf_path, folders["failed"], "failed")


# ── Watchdog handler ──────────────────────────────────────────────────────────

class _PDFHandler(FileSystemEventHandler):
    def __init__(
        self,
        base_url: str,
        token_mgr: _TokenManager,
        folders_ref: dict,
        sched_state: _ScheduleState,
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._token_mgr = token_mgr
        self._folders_ref = folders_ref
        self._sched_state = sched_state

    def _dispatch(self, path: str) -> None:
        if not path.lower().endswith(".pdf"):
            return
        pdf = Path(path)
        if not self._sched_state.mark_seen(pdf):
            return
        logger.info("detected%s", _kv(file=pdf.name))
        if self._sched_state.queue(pdf):
            logger.info("queued: waiting for schedule%s", _kv(file=pdf.name))
        else:
            logger.info("already queued%s", _kv(file=pdf.name))

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._dispatch(str(event.src_path))

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._dispatch(str(event.dest_path))


# ── Background threads ────────────────────────────────────────────────────────

def _token_refresh_loop(token_mgr: _TokenManager, stop: threading.Event) -> None:
    """Re-login proactively every 7 h so the token never expires mid-operation."""
    while not stop.wait(300):  # check every 5 min
        if token_mgr.needs_refresh():
            token_mgr.login()


def _heartbeat_loop(base_url: str, token_mgr: _TokenManager, stop: threading.Event) -> None:
    """Tell the server we are alive every 30 s — drives ACTIVE/INACTIVE in the UI."""
    while not stop.wait(30):
        _call(token_mgr, "POST", base_url + "/api/client/heartbeat", timeout=10)


def _config_poll_loop(
    base_url: str,
    token_mgr: _TokenManager,
    folders_ref: dict,
    observer_ref: list,
    sched_state: _ScheduleState,
    stop: threading.Event,
) -> None:
    """Re-fetch folder paths every 60 s; restart watcher if input_folder changes."""
    while not stop.wait(60):
        resp = _call(token_mgr, "GET", base_url + "/api/config", timeout=15)
        if not resp or resp.status_code != 200:
            continue

        cfg = resp.json().get("config", {})
        new_input = cfg.get("input_folder", "")
        old_input = folders_ref.get("input", "")

        folders_ref.update({
            "input":   new_input,
            "output":  cfg.get("output_folder", ""),
            "success": cfg.get("success_folder", ""),
            "failed":  cfg.get("failed_folder", ""),
        })

        if new_input == old_input and observer_ref[0] is not None:
            continue

        logger.info("input_folder changed — restarting watcher%s", _kv(path=new_input))
        old_obs: Observer | None = observer_ref[0]
        if old_obs:
            old_obs.stop()
            old_obs.join(timeout=5)
        observer_ref[0] = None

        if new_input and Path(new_input).is_dir():
            handler = _PDFHandler(base_url, token_mgr, folders_ref, sched_state)
            obs = Observer()
            obs.schedule(handler, new_input, recursive=False)
            obs.start()
            observer_ref[0] = obs
            logger.info("watcher restarted%s", _kv(path=new_input))
            for pdf in _pdfs_in(Path(new_input)):
                if sched_state.mark_seen(pdf):
                    if sched_state.queue(pdf):
                        logger.info("queued existing PDF after folder change%s", _kv(file=pdf.name))
        else:
            logger.warning("new input_folder does not exist%s", _kv(path=new_input))


def _scheduler_poll_loop(
    base_url: str,
    token_mgr: _TokenManager,
    folders_ref: dict,
    sched_state: _ScheduleState,
    stop: threading.Event,
) -> None:
    """Poll /api/scheduler and start local queued PDFs when a schedule is due."""
    while not stop.is_set():
        state, pending, due_sids = sched_state.claim_due_batch()
        due_count = len(due_sids)
        if state == "busy":
            logger.info("schedule due while previous batch is still running; skipping %d due schedule(s)", due_count)
            pending = []
        elif state == "empty":
            logger.info("schedule due; no queued PDFs")
            pending = []
        elif state != "start":
            pending = []

        if due_sids:
            # Report the run(s) to the server
            for sid in due_sids:
                _call(token_mgr, "POST", f"{base_url}/api/scheduler/{sid}/ran", timeout=10)

        if pending:
            logger.info("schedule due; uploading %d queued PDF(s)", len(pending))

        if pending:
            threading.Thread(
                target=_process_pdf_batch,
                args=(base_url, token_mgr, folders_ref, sched_state, pending),
                daemon=True,
            ).start()

        resp = _call(token_mgr, "GET", base_url + "/api/scheduler", timeout=15)
        if resp and resp.status_code == 200:
            schedules = resp.json().get("schedules", [])
            enabled_count, tracked_count = sched_state.update_from_server(schedules)
            if enabled_count and not tracked_count:
                logger.warning("enabled schedules found, but no next_run could be tracked")

        if stop.wait(_SCHED_POLL_SECS):
            break


def _process_pdf_batch(
    base_url: str,
    token_mgr: _TokenManager,
    folders_ref: dict,
    sched_state: _ScheduleState,
    pending: List[Path],
) -> None:
    try:
        folders = dict(folders_ref)
        for pdf in pending:
            if not pdf.exists():
                logger.warning("queued PDF no longer exists%s", _kv(file=pdf.name))
                sched_state.remove_seen(pdf)
                continue
            logger.info("uploading queued PDF%s", _kv(file=pdf.name))
            try:
                _process_pdf(base_url, token_mgr, folders, pdf)
            finally:
                sched_state.remove_seen(pdf)
    finally:
        sched_state.finish_batch()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="AugmentedOCR Desktop Agent — watches a local folder and uploads PDFs.",
    )
    parser.add_argument("--config",   metavar="PATH",  help="Path to client_agent.json")
    parser.add_argument("--server",   metavar="URL",   help="Server base URL override")
    parser.add_argument("--email",    metavar="EMAIL", help="Login email (skip prompt)")
    parser.add_argument("--password", metavar="PASS",  help="Login password (skip prompt)")
    parser.add_argument("--log-dir",  metavar="DIR",   help="Log directory (default: ./logs)")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    log_dir = Path(args.log_dir) if args.log_dir else script_dir / "logs"
    _configure_logging(log_dir)

    # ── Load server URL from local config ─────────────────────────────────────
    config_path = Path(args.config) if args.config else script_dir / "client_agent.json"
    local: Dict[str, str] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            local = json.load(fh)

    # CLI and env-var overrides for server URL
    if args.server:
        local["server_url"] = args.server
    env_url = os.environ.get("AUGOCR_SERVER_URL")
    if env_url:
        local["server_url"] = env_url

    if not local.get("server_url"):
        logger.error("server_url is required — set it in %s", config_path)
        return 1

    base_url = local["server_url"].rstrip("/")

    # ── Print startup banner ──────────────────────────────────────────────────
    print()
    print("  AugmentedOCR Desktop Agent")
    print("  " + "=" * 34)
    print(f"  Server  :  {base_url}")
    print()

    # ── Resolve email and password (prompt if not supplied) ───────────────────
    email    = args.email    or os.environ.get("AUGOCR_EMAIL")    or ""
    password = args.password or os.environ.get("AUGOCR_PASSWORD") or ""

    if not email:
        try:
            email = input("  Email   : ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1

    if not password:
        try:
            password = getpass.getpass("  Password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1

    if not email or not password:
        logger.error("email and password are required")
        return 1

    print()

    # ── Login ─────────────────────────────────────────────────────────────────
    token_mgr = _TokenManager(base_url, email, password)
    if not token_mgr.login():
        logger.error("login failed — check email, password, and server URL")
        return 1

    # ── Fetch folder config from server ───────────────────────────────────────
    resp = _call(token_mgr, "GET", base_url + "/api/config", timeout=15)
    if not resp or resp.status_code != 200:
        logger.error("failed to fetch folder config from server")
        return 1

    cfg = resp.json().get("config", {})
    folders: Dict[str, str] = {
        "input":   cfg.get("input_folder", ""),
        "output":  cfg.get("output_folder", ""),
        "success": cfg.get("success_folder", ""),
        "failed":  cfg.get("failed_folder", ""),
    }

    if not folders["input"]:
        logger.error(
            "input_folder not configured — set it in the Settings page on the server"
        )
        return 1

    input_path = Path(folders["input"])
    if not input_path.is_dir():
        logger.error("input_folder does not exist%s", _kv(path=str(input_path)))
        return 1

    logger.info(
        "starting%s",
        _kv(input=folders["input"], output=folders["output"],
            success=folders["success"], failed=folders["failed"]),
    )

    # ── Initial schedule check ────────────────────────────────────────────────
    sched_state = _ScheduleState()
    resp_sched = _call(token_mgr, "GET", base_url + "/api/scheduler", timeout=15)
    if resp_sched and resp_sched.status_code == 200:
        schedules = resp_sched.json().get("schedules", [])
        enabled_count, tracked_count = sched_state.update_from_server(schedules)
        if enabled_count:
            logger.info(
                "scheduler: %d enabled schedule(s), %d next run(s) tracked; files wait until schedule time",
                enabled_count, tracked_count,
            )
        else:
            logger.info("scheduler: no enabled schedules; folder uploads paused")
    else:
        logger.warning("scheduler state unavailable; folder uploads paused until scheduler can be read")

    # ── Scan input folder for PDFs already present at startup ─────────────────
    existing_pdfs = _pdfs_in(input_path)
    if existing_pdfs:
        logger.info("found %d existing PDF(s) in input folder", len(existing_pdfs))
        for pdf in existing_pdfs:
            if sched_state.mark_seen(pdf):
                if sched_state.queue(pdf):
                    logger.info("queued: waiting for schedule%s", _kv(file=pdf.name))
                else:
                    logger.info("already queued%s", _kv(file=pdf.name))

    # ── Start watcher ─────────────────────────────────────────────────────────
    handler = _PDFHandler(base_url, token_mgr, folders, sched_state)
    observer = Observer()
    observer.schedule(handler, str(input_path), recursive=False)
    observer.start()

    observer_ref: list = [observer]
    stop = threading.Event()

    threading.Thread(
        target=_token_refresh_loop, args=(token_mgr, stop),
        name="token-refresh", daemon=True,
    ).start()
    threading.Thread(
        target=_heartbeat_loop, args=(base_url, token_mgr, stop),
        name="heartbeat", daemon=True,
    ).start()
    threading.Thread(
        target=_config_poll_loop,
        args=(base_url, token_mgr, folders, observer_ref, sched_state, stop),
        name="config-poll", daemon=True,
    ).start()
    threading.Thread(
        target=_scheduler_poll_loop,
        args=(base_url, token_mgr, folders, sched_state, stop),
        name="scheduler-poll", daemon=True,
    ).start()

    try:
        while True:
            time.sleep(1)
            obs = observer_ref[0]
            if obs and not obs.is_alive():
                logger.error("watchdog observer died unexpectedly")
                return 1
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    finally:
        stop.set()
        obs = observer_ref[0]
        if obs:
            obs.stop()
            obs.join(timeout=5)
        logger.info("stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
