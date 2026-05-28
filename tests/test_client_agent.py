"""
test_client_agent.py — Unit tests for client/client_agent.py.

Covers:
  - _TokenManager  (login, token expiry, invalidate, needs_refresh)
  - _call()        (normal, 401 retry, network error, headers not mutated)
  - _wait_stable() (stable file, timeout)
  - _write_output() (success, failure with error, missing folder)
  - _iter_sse()    (parses events, skips noise, tolerates bad JSON)
  - client_online logic (the timestamp comparison used in backend endpoints)

No live network, no live filesystem beyond tempfile.
All requests calls are mocked with unittest.mock.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the module under test.  It has no backend deps — pure stdlib + requests.
import client.client_agent as ca


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    return resp


def _fixed_datetime(now: datetime):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    return FixedDateTime


# ---------------------------------------------------------------------------
# App path resolution
# ---------------------------------------------------------------------------

class AppDirTests(unittest.TestCase):

    def test_app_dir_uses_source_folder_when_not_frozen(self):
        with patch.object(ca.sys, "frozen", False, create=True):
            self.assertEqual(ca._app_dir(), Path(ca.__file__).resolve().parent)

    def test_app_dir_uses_exe_folder_when_frozen(self):
        exe = str(ROOT / "client" / "dist" / "augocr-agent.exe")
        with patch.object(ca.sys, "frozen", True, create=True), \
             patch.object(ca.sys, "executable", exe):
            self.assertEqual(ca._app_dir(), Path(exe).resolve().parent)


# ---------------------------------------------------------------------------
# _TokenManager
# ---------------------------------------------------------------------------

class TokenManagerLoginTests(unittest.TestCase):

    def _mgr(self):
        return ca._TokenManager("http://localhost:8000", "user@test.com", "secret")

    @patch("client.client_agent.requests.post")
    def test_login_success_stores_token(self, mock_post):
        mock_post.return_value = _mock_response(200, {"access_token": "tok123"})
        mgr = self._mgr()
        result = mgr.login()
        self.assertTrue(result)
        self.assertEqual(mgr.get(), "tok123")

    @patch("client.client_agent.requests.post")
    def test_login_wrong_password_returns_false(self, mock_post):
        mock_post.return_value = _mock_response(401, {"detail": "bad credentials"})
        mgr = self._mgr()
        result = mgr.login()
        self.assertFalse(result)
        self.assertIsNone(mgr.get())

    @patch("client.client_agent.requests.post")
    def test_login_network_error_returns_false(self, mock_post):
        mock_post.side_effect = Exception("connection refused")
        mgr = self._mgr()
        result = mgr.login()
        self.assertFalse(result)

    @patch("client.client_agent.requests.post")
    def test_login_missing_access_token_field_returns_false(self, mock_post):
        # Server returns 200 but body has no access_token key
        mock_post.return_value = _mock_response(200, {"token": "wrong_key"})
        mgr = self._mgr()
        result = mgr.login()
        self.assertFalse(result)
        self.assertIsNone(mgr.get())


class TokenManagerExpiryTests(unittest.TestCase):

    def _mgr_logged_in(self):
        mgr = ca._TokenManager("http://localhost:8000", "u@test.com", "pw")
        with patch("client.client_agent.requests.post",
                   return_value=_mock_response(200, {"access_token": "tok"})):
            mgr.login()
        return mgr

    def test_get_returns_token_when_fresh(self):
        mgr = self._mgr_logged_in()
        self.assertEqual(mgr.get(), "tok")

    def test_get_returns_none_after_invalidate(self):
        mgr = self._mgr_logged_in()
        mgr.invalidate()
        self.assertIsNone(mgr.get())

    def test_needs_refresh_false_when_fresh(self):
        mgr = self._mgr_logged_in()
        self.assertFalse(mgr.needs_refresh())

    def test_needs_refresh_true_after_invalidate(self):
        mgr = self._mgr_logged_in()
        mgr.invalidate()
        self.assertTrue(mgr.needs_refresh())

    def test_get_returns_none_when_expires_at_in_past(self):
        mgr = self._mgr_logged_in()
        # Wind the clock back past expiry
        with mgr._lock:
            mgr._expires_at = time.monotonic() - 1
        self.assertIsNone(mgr.get())


# ---------------------------------------------------------------------------
# _call()
# ---------------------------------------------------------------------------

class CallTests(unittest.TestCase):

    def _mgr_with_token(self, tok="tok"):
        mgr = MagicMock(spec=ca._TokenManager)
        mgr.get.return_value = tok
        mgr.login.return_value = True
        return mgr

    @patch("client.client_agent.requests.request")
    def test_adds_authorization_header(self, mock_req):
        mock_req.return_value = _mock_response(200)
        mgr = self._mgr_with_token("mytoken")
        ca._call(mgr, "GET", "http://localhost/api/config", timeout=5)
        _, kwargs = mock_req.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer mytoken")

    @patch("client.client_agent.requests.request")
    def test_returns_response_on_success(self, mock_req):
        mock_req.return_value = _mock_response(200, {"ok": True})
        mgr = self._mgr_with_token()
        resp = ca._call(mgr, "GET", "http://localhost/x")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 200)

    @patch("client.client_agent.requests.request")
    def test_401_triggers_relogin_and_retry(self, mock_req):
        first = _mock_response(401)
        second = _mock_response(200, {"ok": True})
        mock_req.side_effect = [first, second]

        mgr = self._mgr_with_token()
        resp = ca._call(mgr, "GET", "http://localhost/x")

        self.assertEqual(resp.status_code, 200)
        mgr.invalidate.assert_called_once()
        mgr.login.assert_called_once()

    @patch("client.client_agent.requests.request")
    def test_network_error_returns_none(self, mock_req):
        import requests as req_lib
        mock_req.side_effect = req_lib.exceptions.ConnectionError("refused")
        mgr = self._mgr_with_token()
        resp = ca._call(mgr, "GET", "http://localhost/x")
        self.assertIsNone(resp)

    @patch("client.client_agent.requests.request")
    def test_caller_headers_preserved_on_second_attempt(self, mock_req):
        # Verify that extra headers supplied by the caller are present on both attempts.
        first = _mock_response(401)
        second = _mock_response(200)
        mock_req.side_effect = [first, second]

        mgr = self._mgr_with_token()
        ca._call(mgr, "GET", "http://localhost/x",
                 headers={"X-Custom": "value"}, timeout=5)

        # Both calls should carry X-Custom
        for c in mock_req.call_args_list:
            self.assertEqual(c[1]["headers"]["X-Custom"], "value")

    @patch("client.client_agent.requests.request")
    def test_login_failure_before_request_returns_none(self, mock_req):
        mgr = MagicMock(spec=ca._TokenManager)
        mgr.get.return_value = None   # no token
        mgr.login.return_value = False  # login also fails
        resp = ca._call(mgr, "GET", "http://localhost/x")
        self.assertIsNone(resp)
        mock_req.assert_not_called()


# ---------------------------------------------------------------------------
# _wait_stable()
# ---------------------------------------------------------------------------

class WaitStableTests(unittest.TestCase):

    def test_returns_true_when_size_stable(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.pdf"
            p.write_bytes(b"x" * 100)
            # Size is already stable — should return True quickly
            result = ca._wait_stable(p, stable_secs=0.3, timeout_secs=5.0)
            self.assertTrue(result)

    def test_returns_false_for_nonexistent_file(self):
        p = Path("/nonexistent/path/file.pdf")
        result = ca._wait_stable(p, stable_secs=0.1, timeout_secs=0.5)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _write_output()
# ---------------------------------------------------------------------------

class WriteOutputTests(unittest.TestCase):

    def test_writes_json_on_success(self):
        with TemporaryDirectory() as tmp:
            ca._write_output(tmp, "invoice.pdf", status="done", extraction={"total": 100})
            out = Path(tmp) / "invoice.json"
            self.assertTrue(out.exists())
            data = json.loads(out.read_text())
            self.assertEqual(data["status"], "done")
            self.assertEqual(data["extraction"]["total"], 100)
            self.assertEqual(data["file"], "invoice.pdf")
            self.assertIn("timestamp", data)

    def test_writes_error_field_on_failure(self):
        with TemporaryDirectory() as tmp:
            ca._write_output(tmp, "order.pdf", status="failed", error="vendor unknown")
            data = json.loads((Path(tmp) / "order.json").read_text())
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error"], "vendor unknown")
            self.assertNotIn("extraction", data)

    def test_extraction_none_not_written_to_json(self):
        with TemporaryDirectory() as tmp:
            ca._write_output(tmp, "a.pdf", status="failed", extraction=None, error="boom")
            data = json.loads((Path(tmp) / "a.json").read_text())
            self.assertNotIn("extraction", data)

    def test_extraction_present_written_to_json(self):
        with TemporaryDirectory() as tmp:
            ca._write_output(tmp, "a.pdf", status="done", extraction={"items": []})
            data = json.loads((Path(tmp) / "a.json").read_text())
            self.assertIn("extraction", data)

    def test_raw_extraction_is_written_as_output_shape(self):
        with TemporaryDirectory() as tmp:
            ca._write_output(
                tmp,
                "a.pdf",
                status="done",
                extraction={
                    "_page": 1,
                    "po_number": "DP00001008",
                    "line_items": [{"_page": 1, "_source": "llm", "item": "Glue"}],
                },
            )
            data = json.loads((Path(tmp) / "a.json").read_text())
            extraction = data["extraction"]
            self.assertEqual(list(extraction.keys()), ["po_number", "line_items"])
            self.assertEqual(extraction["line_items"], [{"item": "Glue"}])

    def test_missing_output_folder_does_not_raise(self):
        # Should log a warning and return — no exception
        ca._write_output("", "x.pdf", status="failed", error="no folder")

    def test_creates_output_dir_if_missing(self):
        with TemporaryDirectory() as tmp:
            nested = str(Path(tmp) / "a" / "b" / "c")
            ca._write_output(nested, "file.pdf", status="done")
            self.assertTrue((Path(nested) / "file.json").exists())

    def test_stem_used_as_json_filename(self):
        with TemporaryDirectory() as tmp:
            ca._write_output(tmp, "my-invoice_2024.pdf", status="done")
            self.assertTrue((Path(tmp) / "my-invoice_2024.json").exists())


# ---------------------------------------------------------------------------
# _iter_sse()
# ---------------------------------------------------------------------------

class IterSseTests(unittest.TestCase):

    def _make_response(self, lines: list[str]) -> MagicMock:
        resp = MagicMock()
        resp.iter_lines.return_value = [line.encode() for line in lines]
        return resp

    def test_parses_data_lines(self):
        resp = self._make_response([
            'data: {"event": "progress", "job_id": 1}',
            'data: {"event": "done"}',
        ])
        events = list(ca._iter_sse(resp))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "progress")
        self.assertEqual(events[1]["event"], "done")

    def test_skips_comment_and_empty_lines(self):
        resp = self._make_response([
            ": keepalive",
            "",
            'data: {"event": "done"}',
        ])
        events = list(ca._iter_sse(resp))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "done")

    def test_tolerates_invalid_json(self):
        resp = self._make_response([
            "data: not-valid-json",
            'data: {"event": "done"}',
        ])
        events = list(ca._iter_sse(resp))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "done")

    def test_handles_string_lines(self):
        resp = MagicMock()
        resp.iter_lines.return_value = ['data: {"event": "partial"}']
        events = list(ca._iter_sse(resp))
        self.assertEqual(events[0]["event"], "partial")

    def test_empty_stream_yields_nothing(self):
        resp = self._make_response([])
        self.assertEqual(list(ca._iter_sse(resp)), [])


# ---------------------------------------------------------------------------
# _ScheduleState()
# ---------------------------------------------------------------------------

class ScheduleStateTests(unittest.TestCase):

    def test_schedule_not_due_before_configured_time(self):
        now = datetime(2026, 5, 27, 16, 20, tzinfo=UTC)
        state = ca._ScheduleState()
        with patch.object(ca, "datetime", _fixed_datetime(now)):
            state.update_from_server([
                {"id": 1, "enabled": True, "utc_hour": 16, "utc_minute": 30}
            ])
            self.assertEqual(state.claim_due(), ("none", []))
            self.assertEqual(state.next_fire_time(), datetime(2026, 5, 27, 16, 30, tzinfo=UTC))

    def test_schedule_due_at_configured_time(self):
        now = datetime(2026, 5, 27, 16, 30, 5, tzinfo=UTC)
        state = ca._ScheduleState()
        with patch.object(ca, "datetime", _fixed_datetime(now)):
            state.update_from_server([
                {"id": 1, "enabled": True, "utc_hour": 16, "utc_minute": 30}
            ])
            self.assertEqual(state.claim_due(), ("start", [1]))
            state.finish_batch()
            self.assertEqual(state.claim_due(), ("none", []))

    def test_last_ran_prevents_same_slot_from_running_again(self):
        now = datetime(2026, 5, 27, 16, 40, tzinfo=UTC)
        state = ca._ScheduleState()
        with patch.object(ca, "datetime", _fixed_datetime(now)):
            state.update_from_server([
                {
                    "id": 1,
                    "enabled": True,
                    "utc_hour": 16,
                    "utc_minute": 30,
                    "last_ran_at": "2026-05-27T16:31:00+00:00",
                }
            ])
            self.assertEqual(state.claim_due(), ("none", []))


# ---------------------------------------------------------------------------
# client_online timestamp logic
# (mirrors the logic in backend/main.py — tested standalone so no heavy deps)
# ---------------------------------------------------------------------------

class ClientOnlineLogicTests(unittest.TestCase):
    """
    The client_online flag is computed identically in three backend endpoints.
    Test the logic directly without importing backend.main.
    """

    @staticmethod
    def _compute_client_online(heartbeat_str: str | None) -> bool:
        if not heartbeat_str:
            return False
        try:
            last_beat = datetime.fromisoformat(heartbeat_str)
            return (datetime.now(UTC) - last_beat).total_seconds() < 60
        except Exception:
            return False

    def test_recent_heartbeat_is_online(self):
        hb = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        self.assertTrue(self._compute_client_online(hb))

    def test_old_heartbeat_is_offline(self):
        hb = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
        self.assertFalse(self._compute_client_online(hb))

    def test_exactly_at_boundary_is_offline(self):
        # 60 s ago is NOT < 60, so it's offline
        hb = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        self.assertFalse(self._compute_client_online(hb))

    def test_none_heartbeat_is_offline(self):
        self.assertFalse(self._compute_client_online(None))

    def test_empty_string_heartbeat_is_offline(self):
        self.assertFalse(self._compute_client_online(""))

    def test_corrupt_heartbeat_is_offline(self):
        self.assertFalse(self._compute_client_online("not-a-date"))

    def test_utc_aware_timestamp_parsed_correctly(self):
        # Ensure +00:00 suffix (produced by datetime.now(UTC).isoformat()) is handled
        hb = datetime.now(UTC).isoformat()  # e.g. "2025-05-18T10:30:00.000000+00:00"
        self.assertTrue(self._compute_client_online(hb))

    def test_heartbeat_59_seconds_ago_is_online(self):
        hb = (datetime.now(UTC) - timedelta(seconds=59)).isoformat()
        self.assertTrue(self._compute_client_online(hb))


# ---------------------------------------------------------------------------
# _kv() helper
# ---------------------------------------------------------------------------

class KvHelperTests(unittest.TestCase):

    def test_empty_returns_empty_string(self):
        self.assertEqual(ca._kv(), "")

    def test_single_pair(self):
        result = ca._kv(file="invoice.pdf")
        self.assertIn("file=invoice.pdf", result)
        self.assertTrue(result.startswith("  | "))

    def test_none_values_omitted(self):
        result = ca._kv(file="a.pdf", error=None)
        self.assertNotIn("error", result)
        self.assertIn("file=a.pdf", result)

    def test_values_with_spaces_are_quoted(self):
        result = ca._kv(path="C:\\My Folder\\file.pdf")
        self.assertIn('"C:\\My Folder\\file.pdf"', result)


if __name__ == "__main__":
    unittest.main()
