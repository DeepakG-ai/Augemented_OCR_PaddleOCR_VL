"""
test_logging_config.py -- Unit tests for the logging overhaul.

Covers:
  logging_config.py  — double-init guard, QueueHandler/Listener, ExtractionLogHandler
                       buffering + flush cadence, error correlation (_trace_ref),
                       _SecurityOnlyFilter, shutdown_logging(), CloudWatch fallback.
  page_logger.py     — persistent file handle, env-configurable paths, write correctness.
  auth.py            — security logger fires on invalid token.

Windows note: QueueListener holds file handles open in a background thread.
All configure_logging() tests must call shutdown_logging() BEFORE the
TemporaryDirectory context exits, otherwise Windows raises PermissionError
when it tries to delete the temp dir while handles are still open.

Run:
  .venv\\Scripts\\python.exe -m pytest tests/test_logging_config.py -v
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.logging_config as lc
import backend.page_logger as pl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(name: str = "pipeline", level: int = logging.INFO, msg: str = "msg") -> logging.LogRecord:
    return logging.LogRecord(name, level, "", 0, msg, (), None)


def _configure_in_tmp(tmp_path: Path) -> None:
    """Call configure_logging() with log dirs redirected to a temp dir."""
    with patch.object(lc, "_PIPELINE_LOG_DIR", tmp_path), \
         patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path / "extractions"):
        lc.configure_logging()


# ---------------------------------------------------------------------------
# 1. Double-init guard
# ---------------------------------------------------------------------------

class DoubleInitGuardTests(unittest.TestCase):

    def setUp(self):
        lc.shutdown_logging()

    def tearDown(self):
        lc.shutdown_logging()

    def test_second_call_is_no_op(self):
        """configure_logging() called twice must not double-add handlers."""
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            _configure_in_tmp(Path(tmp))  # second call — must be ignored
            root = logging.getLogger()
            # Exactly console + queue_handler = 2 (not 4 from two full setups)
            count = len(root.handlers)
            lc.shutdown_logging()   # close handles BEFORE tmpdir cleanup (Windows)
        self.assertEqual(count, 2,
                         f"Expected 2 handlers after double configure, got {count}")

    def test_configured_flag_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            flag = lc._CONFIGURED
            lc.shutdown_logging()
        self.assertTrue(flag)

    def test_shutdown_resets_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            lc.shutdown_logging()   # shutdown inside context so handles close first
        self.assertFalse(lc._CONFIGURED)


# ---------------------------------------------------------------------------
# 2. QueueHandler / QueueListener wiring
# ---------------------------------------------------------------------------

class QueueHandlerTests(unittest.TestCase):

    def setUp(self):
        lc.shutdown_logging()

    def tearDown(self):
        lc.shutdown_logging()

    def test_queue_handler_in_root_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            handler_types = [type(h) for h in logging.getLogger().handlers]
            lc.shutdown_logging()
        self.assertIn(logging.handlers.QueueHandler, handler_types)

    def test_console_handler_in_root_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            handler_types = [type(h) for h in logging.getLogger().handlers]
            lc.shutdown_logging()
        self.assertIn(logging.StreamHandler, handler_types)

    def test_queue_listener_alive_before_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            is_alive = lc._QUEUE_LISTENER is not None and lc._QUEUE_LISTENER._thread.is_alive()
            lc.shutdown_logging()
        self.assertTrue(is_alive)

    def test_security_log_file_created_after_emit(self):
        """Emitting to the 'security' logger must create security.log on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _configure_in_tmp(tmp_path)
            logging.getLogger("security").info("login.success  user=test  role=admin  ip=127.0.0.1")
            lc.shutdown_logging()   # drains queue and closes handles
            exists = (tmp_path / "security.log").exists()
        self.assertTrue(exists)

    def test_exactly_two_root_handlers(self):
        """Root must have exactly console (sync) + queue_handler (async) — nothing else."""
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            count = len(logging.getLogger().handlers)
            lc.shutdown_logging()
        self.assertEqual(count, 2)


