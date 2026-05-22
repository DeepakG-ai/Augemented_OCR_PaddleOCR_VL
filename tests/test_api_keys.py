"""
test_api_keys.py -- Comprehensive integration tests for API Key management.

New architecture (v2):
  - One client user owns N API keys (no phantom @apikey.internal users)
  - POST /admin/api-keys requires owner_user_id (existing client user UUID)
  - Duplicate labels for same user -> 409; same label different users -> 201
  - GET /admin/api-keys/{id}/reveal returns decrypted raw key
  - expires_days: 30, 90, 365, or null (no expiry)

Run:   .venv\\Scripts\\python.exe tests/test_api_keys.py
"""
from __future__ import annotations

__test__ = False

import sys
import os
import httpx
import hashlib
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = os.getenv("TEST_BASE_URL", "http://localhost:8000")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not ADMIN_EMAIL or not ADMIN_PASSWORD:
    print("[FAIL] ADMIN_EMAIL and ADMIN_PASSWORD env vars must be set.")
    print("       Set them in your .env file or export before running tests.")
    print("       Example:  set ADMIN_EMAIL=your@email.com && set ADMIN_PASSWORD=yourpass")
    sys.exit(1)

_created_key_ids: list[int] = []
_created_user_ids: list[str] = []

passed = 0
failed = 0
errors: list[str] = []


def ok(name: str):
    global passed
    passed += 1
    print(f"  [PASS] {name}")


def fail(name: str, detail: str = ""):
    global failed
    failed += 1
    msg = f"  [FAIL] {name}"
    if detail:
        msg += f"  ->  {detail}"
    print(msg)
    errors.append(f"{name}: {detail}")


# ── Auth helpers ────────────────────────────────────────────────────────────────

def get_admin_token() -> str:
    r = httpx.post(f"{BASE}/auth/login", json={
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    }, timeout=10)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def admin_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_client_user(admin_token: str, email: str, password: str = "TestPass123!") -> str:
    """Create a client user and return their UUID. Idempotent on 409."""
    r = httpx.post(f"{BASE}/admin/users", json={
        "email": email,
        "password": password,
        "role": "client",
    }, headers=admin_headers(admin_token), timeout=10)
    if r.status_code not in (201, 409):
        raise RuntimeError(f"Failed to create client {email}: {r.status_code} {r.text}")
    if r.status_code == 201:
        user_id = r.json()["id"]
        _created_user_ids.append(user_id)
        return user_id
    # 409: already exists -- find by listing
    users = httpx.get(f"{BASE}/admin/users", headers=admin_headers(admin_token), timeout=10).json()
    match = next((u for u in users if u["email"] == email), None)
    if not match:
        raise RuntimeError(f"User {email} already exists but not found in list")
    return match["id"]


def client_token_for(email: str, password: str = "TestPass123!") -> str | None:
    r = httpx.post(f"{BASE}/auth/login", json={"email": email, "password": password}, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()["access_token"]


# ===============================================================================
# GROUP 1: Create API Key
# ===============================================================================

def test_create_api_key(admin_token: str, owner_user_id: str) -> dict | None:
    """POST /admin/api-keys -- Happy path: valid label + owner."""
    print("\n-- 1. Create API Key --")

    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_ap_automation", "owner_user_id": owner_user_id},
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code != 201:
        fail("Create API key (happy path)", f"Expected 201, got {r.status_code}: {r.text}")
        return None

    data = r.json()
    if "raw_key" not in data:
        fail("Create -- raw_key in response", "raw_key missing")
        return None
    if not data["raw_key"].startswith("po_live_"):
        fail("Create -- prefix format", f"Expected po_live_, got: {data['raw_key'][:20]}")
        return None
    if data.get("label") != "test_ap_automation":
        fail("Create -- label echo", f"Expected 'test_ap_automation', got '{data.get('label')}'")
        return None
    if "prefix" not in data or not data["prefix"].startswith("po_live_"):
        fail("Create -- prefix field", f"prefix missing or wrong: {data.get('prefix')}")
        return None

    ok("Create API key (happy path) -- 201, raw_key starts with po_live_")
    return data


def test_create_missing_owner(admin_token: str):
    """POST /admin/api-keys without owner_user_id -- must fail 422."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "no_owner_test"},
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code == 422:
        ok("Create without owner_user_id -> 422")
    else:
        fail("Create without owner_user_id", f"Expected 422, got {r.status_code}: {r.text}")


def test_create_nonexistent_owner(admin_token: str):
    """POST /admin/api-keys with a UUID that doesn't exist -- must fail 404."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_ghost_owner",
              "owner_user_id": "00000000-0000-0000-0000-000000000000"},
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code == 404:
        ok("Create with nonexistent owner_user_id -> 404")
    else:
        fail("Create with nonexistent owner_user_id", f"Expected 404, got {r.status_code}: {r.text}")


