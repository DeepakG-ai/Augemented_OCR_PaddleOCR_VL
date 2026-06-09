"""
MLflow tracing — scoped to the LLM worker ONLY.

Decision (see docs/workflow): operational observability lives in the central
logs. MLflow is used for the one thing logs can't do — inspecting the exact
prompt/response/token-usage of every Qwen call for quality + eval work.

So we trace ONLY the llm-worker, as one clean in-process hierarchy:

    field_extraction            (AGENT)   one document's whole LLM phase
    └─ page 1                   (CHAIN)
       └─ llm.chat              (LLM)     system+user in, assistant out,
                                          mlflow.chat.tokenUsage set
    └─ page 2 → llm.chat ...

normalize / ocr / postprocess are NOT traced — the legacy trace_* names are
kept as no-ops so existing call sites keep working without change.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

# ── MLflow client hardening ──────────────────────────────────────────────
# Tracing is NON-CRITICAL. When the tracking server is down or still starting,
# the client must fail FAST and QUIET — it must never stall an extraction or
# flood the pipeline logs with urllib3 retry storms. These env vars are read by
# MLflow's REST client at request time; `setdefault` lets an explicit override
# win. (MLflow's own defaults are 7 retries + 120s timeout — far too patient for
# a best-effort observability path.)
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")   # one attempt, no storm
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "3")       # give up after 3s
# Resolve the experiment lazily from the env so traces still land in the right
# experiment even if the eager set_experiment() below failed because the server
# wasn't up yet at startup.
os.environ.setdefault("MLFLOW_EXPERIMENT_NAME", "augmented_ocr")

# pyrefly: ignore [missing-import]
import mlflow
from mlflow.entities import SpanType

logger = logging.getLogger(__name__)


def setup_mlflow(experiment_name: str = "augmented_ocr", force: bool = False) -> None:
    """Point the MLflow client at the tracking server and select the experiment.

    Resilient by design: if the tracking server is unreachable (down, or still
    starting up), this logs at debug and returns. Tracing then degrades to a
    no-op until the server is reachable again (see `span`). It never raises and
    never blocks startup for more than MLFLOW_HTTP_REQUEST_TIMEOUT.
    """
    from .config import MLFLOW_ENABLED, MLFLOW_TRACKING_URI
    if not MLFLOW_ENABLED:
        return
    # urllib3 logs one WARNING per retry attempt ("Retrying (...)"). With retries
    # capped to 0 above these should not fire, but silence the channel anyway so
    # a flapping tracking server can never spam the pipeline logs.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    try:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        mlflow.set_experiment(experiment_name)
        _reset_breaker()
    except Exception as e:
        logger.debug("MLflow setup skipped (tracking server unreachable): %s", e)
        _trip_breaker()


def _enabled() -> bool:
    from .config import MLFLOW_ENABLED
    return bool(MLFLOW_ENABLED)


# ── Circuit breaker ──────────────────────────────────────────────────────
# When the tracking server is down, every span would otherwise pay one full
# MLFLOW_HTTP_REQUEST_TIMEOUT (3s) — adding 3s × N spans of latency to a single
# document. Once a span fails to reach the server we "open" the breaker and make
# all spans instant no-ops for a short cooldown, then probe again. When the
# server is healthy the breaker never opens, so there is zero overhead.
import time as _time

_BREAKER_COOLDOWN_S = 30.0
# A healthy start_span returns in milliseconds. If it takes longer than this the
# server is unreachable and MLflow is silently eating the HTTP timeout (it does
# NOT raise) — treat that as "down" and open the breaker.
_BREAKER_SLOW_S = 1.0
_breaker_open_until = 0.0


def _breaker_open() -> bool:
    return _time.monotonic() < _breaker_open_until


def _trip_breaker() -> None:
    global _breaker_open_until
    _breaker_open_until = _time.monotonic() + _BREAKER_COOLDOWN_S


def _reset_breaker() -> None:
    global _breaker_open_until
    _breaker_open_until = 0.0


# ── The ONE real primitive: a typed, nestable span ──

# Re-export span types so call sites don't import mlflow directly.
AGENT = SpanType.AGENT
CHAIN = SpanType.CHAIN
LLM = SpanType.LLM
TOOL = SpanType.TOOL
PARSER = SpanType.PARSER


@contextmanager
def span(
    name: str,
    span_type: str = SpanType.CHAIN,
    *,
    inputs: Any | None = None,
    attributes: dict | None = None,
) -> Iterator[dict]:
    """
    Open a typed MLflow span that auto-nests under any active parent span
    (same process / async task — MLflow copies context into asyncio tasks).

    Yields a ctx dict; set on it before the block exits:
      ctx["outputs"]      -> span outputs (e.g. assistant text / parsed result)
      ctx["token_usage"]  -> {"input_tokens","output_tokens","total_tokens"}
                             rendered as cost/token charts in the MLflow UI
    """
    ctx: dict[str, Any] = {"outputs": None, "token_usage": None}
    # Skip entirely when tracing is off, or while the breaker is open (server
    # recently seen as down — don't pay the timeout again until it cools off).
    if not _enabled() or _breaker_open():
        yield ctx
        return

    # Enter the MLflow span defensively: ANY tracing failure must degrade to
    # a no-op, never break the extraction that called us.
    cm = sp = None
    try:
        _t0 = _time.monotonic()
        cm = mlflow.start_span(name=name, span_type=span_type)
        sp = cm.__enter__()
        # start_span swallows backend timeouts instead of raising, so judge
        # health by latency: fast = up (close breaker), slow = down (open it).
        if (_time.monotonic() - _t0) > _BREAKER_SLOW_S:
            _trip_breaker()
        else:
            _reset_breaker()
        if inputs is not None:
            sp.set_inputs(inputs)
        if attributes:
            sp.set_attributes(attributes)
    except Exception as e:
        logger.debug("tracing disabled for span %r due to error: %s", name, e)
        cm = sp = None
        _trip_breaker()  # server looks down — stop trying for the cooldown window

    try:
        yield ctx  # caller body — its exceptions propagate untouched
    finally:
        if sp is not None:
            try:
                if ctx.get("token_usage"):
                    sp.set_attribute("mlflow.chat.tokenUsage", ctx["token_usage"])
                if ctx.get("outputs") is not None:
                    sp.set_outputs(ctx["outputs"])
            except Exception as e:
                logger.debug("span finalize failed (%s): %s", name, e)
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception as e:
                logger.debug("span exit failed (%s): %s", name, e)


def trace_tags(**tags: Any) -> None:
    """Tag the active trace (extraction_id, vendor, model, …) for UI filtering."""
    if not _enabled():
        return
    try:
        mlflow.update_current_trace(tags={k: str(v) for k, v in tags.items() if v is not None})
    except Exception as e:
        logger.debug("update_current_trace failed: %s", e)


def clean_chat_messages(messages: list[dict]) -> list[dict]:
    """Drop base64 image blobs from chat messages before they enter a span."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m.get("role"), "content": c})
        elif isinstance(c, list):
            text = " ".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            )
            out.append({"role": m.get("role"), "content": text or "[image]"})
    return out


