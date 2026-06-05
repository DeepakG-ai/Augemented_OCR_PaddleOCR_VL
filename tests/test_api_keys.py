"""
test_api_keys.py  --  Integration tests for API Key management.

Architecture:
  - Admin creates keys; client users cannot.
  - Each key is assigned to an existing client user (owner_user_id).
  - One client user can own many keys; all keys share that user's vendor namespace.
  - No phantom @apikey.internal users are created.

Run:
  .venv\\Scripts\\python.exe tests/test_api_keys.py

Prerequisites:
  Set ADMIN_EMAIL and ADMIN_PASSWORD in backend/.env or as env vars.
"""
from __future__ import annotations

__test__ = False

import sys
import os
import hashlib
from datetime import datetime, timezone, timedelta

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE          = os.getenv("TEST_BASE_URL", "http://localhost:8055")
ADMIN_EMAIL   = os.getenv("ADMIN_EMAIL")
ADMIN_PASS    = os.getenv("ADMIN_PASSWORD")
CLIENT1_EMAIL = "test_client_1@apitest.com"
CLIENT2_EMAIL = "test_client_2@apitest.com"
TEST_PASS     = "TestPass123!"

if not ADMIN_EMAIL or not ADMIN_PASS:
    print("[FAIL] Set ADMIN_EMAIL and ADMIN_PASSWORD in .env")
    sys.exit(1)

passed = 0
failed = 0
_failures: list[str] = []


def ok(name: str):
    global passed
    passed += 1
    print(f"  [PASS] {name}")


def fail(name: str, detail: str = ""):
    global failed
    failed += 1
    label = f"  [FAIL] {name}" + (f"  ->  {detail}" if detail else "")
    print(label)
    _failures.append(label.strip())


# ── helpers ──────────────────────────────────────────────────────────────────

def login(email: str, password: str) -> str | None:
    r = httpx.post(f"{BASE}/auth/login", json={"email": email, "password": password}, timeout=10)
    return r.json().get("access_token") if r.status_code == 200 else None


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_user(admin_token: str, email: str) -> str | None:
    """Create a client user; return their UUID. Returns None on error."""
    r = httpx.post(f"{BASE}/admin/users",
        json={"email": email, "password": TEST_PASS, "role": "client"},
        headers=auth(admin_token), timeout=10)
    if r.status_code == 201:
        return r.json()["id"]
    if r.status_code == 409:
        # already exists — find in list
        users = httpx.get(f"{BASE}/admin/users", headers=auth(admin_token), timeout=10).json()
        match = next((u for u in users if u["email"] == email), None)
        return match["id"] if match else None
    return None


def delete_test_data(admin_token: str):
    """Remove all test keys and test client users."""
    keys = httpx.get(f"{BASE}/admin/api-keys", headers=auth(admin_token), timeout=10)
    if keys.status_code == 200:
        for k in keys.json():
            if k["label"].startswith("test_"):
                httpx.delete(f"{BASE}/admin/api-keys/{k['id']}",
                    headers=auth(admin_token), timeout=10)

    users = httpx.get(f"{BASE}/admin/users", headers=auth(admin_token), timeout=10)
    if users.status_code == 200:
        for u in users.json():
            if u.get("email", "").endswith("@apitest.com"):
                httpx.delete(f"{BASE}/admin/users/{u['id']}/hard",
                    headers=auth(admin_token), timeout=10)


# ── GROUP 1: Create ───────────────────────────────────────────────────────────

def test_create(admin_token: str, owner_id: str) -> dict | None:
    print("\n-- 1. Create API Key --")
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_primary_key", "owner_user_id": owner_id},
        headers=auth(admin_token), timeout=10)

    if r.status_code != 201:
        fail("Create key (happy path)", f"Expected 201, got {r.status_code}: {r.text}")
        return None

    data = r.json()
    if not data.get("raw_key", "").startswith("po_live_"):
        fail("Create key -- raw_key format", f"Got: {data.get('raw_key','')[:20]}")
        return None
    if data.get("label") != "test_primary_key":
        fail("Create key -- label echo", f"Got: {data.get('label')}")
        return None
    if not data.get("prefix", "").startswith("po_live_"):
        fail("Create key -- prefix field", f"Got: {data.get('prefix')}")
        return None

    ok("Create key -> 201, raw_key starts with po_live_")
    return data


