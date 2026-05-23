"""
Central logging for the backend.

Design (one event model, four sinks):
  - logs/pipeline/pipeline.log : every extraction, interleaved, each line
        tagged [ext N] + stage so `grep "ext 1043"` reconstructs one PDF.
  - logs/pipeline/extractions/extraction_<id>_<file>.log : one PDF, linear,
        start -> end, closed with a RESULT block.
  - logs/pipeline/error.log : ERROR+ only, WITH full traceback (keeps the
        other two scannable).
  - logs/pipeline/security.log : auth events only (login success/fail, token errors).

Rules enforced here:
  - Business code emits an event ONCE. A ContextFilter stamps ext/stage from
    contextvars; handlers route it. Callers never format correlation.
  - Every line: `TS.mmm  LEVEL  [ext N] stage      message  | k=v ...  (Nms)`.
  - Lifecycle is explicit: stage_span() emits  > start / # done / x FAIL
    with elapsed ms. `grep x pipeline.log` is the incident list.
  - Errors are loud and located: type+message+file:line+trace=ref inline;
    the full traceback goes ONLY to error.log keyed by that ref.
  - Human-readable, aligned columns. No JSON.

Not named logging.py so it does not shadow the stdlib module.
"""
from __future__ import annotations

import contextvars
import logging
import logging.config
import logging.handlers
import os
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import LOG_LEVEL, PIPELINE_LOG_DIR as _PIPELINE_LOG_DIR_CFG

_PIPELINE_LOG_DIR = _PIPELINE_LOG_DIR_CFG
_EXTRACTION_LOG_DIR = _PIPELINE_LOG_DIR / "extractions"

# ── Double-init guard ────────────────────────────────────────────────────────
_CONFIGURED: bool = False
_QUEUE_LISTENER: logging.handlers.QueueListener | None = None

# ── Context vars (set by worker.process_job / stage_span / api middleware) ──
current_extraction_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_extraction_id", default=None,
)
current_extraction_filename: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_extraction_filename", default=None,
)
current_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_stage", default=None,
)
current_worker: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_worker", default=None,
)

_DATEFMT = "%Y-%m-%d %H:%M:%S"
# columns:  TS.mmm  LEVEL  [ext N]<pad>stage<pad>message
_FMT = "%(asctime)s.%(msecs)03d  %(levelname)-5s %(ctx_ext)s%(ctx_stage)s%(message)s"

_EXT_COL = 11   # "[ext 1042] "
_STAGE_COL = 11  # "normalize  "

_DEF_LOGGER = "pipeline"


# ── Correlation: stamp every record from contextvars ──

class ContextFilter(logging.Filter):
    """Inject ctx_ext / ctx_stage onto every record (incl. 3rd-party libs).

    Must run in the originating thread (not the QueueListener thread) so that
    contextvars return the correct per-task values.  Also stamps _ext_id and
    _filename on the record so ExtractionLogHandler can read them from the
    listener thread without touching contextvars.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        eid = current_extraction_id.get()
        tag = f"[ext {eid}]" if eid is not None else "[ext ----]"
        record.ctx_ext = tag.ljust(_EXT_COL)
        record.ctx_stage = (current_stage.get() or "-").ljust(_STAGE_COL)
        # Carry contextvar snapshot on the record so listener-thread handlers
        # can read them without calling .get() in the wrong thread.
        record._ext_id = eid  # type: ignore[attr-defined]
        record._filename = current_extraction_filename.get()  # type: ignore[attr-defined]
        return True


class _NoTracebackFormatter(logging.Formatter):
    """pipeline.log / console: keep lines scannable — never append a stack."""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text, record.stack_info
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved


class _TracebackFormatter(logging.Formatter):
    """error.log: full stack, fenced with the trace ref so it cross-links to pipeline.log."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if record.exc_info:
            ref = getattr(record, "_trace_ref", None)
            fence = (
                f"  └─ traceback ref={ref} ─────────────────────────────"
                if ref else
                "  └─ traceback ─────────────────────────────"
            )
            return base + "\n" + fence
        return base