# ---------------------------------------------------------------------------
# 3. ExtractionLogHandler — buffering, flush cadence, close
# ---------------------------------------------------------------------------

class ExtractionHandlerTests(unittest.TestCase):

    def test_handle_is_open_and_writable(self):
        """_handle_for() must open the file for appending with buffering=8192."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            tok = lc.current_extraction_id.set(10)
            try:
                with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                    h.emit(_make_record())
                fh = h._handles[10]
                writable = not fh.closed and fh.writable()
            finally:
                lc.current_extraction_id.reset(tok)
                h.close()
        self.assertTrue(writable)

    def test_flush_triggered_on_error_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            tok = lc.current_extraction_id.set(11)
            flush_count = []
            try:
                with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                    h.emit(_make_record())              # INFO record — opens handle
                    fh = h._handles[11]
                    _real = fh.flush
                    fh.flush = lambda: (flush_count.append(1), _real())[1]
                    h.emit(_make_record(level=logging.ERROR, msg="boom"))
            finally:
                lc.current_extraction_id.reset(tok)
                h.close()
        self.assertGreater(len(flush_count), 0,
                           "flush() must fire immediately on an ERROR record")

    def test_flush_triggered_at_20_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            tok = lc.current_extraction_id.set(12)
            flush_count = []
            count_at_20 = 0
            try:
                with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                    for i in range(19):
                        h.emit(_make_record(msg=f"msg{i}"))
                    fh = h._handles[12]
                    _real = fh.flush
                    fh.flush = lambda: (flush_count.append(1), _real())[1]
                    h.emit(_make_record(msg="msg19"))   # 20th → flush
                    count_at_20 = len(flush_count)
                    fh.flush = _real  # restore before h.close() triggers its own flush
            finally:
                lc.current_extraction_id.reset(tok)
                h.close()
        self.assertEqual(count_at_20, 1,
                         "flush() must fire exactly once at the 20th record")

    def test_no_premature_flush_before_20(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            tok = lc.current_extraction_id.set(13)
            flush_count = []
            count_before_20 = -1
            try:
                with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                    h.emit(_make_record())              # record 1 — opens handle
                    fh = h._handles[13]
                    _real = fh.flush
                    fh.flush = lambda: (flush_count.append(1), _real())[1]
                    for i in range(8):                  # records 2-9 (still < 20)
                        h.emit(_make_record(msg=f"x{i}"))
                    count_before_20 = len(flush_count)
                    fh.flush = _real   # restore before h.close() triggers its own flush
            finally:
                lc.current_extraction_id.reset(tok)
                h.close()
        self.assertEqual(count_before_20, 0,
                         "flush() must NOT fire before the 20th record")

    def test_close_flushes_all_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            tok = lc.current_extraction_id.set(14)
            flush_count = []
            try:
                with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                    h.emit(_make_record())
                fh = h._handles[14]
                _real = fh.flush
                fh.flush = lambda: (flush_count.append(1), _real())[1]
                h.close()
                # Note: h.close() is called inside the tmpdir context so handles
                # are closed before the dir is deleted (Windows compatibility)
                flush_called = len(flush_count) > 0
            finally:
                lc.current_extraction_id.reset(tok)
        self.assertTrue(flush_called, "close() must flush each handle before closing")

    def test_no_emit_without_extraction_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                h.emit(_make_record())   # current_extraction_id is None by default
            h.close()
            created = list(tmp_path.iterdir())
        self.assertEqual(created, [],
                         "No file should be created when extraction_id is None")

    def test_counters_cleared_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            tok = lc.current_extraction_id.set(15)
            try:
                with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                    h.emit(_make_record())
                has_counter_before = 15 in h._counters
                h.close()
                empty_after = h._counters == {}
            finally:
                lc.current_extraction_id.reset(tok)
        self.assertTrue(has_counter_before)
        self.assertTrue(empty_after, "close() must clear _counters")


# ---------------------------------------------------------------------------
# 4. Error correlation — _trace_ref cross-links pipeline.log ↔ error.log
# ---------------------------------------------------------------------------

class ErrorCorrelationTests(unittest.TestCase):

    def test_error_returns_ref_starting_with_err(self):
        ref = lc.error("something broke")
        self.assertTrue(ref.startswith("err-"), f"Unexpected ref format: {ref}")

    def test_trace_ref_set_on_dispatched_record(self):
        """error() must stamp _trace_ref on the LogRecord before dispatch."""
        received: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                received.append(r)

        cap = _Cap()
        cap.setLevel(logging.ERROR)
        lg = logging.getLogger("pipeline")
        lg.addHandler(cap)
        try:
            ref = lc.error("pipeline error")
            self.assertTrue(len(received) > 0, "No record was dispatched")
            self.assertEqual(getattr(received[-1], "_trace_ref", None), ref)
        finally:
            lg.removeHandler(cap)

    def test_traceback_formatter_includes_ref_in_fence(self):
        fmt = lc._TracebackFormatter("%(message)s")
        record = _make_record(level=logging.ERROR, msg="kaboom")
        try:
            raise ValueError("simulated error")
        except ValueError:
            record.exc_info = sys.exc_info()
        record._trace_ref = "err-deadbe"  # type: ignore[attr-defined]
        output = fmt.format(record)
        self.assertIn("ref=err-deadbe", output,
                      "Fence line must include the trace ref for cross-log grep")

    def test_traceback_formatter_no_ref_fallback(self):
        """When _trace_ref is absent the fence still renders without error."""
        fmt = lc._TracebackFormatter("%(message)s")
        record = _make_record(level=logging.ERROR, msg="kaboom")
        try:
            raise ValueError("test")
        except ValueError:
            record.exc_info = sys.exc_info()
        # _trace_ref deliberately NOT set
        output = fmt.format(record)
        self.assertIn("└─ traceback", output)
        self.assertNotIn("ref=", output)

    def test_no_traceback_formatter_strips_exc_info(self):
        """_NoTracebackFormatter must never include traceback text."""
        fmt = lc._NoTracebackFormatter("%(message)s")
        record = _make_record(level=logging.ERROR, msg="oops")
        try:
            raise RuntimeError("hidden")
        except RuntimeError:
            record.exc_info = sys.exc_info()
        output = fmt.format(record)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("RuntimeError", output)

    def test_error_ref_embedded_in_inline_message(self):
        """The trace= key must appear in the formatted inline message."""
        received: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                received.append(r.getMessage())

        cap = _Cap()
        cap.setLevel(logging.ERROR)
        lg = logging.getLogger("pipeline")
        lg.addHandler(cap)
        try:
            ref = lc.error("bang")
            self.assertTrue(any(f"trace={ref}" in m for m in received),
                            f"Expected 'trace={ref}' in message; got: {received}")
        finally:
            lg.removeHandler(cap)


# ---------------------------------------------------------------------------
# 5. _SecurityOnlyFilter
# ---------------------------------------------------------------------------

class SecurityFilterTests(unittest.TestCase):

    def setUp(self):
        self._f = lc._SecurityOnlyFilter()

    def test_security_logger_passes(self):
        self.assertTrue(self._f.filter(_make_record(name="security")))

    def test_pipeline_logger_blocked(self):
        self.assertFalse(self._f.filter(_make_record(name="pipeline")))

    def test_root_logger_blocked(self):
        self.assertFalse(self._f.filter(_make_record(name="root")))

    def test_uvicorn_logger_blocked(self):
        self.assertFalse(self._f.filter(_make_record(name="uvicorn.access")))

    def test_security_child_logger_blocked(self):
        # Only the exact name 'security' passes, not children
        self.assertFalse(self._f.filter(_make_record(name="security.subsystem")))


# ---------------------------------------------------------------------------
# 5b. ContextFilter thread-safety: _ext_id and _filename stamped on record
# ---------------------------------------------------------------------------

class ContextFilterThreadSafetyTests(unittest.TestCase):
    """
    ContextFilter must stamp _ext_id and _filename on the LogRecord so that
    ExtractionLogHandler can read them from the QueueListener background thread
    without touching contextvars (which return None in that thread).
    """

    def test_ext_id_stamped_on_record(self):
        f = lc.ContextFilter()
        record = _make_record()
        tok = lc.current_extraction_id.set(999)
        try:
            f.filter(record)
        finally:
            lc.current_extraction_id.reset(tok)
        self.assertEqual(getattr(record, "_ext_id", "MISSING"), 999,
                         "_ext_id must be stamped on the record by ContextFilter")

    def test_filename_stamped_on_record(self):
        f = lc.ContextFilter()
        record = _make_record()
        tok_id = lc.current_extraction_id.set(1)
        tok_fn = lc.current_extraction_filename.set("invoice.pdf")
        try:
            f.filter(record)
        finally:
            lc.current_extraction_id.reset(tok_id)
            lc.current_extraction_filename.reset(tok_fn)
        self.assertEqual(getattr(record, "_filename", "MISSING"), "invoice.pdf")

    def test_ext_id_none_when_no_extraction(self):
        f = lc.ContextFilter()
        record = _make_record()
        f.filter(record)  # current_extraction_id is None by default
        self.assertIsNone(getattr(record, "_ext_id", "MISSING"))

    def test_ctx_ext_correct_when_extraction_set(self):
        f = lc.ContextFilter()
        record = _make_record()
        tok = lc.current_extraction_id.set(42)
        try:
            f.filter(record)
        finally:
            lc.current_extraction_id.reset(tok)
        self.assertIn("42", record.ctx_ext)

    def test_extraction_handler_reads_ext_id_from_record(self):
        """ExtractionLogHandler must use _ext_id from the record, not contextvar.
        This simulates the QueueListener thread where contextvars return None.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = lc.ExtractionLogHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            # Stamp _ext_id on the record as ContextFilter would in the originating thread
            record = _make_record(msg="from listener thread")
            record._ext_id = 77          # type: ignore[attr-defined]
            record._filename = "inv.pdf"  # type: ignore[attr-defined]
            # current_extraction_id contextvar is NOT set — simulates the listener thread
            with patch.object(lc, "_EXTRACTION_LOG_DIR", tmp_path):
                h.emit(record)
            h.close()
            # Read inside the context so the file still exists
            log_files = list(tmp_path.glob("*.log"))
            content = log_files[0].read_text() if log_files else ""
        self.assertTrue(len(log_files) > 0,
                        "ExtractionLogHandler must write using _ext_id from record even "
                        "when current_extraction_id contextvar is None (listener thread)")
        self.assertIn("from listener thread", content)