def test_create_requires_owner(admin_token: str):
    """owner_user_id is required — missing it must return 422."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_no_owner"},
        headers=auth(admin_token), timeout=10)
    if r.status_code == 422:
        ok("Create without owner_user_id -> 422")
    else:
        fail("Create without owner_user_id", f"Expected 422, got {r.status_code}")


def test_create_nonexistent_owner(admin_token: str):
    """A UUID that doesn't exist must return 404."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_ghost", "owner_user_id": "00000000-0000-0000-0000-000000000000"},
        headers=auth(admin_token), timeout=10)
    if r.status_code == 404:
        ok("Create with nonexistent owner_user_id -> 404")
    else:
        fail("Create with nonexistent owner_user_id", f"Expected 404, got {r.status_code}")


def test_duplicate_label_same_user(admin_token: str, owner_id: str):
    """Same label + same user must return 409."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_primary_key", "owner_user_id": owner_id},
        headers=auth(admin_token), timeout=10)
    if r.status_code == 409:
        ok("Duplicate label same user -> 409")
    else:
        fail("Duplicate label same user", f"Expected 409, got {r.status_code}")


def test_duplicate_label_case_insensitive(admin_token: str, owner_id: str):
    """Case-insensitive duplicate for same user must return 409."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "TEST_PRIMARY_KEY", "owner_user_id": owner_id},
        headers=auth(admin_token), timeout=10)
    if r.status_code == 409:
        ok("Duplicate label (upper-case) same user -> 409")
    else:
        fail("Duplicate label case-insensitive", f"Expected 409, got {r.status_code}")


def test_same_label_different_users(admin_token: str, owner1_id: str, owner2_id: str):
    """Same label for two DIFFERENT users must both return 201."""
    r1 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_shared_label", "owner_user_id": owner1_id},
        headers=auth(admin_token), timeout=10)
    r2 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_shared_label", "owner_user_id": owner2_id},
        headers=auth(admin_token), timeout=10)
    if r1.status_code == 201 and r2.status_code == 201:
        ok("Same label different users -> both 201")
    else:
        fail("Same label different users", f"owner1={r1.status_code}, owner2={r2.status_code}")


def test_create_label_validation(admin_token: str, owner_id: str):
    """Empty and single-char labels must return 422."""
    for label, desc in [("", "empty"), ("x", "1-char")]:
        r = httpx.post(f"{BASE}/admin/api-keys",
            json={"label": label, "owner_user_id": owner_id},
            headers=auth(admin_token), timeout=10)
        if r.status_code == 422:
            ok(f"Label '{desc}' -> 422")
        else:
            fail(f"Label '{desc}'", f"Expected 422, got {r.status_code}")


def test_create_requires_admin(admin_token: str, owner_id: str):
    """A client user must get 403 when trying to create a key."""
    client_token = login(CLIENT1_EMAIL, TEST_PASS)
    if not client_token:
        fail("Create as client -- login failed")
        return
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_client_attempt", "owner_user_id": owner_id},
        headers={"Authorization": f"Bearer {client_token}"}, timeout=10)
    if r.status_code == 403:
        ok("Create as client -> 403 (admin only)")
    else:
        fail("Create as client", f"Expected 403, got {r.status_code}")


def test_create_no_auth(owner_id: str):
    """No auth header must return 401."""
    r = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_no_auth", "owner_user_id": owner_id}, timeout=10)
    if r.status_code == 401:
        ok("Create without auth -> 401")
    else:
        fail("Create without auth", f"Expected 401, got {r.status_code}")


# ── GROUP 2: Expiry ───────────────────────────────────────────────────────────