class _SecurityOnlyFilter(logging.Filter):
    """Pass only records emitted by logging.getLogger('security')."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "security"


# ── Per-extraction file tee (cached handles, not open-per-record) ──

class ExtractionLogHandler(logging.Handler):
    """Tee every record into the active extraction's own .log file."""

    _MAX_OPEN = 8

    def __init__(self) -> None:
        super().__init__()
        self._handles: "OrderedDict[int, Any]" = OrderedDict()
        self._counters: dict[int, int] = {}
        self._lock = threading.Lock()

    def _handle_for(self, ext_id: int, filename: str | None):
        h = self._handles.get(ext_id)
        if h is not None:
            self._handles.move_to_end(ext_id)
            return h
        _EXTRACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = _extraction_log_path(ext_id, filename).open("a", encoding="utf-8", buffering=8192)
        self._handles[ext_id] = h
        if len(self._handles) > self._MAX_OPEN:
            _old_id, old = self._handles.popitem(last=False)
            self._counters.pop(_old_id, None)
            try:
                old.flush()
                old.close()
            except Exception:
                pass
        return h

    def emit(self, record: logging.LogRecord) -> None:
        # Prefer the value stamped by ContextFilter in the originating thread
        # (_ext_id on record).  Fall back to the contextvar for tests that call
        # emit() directly without going through the logging system.
        ext_id = getattr(record, "_ext_id", None)
        if ext_id is None:
            ext_id = current_extraction_id.get()
        if ext_id is None:
            return
        filename = getattr(record, "_filename", current_extraction_filename.get())
        try:
            line = self.format(record) + "\n"
            with self._lock:
                fh = self._handle_for(ext_id, filename)
                fh.write(line)
                count = self._counters.get(ext_id, 0) + 1
                self._counters[ext_id] = count
                flush = count % 20 == 0 or record.levelno >= logging.ERROR
            if flush:
                fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        for h in list(self._handles.values()):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
        self._handles.clear()
        self._counters.clear()
        super().close()


