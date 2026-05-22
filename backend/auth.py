"""
auth.py — JWT auth + per-user resource ownership checks.

Simple model: ~5 external clients, each owns 1+ vendors. Every downstream
resource (extractions, jobs, aliases, templates) chains back to a vendor,
so all isolation is enforced via vendor.user_id.

Admin role bypasses every assert.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from . import db as db_mod


# -- Config ----------------------------------------------------------------

SECRET_KEY = os.getenv("SECRET_KEY", "")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 8

if not SECRET_KEY:
    # Allow import-time silence; reject at first signing attempt instead.
    # This keeps tests that don't touch auth from blowing up on import.
    pass


_bearer = HTTPBearer(auto_error=False)


# -- Password ---------------------------------------------------------------

# bcrypt only hashes the first 72 bytes of input; truncate explicitly so
# longer passwords don't produce surprising collisions or backend errors.
_BCRYPT_MAX_BYTES = 72


def _to_bcrypt_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_bytes(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# -- Token ------------------------------------------------------------------

def _require_secret() -> str:
    secret = os.getenv("SECRET_KEY", SECRET_KEY)
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="SECRET_KEY is not configured on the server",
        )
    return secret


def create_access_token(user_id: str, role: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _require_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _require_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# -- API Key generation -----------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key. Returns (raw_key, key_hash, prefix).

    The raw key is shown to the admin ONCE. We store only the SHA-256 hash.
    """
    raw = "po_live_" + secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    prefix = raw[:16] + "..."
    return raw, hashed, prefix


def _fernet():
    from cryptography.fernet import Fernet
    secret = os.getenv("SECRET_KEY", SECRET_KEY)
    if not secret:
        raise RuntimeError("SECRET_KEY not configured — cannot encrypt API key")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_api_key(raw_key: str) -> str:
    return _fernet().encrypt(raw_key.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    return _fernet().decrypt(encrypted.encode()).decode()


# -- FastAPI dependency -----------------------------------------------------

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None, description="Token for SSE/EventSource clients"),
) -> dict:
    """
    Extract user from Bearer header OR ?token= query param (needed for SSE,
    since EventSource cannot set headers).
    """
    raw_token: str | None = None
    if credentials and credentials.scheme.lower() == "bearer":
        raw_token = credentials.credentials
    elif token:
        raw_token = token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(raw_token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Malformed token")

    role = payload.get("role")
    email = payload.get("email")
    pool = getattr(request.app.state, "pool", None)
    if pool is not None:
        record = await db_mod.get_user_by_id(pool, user_id)
        if not record or not record.get("is_active", True):
            raise HTTPException(status_code=401, detail="User disabled or missing")
        role = record.get("role")
        email = record.get("email")

    if not role:
        raise HTTPException(status_code=401, detail="Malformed token")

    return {"id": str(user_id), "role": role, "email": email}


async def get_current_user_or_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None, description="Token for SSE/EventSource clients"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> dict:
    """Dual auth: X-API-Key header OR JWT Bearer token.

    API keys resolve to the owning user, so all downstream vendor-isolation
    logic works unchanged.
    """
    pool = getattr(request.app.state, "pool", None)

    # ── API Key path ──────────────────────────────────────────────────
    if x_api_key:
        if not pool:
            raise HTTPException(status_code=500, detail="Database pool unavailable")

        hashed = hashlib.sha256(x_api_key.encode("utf-8")).hexdigest()
        key_row = await db_mod.verify_api_key_hash(pool, hashed)

        if not key_row or not key_row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid or inactive API key")

        user_id = key_row["user_id"]
        record = await db_mod.get_user_by_id(pool, str(user_id))
        if not record or not record.get("is_active", True):
            raise HTTPException(status_code=401, detail="API key owner disabled")

        # Fire-and-forget: update last_used_at
        asyncio.ensure_future(db_mod.touch_api_key(pool, hashed))

        return {
            "id": str(user_id),
            "role": record.get("role"),
            "email": record.get("email"),
            "auth_method": "api_key",
            "api_key_id": key_row["id"],
        }

    # ── JWT path (fallback) ───────────────────────────────────────────
    raw_token: str | None = None
    if credentials and credentials.scheme.lower() == "bearer":
        raw_token = credentials.credentials
    elif token:
        raw_token = token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(raw_token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Malformed token")

    role = payload.get("role")
    email = payload.get("email")
    if pool is not None:
        record = await db_mod.get_user_by_id(pool, user_id)
        if not record or not record.get("is_active", True):
            raise HTTPException(status_code=401, detail="User disabled or missing")
        role = record.get("role")
        email = record.get("email")

    if not role:
        raise HTTPException(status_code=401, detail="Malformed token")

    return {"id": str(user_id), "role": role, "email": email, "auth_method": "jwt", "api_key_id": None}


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# -- Ownership assertions ---------------------------------------------------

def _is_admin(user: dict | None) -> bool:
    return bool(user) and user.get("role") == "admin"


async def assert_vendor_access(pool, vendor_id: str, user: dict) -> None:
    """Admin bypasses. Client must own the vendor."""
    if _is_admin(user):
        return
    owner = await db_mod.get_vendor_owner(pool, vendor_id)
    if owner is None:
        # Either vendor doesn't exist, or it's an unowned legacy row.
        raise HTTPException(status_code=404, detail="Vendor not found")
    if owner != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")


async def assert_extraction_access(pool, extraction_id: int, user: dict) -> None:
    if _is_admin(user):
        return
    ext = await db_mod.get_extraction(pool, int(extraction_id))
    if not ext:
        raise HTTPException(status_code=404, detail="Extraction not found")

    # Verify the billing_user_id if present in document metadata
    billing_user_id = None
    if ext.get("document_id"):
        doc = await db_mod.get_document(pool, ext["document_id"])
        if doc and doc.get("metadata"):
            billing_user_id = doc["metadata"].get("billing_user_id")

    if billing_user_id:
        if billing_user_id != user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        await assert_vendor_access(pool, ext["vendor_id"], user)


async def assert_job_access(pool, job_id: int, user: dict) -> None:
    if _is_admin(user):
        return
    job = await db_mod.get_job(pool, int(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ext_id = job.get("extraction_id")
    if not ext_id:
        # Job not bound to an extraction (shouldn't happen for client-visible
        # jobs); deny non-admins.
        raise HTTPException(status_code=403, detail="Access denied")
    await assert_extraction_access(pool, int(ext_id), user)


async def assert_alias_access(pool, alias_id: int, user: dict) -> None:
    if _is_admin(user):
        return
    vendor_id = await db_mod.get_alias_vendor_id(pool, int(alias_id))
    if not vendor_id:
        raise HTTPException(status_code=404, detail="Alias not found")
    await assert_vendor_access(pool, vendor_id, user)
