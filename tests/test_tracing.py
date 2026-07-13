"""
test_tracing.py — contract tests for MLflow tracing.

We do NOT test MLflow itself and we never need a live tracking server.
We lock the two properties that actually matter in production:

  1. Tracing is NON-FATAL: disabled, or if mlflow raises, span() degrades
     to a clean no-op and the caller's code (and its exceptions) are
     unaffected.
  2. The import surface is stable: every legacy trace_* name stays
     importable and usable as a context manager (this exact thing broke
     once with trace_file_upload).
"""
from __future__ import annotations

import unittest
from unittest import mock

from backend import mlflow_tracing as mlf


LEGACY_CTX_NAMES = [
    "trace_span", "trace_named_step", "trace_pipeline_stage",
    "trace_extraction_root", "trace_extraction_pipeline", "trace_llm_call",
    "trace_page_extraction", "trace_build_user_message", "trace_merge_results",
    "trace_file_upload", "trace_pdf_rendering", "trace_text_matching",
    "trace_db_persist", "trace_paddle_ocr", "trace_prompt_building",
    "trace_ocr_page", "use_trace_context", "start_span",
]


class TracingNonFatalTests(unittest.TestCase):
    def test_span_is_noop_when_disabled(self):
        with mock.patch("backend.config.MLFLOW_ENABLED", False):
            with mlf.span("llm.chat", mlf.LLM, inputs={"x": 1}) as ctx:
                ctx["outputs"] = "ok"
                ctx["token_usage"] = {"input_tokens": 1, "output_tokens": 2,
                                      "total_tokens": 3}
        self.assertIsInstance(ctx, dict)

    def test_span_never_breaks_caller_when_mlflow_raises(self):
        with mock.patch("backend.config.MLFLOW_ENABLED", True), \
             mock.patch.object(mlf.mlflow, "start_span",
                               side_effect=RuntimeError("tracking server down")):
            ran = False
            with mlf.span("field_extraction", mlf.AGENT) as ctx:
                ran = True
                ctx["outputs"] = {"pages": 2}
            self.assertTrue(ran)
            self.assertIsInstance(ctx, dict)

    def test_caller_exception_propagates_through_span(self):
        with mock.patch("backend.config.MLFLOW_ENABLED", False):
            with self.assertRaises(ValueError):
                with mlf.span("page 1", mlf.CHAIN):
                    raise ValueError("page failed")

    def test_trace_tags_never_raises(self):
        with mock.patch("backend.config.MLFLOW_ENABLED", True), \
             mock.patch.object(mlf.mlflow, "update_current_trace",
                               side_effect=RuntimeError("no active trace")):
            mlf.trace_tags(extraction_id=1, vendor="V1", model="qwen3vl")

    def test_setup_failure_opens_breaker(self):
        mlf._reset_breaker()
        try:
            with mock.patch("backend.config.MLFLOW_ENABLED", True), \
                 mock.patch.object(mlf.mlflow, "set_tracking_uri"), \
                 mock.patch.object(mlf.mlflow, "set_experiment",
                                   side_effect=RuntimeError("tracking down")):
                mlf.setup_mlflow()
            self.assertTrue(mlf._breaker_open())
        finally:
            mlf._reset_breaker()


class CleanChatMessagesTests(unittest.TestCase):
    def test_base64_image_is_stripped(self):
        messages = [
            {"role": "system", "content": "you are an assistant"},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,/9j/HUGE"}},
                {"type": "text", "text": "extract fields"},
            ]},
        ]
        cleaned = mlf.clean_chat_messages(messages)
        blob = str(cleaned)
        self.assertNotIn("base64", blob)
        self.assertNotIn("/9j/HUGE", blob)
        self.assertIn("extract fields", blob)
        self.assertEqual(cleaned[0]["content"], "you are an assistant")


class ImportSurfaceTests(unittest.TestCase):
    def test_legacy_names_importable_and_usable_as_context_managers(self):
        for name in LEGACY_CTX_NAMES:
            self.assertTrue(hasattr(mlf, name), f"missing legacy name: {name}")
            factory = getattr(mlf, name)
            with factory("x", kind="TOOL", page_num=1) as ctx:  # arbitrary args
                # legacy shims must accept anything and yield safely
                if isinstance(ctx, dict):
                    ctx["output"] = "ignored"

    def test_real_span_types_exposed(self):
        for t in (mlf.AGENT, mlf.CHAIN, mlf.LLM, mlf.TOOL, mlf.PARSER):
            self.assertIsInstance(t, str)


if __name__ == "__main__":
    unittest.main()