def test_expiry_options(admin_token: str, owner_id: str):
    print("\n-- 2. Expiry Options --")
    cases = [(30, "test_expiry_30d"), (90, "test_expiry_90d"), (365, "test_expiry_365d"), (None, "test_expiry_none")]
    for days, label in cases:
        r = httpx.post(f"{BASE}/admin/api-keys",
            json={"label": label, "owner_user_id": owner_id, "expires_days": days},
            headers=auth(admin_token), timeout=10)
        if r.status_code != 201:
            fail(f"Expiry {days}d create", f"Expected 201, got {r.status_code}")
            continue
        data = r.json()
        ok(f"Create with expires_days={days} -> 201")
        if days is None:
            if data.get("expires_at") is None:
                ok(f"  No-expiry key: expires_at is null")
            else:
                fail(f"  No-expiry key: expires_at", f"Expected null, got {data.get('expires_at')}")
        else:
            exp_raw = data.get("expires_at")
            if not exp_raw:
                fail(f"  {days}d key: expires_at missing")
            else:
                exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                delta = abs((exp - (datetime.now(timezone.utc) + timedelta(days=days))).total_seconds())
                if delta < 86400:
                    ok(f"  {days}d key: expires_at correct (delta {delta:.0f}s)")
                else:
                    fail(f"  {days}d key: expires_at wrong", f"delta={delta:.0f}s")


# ── GROUP 3: List ─────────────────────────────────────────────────────────────

def test_list(admin_token: str) -> int | None:
    print("\n-- 3. List API Keys --")
    r = httpx.get(f"{BASE}/admin/api-keys", headers=auth(admin_token), timeout=10)
    if r.status_code != 200:
        fail("List keys", f"Expected 200, got {r.status_code}")
        return None

    keys = r.json()
    ok(f"List keys -> 200 ({len(keys)} key(s))")

    key = next((k for k in keys if k.get("label") == "test_primary_key"), None)
    if not key:
        fail("List -- test_primary_key not found")
        return None
    ok("List -- test_primary_key present")

    required = ["id", "label", "prefix", "is_active", "owner_email",
                "created_at", "total_pages", "total_tokens", "total_documents", "expires_at"]
    missing = [f for f in required if f not in key]
    if missing:
        fail("List -- required fields", f"Missing: {missing}")
    else:
        ok("List -- all required fields present")

    if "@apikey.internal" in key.get("owner_email", ""):
        fail("List -- owner_email is phantom user (old arch)", key["owner_email"])
    else:
        ok(f"List -- owner_email is real user: {key['owner_email']}")

    if key.get("is_active") is True:
        ok("List -- is_active=True")
    else:
        fail("List -- is_active", f"Got {key.get('is_active')}")

    r2 = httpx.get(f"{BASE}/admin/api-keys",
        headers={"Authorization": f"Bearer {login(CLIENT1_EMAIL, TEST_PASS)}"}, timeout=10)
    if r2.status_code == 403:
        ok("List as client -> 403")
    else:
        fail("List as client", f"Expected 403, got {r2.status_code}")

    r3 = httpx.get(f"{BASE}/admin/api-keys", timeout=10)
    if r3.status_code == 401:
        ok("List no auth -> 401")
    else:
        fail("List no auth", f"Expected 401, got {r3.status_code}")

    return key["id"]


# ── GROUP 4: Reveal ───────────────────────────────────────────────────────────

def test_reveal(admin_token: str, key_id: int, expected_raw_key: str):
    print("\n-- 4. Reveal Endpoint --")
    r = httpx.get(f"{BASE}/admin/api-keys/{key_id}/reveal",
        headers=auth(admin_token), timeout=10)
    if r.status_code != 200:
        fail("Reveal -> 200", f"Got {r.status_code}: {r.text}")
        return
    data = r.json()
    if data.get("raw_key") == expected_raw_key:
        ok("Reveal -- raw_key matches original")
    else:
        fail("Reveal -- raw_key mismatch",
             f"got {data.get('raw_key','')[:20]}... expected {expected_raw_key[:20]}...")
    if "label" in data:
        ok(f"Reveal -- label present: {data['label']}")
    else:
        fail("Reveal -- label missing")

    r2 = httpx.get(f"{BASE}/admin/api-keys/999999/reveal",
        headers=auth(admin_token), timeout=10)
    if r2.status_code == 404:
        ok("Reveal nonexistent key -> 404")
    else:
        fail("Reveal nonexistent", f"Expected 404, got {r2.status_code}")

    client_token = login(CLIENT1_EMAIL, TEST_PASS)
    if client_token:
        r3 = httpx.get(f"{BASE}/admin/api-keys/{key_id}/reveal",
            headers={"Authorization": f"Bearer {client_token}"}, timeout=10)
        if r3.status_code == 403:
            ok("Reveal as client -> 403")
        else:
            fail("Reveal as client", f"Expected 403, got {r3.status_code}")


