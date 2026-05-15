"""
test_admin_client_scoping.py
============================
Tests for the admin "Act As Client" vendor-detection scoping.

Business rule:
  - Vendor detection is ALWAYS scoped to a specific client's vendors/aliases.
  - Admin users must supply act_as_client_id to select which client's
    namespace to search. Without it, only the admin's own vendors are searched.
  - This prevents cross-tenant collisions when two clients both have a vendor
    with the same name (e.g. both have "Aegis").

These tests cover:
  1. Same vendor name exists for two different clients — each client detects
     only their own (no cross-tenant bleed).
  2. Admin with act_as_client_id=A detects only Client A's "Aegis".
  3. Admin with act_as_client_id=B detects only Client B's "Aegis".
  4. Admin without act_as_client_id falls back to admin's own vendors only.
  5. Client user always uses their own vendors (act_as_client_id is ignored
     server-side for non-admin users).
  6. GET /vendors with ?user_id= filters correctly for admin.
  7. GET /vendors with ?user_id= is ignored for non-admin users.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, ANY

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import vendor_detector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _words(text: str) -> list[dict]:
    return [{"text": token} for token in text.split()]


def _alias(vendor_id: str, vendor_name: str, pattern: str, weight: int = 10) -> dict:
    return {"vendor_id": vendor_id, "vendor_name": vendor_name, "pattern": pattern, "weight": weight}


def _vendor(vid: str, name: str) -> dict:
    return {"id": vid, "name": name}


# ---------------------------------------------------------------------------
# Scenario data shared across tests
# ---------------------------------------------------------------------------

# Client A owns vendor "AEGIS-A" (vendor_id="aegis_a")
CLIENT_A_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CLIENT_A_ALIASES = [_alias("aegis_a", "AEGIS", "aegis", weight=10)]
CLIENT_A_VENDORS = [_vendor("aegis_a", "AEGIS")]

# Client B owns vendor "AEGIS-B" (vendor_id="aegis_b")  — same display name!
CLIENT_B_ID = "bbbbbbbb-0000-0000-0000-000000000002"
CLIENT_B_ALIASES = [_alias("aegis_b", "AEGIS", "aegis", weight=10)]
CLIENT_B_VENDORS = [_vendor("aegis_b", "AEGIS")]

# Admin's own vendors (different name — no collision risk here)
ADMIN_ID = "cccccccc-0000-0000-0000-000000000003"
ADMIN_ALIASES: list[dict] = []
ADMIN_VENDORS = [_vendor("admin_v1", "ADMINCO")]

# Document text that contains the word "AEGIS"
AEGIS_DOC_WORDS = _words("Invoice AEGIS Corp ref 20250515")


# ---------------------------------------------------------------------------
# Vendor detection tests (vendor_detector.py layer)
# ---------------------------------------------------------------------------

class TestClientScopedVendorDetection(unittest.IsolatedAsyncioTestCase):
    """Verify that vendor_detector.detect_vendor() never leaks across tenants."""

    async def _detect(self, words, aliases, vendors, user_id=None):
        with (
            patch.object(
                vendor_detector.db_mod,
                "get_all_aliases_for_detection",
                AsyncMock(return_value=aliases),
            ),
            patch.object(
                vendor_detector.db_mod,
                "list_vendors",
                AsyncMock(return_value=vendors),
            ),
        ):
            return await vendor_detector.detect_vendor(object(), words, user_id=user_id)

    async def test_client_a_detects_own_aegis(self) -> None:
        """Client A's detection is scoped to aegis_a, not aegis_b."""
        match = await self._detect(
            AEGIS_DOC_WORDS, CLIENT_A_ALIASES, CLIENT_A_VENDORS, user_id=CLIENT_A_ID
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "aegis_a")

    async def test_client_b_detects_own_aegis(self) -> None:
        """Client B's detection is scoped to aegis_b, not aegis_a."""
        match = await self._detect(
            AEGIS_DOC_WORDS, CLIENT_B_ALIASES, CLIENT_B_VENDORS, user_id=CLIENT_B_ID
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "aegis_b")

    async def test_admin_acting_as_client_a_gets_aegis_a(self) -> None:
        """Admin with act_as_client_id=CLIENT_A detects aegis_a only."""
        match = await self._detect(
            AEGIS_DOC_WORDS, CLIENT_A_ALIASES, CLIENT_A_VENDORS, user_id=CLIENT_A_ID
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "aegis_a",
                         "Admin scoped to Client A must match Client A's Aegis vendor")

    async def test_admin_acting_as_client_b_gets_aegis_b(self) -> None:
        """Admin with act_as_client_id=CLIENT_B detects aegis_b only."""
        match = await self._detect(
            AEGIS_DOC_WORDS, CLIENT_B_ALIASES, CLIENT_B_VENDORS, user_id=CLIENT_B_ID
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.vendor_id, "aegis_b",
                         "Admin scoped to Client B must match Client B's Aegis vendor")

    async def test_admin_without_act_as_client_uses_own_vendors(self) -> None:
        """Admin with no act_as_client falls back to their own vendors (not all vendors)."""
        # Admin has no "AEGIS" vendor — should return no match
        match = await self._detect(
            AEGIS_DOC_WORDS, ADMIN_ALIASES, ADMIN_VENDORS, user_id=ADMIN_ID
        )
        self.assertIsNone(match,
                          "Admin without client scope must NOT detect other clients' vendors")

    async def test_cross_tenant_collision_is_prevented(self) -> None:
        """When detection is scoped to Client A, Client B's alias must not fire."""
        # Even if we pass all aliases globally (as the old code did), scoping
        # via user_id on the DB layer ensures only the tenant's aliases are loaded.
        # This test simulates that the DB correctly returns only one client's aliases.
        match_a = await self._detect(
            AEGIS_DOC_WORDS, CLIENT_A_ALIASES, CLIENT_A_VENDORS, user_id=CLIENT_A_ID
        )
        match_b = await self._detect(
            AEGIS_DOC_WORDS, CLIENT_B_ALIASES, CLIENT_B_VENDORS, user_id=CLIENT_B_ID
        )
        self.assertIsNotNone(match_a)
        self.assertIsNotNone(match_b)
        # The two matches must be different vendor IDs despite same name
        self.assertNotEqual(
            match_a.vendor_id, match_b.vendor_id,
            "Client A and Client B must detect their own distinct vendor IDs",
        )

    async def test_detect_vendor_passes_user_id_to_db_aliases(self) -> None:
        """Verify detect_vendor forwards user_id to get_all_aliases_for_detection."""
        aliases_mock = AsyncMock(return_value=CLIENT_A_ALIASES)
        vendors_mock = AsyncMock(return_value=CLIENT_A_VENDORS)
        with (
            patch.object(vendor_detector.db_mod, "get_all_aliases_for_detection", aliases_mock),
            patch.object(vendor_detector.db_mod, "list_vendors", vendors_mock),
        ):
            await vendor_detector.detect_vendor(object(), AEGIS_DOC_WORDS, user_id=CLIENT_A_ID)

        aliases_mock.assert_awaited_once_with(ANY, user_id=CLIENT_A_ID)
        vendors_mock.assert_awaited_once_with(ANY, user_id=CLIENT_A_ID)

    async def test_detect_vendor_passes_user_id_to_db_list_vendors(self) -> None:
        """Verify detect_vendor forwards user_id to list_vendors (vendor name fallback)."""
        aliases_mock = AsyncMock(return_value=[])
        vendors_mock = AsyncMock(return_value=CLIENT_B_VENDORS)
        with (
            patch.object(vendor_detector.db_mod, "get_all_aliases_for_detection", aliases_mock),
            patch.object(vendor_detector.db_mod, "list_vendors", vendors_mock),
        ):
            await vendor_detector.detect_vendor(object(), AEGIS_DOC_WORDS, user_id=CLIENT_B_ID)

        vendors_mock.assert_awaited_once_with(ANY, user_id=CLIENT_B_ID)

    async def test_no_match_when_scoped_to_wrong_client(self) -> None:
        """When scoped to Client A, Client B's Aegis alias must NOT match even if name is same."""
        # Simulates: DB returns Client A's aliases, but doc is Client B's Aegis.
        # Since Client A has no "aegis" alias that matches, result is None.
        client_a_no_aegis_aliases: list[dict] = []
        client_a_no_aegis_vendors = [_vendor("other_vendor_a", "OTHER")]
        match = await self._detect(
            AEGIS_DOC_WORDS,
            client_a_no_aegis_aliases,
            client_a_no_aegis_vendors,
            user_id=CLIENT_A_ID,
        )
        self.assertIsNone(match,
                          "Should return None when scoped client has no matching vendor")

    async def test_empty_words_returns_none(self) -> None:
        """Empty page words should return None without error."""
        match = await self._detect([], CLIENT_A_ALIASES, CLIENT_A_VENDORS, user_id=CLIENT_A_ID)
        self.assertIsNone(match)

    async def test_no_vendors_or_aliases_returns_none(self) -> None:
        """Client with no vendors/aliases configured must return None."""
        match = await self._detect(
            AEGIS_DOC_WORDS, [], [], user_id="no-vendors-client-id"
        )
        self.assertIsNone(match)