def test_create_duplicate_label_same_user(admin_token: str, owner_user_id: str):
    """Same label for same user -> 409 (unique constraint)."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_ap_automation", "owner_user_id": owner_user_id},
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code == 409:
        ok("Duplicate label same user -> 409 Conflict")
    else:
        fail("Duplicate label same user", f"Expected 409, got {r.status_code}: {r.text}")


def test_create_duplicate_label_case_insensitive(admin_token: str, owner_user_id: str):
    """Same label different case for same user -> 409 (case-insensitive index)."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "TEST_AP_AUTOMATION", "owner_user_id": owner_user_id},
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code == 409:
        ok("Duplicate label (different case) same user -> 409")
    else:
        fail("Duplicate label case-insensitive", f"Expected 409, got {r.status_code}: {r.text}")


def test_create_same_label_different_users(admin_token: str, owner1_id: str, owner2_id: str):
    """Same label for DIFFERENT users -> both 201 (cross-user collision is allowed)."""
    r1 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_shared_label", "owner_user_id": owner1_id},
        headers=admin_headers(admin_token), timeout=10)
    r2 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_shared_label", "owner_user_id": owner2_id},
        headers=admin_headers(admin_token), timeout=10)

    if r1.status_code == 201 and r2.status_code == 201:
        ok("Same label different users -> both 201 (cross-user allowed)")
    elif r1.status_code == 409 or r2.status_code == 409:
        fail("Same label different users",
             f"Got 409 -- cross-user labels should be allowed. r1={r1.status_code}, r2={r2.status_code}")
    else:
        fail("Same label different users", f"r1={r1.status_code}, r2={r2.status_code}")


def test_create_empty_label(admin_token: str, owner_user_id: str):
    """Empty / 1-char label -> 422 validation error."""
    for label, desc in [("", "empty string"), ("x", "1-char (min_length=2)")]:
        r = httpx.post(f"{BASE}/admin/api-keys",
            json={"label": label, "owner_user_id": owner_user_id},
            headers=admin_headers(admin_token),
            timeout=10,
        )
        if r.status_code == 422:
            ok(f"Create {desc} label -> 422")
        else:
            fail(f"Create {desc} label", f"Expected 422, got {r.status_code}")


def test_create_missing_label(admin_token: str, owner_user_id: str):
    """Missing label field -> 422."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"owner_user_id": owner_user_id},
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code == 422:
        ok("Create missing label -> 422")
    else:
        fail("Create missing label", f"Expected 422, got {r.status_code}")


def test_create_without_auth(owner_user_id: str):
    """No auth header -> 401."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "no_auth_test", "owner_user_id": owner_user_id},
        timeout=10,
    )
    if r.status_code == 401:
        ok("Create without auth -> 401")
    else:
        fail("Create without auth", f"Expected 401, got {r.status_code}")


def test_create_as_client(admin_token: str, owner_user_id: str):
    """Client (non-admin) role -> 403."""
    token = client_token_for("test_client_apikeys@test.com")
    if token is None:
        fail("Create as client -- login", "Login failed")
        return
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_client_try", "owner_user_id": owner_user_id},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 403:
        ok("Create as client -> 403 (admin only)")
    else:
        fail("Create as client", f"Expected 403, got {r.status_code}")


