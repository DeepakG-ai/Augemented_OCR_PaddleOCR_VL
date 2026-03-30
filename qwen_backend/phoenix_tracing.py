"""
Arize Phoenix tracing via auto-instrumentation.
"""
import os
import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Suppress verbose OpenTelemetry exporter errors when the Phoenix collector is offline
logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(logging.CRITICAL)

_INITIALIZED = False


def setup_phoenix(project_name: str = "augmented_ocr") -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    enabled = os.getenv("PHOENIX_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("[Phoenix] Tracing disabled")
        return

    try:
        from phoenix.otel import register
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from openinference.instrumentation.openai import OpenAIInstrumentor

        tracer_provider = register(
            project_name=project_name,
            endpoint=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:4317"),
        )

        # Auto-instruments ALL LangGraph nodes, LLM calls, chain executions
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

        # Auto-instruments your Qwen/OpenAI-compatible LLM calls
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

        logger.info(f"[Phoenix] Tracing ENABLED → project={project_name}")

    except ImportError as e:
        logger.warning(f"[Phoenix] Not installed, skipping: {e}")
    except Exception as e:
        logger.error(f"[Phoenix] Init failed: {e}", exc_info=True)


# For backward compatibility with extractor.py, we provide a dummy context manager.
# Because OpenAIInstrumentor automatically captures OpenAI client calls, 
# manual span tracking is no longer needed.
@contextmanager
def start_span(*args, **kwargs) -> Iterator[Any]:
    yield None

def set_span_output(*args, **kwargs) -> None:
    pass