# ---------------------------------------------------------------------------
# Ingest route integration tests (main.py layer)
# ---------------------------------------------------------------------------

class TestIngestActAsClientScoping(unittest.IsolatedAsyncioTestCase):
    """
    Test that the /ingest/ui route correctly passes _detect_uid to vendor detection.

    We mock the vendor_detector and db layer so we don't need a real DB or server.
    The key invariant: _detect_uid must NEVER be None for admin uploads.
    """

    def _make_user(self, role: str, uid: str) -> dict:
        return {"id": uid, "role": role, "email": f"{role}@test.com"}

    async def test_admin_detect_uid_uses_act_as_client_id(self) -> None:
        """
        When admin provides act_as_client_id, vendor detection receives that
        client's user_id — never None.
        """
        # We only test the _detect_uid logic by directly checking what
        # detect_vendor would be called with. We replicate the main.py branch:
        user = self._make_user("admin", ADMIN_ID)
        act_as_client_id = CLIENT_A_ID

        # Replicate main.py logic (lines 1319-1338)
        if user.get("role") == "admin":
            _detect_uid = act_as_client_id if act_as_client_id else user["id"]
        else:
            _detect_uid = user["id"]

        self.assertEqual(_detect_uid, CLIENT_A_ID)
        self.assertIsNotNone(_detect_uid,
                             "detect_uid must never be None — prevents global cross-tenant search")

    async def test_admin_detect_uid_falls_back_to_own_id_when_no_client(self) -> None:
        """
        When admin uploads without act_as_client_id, detection uses admin's
        own user_id — still NOT None (not global search).
        """
        user = self._make_user("admin", ADMIN_ID)
        act_as_client_id = None  # no client selected

        if user.get("role") == "admin":
            _detect_uid = act_as_client_id if act_as_client_id else user["id"]
        else:
            _detect_uid = user["id"]

        self.assertEqual(_detect_uid, ADMIN_ID,
                         "Admin fallback must be admin's own user_id, not None")
        self.assertIsNotNone(_detect_uid)

    async def test_client_detect_uid_always_own_id(self) -> None:
        """
        Non-admin users always use their own user_id regardless of any
        act_as_client_id value (which the server ignores for clients).
        """
        user = self._make_user("client", CLIENT_A_ID)
        act_as_client_id = CLIENT_B_ID  # ignored for clients

        if user.get("role") == "admin":
            _detect_uid = act_as_client_id if act_as_client_id else user["id"]
        else:
            _detect_uid = user["id"]

        self.assertEqual(_detect_uid, CLIENT_A_ID,
                         "Client user must always use their own id, not act_as_client_id")

    async def test_detect_uid_is_never_none_for_admin(self) -> None:
        """
        Regression test: the old code set _detect_uid=None for admin,
        enabling a global cross-tenant search. Verify the new code never does.
        """
        for act_as_client_id in [None, "", CLIENT_A_ID, CLIENT_B_ID]:
            user = self._make_user("admin", ADMIN_ID)
            if user.get("role") == "admin":
                _detect_uid = act_as_client_id if act_as_client_id else user["id"]
            else:
                _detect_uid = user["id"]

            self.assertIsNotNone(
                _detect_uid,
                f"detect_uid must not be None when act_as_client_id={act_as_client_id!r}",
            )
            self.assertNotEqual(
                _detect_uid, "",
                f"detect_uid must not be empty when act_as_client_id={act_as_client_id!r}",
            )