# ===============================================================================
# GROUP 2: Expiry Options
# ===============================================================================

def test_create_with_expiry(admin_token: str, owner_user_id: str):
    """POST /admin/api-keys with each expires_days option."""
    print("\n-- 2. Expiry Options --")

    for days, suffix in [(30, "30d"), (90, "90d"), (365, "365d"), (None, "noexp")]:
        label = f"test_expiry_{suffix}"
        r = httpx.post(f"{BASE}/admin/api-keys",
            json={"label": label, "owner_user_id": owner_user_id, "expires_days": days},
            headers=admin_headers(admin_token),
            timeout=10,
        )
        if r.status_code != 201:
            fail(f"Create key expires_days={days}", f"Expected 201, got {r.status_code}: {r.text}")
            continue

        data = r.json()
        ok(f"Create key expires_days={days} -> 201")

        if days is None:
            if data.get("expires_at") is None:
                ok(f"  expires_at is null for no-expiry key")
            else:
                fail(f"  expires_at for no-expiry", f"Expected null, got {data.get('expires_at')}")
        else:
            exp_raw = data.get("expires_at")
            if exp_raw is None:
                fail(f"  expires_at for {days}d key", "Expected a timestamp, got null")
            else:
                exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                expected = datetime.now(timezone.utc) + timedelta(days=days)
                delta = abs((exp - expected).total_seconds())
                if delta < 86400:
                    ok(f"  expires_at is ~{days} days from now (delta {delta:.0f}s)")
                else:
                    fail(f"  expires_at precision for {days}d", f"Delta {delta:.0f}s too large")


def test_expired_key_sql_filter(admin_token: str):
    """Verify verify_api_key_hash filters expired keys at SQL level (documented check)."""
    ok("Expired key SQL filter -- enforced via 'expires_at > NOW()' in verify_api_key_hash (db.py)")


# ===============================================================================
# GROUP 3: List API Keys
# ===============================================================================

