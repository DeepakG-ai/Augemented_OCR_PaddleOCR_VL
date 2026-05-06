"""
Arize Phoenix tracing — complete hierarchical pipeline observability.

One PDF upload = one trace.  Every stage, agent call, per-page LLM call,
OCR step, and post-process overwrite nests under a single root span so the
Phoenix UI shows the full story of each document extraction.

Hierarchy for a 5-page digital PDF:

    extraction (root CHAIN)
    ├── stage.normalize
    │   ├── pdf_rendering
    │   └── page_classification
    ├── stage.ocr
    │   ├── geometry_routing
    │   ├── paddleocr_batch (if scanned pages)
    │   │   └── ocr_page_N (per scanned page)
    │   └── unified_geometry_saved
    ├── stage.llm
    │   ├── bbox_agent.learn_layout
    │   │   ├── bbox_agent.build_prompt
    │   │   ├── bbox_agent.llm_call (LLM span)
    │   │   └── bbox_agent.parse_and_snap
    │   ├── system_prompt_built
    │   ├── field_agent.extract_document
    │   │   ├── page_1_extraction
    │   │   │   ├── build_user_message
    │   │   │   └── llm.chat page_1/5 (LLM span)
    │   │   ├── page_2_extraction … page_5_extraction
    │   │   └── merge_results
    │   └── llm_result_persisted
    ├── stage.postprocess
    │   ├── field_mapping
    │   ├── spatial_memory.apply
    │   └── final_result
    └── stage.outbound
        ├── excel_export
        └── csv_export
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace, context as otel_context
from opentelemetry.propagate import extract as otel_extract
from opentelemetry.propagate import inject as otel_inject
from opentelemetry.trace import Status, StatusCode, Span

logger = logging.getLogger(__name__)

# Suppress noisy OTLP exporter when Phoenix collector is offline
logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(
    logging.CRITICAL
)

# ═══════════════════════════════════════════════════════════════════════
# Module state
# ═══════════════════════════════════════════════════════════════════════

_INITIALIZED = False
_TRACER: trace.Tracer | None = None


def setup_phoenix(project_name: str = "augmented_ocr") -> None:
    """Initialise OpenTelemetry → Phoenix once per process."""
    global _INITIALIZED, _TRACER
    if _INITIALIZED:
        return
    _INITIALIZED = True

    try:
        from .config import PHOENIX_ENABLED, PHOENIX_COLLECTOR_ENDPOINT
    except ImportError:
        from config import PHOENIX_ENABLED, PHOENIX_COLLECTOR_ENDPOINT  # type: ignore[no-redef]

    if not PHOENIX_ENABLED:
        logger.info("[Phoenix] Tracing disabled (PHOENIX_ENABLED=false)")
        return

    try:
        from phoenix.otel import register

        tracer_provider = register(
            project_name=project_name,
            endpoint=PHOENIX_COLLECTOR_ENDPOINT,
        )

        # Optional auto-instrumentors
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
        except ImportError:
            pass

        _TRACER = trace.get_tracer("augmented_ocr")
        logger.info("[Phoenix] Tracing ENABLED → project=%s endpoint=%s",
                     project_name, PHOENIX_COLLECTOR_ENDPOINT)

    except ImportError as exc:
        logger.warning("[Phoenix] SDK not installed, skipping: %s", exc)
    except Exception as exc:
        logger.error("[Phoenix] Init failed: %s", exc, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════
# JSON helper
# ═══════════════════════════════════════════════════════════════════════

def _safe_json(data: Any, *, max_len: int = 32_000) -> str:
    """Serialize to JSON string, truncating oversized payloads."""
    try:
        s = json.dumps(data, ensure_ascii=False, default=str)
        if len(s) > max_len:
            return s[:max_len] + "…<truncated>"
        return s
    except Exception:
        return str(data)[:max_len]


# ═══════════════════════════════════════════════════════════════════════
# Context propagation  (W3C trace-context across durable worker jobs)
# ═══════════════════════════════════════════════════════════════════════

def get_current_context():
    """Return the live OTel context (for passing to async generators)."""
    return otel_context.get_current()


def attach_context(ctx):
    """Attach a saved context to the current execution."""
    return otel_context.attach(ctx)


def detach_context(token):
    """Detach a previously attached context."""
    otel_context.detach(token)


def current_trace_context() -> dict[str, str]:
    """Serialize the active span into a W3C trace-context carrier dict."""
    carrier: dict[str, str] = {}
    try:
        otel_inject(carrier)
    except Exception as exc:
        logger.debug("Failed to inject trace context: %s", exc)
    return carrier


def context_from_carrier(carrier: dict | None):
    """Deserialize a W3C carrier dict back into an OTel context."""
    if not isinstance(carrier, dict) or not carrier:
        return otel_context.get_current()
    try:
        return otel_extract({str(k): str(v) for k, v in carrier.items()})
    except Exception as exc:
        logger.debug("Failed to extract trace context: %s", exc)
        return otel_context.get_current()


@contextmanager
def use_trace_context(carrier: dict | None) -> Iterator[None]:
    """Attach a serialized trace context for a block of work."""
    token = otel_context.attach(context_from_carrier(carrier))
    try:
        yield
    finally:
        otel_context.detach(token)


# ═══════════════════════════════════════════════════════════════════════
# Universal span builder — eliminates per-span boilerplate
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_span(
    name: str,
    *,
    kind: str = "CHAIN",
    input_data: Any | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Generic span context manager.

    Usage::

        with trace_span("my_step", kind="TOOL", input_data={...}) as ctx:
            result = do_work()
            ctx["output"] = result          # shown in Phoenix output panel
            ctx["my_attr"] = "custom"       # set as span attribute

    Reserved ctx keys:
        "output"  → serialised to output.value
        "_span"   → the raw OTel Span (for advanced use)
    """
    ctx: dict[str, Any] = {"output": None, "_span": None}

    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span(name) as span:
        start = time.perf_counter()
        ctx["_span"] = span

        span.set_attribute("openinference.span.kind", kind)

        if input_data is not None:
            span.set_attribute("input.value", _safe_json(input_data))
            span.set_attribute("input.mime_type", "application/json")

        for key, value in (attributes or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                span.set_attribute(str(key), _safe_json(value))
            else:
                span.set_attribute(str(key), value)

        try:
            yield ctx

            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("duration_ms", elapsed_ms)

            # Write all non-private ctx keys as span attributes
            for key, value in ctx.items():
                if key.startswith("_") or key == "output":
                    continue
                if value is not None:
                    if isinstance(value, (dict, list)):
                        span.set_attribute(key, _safe_json(value))
                    elif isinstance(value, (str, int, float, bool)):
                        span.set_attribute(key, value)

            if ctx.get("output") is not None:
                span.set_attribute("output.value", _safe_json(ctx["output"]))
                span.set_attribute("output.mime_type", "application/json")

            span.set_status(Status(StatusCode.OK))

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("duration_ms", elapsed_ms)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# Root trace — one per PDF extraction
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_extraction_root(
    extraction_id: int,
    vendor_id: str,
    filename: str,
    total_pages: int,
    format_type: str,
    header_fields: list[str],
    line_item_fields: list[str],
    vendor_name: str | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Root span for one PDF's entire lifecycle.
    All stage spans nest under this as children via W3C context propagation.

    The ctx dict supports:
        ctx["result"]  — final extraction result (set at end)
        ctx["status"]  — "done" / "failed" / "cancelled"
        ctx["digital_pages"] / ctx["scanned_pages"] — page classification counts
    """
    ctx: dict[str, Any] = {
        "result": None, "status": "unknown", "error": None,
        "digital_pages": 0, "scanned_pages": 0,
    }

    if _TRACER is None:
        yield ctx
        return

    span_name = filename if filename else f"extraction.{extraction_id}"
    with _TRACER.start_as_current_span(span_name) as span:
        start = time.perf_counter()

        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("extraction.id", extraction_id)
        span.set_attribute("extraction.vendor_id", vendor_id or "")
        span.set_attribute("extraction.vendor_name", vendor_name or "")
        span.set_attribute("extraction.filename", filename)
        span.set_attribute("extraction.total_pages", total_pages)
        span.set_attribute("extraction.format_type", format_type)

        span.set_attribute("input.value", _safe_json({
            "extraction_id": extraction_id,
            "filename": filename,
            "vendor_id": vendor_id,
            "vendor_name": vendor_name,
            "total_pages": total_pages,
            "format_type": format_type,
            "header_fields": header_fields,
            "line_item_fields": line_item_fields,
        }))
        span.set_attribute("input.mime_type", "application/json")

        try:
            yield ctx

            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("extraction.status", ctx.get("status", "unknown"))
            span.set_attribute("extraction.digital_pages", ctx.get("digital_pages", 0))
            span.set_attribute("extraction.scanned_pages", ctx.get("scanned_pages", 0))

            result = ctx.get("result")
            if result and isinstance(result, dict):
                line_items = result.get("line_items", [])
                span.set_attribute("extraction.line_items_count", len(line_items) if isinstance(line_items, list) else 0)
                span.set_attribute("output.value", _safe_json({
                    "status": ctx.get("status"),
                    "duration_ms": elapsed_ms,
                    "digital_pages": ctx.get("digital_pages", 0),
                    "scanned_pages": ctx.get("scanned_pages", 0),
                    "header_fields_extracted": len([
                        k for k, v in result.items()
                        if k != "line_items" and v is not None
                    ]),
                    "line_items_count": len(line_items) if isinstance(line_items, list) else 0,
                    "result": result,
                }))
                span.set_attribute("output.mime_type", "application/json")

            if ctx.get("error"):
                span.set_attribute("extraction.error", str(ctx["error"]))

            span.set_status(Status(StatusCode.OK))

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("extraction.status", "failed")
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# Stage span — one per durable worker stage
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_pipeline_stage(
    stage: str,
    *,
    extraction_id: int | None = None,
    document_id: int | None = None,
    job_id: int | None = None,
    vendor_id: str | None = None,
    filename: str | None = None,
    input_data: dict | None = None,
) -> Iterator[dict[str, Any]]:
    """Span for one durable worker stage (normalize / ocr / llm / postprocess / outbound)."""
    with trace_span(
        f"stage.{stage}",
        kind="CHAIN",
        input_data=input_data,
        attributes={
            "pipeline.stage": stage,
            "extraction.id": extraction_id,
            "document.id": document_id,
            "job.id": job_id,
            "extraction.vendor_id": vendor_id or "",
            "extraction.filename": filename or "",
        },
    ) as ctx:
        yield ctx


# ═══════════════════════════════════════════════════════════════════════
# LLM span — OpenInference‐compliant with token tracking
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_llm_call(
    model: str,
    messages: list[dict],
    temperature: float = 0.0,
    page_num: int = 0,
    total_pages: int = 0,
) -> Iterator[dict[str, Any]]:
    """
    LLM span with full OpenInference attributes.

    After the LLM call, set:
        ctx["response"] = raw response text
        ctx["usage"]    = {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    """
    ctx: dict[str, Any] = {"response": None, "usage": {}}

    if _TRACER is None:
        yield ctx
        return

    span_name = f"llm.chat page_{page_num}/{total_pages}" if page_num else "llm.chat"

    with _TRACER.start_as_current_span(span_name) as span:
        start = time.perf_counter()

        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", model)

        # Build text-only input (skip base64 images to avoid huge spans)
        input_messages = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                input_messages.append({"role": msg["role"], "content": content})
            elif isinstance(content, list):
                text_parts = [
                    part["text"] for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                image_count = sum(
                    1 for part in content
                    if isinstance(part, dict) and part.get("type") == "image_url"
                )
                input_messages.append({
                    "role": msg["role"],
                    "content": " ".join(text_parts),
                    "_images_omitted": image_count,
                })

        span.set_attribute("input.value", _safe_json(input_messages))
        span.set_attribute("input.mime_type", "application/json")
        span.set_attribute("llm.invocation_parameters", _safe_json({
            "temperature": temperature,
        }))

        if page_num:
            span.set_attribute("page.number", page_num)
            span.set_attribute("page.total", total_pages)

        try:
            yield ctx

            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("llm.latency_ms", elapsed_ms)

            if ctx.get("response"):
                span.set_attribute("output.value", _safe_json(ctx["response"]))
                span.set_attribute("output.mime_type", "application/json")

            usage = ctx.get("usage") or {}
            if usage:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if key in usage:
                        otel_key = key.replace("_tokens", "").replace("_", ".")
                        span.set_attribute(f"llm.token_count.{otel_key}", usage[key])

            span.set_status(Status(StatusCode.OK))

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("llm.latency_ms", elapsed_ms)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# Per-page extraction span (parent of build_user_message + llm_call)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_page_extraction(page_num: int, total_pages: int) -> Iterator[dict[str, Any]]:
    """Span for a single page's extraction (wraps user-message build + LLM call)."""
    with trace_span(
        f"page_{page_num}_extraction",
        kind="CHAIN",
        attributes={"page.number": page_num, "page.total": total_pages},
    ) as ctx:
        yield ctx


@contextmanager
def trace_build_user_message(page_num: int, total_pages: int) -> Iterator[dict[str, Any]]:
    """Span for building the user message for one page."""
    with trace_span(
        "build_user_message",
        kind="TOOL",
        attributes={"page.number": page_num, "page.total": total_pages},
    ) as ctx:
        yield ctx


# ═══════════════════════════════════════════════════════════════════════
# Merge results
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_merge_results(total_pages: int, format_type: str) -> Iterator[dict[str, Any]]:
    """Span for merging per-page results into final extraction output."""
    with trace_span(
        "merge_results",
        kind="CHAIN",
        input_data={"total_pages": total_pages, "format_type": format_type},
    ) as ctx:
        yield ctx


# ═══════════════════════════════════════════════════════════════════════
# OCR per-page (child of paddleocr_batch)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_ocr_page(page_num: int) -> Iterator[dict[str, Any]]:
    """Span for PaddleOCR on a single page."""
    with trace_span(
        f"ocr_page_{page_num}",
        kind="TOOL",
        attributes={"page.number": page_num},
    ) as ctx:
        yield ctx


# ═══════════════════════════════════════════════════════════════════════
# Convenience aliases used by main.py inline path
# ═══════════════════════════════════════════════════════════════════════

# Root trace for the inline SSE extraction path
trace_extraction_pipeline = trace_extraction_root

# Kept for backward compat with main.py
trace_named_step = trace_span


@contextmanager
def trace_file_upload(filename: str, file_size_bytes: int, file_type: str) -> Iterator[dict[str, Any]]:
    """Span for file upload."""
    with trace_span(
        "file_upload",
        kind="TOOL",
        input_data={"filename": filename, "size_bytes": file_size_bytes, "type": file_type},
    ) as ctx:
        yield ctx


@contextmanager
def trace_pdf_rendering(filename: str, total_pages: int = 0) -> Iterator[dict[str, Any]]:
    """Span for PDF-to-images rendering."""
    with trace_span(
        "pdf_rendering",
        kind="TOOL",
        input_data={"filename": filename},
    ) as ctx:
        yield ctx


@contextmanager
def trace_prompt_building(vendor_id: str, prompt_hash: str = "", cache_hit: str = "miss") -> Iterator[dict[str, Any]]:
    """Span for system prompt building."""
    with trace_span(
        "prompt_building",
        kind="CHAIN",
        input_data={"vendor_id": vendor_id},
        attributes={"prompt.cache_hit": cache_hit, "prompt.hash": prompt_hash},
    ) as ctx:
        yield ctx


@contextmanager
def trace_text_matching(total_fields: int) -> Iterator[dict[str, Any]]:
    """Span for text matching."""
    with trace_span(
        "text_matching",
        kind="TOOL",
        input_data={"total_fields": total_fields},
    ) as ctx:
        yield ctx


@contextmanager
def trace_db_persist(extraction_id: int, status: str) -> Iterator[dict[str, Any]]:
    """Span for persisting results to database."""
    with trace_span(
        "db_persist",
        kind="TOOL",
        input_data={"extraction_id": extraction_id, "status": status},
    ) as ctx:
        yield ctx


@contextmanager
def trace_paddle_ocr(total_pages: int) -> Iterator[dict[str, Any]]:
    """Span for PaddleOCR batch run."""
    with trace_span(
        "paddleocr_batch",
        kind="TOOL",
        input_data={"total_pages": total_pages},
    ) as ctx:
        yield ctx


# ═══════════════════════════════════════════════════════════════════════
# Backward-compat stubs (used in legacy code paths)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def start_span(*args, **kwargs) -> Iterator[Any]:
    yield None


def set_span_output(*args, **kwargs) -> None:
    pass