# ---------------------------------------------------------------------------
# 6. shutdown_logging()
# ---------------------------------------------------------------------------

class ShutdownLoggingTests(unittest.TestCase):

    def setUp(self):
        lc.shutdown_logging()

    def tearDown(self):
        lc.shutdown_logging()

    def test_shutdown_clears_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            self.assertIsNotNone(lc._QUEUE_LISTENER)
            lc.shutdown_logging()  # inside context — closes handles before dir delete
        self.assertIsNone(lc._QUEUE_LISTENER)

    def test_shutdown_resets_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            lc.shutdown_logging()
        self.assertFalse(lc._CONFIGURED)

    def test_shutdown_idempotent_when_not_configured(self):
        """Must not raise even when called without a prior configure_logging()."""
        lc.shutdown_logging()
        lc.shutdown_logging()

    def test_listener_thread_stops_after_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_in_tmp(Path(tmp))
            thread = lc._QUEUE_LISTENER._thread
            lc.shutdown_logging()
        self.assertFalse(thread.is_alive(),
                         "QueueListener thread must be dead after shutdown_logging()")


# ---------------------------------------------------------------------------
# 7. CloudWatch fallback (watchtower not installed)
# ---------------------------------------------------------------------------

class CloudWatchFallbackTests(unittest.TestCase):

    def test_missing_watchtower_logs_warning_not_raises(self):
        """If watchtower is not installed, warn and do not raise."""
        warnings: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                warnings.append(r.getMessage())

        cap = _Cap()
        logging.getLogger("pipeline").addHandler(cap)
        try:
            # sys.modules[name] = None causes ImportError on `import name`
            with patch.dict("sys.modules", {"watchtower": None, "boto3": None}):
                lc._attach_cloudwatch_handler(logging.getLogger(), logging.INFO)
            self.assertTrue(
                any("watchtower not installed" in w for w in warnings),
                f"Expected watchtower-missing warning; got: {warnings}",
            )
        finally:
            logging.getLogger("pipeline").removeHandler(cap)