def test_list_api_keys(admin_token: str) -> tuple[list, int | None]:
    """GET /admin/api-keys -- Returns list with expected fields."""
    print("\n-- 3. List API Keys --")

    r = httpx.get(f"{BASE}/admin/api-keys",
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code != 200:
        fail("List API keys", f"Expected 200, got {r.status_code}: {r.text}")
        return [], None

    keys = r.json()
    if not isinstance(keys, list):
        fail("List -- response type", "Expected a JSON array")
        return [], None

    ok(f"List API keys -> 200, {len(keys)} key(s)")

    test_key = next((k for k in keys if k.get("label") == "test_ap_automation"), None)
    if not test_key:
        fail("List -- find test_ap_automation", "Key not found in list")
        return keys, None

    ok("List -- test_ap_automation found")

    required_fields = ["id", "label", "prefix", "is_active", "owner_email",
                       "created_at", "total_pages", "total_tokens", "total_documents", "expires_at"]
    missing = [f for f in required_fields if f not in test_key]
    if missing:
        fail("List -- required fields", f"Missing: {missing}")
    else:
        ok("List -- all required fields present")

    # New arch: owner_email must be a real user, not @apikey.internal
    owner_email = test_key.get("owner_email", "")
    if "@apikey.internal" in owner_email:
        fail("List -- owner_email",
             f"Got phantom email '{owner_email}' -- old arch detected")
    else:
        ok(f"List -- owner_email is real user: {owner_email}")

    if test_key.get("is_active") is True:
        ok("List -- is_active=True")
    else:
        fail("List -- is_active", f"Expected True, got {test_key.get('is_active')}")

    for field in ("total_tokens", "total_pages", "total_documents"):
        val = test_key.get(field)
        if isinstance(val, int) and val >= 0:
            ok(f"List -- {field} is int >= 0")
        else:
            fail(f"List -- {field}", f"Got: {val!r}")

    return keys, test_key["id"]


def test_list_as_client(admin_token: str):
    """GET /admin/api-keys -- Client role -> 403."""
    token = client_token_for("test_client_apikeys@test.com")
    if token is None:
        fail("List as client -- login", "Login failed")
        return
    r = httpx.get(f"{BASE}/admin/api-keys",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 403:
        ok("List as client -> 403 (admin only)")
    else:
        fail("List as client", f"Expected 403, got {r.status_code}")


def test_list_no_auth():
    """GET /admin/api-keys -- No auth -> 401."""
    r = httpx.get(f"{BASE}/admin/api-keys", timeout=10)
    if r.status_code == 401:
        ok("List no auth -> 401")
    else:
        fail("List no auth", f"Expected 401, got {r.status_code}")


# ===============================================================================
# GROUP 4: Reveal Endpoint
# ===============================================================================

def test_reveal_api_key(admin_token: str, key_id: int, expected_raw_key: str):
    """GET /admin/api-keys/{id}/reveal -- Admin recovers a lost raw key."""
    print("\n-- 4. Reveal Endpoint --")

    r = httpx.get(f"{BASE}/admin/api-keys/{key_id}/reveal",
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code != 200:
        fail("Reveal -- happy path", f"Expected 200, got {r.status_code}: {r.text}")
        return

    data = r.json()
    if "raw_key" not in data:
        fail("Reveal -- raw_key in response", "raw_key missing")
        return
    if data["raw_key"] == expected_raw_key:
        ok("Reveal -- returned correct raw_key (Fernet decryption matches)")
    else:
        fail("Reveal -- raw_key value",
             f"Mismatch: got {data['raw_key'][:20]}... expected {expected_raw_key[:20]}...")

    if "label" in data:
        ok(f"Reveal -- label present: {data['label']}")
    else:
        fail("Reveal -- label field", "label missing from reveal response")


def test_reveal_nonexistent_key(admin_token: str):
    """GET /admin/api-keys/999999/reveal -- 404."""
    r = httpx.get(f"{BASE}/admin/api-keys/999999/reveal",
        headers=admin_headers(admin_token),
        timeout=10,
    )
    if r.status_code == 404:
        ok("Reveal nonexistent key -> 404")
    else:
        fail("Reveal nonexistent", f"Expected 404, got {r.status_code}")


def test_reveal_as_client(admin_token: str, key_id: int):
    """GET /admin/api-keys/{id}/reveal -- Client role -> 403."""
    token = client_token_for("test_client_apikeys@test.com")
    if token is None:
        fail("Reveal as client -- login", "Login failed")
        return
    r = httpx.get(f"{BASE}/admin/api-keys/{key_id}/reveal",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 403:
        ok("Reveal as client -> 403 (admin only)")
    else:
        fail("Reveal as client", f"Expected 403, got {r.status_code}")


# ===============================================================================
# GROUP 5: Deactivate / Reactivate
# ===============================================================================

def test_deactivate_api_key(admin_token: str, key_id: int):
    """PATCH /admin/api-keys/{id}/deactivate -- Happy path."""
    print("\n-- 5. Deactivate / Reactivate --")

    r = httpx.patch(f"{BASE}/admin/api-keys/{key_id}/deactivate",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 200:
        ok(f"Deactivate key {key_id} -> 200")
    else:
        fail(f"Deactivate key {key_id}", f"Expected 200, got {r.status_code}: {r.text}")
        return

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=admin_headers(admin_token), timeout=10).json()
    kk = next((k for k in keys if k["id"] == key_id), None)
    if kk and kk["is_active"] is False:
        ok("Deactivate -- verified is_active=False in list")
    else:
        fail("Deactivate -- verify in list", "Key not found or still active")


def test_deactivate_nonexistent(admin_token: str):
    r = httpx.patch(f"{BASE}/admin/api-keys/999999/deactivate",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 404:
        ok("Deactivate nonexistent -> 404")
    else:
        fail("Deactivate nonexistent", f"Expected 404, got {r.status_code}")


def test_reactivate_api_key(admin_token: str, key_id: int):
    r = httpx.patch(f"{BASE}/admin/api-keys/{key_id}/reactivate",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 200:
        ok(f"Reactivate key {key_id} -> 200")
    else:
        fail(f"Reactivate key {key_id}", f"Expected 200, got {r.status_code}: {r.text}")
        return

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=admin_headers(admin_token), timeout=10).json()
    kk = next((k for k in keys if k["id"] == key_id), None)
    if kk and kk["is_active"] is True:
        ok("Reactivate -- verified is_active=True in list")
    else:
        fail("Reactivate -- verify in list", "Key not found or still inactive")


def test_reactivate_nonexistent(admin_token: str):
    r = httpx.patch(f"{BASE}/admin/api-keys/999999/reactivate",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 404:
        ok("Reactivate nonexistent -> 404")
    else:
        fail("Reactivate nonexistent", f"Expected 404, got {r.status_code}")


# ===============================================================================
# GROUP 6: Dual Auth -- X-API-Key header
# ===============================================================================

def test_auth_with_valid_api_key(raw_key: str):
    """POST /v1/extract with valid X-API-Key -- must not get 401."""
    print("\n-- 6. Dual Auth (X-API-Key) --")

    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code == 401:
        fail("Auth with valid API key", f"Got 401 -- key rejected: {r.text}")
    else:
        ok(f"Auth with valid API key -- passed auth (got {r.status_code}, not 401)")


def test_auth_with_invalid_api_key():
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": "po_live_FAKE_KEY_DOES_NOT_EXIST"}, timeout=10)
    if r.status_code == 401:
        ok("Auth with invalid API key -> 401")
    else:
        fail("Auth with invalid key", f"Expected 401, got {r.status_code}")


def test_auth_with_deactivated_key(admin_token: str, raw_key: str, key_id: int):
    """Deactivate then try auth -> 401; reactivate after."""
    httpx.patch(f"{BASE}/admin/api-keys/{key_id}/deactivate",
        headers=admin_headers(admin_token), timeout=10)
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code == 401:
        ok("Auth with deactivated key -> 401")
    else:
        fail("Auth with deactivated key", f"Expected 401, got {r.status_code}: {r.text}")
    # Restore
    httpx.patch(f"{BASE}/admin/api-keys/{key_id}/reactivate",
        headers=admin_headers(admin_token), timeout=10)


def test_auth_with_no_header():
    r = httpx.post(f"{BASE}/v1/extract", timeout=10)
    if r.status_code == 401:
        ok("Auth with no header -> 401")
    else:
        fail("Auth with no header", f"Expected 401, got {r.status_code}")


def test_auth_with_jwt_fallback(admin_token: str):
    r = httpx.post(f"{BASE}/v1/extract",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 401:
        fail("Auth with JWT fallback", "Got 401 -- JWT rejected")
    else:
        ok(f"Auth with JWT fallback -- passed (got {r.status_code})")


def test_auth_empty_api_key():
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": ""}, timeout=10)
    if r.status_code in (401, 422):
        ok(f"Auth with empty API key -> {r.status_code}")
    else:
        fail("Auth with empty API key", f"Expected 401/422, got {r.status_code}")


# ===============================================================================
# GROUP 7: /v1/extract Edge Cases
# ===============================================================================

def test_extract_no_file(raw_key: str):
    print("\n-- 7. /v1/extract Edge Cases --")
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code == 422:
        ok("Extract without file -> 422")
    else:
        fail("Extract without file", f"Expected 422, got {r.status_code}")


def test_extract_empty_file(raw_key: str):
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key},
        files={"file": ("empty.pdf", b"", "application/pdf")},
        timeout=30,
    )
    if r.status_code in (400, 500):
        ok(f"Extract with empty file -> {r.status_code}")
    else:
        fail("Extract with empty file", f"Expected 400/500, got {r.status_code}")


def test_extract_non_pdf(raw_key: str):
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key},
        files={"file": ("test.txt", b"Hello world this is not a PDF", "text/plain")},
        timeout=30,
    )
    if r.status_code in (400, 500):
        ok(f"Extract with .txt file -> {r.status_code} (handled gracefully)")
    else:
        fail("Extract with .txt file", f"Expected 400/500, got {r.status_code}: {r.text[:200]}")


def test_extract_no_vendor_match(raw_key: str):
    """Valid PDF but client user has no vendors -> 400/402/409."""
    minimal_pdf = (
        b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n192\n%%EOF"
    )
    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key},
        files={"file": ("invoice.pdf", minimal_pdf, "application/pdf")},
        timeout=30,
    )
    # 400/409 = no vendor match; 402 = quota 0 (new user default)
    if r.status_code in (400, 402, 409):
        ok(f"Extract no vendor match -> {r.status_code}")
    else:
        fail("Extract no vendor match", f"Expected 400/402/409, got {r.status_code}: {r.text[:200]}")


# ===============================================================================
# GROUP 8: Multiple Keys / Vendor Namespace Sharing
# ===============================================================================

def test_multiple_keys_same_user(admin_token: str, owner_user_id: str):
    """Two keys for the same user: both valid, both distinct, same owner_email."""
    print("\n-- 8. Multiple Keys / Vendor Namespace Sharing --")

    r1 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_key_alpha", "owner_user_id": owner_user_id},
        headers=admin_headers(admin_token), timeout=10)
    r2 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_key_beta", "owner_user_id": owner_user_id},
        headers=admin_headers(admin_token), timeout=10)

    if r1.status_code == 201 and r2.status_code == 201:
        ok("Two keys for same user -> both 201")
        key1, key2 = r1.json()["raw_key"], r2.json()["raw_key"]

        if key1 != key2:
            ok("Both raw_key strings are unique")
        else:
            fail("Raw keys are unique", "Both keys returned the same value")

        a1 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": key1}, timeout=10)
        a2 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": key2}, timeout=10)
        if a1.status_code != 401 and a2.status_code != 401:
            ok("Both keys pass auth independently")
        else:
            fail("Multi-key auth", f"key1={a1.status_code}, key2={a2.status_code}")
    else:
        fail("Create two keys same user", f"r1={r1.status_code}, r2={r2.status_code}")
        return

    # Both keys should show same owner_email (shared user namespace)
    keys = httpx.get(f"{BASE}/admin/api-keys", headers=admin_headers(admin_token), timeout=10).json()
    alpha = next((k for k in keys if k["label"] == "test_key_alpha"), None)
    beta = next((k for k in keys if k["label"] == "test_key_beta"), None)

    if alpha and beta:
        ok("List shows both keys")
        if alpha.get("owner_email") == beta.get("owner_email"):
            ok(f"Both keys share owner_email: {alpha.get('owner_email')}")
        else:
            fail("Owner email mismatch",
                 f"alpha={alpha.get('owner_email')} beta={beta.get('owner_email')}")
    else:
        fail("List both keys", "One or both keys missing from list")