# ── GROUP 5: Deactivate / Reactivate ─────────────────────────────────────────

def test_deactivate_reactivate(admin_token: str, key_id: int, raw_key: str):
    print("\n-- 5. Deactivate / Reactivate --")

    r = httpx.patch(f"{BASE}/admin/api-keys/{key_id}/deactivate",
        headers=auth(admin_token), timeout=10)
    if r.status_code != 200:
        fail("Deactivate", f"Expected 200, got {r.status_code}")
        return
    ok(f"Deactivate key {key_id} -> 200")

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=auth(admin_token), timeout=10).json()
    k = next((x for x in keys if x["id"] == key_id), None)
    if k and k["is_active"] is False:
        ok("Deactivate -- is_active=False confirmed in list")
    else:
        fail("Deactivate -- list check", "Key not found or still active")

    r2 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": raw_key}, timeout=10)
    if r2.status_code == 401:
        ok("Deactivated key rejected -> 401")
    else:
        fail("Deactivated key auth", f"Expected 401, got {r2.status_code}")

    r3 = httpx.patch(f"{BASE}/admin/api-keys/{key_id}/reactivate",
        headers=auth(admin_token), timeout=10)
    if r3.status_code == 200:
        ok(f"Reactivate key {key_id} -> 200")
    else:
        fail("Reactivate", f"Expected 200, got {r3.status_code}")

    r4 = httpx.patch(f"{BASE}/admin/api-keys/999999/deactivate",
        headers=auth(admin_token), timeout=10)
    if r4.status_code == 404:
        ok("Deactivate nonexistent -> 404")
    else:
        fail("Deactivate nonexistent", f"Expected 404, got {r4.status_code}")

    r5 = httpx.patch(f"{BASE}/admin/api-keys/999999/reactivate",
        headers=auth(admin_token), timeout=10)
    if r5.status_code == 404:
        ok("Reactivate nonexistent -> 404")
    else:
        fail("Reactivate nonexistent", f"Expected 404, got {r5.status_code}")


# ── GROUP 6: Auth (X-API-Key header) ─────────────────────────────────────────

def test_auth(admin_token: str, raw_key: str):
    print("\n-- 6. API Key Authentication --")

    r = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code != 401:
        ok(f"Valid key passes auth (got {r.status_code}, not 401)")
    else:
        fail("Valid key rejected", "Got 401")

    r2 = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": "po_live_FAKEKEYDOESNOTEXIST"}, timeout=10)
    if r2.status_code == 401:
        ok("Invalid key -> 401")
    else:
        fail("Invalid key", f"Expected 401, got {r2.status_code}")

    r3 = httpx.post(f"{BASE}/v1/extract", timeout=10)
    if r3.status_code == 401:
        ok("No auth header -> 401")
    else:
        fail("No auth header", f"Expected 401, got {r3.status_code}")

    r4 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": ""}, timeout=10)
    if r4.status_code in (401, 422):
        ok(f"Empty API key -> {r4.status_code}")
    else:
        fail("Empty API key", f"Expected 401/422, got {r4.status_code}")

    r5 = httpx.post(f"{BASE}/v1/extract", headers=auth(admin_token), timeout=10)
    if r5.status_code != 401:
        ok(f"JWT Bearer also accepted (got {r5.status_code})")
    else:
        fail("JWT Bearer rejected", "Got 401")


# ── GROUP 7: /v1/extract edge cases ──────────────────────────────────────────