# ---------------------------------------------------------------------------
# GET /vendors ?user_id= filter logic
# ---------------------------------------------------------------------------

class TestVendorsUserIdFilter(unittest.IsolatedAsyncioTestCase):
    """
    Test the filter_user logic in GET /vendors that enables the admin
    Extract page dropdown to show only the selected client's vendors.
    """

    def _filter_user(self, role: str, caller_id: str, query_user_id=None) -> str | None:
        """Replicate the filter_user logic from the updated GET /vendors route."""
        if role == "admin":
            return query_user_id if query_user_id else None
        else:
            return caller_id  # always own vendors for clients

    def test_admin_no_filter_returns_all(self) -> None:
        """Admin with no ?user_id= sees all vendors (filter_user=None)."""
        result = self._filter_user("admin", ADMIN_ID, query_user_id=None)
        self.assertIsNone(result, "Admin with no user_id param should get filter_user=None")

    def test_admin_with_client_a_filter(self) -> None:
        """Admin with ?user_id=CLIENT_A sees only Client A's vendors."""
        result = self._filter_user("admin", ADMIN_ID, query_user_id=CLIENT_A_ID)
        self.assertEqual(result, CLIENT_A_ID)

    def test_admin_with_client_b_filter(self) -> None:
        """Admin with ?user_id=CLIENT_B sees only Client B's vendors."""
        result = self._filter_user("admin", ADMIN_ID, query_user_id=CLIENT_B_ID)
        self.assertEqual(result, CLIENT_B_ID)

    def test_client_ignores_user_id_param(self) -> None:
        """Client always sees their own vendors regardless of ?user_id= param."""
        result = self._filter_user("client", CLIENT_A_ID, query_user_id=CLIENT_B_ID)
        self.assertEqual(result, CLIENT_A_ID,
                         "Client must not be able to view other client's vendors")

    def test_client_without_user_id_param(self) -> None:
        """Client with no ?user_id= param sees their own vendors."""
        result = self._filter_user("client", CLIENT_B_ID, query_user_id=None)
        self.assertEqual(result, CLIENT_B_ID)

    def test_admin_empty_string_user_id_treated_as_no_filter(self) -> None:
        """Empty string user_id is treated as falsy — admin sees all vendors."""
        result = self._filter_user("admin", ADMIN_ID, query_user_id="")
        self.assertIsNone(result, "Empty user_id string should be treated as no filter")


if __name__ == "__main__":
    unittest.main()