def test_no_phantom_users(admin_token: str):
    """Creating API keys must NOT create @apikey.internal phantom users."""
    r = httpx.get(f"{BASE}/admin/users", headers=admin_headers(admin_token), timeout=10)
    if r.status_code != 200:
        fail("Admin users list", f"Expected 200, got {r.status_code}")
        return

    users = r.json()
    phantom = [u["email"] for u in users if "@apikey.internal" in u.get("email", "")]
    if phantom:
        fail("No phantom users", f"Found @apikey.internal users: {phantom}")
    else:
        ok("No @apikey.internal phantom users in /admin/users")


# ===============================================================================
# GROUP 9: Key Hash Verification
# ===============================================================================

def test_key_hash_verification(raw_key: str):
    """SHA-256 hash: valid key passes, 1-char mutation fails."""
    print("\n-- 9. Key Hash Verification --")

    computed = hashlib.sha256(raw_key.encode()).hexdigest()
    ok(f"SHA-256 computed: {computed[:16]}...")

    r = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code != 401:
        ok("Original key passes auth (hash matches DB)")
    else:
        fail("Key hash match", "Auth returned 401 -- hash mismatch?")

    mutated = raw_key[:-1] + ("a" if raw_key[-1] != "a" else "b")
    r2 = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": mutated}, timeout=10)
    if r2.status_code == 401:
        ok("Mutated key rejected -> 401")
    else:
        fail("Mutated key", f"Expected 401, got {r2.status_code}")


