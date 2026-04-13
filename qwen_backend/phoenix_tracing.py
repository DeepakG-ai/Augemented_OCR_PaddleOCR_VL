"""
Arize Phoenix tracing — full pipeline observability for document extraction.

Provides hierarchical tracing for the entire extraction workflow:
    document_extraction (root span)
    ├── file_upload
    ├── pdf_to_images
    ├── prompt_building
    ├── page_N_extraction
    │   ├── build_user_message
    │   └── llm_call (with full prompt/response/token data)
    ├── merge_results
    ├── paddle_ocr
    ├── text_matching
    └── db_persist

Auto-instrumentation captures LangChain/OpenAI client calls if available.
Manual spans capture raw httpx LLM calls with full prompt/response/token data.
"""
import os
import time
import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace, context as otel_context
from opentelemetry.trace import Status, StatusCode, Span

logger = logging.getLogger(__name__)

# Suppress verbose OpenTelemetry exporter errors when the Phoenix collector is offline
logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(logging.CRITICAL)

_INITIALIZED = False
_TRACER = None


def setup_phoenix(project_name: str = "augmented_ocr") -> None:
    global _INITIALIZED, _TRACER
    if _INITIALIZED:
        return
    _INITIALIZED = True

    enabled = os.getenv("PHOENIX_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("[Phoenix] Tracing disabled")
        return

    try:
        from phoenix.otel import register

        tracer_provider = register(
            project_name=project_name,
            endpoint=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:4317"),
        )

        # Try auto-instrumentors (optional — only if openai/langchain clients are used)
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
        except ImportError:
            pass

        try:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        except ImportError:
            pass

        # Manual tracer for pipeline spans
        _TRACER = trace.get_tracer("augmented_ocr")
        logger.info(f"[Phoenix] Tracing ENABLED → project={project_name}")

    except ImportError as e:
        logger.warning(f"[Phoenix] Not installed, skipping: {e}")
    except Exception as e:
        logger.error(f"[Phoenix] Init failed: {e}", exc_info=True)


def _safe_json(data: Any) -> str:
    """Safely serialize data to JSON string for span attributes."""
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return str(data)


# ── Helper: get current OTel context token ────────────────────────────
# This is used to propagate the parent span across async boundaries.

def get_current_context():
    """Return the current OpenTelemetry context (for passing to async tasks)."""
    return otel_context.get_current()


def attach_context(ctx):
    """Attach a saved context to the current execution."""
    return otel_context.attach(ctx)


def detach_context(token):
    """Detach a previously attached context."""
    otel_context.detach(token)


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE SPAN: Root span for entire document extraction
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_extraction_pipeline(
    extraction_id: int,
    vendor_id: str,
    filename: str,
    total_pages: int,
    format_type: str,
    header_fields: list[str],
    line_item_fields: list[str],
) -> Iterator[dict]:
    """
    Root span for the entire document extraction pipeline.
    All child spans (pdf_to_images, llm_call, etc.) nest under this.

    Usage:
        with trace_extraction_pipeline(...) as pipeline:
            # ... do extraction work ...
            pipeline["result"] = final_result
            pipeline["status"] = "done"
    """
    pipeline_ctx = {"result": None, "status": "unknown", "error": None}

    if _TRACER is None:
        yield pipeline_ctx
        return

    with _TRACER.start_as_current_span("document_extraction") as span:
        start_time = time.perf_counter()

        # Core identifiers
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("extraction.id", extraction_id)
        span.set_attribute("extraction.vendor_id", vendor_id)
        span.set_attribute("extraction.filename", filename)
        span.set_attribute("extraction.total_pages", total_pages)
        span.set_attribute("extraction.format_type", format_type)

        # Fields being extracted
        span.set_attribute("extraction.header_fields", _safe_json(header_fields))
        span.set_attribute("extraction.line_item_fields", _safe_json(line_item_fields))

        # Input summary
        span.set_attribute("input.value", _safe_json({
            "filename": filename,
            "vendor_id": vendor_id,
            "total_pages": total_pages,
            "format_type": format_type,
            "header_fields": header_fields,
            "line_item_fields": line_item_fields,
        }))

        try:
            yield pipeline_ctx

            # After pipeline completes
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            span.set_attribute("extraction.duration_ms", elapsed_ms)
            span.set_attribute("extraction.status", pipeline_ctx.get("status", "unknown"))

            if pipeline_ctx.get("result"):
                result = pipeline_ctx["result"]
                # Summary output — don't dump the entire result (can be huge)
                line_items = result.get("line_items", []) if isinstance(result, dict) else []
                output_summary = {
                    "status": pipeline_ctx.get("status"),
                    "duration_ms": elapsed_ms,
                    "header_fields_extracted": len([
                        k for k, v in result.items()
                        if k != "line_items" and v is not None
                    ]) if isinstance(result, dict) else 0,
                    "line_items_count": len(line_items),
                }
                span.set_attribute("output.value", _safe_json(output_summary))
                span.set_attribute("extraction.line_items_count", len(line_items))

            if pipeline_ctx.get("error"):
                span.set_attribute("extraction.error", str(pipeline_ctx["error"]))

            span.set_status(Status(StatusCode.OK))

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            span.set_attribute("extraction.duration_ms", elapsed_ms)
            span.set_attribute("extraction.status", "failed")
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: File upload / preparation
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_file_upload(filename: str, file_size_bytes: int, file_type: str) -> Iterator[dict]:
    """Span for file upload and initial validation."""
    ctx = {}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("file_upload") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("file.name", filename)
        span.set_attribute("file.size_bytes", file_size_bytes)
        span.set_attribute("file.type", file_type)
        span.set_attribute("input.value", _safe_json({
            "filename": filename,
            "size_bytes": file_size_bytes,
            "type": file_type,
        }))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: PDF → Images rendering
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_pdf_rendering(filename: str, total_pages: int = 0) -> Iterator[dict]:
    """Span for PDF-to-images conversion (processor.py)."""
    ctx = {"pages_rendered": 0}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("pdf_to_images") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("file.name", filename)
        span.set_attribute("input.value", _safe_json({"filename": filename}))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("rendering.pages_rendered", ctx.get("pages_rendered", 0))
            span.set_attribute("output.value", _safe_json({
                "pages_rendered": ctx.get("pages_rendered", 0),
                "duration_ms": elapsed_ms,
            }))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: System prompt building / cache lookup
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_prompt_building(
    vendor_id: str,
    prompt_hash: str = "",
    cache_hit: str = "miss",
) -> Iterator[dict]:
    """Span for system prompt building / cache lookup."""
    ctx = {"system_prompt": "", "cache_hit": cache_hit, "prompt_hash": prompt_hash}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("prompt_building") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("prompt.vendor_id", vendor_id)
        span.set_attribute("input.value", _safe_json({"vendor_id": vendor_id}))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("prompt.cache_hit", ctx.get("cache_hit", "miss"))
            span.set_attribute("prompt.hash", ctx.get("prompt_hash", ""))

            # Store the full system prompt as output
            if ctx.get("system_prompt"):
                span.set_attribute("output.value", ctx["system_prompt"])

            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: Per-page extraction (wraps build_user_message + llm_call)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_page_extraction(page_num: int, total_pages: int) -> Iterator[dict]:
    """
    Span for a single page's extraction (parent of build_user_message + llm_call).
    """
    ctx = {"user_message": "", "result": None, "error": None}
    if _TRACER is None:
        yield ctx
        return

    span_name = f"page_{page_num}_extraction"
    with _TRACER.start_as_current_span(span_name) as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("page.number", page_num)
        span.set_attribute("page.total", total_pages)

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)

            if ctx.get("error"):
                span.set_attribute("page.error", str(ctx["error"]))
                span.set_status(Status(StatusCode.ERROR, str(ctx["error"])))
            else:
                # Show what came back for this page
                result = ctx.get("result")
                if result and isinstance(result, dict):
                    line_items = result.get("line_items", [])
                    output_summary = {
                        "page": page_num,
                        "line_items_count": len(line_items),
                        "fields": [k for k in result.keys() if k not in ("_page", "_total_pages", "_error", "line_items")],
                    }
                    span.set_attribute("output.value", _safe_json(output_summary))
                    span.set_attribute("page.line_items_count", len(line_items))
                span.set_status(Status(StatusCode.OK))

        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: Build user message (child of page extraction)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_build_user_message(page_num: int, total_pages: int) -> Iterator[dict]:
    """Span for building the user message for a page."""
    ctx = {"user_message": ""}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("build_user_message") as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("page.number", page_num)
        span.set_attribute("page.total", total_pages)

        try:
            yield ctx
            if ctx.get("user_message"):
                span.set_attribute("output.value", ctx["user_message"])
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: LLM Call (child of page extraction)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_llm_call(
    model: str,
    messages: list[dict],
    temperature: float = 0.0,
    page_num: int = 0,
    total_pages: int = 0,
) -> Iterator[dict]:
    """
    Context manager that creates a span for an LLM call.

    Usage:
        with trace_llm_call(model, messages, temperature, page_num, total_pages) as trace_ctx:
            resp = await client.post(...)
            trace_ctx["response"] = resp_content  # set response text
            trace_ctx["usage"] = resp_usage        # set token counts

    Phoenix will display: prompt, response, tokens, latency, model name.
    """
    trace_ctx = {"response": None, "usage": {}}

    if _TRACER is None:
        yield trace_ctx
        return

    span_name = f"llm.chat page_{page_num}/{total_pages}" if page_num else "llm.chat"

    with _TRACER.start_as_current_span(span_name) as span:
        start_time = time.perf_counter()

        # ── OpenInference semantic attributes ──
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", model)

        # Input: system prompt + user message (skip base64 images to avoid huge spans)
        input_messages = []
        for msg in messages:
            if isinstance(msg.get("content"), str):
                input_messages.append({"role": msg["role"], "content": msg["content"]})
            elif isinstance(msg.get("content"), list):
                # Extract only text parts, skip image_url (base64 is huge)
                text_parts = [
                    part["text"] for part in msg["content"]
                    if part.get("type") == "text"
                ]
                input_messages.append({
                    "role": msg["role"],
                    "content": " ".join(text_parts),
                    "_note": "image_url omitted from trace"
                })

        span.set_attribute("input.value", _safe_json(input_messages))
        span.set_attribute("llm.invocation_parameters", _safe_json({
            "temperature": temperature,
        }))

        if page_num:
            span.set_attribute("page_number", page_num)
            span.set_attribute("total_pages", total_pages)

        try:
            yield trace_ctx

            # ── After the LLM call completes ──
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            span.set_attribute("llm.latency_ms", elapsed_ms)

            # Output
            if trace_ctx.get("response"):
                span.set_attribute("output.value", _safe_json(trace_ctx["response"]))

            # Token counts
            usage = trace_ctx.get("usage", {})
            if usage:
                if "prompt_tokens" in usage:
                    span.set_attribute("llm.token_count.prompt", usage["prompt_tokens"])
                if "completion_tokens" in usage:
                    span.set_attribute("llm.token_count.completion", usage["completion_tokens"])
                if "total_tokens" in usage:
                    span.set_attribute("llm.token_count.total", usage["total_tokens"])

            span.set_status(Status(StatusCode.OK))

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            span.set_attribute("llm.latency_ms", elapsed_ms)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: Merge results (multi-page)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_merge_results(total_pages: int, format_type: str) -> Iterator[dict]:
    """Span for merging per-page results into final output."""
    ctx = {"merged_line_items": 0, "merged_fields": 0}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("merge_results") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("merge.total_pages", total_pages)
        span.set_attribute("merge.format_type", format_type)
        span.set_attribute("input.value", _safe_json({
            "total_pages": total_pages,
            "format_type": format_type,
        }))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("output.value", _safe_json({
                "merged_line_items": ctx.get("merged_line_items", 0),
                "merged_header_fields": ctx.get("merged_fields", 0),
            }))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: PaddleOCR
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_paddle_ocr(total_pages: int) -> Iterator[dict]:
    """Span for PaddleOCR text detection across all pages."""
    ctx = {"total_words": 0, "pages_processed": 0}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("paddle_ocr") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("ocr.total_pages", total_pages)
        span.set_attribute("input.value", _safe_json({"total_pages": total_pages}))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("ocr.total_words", ctx.get("total_words", 0))
            span.set_attribute("ocr.pages_processed", ctx.get("pages_processed", 0))
            span.set_attribute("output.value", _safe_json({
                "total_words": ctx.get("total_words", 0),
                "pages_processed": ctx.get("pages_processed", 0),
                "duration_ms": elapsed_ms,
            }))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: OCR per-page (child of paddle_ocr)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_ocr_page(page_num: int) -> Iterator[dict]:
    """Span for OCR on a single page."""
    ctx = {"words_detected": 0}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span(f"ocr_page_{page_num}") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("page.number", page_num)

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("ocr.words_detected", ctx.get("words_detected", 0))
            span.set_attribute("output.value", _safe_json({
                "page": page_num,
                "words_detected": ctx.get("words_detected", 0),
                "duration_ms": elapsed_ms,
            }))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: Text matching (field→bounding box mapping)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_text_matching(total_fields: int) -> Iterator[dict]:
    """Span for matching extracted values to OCR bounding boxes."""
    ctx = {"matched": 0, "missed": 0, "strategies": {}}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("text_matching") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("matching.total_fields", total_fields)
        span.set_attribute("input.value", _safe_json({"total_fields": total_fields}))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("matching.matched", ctx.get("matched", 0))
            span.set_attribute("matching.missed", ctx.get("missed", 0))
            span.set_attribute("output.value", _safe_json({
                "matched": ctx.get("matched", 0),
                "missed": ctx.get("missed", 0),
                "strategies_used": ctx.get("strategies", {}),
                "duration_ms": elapsed_ms,
            }))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# STEP: DB persist (save results + cache)
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def trace_db_persist(extraction_id: int, status: str) -> Iterator[dict]:
    """Span for persisting results to database and cache."""
    ctx = {}
    if _TRACER is None:
        yield ctx
        return

    with _TRACER.start_as_current_span("db_persist") as span:
        start = time.perf_counter()
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("db.extraction_id", extraction_id)
        span.set_attribute("db.status", status)
        span.set_attribute("input.value", _safe_json({
            "extraction_id": extraction_id,
            "status": status,
        }))

        try:
            yield ctx
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            span.set_attribute("duration_ms", elapsed_ms)
            span.set_attribute("output.value", _safe_json({
                "extraction_id": extraction_id,
                "status": status,
                "duration_ms": elapsed_ms,
            }))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ═══════════════════════════════════════════════════════════════════════
# Backward-compat stubs
# ═══════════════════════════════════════════════════════════════════════

@contextmanager
def start_span(*args, **kwargs) -> Iterator[Any]:
    yield None

def set_span_output(*args, **kwargs) -> None:
    pass