# ---------------------------------------------------------------------------
# 8. page_logger — persistent handle & env-configurable paths
# ---------------------------------------------------------------------------

class PageLoggerPersistentHandleTests(unittest.TestCase):

    def setUp(self):
        # Snapshot and reset module-level handles before each test
        self._orig_log = pl._log_file
        self._orig_alerts = pl._alerts_file
        pl._log_file = None
        pl._alerts_file = None

    def tearDown(self):
        # Close any files opened during the test, then restore originals
        for attr, orig in (("_log_file", self._orig_log), ("_alerts_file", self._orig_alerts)):
            current = getattr(pl, attr)
            if current is not None and current is not orig:
                try:
                    current.close()
                except Exception:
                    pass
            setattr(pl, attr, orig)

    def _run_with_log_path(self, fn):
        """Run fn(path) inside a temp dir; close pl._log_file before dir cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            with patch.object(pl, "_LOG_PATH", path):
                result = fn(path)
            if pl._log_file is not None and not pl._log_file.closed:
                pl._log_file.close()
                pl._log_file = None
        return result

    def _run_with_alerts_path(self, fn):
        """Run fn(path) inside a temp dir; close pl._alerts_file before dir cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alerts.log")
            with patch.object(pl, "_ALERTS_LOG_PATH", path):
                result = fn(path)
            if pl._alerts_file is not None and not pl._alerts_file.closed:
                pl._alerts_file.close()
                pl._alerts_file = None
        return result

    def test_get_log_file_returns_same_object(self):
        def _check(path):
            fh1 = pl._get_log_file()
            fh2 = pl._get_log_file()
            return fh1 is fh2

        same = self._run_with_log_path(_check)
        self.assertTrue(same, "_get_log_file() must return the same handle on repeated calls")

    def test_get_alerts_file_returns_same_object(self):
        def _check(path):
            fh1 = pl._get_alerts_file()
            fh2 = pl._get_alerts_file()
            return fh1 is fh2

        same = self._run_with_alerts_path(_check)
        self.assertTrue(same, "_get_alerts_file() must return the same handle on repeated calls")

    def test_append_log_writes_page_usage_line(self):
        def _check(path):
            pl.append_log({
                "status": "success",
                "extraction_id": 42,
                "total_pages": 3,
                "billable_pages": 3,
            })
            return Path(path).read_text()

        content = self._run_with_log_path(_check)
        self.assertIn("PAGE_USAGE", content)
        self.assertIn("extraction_id=42", content)
        self.assertIn("total_pages=3", content)

    def test_append_log_never_raises(self):
        """append_log() must swallow all exceptions — billing must not crash the pipeline."""
        with patch.object(pl, "_LOG_PATH", "/nonexistent/path/that/cannot/be/created/log.txt"):
            pl._log_file = None
            pl.append_log({"status": "ok"})   # must not raise

    def test_log_limit_alert_writes_alert_line(self):
        def _check(path):
            pl.log_limit_alert(
                user_id="usr-99",
                email="client@example.com",
                total_extracted_pages=95,
                subscription_limit=100,
                alert_type="warning",
            )
            return Path(path).read_text()

        content = self._run_with_alerts_path(_check)
        self.assertIn("LIMIT_ALERT", content)
        self.assertIn("user_id=usr-99", content)
        self.assertIn("subscription_limit=100", content)

    def test_log_path_default_has_expected_suffix(self):
        expected = os.path.join("logs", "page_usage", "log.txt")
        self.assertTrue(
            pl._LOG_PATH.endswith(expected),
            f"_LOG_PATH '{pl._LOG_PATH}' must end with '{expected}'",
        )

    def test_alerts_path_default_has_expected_suffix(self):
        expected = os.path.join("logs", "page_usage", "alerts.log")
        self.assertTrue(
            pl._ALERTS_LOG_PATH.endswith(expected),
            f"_ALERTS_LOG_PATH '{pl._ALERTS_LOG_PATH}' must end with '{expected}'",
        )

    def test_append_log_line_is_line_buffered(self):
        """Each write must be visible immediately (buffering=1 → line-buffered)."""
        def _check(path):
            pl.append_log({"status": "ok", "extraction_id": 77})
            # Read back without closing — if line-buffered, data is already on disk
            return Path(path).read_text()

        content = self._run_with_log_path(_check)
        self.assertIn("extraction_id=77", content,
                      "Line-buffered writes must be visible immediately")