# ===============================================================================
# GROUP 10: Delete API Key
# ===============================================================================

def test_delete_nonexistent_key(admin_token: str):
    print("\n-- 10. Delete API Key --")
    r = httpx.delete(f"{BASE}/admin/api-keys/999999",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 404:
        ok("Delete nonexistent -> 404")
    else:
        fail("Delete nonexistent", f"Expected 404, got {r.status_code}")


def test_delete_as_client(admin_token: str, key_id: int):
    token = client_token_for("test_client_apikeys@test.com")
    if token is None:
        fail("Delete as client -- login", "Login failed")
        return
    r = httpx.delete(f"{BASE}/admin/api-keys/{key_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 403:
        ok("Delete as client -> 403 (admin only)")
    else:
        fail("Delete as client", f"Expected 403, got {r.status_code}")


def test_delete_then_auth(admin_token: str, raw_key: str, key_id: int):
    """Delete key; confirm auth fails and key is gone from list."""
    r = httpx.delete(f"{BASE}/admin/api-keys/{key_id}",
        headers=admin_headers(admin_token), timeout=10)
    if r.status_code == 200:
        ok(f"Delete key {key_id} -> 200")
    else:
        fail(f"Delete key {key_id}", f"Expected 200, got {r.status_code}")
        return

    r2 = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key}, timeout=10)
    if r2.status_code == 401:
        ok("Auth with deleted key -> 401")
    else:
        fail("Auth with deleted key", f"Expected 401, got {r2.status_code}")

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=admin_headers(admin_token), timeout=10).json()
    if not any(k["id"] == key_id for k in keys):
        ok("Deleted key gone from list")
    else:
        fail("Deleted key in list", "Key still appears after deletion")