def test_extract_edge_cases(raw_key: str):
    print("\n-- 7. /v1/extract Edge Cases --")

    r = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code == 422:
        ok("No file -> 422")
    else:
        fail("No file", f"Expected 422, got {r.status_code}")

    r2 = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key},
        files={"file": ("empty.pdf", b"", "application/pdf")}, timeout=30)
    if r2.status_code in (400, 415, 500):
        ok(f"Empty file -> {r2.status_code}")
    else:
        fail("Empty file", f"Expected 400/415/500, got {r2.status_code}")

    r3 = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key},
        files={"file": ("doc.txt", b"not a pdf", "text/plain")}, timeout=30)
    if r3.status_code in (400, 415, 500):
        ok(f"Non-PDF file -> {r3.status_code}")
    else:
        fail("Non-PDF file", f"Expected 400/415/500, got {r3.status_code}")

    minimal_pdf = (
        b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n192\n%%EOF"
    )
    r4 = httpx.post(f"{BASE}/v1/extract",
        headers={"X-API-Key": raw_key},
        files={"file": ("invoice.pdf", minimal_pdf, "application/pdf")}, timeout=30)
    if r4.status_code in (400, 402, 409):
        ok(f"Valid PDF, no vendor match -> {r4.status_code}")
    else:
        fail("No vendor match", f"Expected 400/402/409, got {r4.status_code}")


# ── GROUP 8: Multiple keys / shared vendor namespace ─────────────────────────

def test_multiple_keys(admin_token: str, owner_id: str):
    print("\n-- 8. Multiple Keys / Shared Vendor Namespace --")

    r1 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_key_alpha", "owner_user_id": owner_id},
        headers=auth(admin_token), timeout=10)
    r2 = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_key_beta", "owner_user_id": owner_id},
        headers=auth(admin_token), timeout=10)

    if r1.status_code != 201 or r2.status_code != 201:
        fail("Two keys same user", f"r1={r1.status_code}, r2={r2.status_code}")
        return
    ok("Two keys for same user -> both 201")

    key_a, key_b = r1.json()["raw_key"], r2.json()["raw_key"]
    if key_a != key_b:
        ok("Both keys are unique values")
    else:
        fail("Keys are unique", "Got same raw_key value for both")

    a1 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": key_a}, timeout=10)
    a2 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": key_b}, timeout=10)
    if a1.status_code != 401 and a2.status_code != 401:
        ok("Both keys authenticate independently")
    else:
        fail("Both keys auth", f"key_a={a1.status_code}, key_b={a2.status_code}")

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=auth(admin_token), timeout=10).json()
    alpha = next((k for k in keys if k["label"] == "test_key_alpha"), None)
    beta  = next((k for k in keys if k["label"] == "test_key_beta"),  None)
    if alpha and beta and alpha.get("owner_email") == beta.get("owner_email"):
        ok(f"Both keys share owner_email: {alpha['owner_email']}")
    else:
        fail("Shared owner_email", "Emails differ or keys not found")

    # Confirm no @apikey.internal phantom users were created
    users = httpx.get(f"{BASE}/admin/users", headers=auth(admin_token), timeout=10).json()
    phantom = [u["email"] for u in users if "@apikey.internal" in u.get("email", "")]
    if phantom:
        fail("No phantom users", f"Found: {phantom}")
    else:
        ok("No @apikey.internal phantom users in system")


# ── GROUP 9: Hash verification ────────────────────────────────────────────────

def test_key_hash(raw_key: str):
    print("\n-- 9. Key Hash Verification --")
    h = hashlib.sha256(raw_key.encode()).hexdigest()
    ok(f"SHA-256: {h[:16]}...")

    r = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": raw_key}, timeout=10)
    if r.status_code != 401:
        ok("Original key passes auth (hash matches DB)")
    else:
        fail("Hash match", "Got 401 on original key")

    mutated = raw_key[:-1] + ("a" if raw_key[-1] != "a" else "b")
    r2 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": mutated}, timeout=10)
    if r2.status_code == 401:
        ok("1-char mutation rejected -> 401")
    else:
        fail("Mutated key", f"Expected 401, got {r2.status_code}")


# ── GROUP 10: Delete ──────────────────────────────────────────────────────────

