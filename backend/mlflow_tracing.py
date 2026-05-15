"""
Simplified MLflow Tracing.
One trace per PDF extraction using OpenTelemetry span propagation.
"""
import logging
from contextlib import contextmanager
from typing import Any, Iterator
# pyrefly: ignore [missing-import]
import mlflow
import mlflow.tracing

logger = logging.getLogger(__name__)

def setup_mlflow(experiment_name="augmented_ocr", force=False):
    from .config import MLFLOW_ENABLED, MLFLOW_TRACKING_URI
    if MLFLOW_ENABLED:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        mlflow.set_experiment(experiment_name)

def current_trace_context() -> dict:
    """Serialize the MLflow trace context for job payload."""
    try:
        return mlflow.tracing.get_tracing_context_headers_for_http_request()
    except Exception:
        return {}

def get_current_context():
    return None

def attach_context(ctx):
    return None

def detach_context(token):
    pass

def context_from_carrier(carrier: dict | None) -> dict:
    return carrier or {}

@contextmanager
def use_trace_context(carrier: dict | None):
    """Resume the trace encoded in carrier for a block of worker code."""
    if carrier:
        with mlflow.tracing.set_tracing_context_from_http_request_headers(carrier):
            yield
    else:
        yield

@contextmanager
def trace_span(name: str, kind: str = "CHAIN", input_data=None, attributes=None) -> Iterator[dict]:
    """Universal simple span builder."""
    with mlflow.start_span(name=name) as span:
        if input_data:
            try:
                span.set_inputs(input_data)
            except Exception as e:
                logger.debug("MLflow set_inputs failed: %s", e)
        if attributes:
            try:
                span.set_attributes(attributes)
            except Exception as e:
                logger.debug("MLflow set_attributes failed: %s", e)

        ctx = {"output": None}
        try:
            yield ctx
        finally:
            if ctx.get("output") is not None:
                try:
                    span.set_outputs(ctx["output"])
                except Exception as e:
                    logger.debug("MLflow set_outputs failed: %s", e)

@contextmanager
def trace_extraction_root(extraction_id, vendor_id, filename, total_pages, format_type, header_fields, line_item_fields, vendor_name=None):
    """Root span for one PDF's entire lifecycle."""
    attrs = {"extraction_id": extraction_id, "vendor_id": vendor_id, "filename": filename, "pages": total_pages}
    with trace_span(f"Extraction: {filename or extraction_id}", attributes=attrs) as ctx:
        yield ctx

trace_extraction_pipeline = trace_extraction_root

@contextmanager
def trace_pipeline_stage(stage: str, **kwargs):
    with trace_span(f"stage.{stage}", attributes=kwargs) as ctx:
        yield ctx

@contextmanager
def trace_llm_call(model, messages, **kwargs):
    # Extract text content from messages (omitting massive base64 images)
    clean_messages = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            clean_messages.append({"role": msg.get("role"), "content": content})
        elif isinstance(content, list):
            text_parts = [p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"]
            clean_messages.append({"role": msg.get("role"), "content": " ".join(text_parts)})
            
    input_data = {"model": model, "messages": clean_messages}
    with trace_span("llm.chat", input_data=input_data, attributes=kwargs) as ctx:
        yield ctx
        if "response" in ctx:
            ctx["output"] = ctx["response"]

# Convenience aliases for individual steps
def trace_named_step(name, **kwargs): return trace_span(name, **kwargs)
def trace_page_extraction(page_num, total_pages): return trace_span(f"page_{page_num}_extraction", attributes={"page": page_num})
def trace_build_user_message(page_num, total_pages): return trace_span("build_user_message", attributes={"page": page_num})
def trace_merge_results(total_pages, format_type): return trace_span("merge_results", attributes={"total_pages": total_pages})
def trace_ocr_page(page_num): return trace_span(f"ocr_page_{page_num}")
def trace_file_upload(filename, file_size_bytes, file_type): return trace_span("file_upload", input_data={"filename": filename, "size": file_size_bytes})
def trace_pdf_rendering(filename, **kwargs): return trace_span("pdf_rendering", input_data={"filename": filename})
def trace_prompt_building(vendor_id, **kwargs): return trace_span("prompt_building", input_data={"vendor_id": vendor_id})
def trace_text_matching(total_fields): return trace_span("text_matching", input_data={"fields": total_fields})
def trace_db_persist(extraction_id, status): return trace_span("db_persist", input_data={"id": extraction_id, "status": status})
def trace_paddle_ocr(total_pages): return trace_span("paddleocr_batch", input_data={"total_pages": total_pages})

@contextmanager
def start_span(*args, **kwargs): yield None
def set_span_output(*args, **kwargs): pass