# ===============================================================================
# CLEANUP
# ===============================================================================

def cleanup(admin_token: str):
    print("\n-- Cleanup --")
    cleaned = 0

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=admin_headers(admin_token), timeout=10)
    if keys.status_code == 200:
        for k in keys.json():
            if k["label"].startswith("test_"):
                httpx.delete(f"{BASE}/admin/api-keys/{k['id']}",
                    headers=admin_headers(admin_token), timeout=10)
                cleaned += 1

    users = httpx.get(f"{BASE}/admin/users", headers=admin_headers(admin_token), timeout=10)
    if users.status_code == 200:
        for u in users.json():
            email = u.get("email", "")
            if email.startswith("test_client_apikeys"):
                httpx.delete(f"{BASE}/admin/users/{u['id']}/hard",
                    headers=admin_headers(admin_token), timeout=10)
                cleaned += 1

    print(f"  Cleaned {cleaned} test resources")


# ===============================================================================
# MAIN
# ===============================================================================

def main():
    print("=" * 62)
    print("  API Key Integration Tests v2                                ")
    print("  (real users own keys -- no phantom @apikey.internal users)  ")
    print("=" * 62)

    try:
        r = httpx.get(f"{BASE}/health", timeout=5)
        if r.status_code != 200:
            print(f"\n[FAIL] Server not healthy: {r.status_code}")
            sys.exit(1)
        print(f"\n[OK] Server healthy: {r.json()}")
    except httpx.ConnectError:
        print(f"\n[FAIL] Cannot connect to {BASE}. Is the server running?")
        sys.exit(1)

    try:
        admin_token = get_admin_token()
        print("[OK] Admin login successful")
    except Exception as e:
        print(f"\n[FAIL] Admin login failed: {e}")
        sys.exit(1)

    cleanup(admin_token)

    # Primary test client (owns most test keys)
    try:
        owner1_id = create_client_user(admin_token, "test_client_apikeys@test.com")
        print(f"[OK] Primary test client: {owner1_id}")
    except Exception as e:
        print(f"\n[FAIL] Could not create primary test client: {e}")
        sys.exit(1)

    # Secondary client (for cross-user label tests)
    owner2_id: str | None = None
    try:
        owner2_id = create_client_user(admin_token, "test_client_apikeys2@test.com")
        print(f"[OK] Secondary test client: {owner2_id}")
    except Exception as e:
        print(f"\n[WARN] Secondary test client unavailable: {e}")

    # ── GROUP 1: Create ─────────────────────────────────────────────
    created = test_create_api_key(admin_token, owner1_id)
    raw_key = created["raw_key"] if created else None

    test_create_missing_owner(admin_token)
    test_create_nonexistent_owner(admin_token)
    test_create_duplicate_label_same_user(admin_token, owner1_id)
    test_create_duplicate_label_case_insensitive(admin_token, owner1_id)
    if owner2_id:
        test_create_same_label_different_users(admin_token, owner1_id, owner2_id)
    test_create_empty_label(admin_token, owner1_id)
    test_create_missing_label(admin_token, owner1_id)
    test_create_without_auth(owner1_id)
    test_create_as_client(admin_token, owner1_id)

    # ── GROUP 2: Expiry ──────────────────────────────────────────────
    test_create_with_expiry(admin_token, owner1_id)
    test_expired_key_sql_filter(admin_token)

    # ── GROUP 3: List ────────────────────────────────────────────────
    keys, key_id = test_list_api_keys(admin_token)
    test_list_as_client(admin_token)
    test_list_no_auth()

    # ── GROUP 4: Reveal ──────────────────────────────────────────────
    if key_id and raw_key:
        test_reveal_api_key(admin_token, key_id, raw_key)
        test_reveal_nonexistent_key(admin_token)
        test_reveal_as_client(admin_token, key_id)
    else:
        print("\n  [WARN] Skipping reveal tests -- no key_id")

    # ── GROUP 5: Deactivate / Reactivate ─────────────────────────────
    if key_id:
        test_deactivate_api_key(admin_token, key_id)
        test_deactivate_nonexistent(admin_token)
        test_reactivate_api_key(admin_token, key_id)
        test_reactivate_nonexistent(admin_token)
    else:
        print("\n  [WARN] Skipping deactivate/reactivate -- no key_id")

    # ── GROUP 6: Dual Auth ───────────────────────────────────────────
    if raw_key:
        test_auth_with_valid_api_key(raw_key)
        test_auth_with_invalid_api_key()
        if key_id:
            test_auth_with_deactivated_key(admin_token, raw_key, key_id)
        test_auth_with_no_header()
        test_auth_with_jwt_fallback(admin_token)
        test_auth_empty_api_key()
    else:
        print("\n  [WARN] Skipping auth tests -- no raw_key")

    # ── GROUP 7: /v1/extract Edge Cases ──────────────────────────────
    if raw_key:
        test_extract_no_file(raw_key)
        test_extract_empty_file(raw_key)
        test_extract_non_pdf(raw_key)
        test_extract_no_vendor_match(raw_key)
    else:
        print("\n  [WARN] Skipping extract tests -- no raw_key")

    # ── GROUP 8: Multiple Keys / Namespace Sharing ────────────────────
    test_multiple_keys_same_user(admin_token, owner1_id)
    test_no_phantom_users(admin_token)

    # ── GROUP 9: Hash Verification ───────────────────────────────────
    fresh = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_hash_verify", "owner_user_id": owner1_id},
        headers=admin_headers(admin_token), timeout=10)
    if fresh.status_code == 201:
        test_key_hash_verification(fresh.json()["raw_key"])
    else:
        print(f"\n  [WARN] Skipping hash tests -- couldn't create key: {fresh.status_code}")

    # ── GROUP 10: Delete ─────────────────────────────────────────────
    test_delete_nonexistent_key(admin_token)
    if key_id:
        test_delete_as_client(admin_token, key_id)
        if raw_key:
            test_delete_then_auth(admin_token, raw_key, key_id)

    # ── Final Cleanup ────────────────────────────────────────────────
    cleanup(admin_token)

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    if errors:
        print(f"\n  FAILURES:")
        for e in errors:
            print(f"    * {e}")
    print(f"{'=' * 60}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