def test_delete(admin_token: str, raw_key: str, key_id: int):
    print("\n-- 10. Delete API Key --")

    r = httpx.delete(f"{BASE}/admin/api-keys/999999",
        headers=auth(admin_token), timeout=10)
    if r.status_code == 404:
        ok("Delete nonexistent -> 404")
    else:
        fail("Delete nonexistent", f"Expected 404, got {r.status_code}")

    client_token = login(CLIENT1_EMAIL, TEST_PASS)
    if client_token:
        r2 = httpx.delete(f"{BASE}/admin/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {client_token}"}, timeout=10)
        if r2.status_code == 403:
            ok("Delete as client -> 403")
        else:
            fail("Delete as client", f"Expected 403, got {r2.status_code}")

    r3 = httpx.delete(f"{BASE}/admin/api-keys/{key_id}",
        headers=auth(admin_token), timeout=10)
    if r3.status_code == 200:
        ok(f"Delete key {key_id} -> 200")
    else:
        fail(f"Delete key {key_id}", f"Expected 200, got {r3.status_code}")
        return

    r4 = httpx.post(f"{BASE}/v1/extract", headers={"X-API-Key": raw_key}, timeout=10)
    if r4.status_code == 401:
        ok("Deleted key auth -> 401")
    else:
        fail("Deleted key auth", f"Expected 401, got {r4.status_code}")

    keys = httpx.get(f"{BASE}/admin/api-keys", headers=auth(admin_token), timeout=10).json()
    if not any(k["id"] == key_id for k in keys):
        ok("Deleted key gone from list")
    else:
        fail("Deleted key still in list")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  API Key Integration Tests")
    print("  (one real user owns N keys -- no phantom users)")
    print("=" * 60)

    try:
        httpx.get(f"{BASE}/health", timeout=5).raise_for_status()
        print(f"\n[OK] Server reachable at {BASE}")
    except Exception as e:
        print(f"\n[FAIL] Server not reachable: {e}")
        sys.exit(1)

    admin_token = login(ADMIN_EMAIL, ADMIN_PASS)
    if not admin_token:
        print("[FAIL] Admin login failed")
        sys.exit(1)
    print("[OK] Admin login OK")

    delete_test_data(admin_token)

    owner1_id = create_user(admin_token, CLIENT1_EMAIL)
    if not owner1_id:
        print("[FAIL] Could not create test client 1")
        sys.exit(1)
    print(f"[OK] Client 1: {owner1_id}")

    owner2_id = create_user(admin_token, CLIENT2_EMAIL)
    if not owner2_id:
        print("[WARN] Could not create test client 2 -- cross-user test skipped")

    # ── run groups ──────────────────────────────────────────────────────────
    created = test_create(admin_token, owner1_id)
    raw_key = created["raw_key"] if created else None

    test_create_requires_owner(admin_token)
    test_create_nonexistent_owner(admin_token)
    test_duplicate_label_same_user(admin_token, owner1_id)
    test_duplicate_label_case_insensitive(admin_token, owner1_id)
    if owner2_id:
        test_same_label_different_users(admin_token, owner1_id, owner2_id)
    test_create_label_validation(admin_token, owner1_id)
    test_create_requires_admin(admin_token, owner1_id)
    test_create_no_auth(owner1_id)

    test_expiry_options(admin_token, owner1_id)

    keys, key_id = test_list(admin_token), None
    # re-fetch key_id from list (test_list returns key_id directly)
    _listed = httpx.get(f"{BASE}/admin/api-keys", headers=auth(admin_token), timeout=10).json()
    _k = next((k for k in _listed if k.get("label") == "test_primary_key"), None)
    key_id = _k["id"] if _k else None

    if key_id and raw_key:
        test_reveal(admin_token, key_id, raw_key)

    if key_id and raw_key:
        test_deactivate_reactivate(admin_token, key_id, raw_key)

    if raw_key:
        test_auth(admin_token, raw_key)
        test_extract_edge_cases(raw_key)

    test_multiple_keys(admin_token, owner1_id)

    # fresh key for hash test (original may have been deactivated/deleted above)
    fresh = httpx.post(f"{BASE}/admin/api-keys",
        json={"label": "test_hash_key", "owner_user_id": owner1_id},
        headers=auth(admin_token), timeout=10)
    if fresh.status_code == 201:
        test_key_hash(fresh.json()["raw_key"])
    else:
        print(f"\n  [WARN] Skipping hash test (create returned {fresh.status_code})")

    if key_id and raw_key:
        test_delete(admin_token, raw_key, key_id)

    delete_test_data(admin_token)

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"  RESULTS: {passed}/{total} passed,  {failed} failed")
    if _failures:
        print("\n  FAILURES:")
        for f in _failures:
            print(f"    * {f}")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