# ── Legacy no-op shims — non-LLM stages are intentionally NOT traced ──
# Kept so existing call sites in worker.py / main.py / extractor.py keep
# working untouched. They do nothing.

@contextmanager
def _noop_ctx(*_a: Any, **_k: Any) -> Iterator[dict]:
    yield {}


def trace_span(*_a: Any, **_k: Any): return _noop_ctx()
def trace_named_step(*_a: Any, **_k: Any): return _noop_ctx()
def trace_pipeline_stage(*_a: Any, **_k: Any): return _noop_ctx()
def trace_extraction_root(*_a: Any, **_k: Any): return _noop_ctx()
def trace_extraction_pipeline(*_a: Any, **_k: Any): return _noop_ctx()
def trace_llm_call(*_a: Any, **_k: Any): return _noop_ctx()
def trace_page_extraction(*_a: Any, **_k: Any): return _noop_ctx()
def trace_build_user_message(*_a: Any, **_k: Any): return _noop_ctx()
def trace_merge_results(*_a: Any, **_k: Any): return _noop_ctx()
def trace_file_upload(*_a: Any, **_k: Any): return _noop_ctx()
def trace_pdf_rendering(*_a: Any, **_k: Any): return _noop_ctx()
def trace_text_matching(*_a: Any, **_k: Any): return _noop_ctx()
def trace_db_persist(*_a: Any, **_k: Any): return _noop_ctx()
def trace_paddle_ocr(*_a: Any, **_k: Any): return _noop_ctx()
def trace_prompt_building(*_a: Any, **_k: Any): return _noop_ctx()
def trace_ocr_page(*_a: Any, **_k: Any): return _noop_ctx()


@contextmanager
def use_trace_context(*_a: Any, **_k: Any) -> Iterator[None]:
    yield None


def current_trace_context() -> dict:
    return {}


def get_current_context(): return None
def attach_context(_ctx): return None
def detach_context(_token): return None
def context_from_carrier(carrier: dict | None) -> dict: return carrier or {}
def set_span_output(*_a: Any, **_k: Any) -> None: return None
@contextmanager
def start_span(*_a: Any, **_k: Any) -> Iterator[None]: yield None