def _safe_stem(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", Path(name).stem)[:60]


def _extraction_log_path(ext_id: int, filename: str | None = None) -> Path:
    safe = _safe_stem(filename)
    name = f"extraction_{ext_id}_{safe}.log" if safe else f"extraction_{ext_id}.log"
    return _EXTRACTION_LOG_DIR / name


# ── Optional CloudWatch handler ──────────────────────────────────────────────

def _attach_cloudwatch_handler(root: logging.Logger, level: int) -> None:
    try:
        import watchtower
        import boto3
        log_group = os.environ.get("AWS_LOG_GROUP", "/augmented-ocr/backend")
        stream_name = os.environ.get("AWS_LOG_STREAM_PREFIX", "worker") + "-{strftime:%Y-%m-%d}"
        region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
        cw_handler = watchtower.CloudWatchLogHandler(
            log_group=log_group,
            stream_name=stream_name,
            boto3_client=boto3.client("logs", region_name=region),
        )
        cw_handler.addFilter(ContextFilter())
        root.addHandler(cw_handler)
    except ImportError:
        logging.getLogger(_DEF_LOGGER).warning(
            "LOG_TO_CLOUDWATCH=true but watchtower not installed"
        )


# ── Setup ────────────────────────────────────────────────────────────────────

def configure_logging() -> None:
    global _CONFIGURED, _QUEUE_LISTENER
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level = LOG_LEVEL
    _PIPELINE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _EXTRACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    pipeline_log = _PIPELINE_LOG_DIR / "pipeline.log"
    error_log = _PIPELINE_LOG_DIR / "error.log"
    security_log_path = _PIPELINE_LOG_DIR / "security.log"

    plain = _NoTracebackFormatter(_FMT, datefmt=_DATEFMT)
    traced = _TracebackFormatter(_FMT, datefmt=_DATEFMT)
    ctx_filter = ContextFilter()

    # Console is called directly in the originating (asyncio) thread — safe to
    # add ContextFilter here; it reads the correct contextvar values.
    console = logging.StreamHandler()
    console.setFormatter(plain)
    console.setLevel(level)
    console.addFilter(ctx_filter)

    # File handlers live inside the QueueListener background thread.
    # Do NOT add ContextFilter to them: contextvars return None in that thread.
    # ContextFilter is added to queue_handler instead (see below), so ctx_ext /
    # ctx_stage / _ext_id / _filename are stamped on the record in the
    # originating thread before it is enqueued.
    pipeline_file = logging.handlers.RotatingFileHandler(
        str(pipeline_log), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    pipeline_file.setFormatter(plain)
    pipeline_file.setLevel(level)

    error_file = logging.handlers.RotatingFileHandler(
        str(error_log), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    error_file.setFormatter(traced)
    error_file.setLevel(logging.ERROR)

    per_pdf = ExtractionLogHandler()
    per_pdf.setFormatter(plain)
    per_pdf.setLevel(level)

    security_file = logging.handlers.RotatingFileHandler(
        str(security_log_path), maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8",
    )
    security_file.setFormatter(plain)
    security_file.setLevel(logging.INFO)
    security_file.addFilter(_SecurityOnlyFilter())  # safe: checks record.name, not contextvars

    # File I/O is async-safe: QueueListener's background thread drains the
    # queue and calls each handler's emit(). The asyncio event loop never
    # blocks on disk writes.
    log_queue: queue.Queue = queue.Queue(maxsize=-1)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    # ContextFilter on queue_handler runs in the originating thread (before
    # enqueue), stamping ctx_ext/ctx_stage/_ext_id/_filename on the record.
    queue_handler.addFilter(ctx_filter)
    _QUEUE_LISTENER = logging.handlers.QueueListener(
        log_queue,
        pipeline_file,
        error_file,
        per_pdf,
        security_file,
        respect_handler_level=True,
    )
    _QUEUE_LISTENER.start()

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(console)        # sync — stdout, ContextFilter already on it
    root.addHandler(queue_handler)  # async — all disk sinks, ContextFilter stamps before enqueue
    root.setLevel(level)

    # uvicorn access logs are redundant with our api middleware line
    logging.getLogger("uvicorn.access").propagate = False

    if os.environ.get("LOG_TO_CLOUDWATCH", "").lower() == "true":
        _attach_cloudwatch_handler(root, level)


def shutdown_logging() -> None:
    """Drain the log queue and stop the background listener thread.

    Call this at the bottom of the FastAPI lifespan shutdown so no records
    are lost when the ECS container receives SIGTERM.
    """
    global _QUEUE_LISTENER, _CONFIGURED
    if _QUEUE_LISTENER:
        _QUEUE_LISTENER.stop()
        _QUEUE_LISTENER = None
    _CONFIGURED = False
    logging.shutdown()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ── Event helpers — the ONLY way business code should log ──

def _kv(pairs: dict[str, Any]) -> str:
    out = []
    for k, v in pairs.items():
        if v is None:
            continue
        s = str(v)
        if " " in s and not (s.startswith('"') and s.endswith('"')):
            s = f'"{s}"'
        out.append(f"{k}={s}")
    return " ".join(out)


def _compose(msg: str, kv: dict[str, Any], ms: str | None) -> str:
    body = msg
    tail = _kv(kv)
    if tail:
        body += "  | " + tail
    if ms:
        body += (" " if tail else "  | ") + ms
    return body


def fmt_ms(ms: float) -> str:
    """532.0 -> '532ms' ; 8104.0 -> '8.10s'."""
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.2f}s"


def event(level: int, msg: str, *, logger: str = _DEF_LOGGER,
          ms: str | float | None = None, **kv: Any) -> None:
    """One structured-but-human line. e.g. event(INFO, 'page 1/3 ok', tok_in=611, ms=8104)."""
    if isinstance(ms, (int, float)):
        ms = fmt_ms(float(ms))
    logging.getLogger(logger).log(level, _compose(msg, kv, ms))


def info(msg: str, **kw: Any) -> None:
    event(logging.INFO, msg, **kw)


def warn(msg: str, **kw: Any) -> None:
    event(logging.WARNING, msg, **kw)


def _exc_location(exc: BaseException) -> str:
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is None:
        return "?"
    f = last.tb_frame
    mod = f.f_globals.get("__name__", "?").split(".")[-1]
    return f"{mod}.{f.f_code.co_name}:{last.tb_lineno}"


def error(msg: str, *, exc: BaseException | None = None,
          logger: str = _DEF_LOGGER, ms: str | float | None = None, **kv: Any) -> str:
    """
    Loud, located error. Returns the trace ref.
    Inline (console/pipeline/per-pdf): type+message+file:line+trace=ref.
    error.log additionally gets the full traceback under the same ref.
    """
    ref = "err-" + uuid.uuid4().hex[:6]
    if exc is not None:
        kv["exc"] = f"{type(exc).__name__}: {exc}"
        kv["at"] = _exc_location(exc)
    kv["trace"] = ref
    if isinstance(ms, (int, float)):
        ms = fmt_ms(float(ms))

    lg = logging.getLogger(logger)
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    # Build the record manually so we can set _trace_ref before dispatch.
    # _TracebackFormatter reads this to stamp the fence line in error.log,
    # giving a bidirectional grep key between pipeline.log and error.log.
    record = lg.makeRecord(
        lg.name,
        logging.ERROR,
        "(unknown)",
        0,
        _compose(msg, kv, ms),
        (),
        exc_info,
    )
    record._trace_ref = ref  # type: ignore[attr-defined]
    lg.handle(record)
    return ref


# ── Stage lifecycle: > start / # done / x FAIL with elapsed ──

@contextmanager
def stage_span(stage: str, *, logger: str = _DEF_LOGGER, **start_kv: Any) -> Iterator[dict]:
    """
    Wrap a pipeline stage. Emits the start line, then on exit either the
    done line (with ctx['end'] kv) or a FAIL line (with the exception),
    always with elapsed ms. Re-raises on failure.

        with stage_span("normalize", worker="normalize-1") as s:
            ...
            s["end"] = {"pages": 3, "enqueued": "ocr,llm"}
    """
    token = current_stage.set(stage)
    t0 = time.perf_counter()
    ctx: dict[str, Any] = {"end": {}}
    try:
        event(logging.INFO, "▶ start", logger=logger, **start_kv)
        yield ctx
    except BaseException as exc:  # noqa: BLE001 — log then re-raise
        ms = (time.perf_counter() - t0) * 1000
        if isinstance(exc, Exception):
            error("✗ FAIL", exc=exc, logger=logger, ms=ms,
                  **{k: v for k, v in ctx.get("end", {}).items()})
        raise
    else:
        ms = (time.perf_counter() - t0) * 1000
        event(logging.INFO, "■ done", logger=logger, ms=ms, **ctx.get("end", {}))
    finally:
        current_stage.reset(token)


# ── Per-PDF RESULT footer (written straight to the extraction file) ──

def result_block(
    extraction_id: int,
    *,
    status: str,
    filename: str | None,
    vendor: str | None,
    pages: int | None = None,
    digital: int | None = None,
    scanned: int | None = None,
    fields: int | None = None,
    tok_in: int | None = None,
    tok_out: int | None = None,
    llm_calls: int | None = None,
    latency_total_s: float | None = None,
    stage_latency: dict[str, float] | None = None,
    failed_stage: str | None = None,
    errors: list[str] | None = None,
) -> None:
    """Append the closing RESULT block to the per-extraction file. Never raises."""
    try:
        matches = list(_EXTRACTION_LOG_DIR.glob(f"extraction_{int(extraction_id)}*.log"))
        path = matches[0] if matches else _extraction_log_path(extraction_id, filename)
        bar = "─" * 78
        L = [bar]
        L.append(
            f"RESULT  status={status.upper()}   ext={extraction_id}   "
            f"file={filename or '-'}   vendor={vendor or '-'}"
        )
        seg = []
        if pages is not None:
            seg.append(f"pages={pages}")
        if digital is not None:
            seg.append(f"digital={digital}")
        if scanned is not None:
            seg.append(f"scanned={scanned}")
        if fields is not None:
            seg.append(f"fields={fields}")
        if seg:
            L.append("        " + "  ".join(seg))
        tok = int(tok_in or 0) + int(tok_out or 0)
        L.append(
            f"        tokens  in={tok_in or 0}  out={tok_out or 0}  total={tok}"
            f"   llm_calls={llm_calls or 0}"
        )
        if latency_total_s is not None:
            sl = ""
            if stage_latency:
                sl = "  (" + " · ".join(
                    f"{k} {v:.2f}" for k, v in stage_latency.items()
                ) + ")"
            L.append(f"        latency total={latency_total_s:.2f}s{sl}")
        if failed_stage:
            L.append(f"        failed_stage = {failed_stage}")
        if errors:
            L.append("        errors:")
            for e in errors:
                L.append(f"          • {e}")
        else:
            L.append("        errors: none")
        L.append(bar)
        _EXTRACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    except Exception as exc:  # never break the pipeline over a log line
        logging.getLogger(_DEF_LOGGER).warning("result_block write failed: %s", exc)


# ── Simple timer (kept: used widely as plog.timed) ──

@contextmanager
def timed(label: str) -> Iterator[dict[str, Any]]:
    """Measure elapsed time; yielded dict gets ``ms`` set on exit."""
    ctx: dict[str, Any] = {}
    start = time.perf_counter()
    try:
        yield ctx
    finally:
        ctx["ms"] = round((time.perf_counter() - start) * 1000, 1)


# ── Read-side helpers (used by the trace API) ──

def log_paths(extraction_id: int | None = None) -> dict[str, str]:
    paths: dict[str, str] = {}
    if extraction_id is not None:
        matches = list(_EXTRACTION_LOG_DIR.glob(f"extraction_{int(extraction_id)}*.log"))
        paths["extraction_log"] = str(matches[0]) if matches else str(
            _extraction_log_path(int(extraction_id))
        )
    return paths


_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
    r"  (?P<level>[A-Z ]{5})"
    r" \[ext [^\]]*\]\s*"
    r"(?P<stage>\S+)?\s*"
    r"(?P<message>.*)$"
)


def read_extraction_events(extraction_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Parse the per-extraction file back into events for the trace API."""
    matches = list(_EXTRACTION_LOG_DIR.glob(f"extraction_{int(extraction_id)}*.log"))
    if not matches:
        return []
    events: list[dict[str, Any]] = []
    with matches[0].open("r", encoding="utf-8") as f:
        for raw in f:
            m = _LINE_RE.match(raw.rstrip("\n"))
            if m:
                events.append({
                    "ts": m.group("ts"),
                    "level": (m.group("level") or "").strip(),
                    "stage": (m.group("stage") or "").strip(),
                    "message": m.group("message"),
                })
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def build_extraction_trace_summary(
    extraction_id: int,
    events: list[dict[str, Any]],
    *,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extraction = extraction or {}
    errors = [e for e in events if e.get("level") == "ERROR"]
    return {
        "extraction_id": extraction_id,
        "filename": extraction.get("filename"),
        "status": extraction.get("status"),
        "vendor_id": extraction.get("vendor_id"),
        "total_pages": extraction.get("total_pages"),
        "event_count": len(events),
        "errors": errors,
        "timeline": events,
        "log_paths": log_paths(extraction_id),
    }