# ---------------------------------------------------------------------------
# 9. auth.py — security logger fires on invalid JWT
# ---------------------------------------------------------------------------

class AuthSecurityLoggingTests(unittest.TestCase):

    def test_invalid_token_emits_security_warning(self):
        import backend.auth as auth

        captured: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                captured.append(r)

        cap = _Cap()
        cap.setLevel(logging.DEBUG)
        sec = logging.getLogger("security")
        sec.addHandler(cap)
        sec.setLevel(logging.DEBUG)
        try:
            with patch.dict("os.environ", {"SECRET_KEY": "test-secret-for-unit-tests"}):
                with self.assertRaises(Exception):   # HTTPException 401
                    auth.decode_token("not.a.valid.jwt.token")
            self.assertTrue(
                any("auth.token_invalid" in r.getMessage() for r in captured),
                f"Expected 'auth.token_invalid' in security log; got: {[r.getMessage() for r in captured]}",
            )
        finally:
            sec.removeHandler(cap)

    def test_token_value_not_logged(self):
        """The raw token string must NEVER appear in any log record."""
        import backend.auth as auth

        captured: list[str] = []
        sentinel = "SUPER_SECRET_TOKEN_VALUE_abc123"

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                captured.append(r.getMessage())

        cap = _Cap()
        cap.setLevel(logging.DEBUG)
        for name in ("security", "pipeline", "root"):
            lg = logging.getLogger(name)
            lg.addHandler(cap)
            lg.setLevel(logging.DEBUG)
        try:
            with patch.dict("os.environ", {"SECRET_KEY": "test-secret-for-unit-tests"}):
                with self.assertRaises(Exception):
                    auth.decode_token(sentinel)
            self.assertFalse(
                any(sentinel in m for m in captured),
                "Raw token value must never appear in any log record",
            )
        finally:
            for name in ("security", "pipeline", "root"):
                logging.getLogger(name).removeHandler(cap)

    def test_security_logger_is_module_level_in_auth(self):
        """auth._sec must be the 'security' logger (not None or a wrong logger)."""
        import backend.auth as auth
        self.assertEqual(auth._sec.name, "security")


if __name__ == "__main__":
    unittest.main()
