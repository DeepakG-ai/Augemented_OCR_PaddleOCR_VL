from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

# Make sure backend imports work
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("SECRET_KEY", "test-only-secret-do-not-use-in-prod")

from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials

import backend.main as main
import backend.auth as auth_mod
from backend import db as db_mod


class ReliabilityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Sentinel so endpoints can read app.state.pool without AttributeError.
        # Individual DB calls are patched per-test at the db_mod layer.
        main.app.state.pool = object()
        main.app.state.store = MagicMock()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    # ── 1. HEALTH AND LIVENESS CHECKS ──────────────────────────────────────────

    def test_liveness_endpoint_always_returns_200(self) -> None:
        response = self.client.get("/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("backend.main.app.state.pool")
    def test_health_endpoint_db_connected(self, mock_pool) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = 1
        
        # Async context manager mock
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "db": "connected"})

    @patch("backend.main.app.state.pool")
    def test_health_endpoint_db_disconnected(self, mock_pool) -> None:
        # DB failure simulation
        mock_pool.acquire.side_effect = Exception("DB Connection Refused")

        response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error", "db": "disconnected"})

    # ── 2. AUTH REGULATION & SSE SPECIFIC QUERY STRING TOKEN ────────────────────

    @patch("backend.auth.decode_token")
    @patch("backend.auth.db_mod.get_user_by_id")
    def test_get_current_user_strictly_header_bearer(self, mock_get_user, mock_decode) -> None:
        mock_decode.return_value = {"sub": "u-1", "role": "client", "email": "client@test"}
        mock_get_user.return_value = {"id": "u-1", "role": "client", "email": "client@test", "is_active": True}

        # 1. No credentials -> Unauthorized
        mock_request = MagicMock()
        mock_request.app.state.pool = MagicMock()
        with self.assertRaises(HTTPException) as ctx:
            import asyncio
            asyncio.run(auth_mod.get_current_user(mock_request, credentials=None))
        self.assertEqual(ctx.exception.status_code, 401)

        # 2. Bearer credentials in header -> Allowed
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-jwt-token")
        res = asyncio.run(auth_mod.get_current_user(mock_request, credentials=creds))
        self.assertEqual(res["id"], "u-1")

    @patch("backend.auth.decode_token")
    @patch("backend.auth.db_mod.get_user_by_id")
    def test_get_current_user_sse_is_header_only(self, mock_get_user, mock_decode) -> None:
        mock_decode.return_value = {"sub": "u-1", "role": "client", "email": "client@test"}
        mock_get_user.return_value = {"id": "u-1", "role": "client", "email": "client@test", "is_active": True}

        # Query-parameter token auth was removed (tokens would leak into access
        # logs). SSE auth is now Bearer-header only.
        mock_request = MagicMock()
        mock_request.app.state.pool = MagicMock()
        import asyncio

        # 1. No header credentials -> Unauthorized
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(auth_mod.get_current_user_sse(mock_request, credentials=None))
        self.assertEqual(ctx.exception.status_code, 401)

        # 2. Bearer credentials in header -> Allowed
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-jwt-token")
        res = asyncio.run(auth_mod.get_current_user_sse(mock_request, credentials=creds))
        self.assertEqual(res["id"], "u-1")

    # ── 3. CANCEL TERMINAL STATE GUARD ──────────────────────────────────────────

    @patch("backend.main.db_mod.get_extraction")
    @patch("backend.main.assert_extraction_access")
    def test_cancel_extraction_in_terminal_state_raises_409(self, mock_assert, mock_get_extraction) -> None:
        mock_get_extraction.return_value = {"id": 123, "status": "done"}
        
        # Test request to cancel
        response = self.client.post("/jobs/extractions/123/cancel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CONFLICT")
        self.assertIn("Cannot cancel extraction in terminal state", response.json()["error"]["message"])

    # ── 4. ATOMIC STALE JOB RECOVERY ────────────────────────────────────────────

    def test_recover_stale_jobs_atomic_transaction(self) -> None:
        import asyncio
        mock_pool = MagicMock()
        mock_conn = AsyncMock()

        # asyncpg conn.transaction() returns a non-coroutine context manager object.
        # AsyncMock makes methods return coroutines, which breaks `async with`.
        # Override transaction to return a plain MagicMock with async enter/exit.
        mock_txn = MagicMock()
        mock_txn.__aenter__ = AsyncMock(return_value=None)
        mock_txn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_txn)

        mock_conn.fetch.return_value = [
            {"id": 1, "extraction_id": 101, "status": "failed"},
            {"id": 2, "extraction_id": None, "status": "queued"},
        ]
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        recovered = asyncio.run(db_mod.recover_stale_jobs(mock_pool, "normalize", 10))

        self.assertEqual(recovered, 2)
        mock_txn.__aenter__.assert_called_once()
        self.assertTrue(any("UPDATE jobs" in call[0][0] for call in mock_conn.fetch.call_args_list))
        mock_conn.execute.assert_any_call(unittest.mock.ANY, [101])

    # ── 5. /v1/extract IDEMPOTENCY AND DEDUPLICATION ─────────────────────────────

    @patch("backend.main.assert_vendor_access")
    @patch("backend.main.db_mod.claim_idempotency")
    @patch("backend.main.db_mod.bind_idempotency_claim")
    @patch("backend.main._submit_ingestion_job")
    @patch("backend.main.processor.count_pdf_pages")
    def test_extract_idempotency_fresh_run_claims_and_binds(self, mock_count, mock_submit, mock_bind, mock_claim, mock_vendor_access) -> None:
        mock_count.return_value = 1
        mock_claim.return_value = {"status": "claimed", "claim_id": 999}
        mock_submit.return_value = {
            "job": {"id": 1000},
            "extraction": {"id": 2000, "document_id": 3000, "status": "queued"}
        }

        # Pass vendor_id to skip the PDF-render + vendor-detection block
        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "unique-key-123"},
            files=payload,
            data={"vendor_id": "vendor-abc"},
            params={"async": "true"},
        )

        self.assertEqual(response.status_code, 202)
        mock_claim.assert_called_once()
        mock_bind.assert_called_once_with(unittest.mock.ANY, 999, 2000, 3000)

    @patch("backend.main.assert_vendor_access")
    @patch("backend.main.asyncio.sleep", new=AsyncMock())
    @patch("backend.main.db_mod.claim_idempotency")
    @patch("backend.main.db_mod.delete_idempotency_claim")
    @patch("backend.main.db_mod.bind_idempotency_claim")
    @patch("backend.main._submit_ingestion_job")
    @patch("backend.main.processor.count_pdf_pages")
    def test_extract_bind_retries_transient_failure_then_succeeds(
        self, mock_count, mock_submit, mock_bind, mock_delete, mock_claim, mock_vendor_access
    ) -> None:
        mock_count.return_value = 1
        mock_claim.return_value = {"status": "claimed", "claim_id": 999}
        mock_submit.return_value = {
            "job": {"id": 1000},
            "extraction": {"id": 2000, "document_id": 3000, "status": "queued"},
        }
        # Bind fails twice (transient DB hiccup) then succeeds on the third try.
        mock_bind.side_effect = [RuntimeError("hiccup"), RuntimeError("hiccup"), None]

        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "bind-retry-key"},
            files=payload,
            data={"vendor_id": "vendor-abc"},
            params={"async": "true"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_bind.call_count, 3)
        # The job already exists and owns the reservation — the claim must NOT be
        # deleted (deleting it would let a retry spawn a duplicate extraction).
        mock_delete.assert_not_called()

    @patch("backend.main.assert_vendor_access")
    @patch("backend.main.asyncio.sleep", new=AsyncMock())
    @patch("backend.main.db_mod.claim_idempotency")
    @patch("backend.main.db_mod.delete_idempotency_claim")
    @patch("backend.main.db_mod.bind_idempotency_claim")
    @patch("backend.main._submit_ingestion_job")
    @patch("backend.main.processor.count_pdf_pages")
    def test_extract_bind_permanent_failure_keeps_claim_and_still_returns_202(
        self, mock_count, mock_submit, mock_bind, mock_delete, mock_claim, mock_vendor_access
    ) -> None:
        mock_count.return_value = 1
        mock_claim.return_value = {"status": "claimed", "claim_id": 999}
        mock_submit.return_value = {
            "job": {"id": 1000},
            "extraction": {"id": 2000, "document_id": 3000, "status": "queued"},
        }
        # Bind never succeeds. The extraction was still created, so the request
        # must succeed; the unbound claim self-heals at TTL. No duplicate, no 500.
        mock_bind.side_effect = RuntimeError("db down")

        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "bind-fail-key"},
            files=payload,
            data={"vendor_id": "vendor-abc"},
            params={"async": "true"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_bind.call_count, 3)
        mock_delete.assert_not_called()

    @patch("backend.main.db_mod.claim_idempotency")
    @patch("backend.main.db_mod.get_extraction")
    @patch("backend.main.db_mod.get_extraction_mapped_result")
    def test_extract_idempotency_returns_cached_on_done(self, mock_mapped, mock_get_ext, mock_claim) -> None:
        mock_claim.return_value = {"status": "duplicate", "extraction_id": 2000, "extraction_status": "done"}
        mock_get_ext.return_value = {
            "id": 2000, "vendor_id": "v-1", "total_pages": 1, "status": "done", "result": {"total": 100.0}
        }
        mock_mapped.return_value = None

        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "duplicate-key"},
            files=payload
        )

        self.assertEqual(response.status_code, 200)
        res_json = response.json()
        self.assertEqual(res_json["extraction_id"], 2000)
        self.assertTrue(res_json["cached"])
        self.assertEqual(res_json["result"], {"total": 100.0})

    @patch("backend.main.db_mod.claim_idempotency")
    def test_extract_idempotency_conflict_returns_409(self, mock_claim) -> None:
        mock_claim.return_value = {"status": "conflict"}

        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "conflict-key"},
            files=payload
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CONFLICT")

    @patch("backend.main.assert_vendor_access")
    @patch("backend.main.db_mod.claim_idempotency")
    @patch("backend.main.db_mod.delete_idempotency_claim")
    @patch("backend.main.db_mod.bind_idempotency_claim")
    @patch("backend.main._submit_ingestion_job")
    @patch("backend.main.processor.count_pdf_pages")
    def test_extract_idempotency_failed_evicts_and_retries(self, mock_count, mock_submit, mock_bind, mock_delete, mock_claim, mock_vendor_access) -> None:
        mock_count.return_value = 1
        mock_claim.side_effect = [
            {"status": "duplicate", "extraction_id": 2000, "extraction_status": "failed"},
            {"status": "claimed", "claim_id": 999}
        ]
        mock_submit.return_value = {
            "job": {"id": 1000},
            "extraction": {"id": 2001, "document_id": 3000, "status": "queued"}
        }

        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "failed-then-retry-key"},
            files=payload,
            data={"vendor_id": "vendor-abc"},
            params={"async": "true"},
        )

        self.assertEqual(response.status_code, 202)
        mock_delete.assert_called_once_with(unittest.mock.ANY, "00000000-0000-0000-0000-000000000000", "failed-then-retry-key")
        self.assertEqual(mock_claim.call_count, 2)

    @patch("backend.main.db_mod.claim_idempotency")
    @patch("backend.main.db_mod.delete_idempotency_claim")
    def test_extract_idempotency_unbound_duplicate_returns_202_initializing(self, mock_delete, mock_claim) -> None:
        mock_claim.return_value = {
            "status": "duplicate",
            "extraction_id": None,
            "extraction_status": None,
        }

        payload = {"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")}
        response = self.client.post(
            "/v1/extract",
            headers={"Idempotency-Key": "in-flight-key"},
            files=payload,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "initializing")
        mock_delete.assert_not_called()

    def test_extract_releases_quota_when_vendor_render_fails(self) -> None:
        client_user = {
            "id": "11111111-1111-1111-1111-111111111111",
            "role": "client",
            "email": "client@test",
            "auth_method": "api_key",
            "api_key_id": 7,
        }
        previous_override = main.app.dependency_overrides.get(auth_mod.get_current_user_or_api_key)
        main.app.dependency_overrides[auth_mod.get_current_user_or_api_key] = lambda: client_user
        try:
            with patch("backend.main.processor.count_pdf_pages", return_value=2), \
                 patch("backend.main.db_mod.reserve_quota", new=AsyncMock(return_value={
                     "allowed": True,
                     "reason": "ok",
                     "used": 10,
                     "limit": 100,
                     "remaining": 88,
                     "pending": 0,
                 })), \
                 patch("backend.main.render_page_1_for_detection", new=AsyncMock(return_value=[])), \
                 patch("backend.main.db_mod.release_quota_reservation", new=AsyncMock()) as mock_release:
                response = self.client.post(
                    "/v1/extract",
                    files={"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")},
                )

            self.assertEqual(response.status_code, 400)
            mock_release.assert_called_once_with(unittest.mock.ANY, client_user["id"], 2)
        finally:
            if previous_override is None:
                main.app.dependency_overrides.pop(auth_mod.get_current_user_or_api_key, None)
            else:
                main.app.dependency_overrides[auth_mod.get_current_user_or_api_key] = previous_override

    def test_ingest_releases_quota_when_vendor_render_raises(self) -> None:
        client_user = {
            "id": "11111111-1111-1111-1111-111111111111",
            "role": "client",
            "email": "client@test",
        }
        previous_override = main.app.dependency_overrides.get(auth_mod.get_current_user)
        main.app.dependency_overrides[auth_mod.get_current_user] = lambda: client_user
        try:
            with patch("backend.main.db_mod.get_user_is_executing", new=AsyncMock(return_value=False)), \
                 patch("backend.main.processor.count_pdf_pages", return_value=2), \
                 patch("backend.main.db_mod.reserve_quota", new=AsyncMock(return_value={
                     "allowed": True,
                     "reason": "ok",
                     "used": 10,
                     "limit": 100,
                     "remaining": 88,
                     "pending": 0,
                 })), \
                 patch("backend.main.render_page_1_for_detection", new=AsyncMock(side_effect=RuntimeError("renderer down"))), \
                 patch("backend.main.db_mod.release_quota_reservation", new=AsyncMock()) as mock_release:
                response = self.client.post(
                    "/ingest/ui",
                    files={"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "PDF_RENDER_FAILED")
            mock_release.assert_called_once_with(unittest.mock.ANY, client_user["id"], 2)
        finally:
            if previous_override is None:
                main.app.dependency_overrides.pop(auth_mod.get_current_user, None)
            else:
                main.app.dependency_overrides[auth_mod.get_current_user] = previous_override

    def test_error_envelope_backfills_code_and_message_for_structured_details(self) -> None:
        body = main._error_body(
            409,
            {
                "reason": "unknown_vendor",
                "hint": "Create the vendor and vendor id first, then retry this document.",
            },
        )

        self.assertEqual(body["error"]["code"], "CONFLICT")
        self.assertEqual(body["error"]["reason"], "unknown_vendor")
        self.assertIn("Create the vendor", body["error"]["message"])
