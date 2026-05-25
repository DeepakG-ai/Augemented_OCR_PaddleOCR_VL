# auth.py — JWT + Ownership Assertions

> Source file: [backend/auth.py](../../backend/auth.py)

The auth module is split into 4 concerns: **password**, **token**, **dependency** (`get_current_user`), **ownership assertions**. Read each section in order — later sections build on earlier.

---

## Module-level config (lines 28–38)

```python
SECRET_KEY = os.getenv("SECRET_KEY", "")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 8
```

- **`SECRET_KEY`**: Loaded from env at import time. If unset, *import* still succeeds (so unrelated tests don't blow up), but the first attempt to sign or verify a token raises HTTP 500. This deferred-failure pattern is intentional — see `_require_secret()` below.
- **`HS256`**: Symmetric HMAC. Single key signs and verifies. Fine for a closed system; would need RS256 (asymmetric) if tokens were verified by a separate service.
- **`TOKEN_TTL_HOURS = 8`**: One workday. No refresh token; clients log in each morning.

```python
_bearer = HTTPBearer(auto_error=False)
```

`auto_error=False` is critical: by default, FastAPI's `HTTPBearer` raises 403 if the header is missing. We want to handle the missing-header case ourselves so we can also accept `?token=...` (for SSE — see `get_current_user` below).

---

## Password hashing (lines 41–60)

```python
_BCRYPT_MAX_BYTES = 72

def _to_bcrypt_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
```

**Why truncate to 72 bytes?** bcrypt's input is fixed at 72 bytes — anything beyond that is silently ignored by the algorithm. If you pass a 100-character password to `bcrypt.hashpw`, it hashes only the first 72. Different libraries handle this differently (some raise, some truncate, some pre-hash with SHA). We truncate explicitly so:
1. Callers see the same byte limit on hashing and verification.
2. We never get a "password too long" runtime error from a stricter bcrypt build.

```python
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_bytes(plain), bcrypt.gensalt()).decode("utf-8")
```

`bcrypt.gensalt()` defaults to a work factor of 12 (~250ms on commodity hardware in 2026). Returns a `$2b$...` string that contains the salt + hash + cost factor — self-describing, no separate salt column needed.

```python
def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False
```

The blanket `except` catches malformed hashes (corrupted DB rows, manual edits). Returning `False` instead of raising keeps the login endpoint's response surface boring — wrong password and corrupt hash both produce the same 401 from the caller.

---

## Token signing & decoding (lines 65–95)

```python
def _require_secret() -> str:
    secret = os.getenv("SECRET_KEY", SECRET_KEY)
    if not secret:
        raise HTTPException(500, "SECRET_KEY is not configured on the server")
    return secret
```

Re-reads `SECRET_KEY` from the environment on each call. This means:
- A test that monkeypatches `os.environ["SECRET_KEY"]` after import still works.
- If the env var is missing at runtime (misconfigured deployment), the error surfaces as a clear 500 on the first auth attempt — not a cryptic JWT signature error.

```python
def create_access_token(user_id: str, role: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),         # JWT standard claim — subject
        "role": role,                # custom claim — used by require_admin
        "email": email,              # custom claim — used in audit logs / UI
        "iat": int(now.timestamp()), # issued at (epoch seconds)
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),  # expiry
    }
    return jwt.encode(payload, _require_secret(), algorithm=JWT_ALGORITHM)
```

**Note**: the entire payload is encoded as the JWT body — base64-encoded, **not encrypted**. Anyone who captures the token can read role and email. The signature only proves *we* issued it. So:
- Don't put secrets in the token.
- Treat the token like a session cookie: HTTPS-only in production.

```python
def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _require_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(401, f"Invalid or expired token: {exc}",
                            headers={"WWW-Authenticate": "Bearer"})
```

`jwt.decode` with `algorithms=[JWT_ALGORITHM]` is mandatory — passing `algorithms=None` allows `alg=none` tokens, a classic JWT bypass. Always specify.

The `WWW-Authenticate: Bearer` header on the 401 is HTTP-standard and tells well-behaved clients that this endpoint expects a Bearer token. Browsers won't act on it (no auto-prompt for Bearer), but it's the right thing for API contracts.

---

## The big dependency: `get_current_user` (lines 100–135)

```python
async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None, description="Token for SSE/EventSource clients"),
) -> dict:
```

This is the single function that gates every protected route. It resolves a token from one of two sources:

```python
raw_token: str | None = None
if credentials and credentials.scheme.lower() == "bearer":
    raw_token = credentials.credentials  # standard "Authorization: Bearer xxx"
elif token:
    raw_token = token                    # ?token=xxx (for SSE clients)
```

**Why the dual source?** Server-Sent Events use `EventSource`, which cannot attach custom headers. The only way to authenticate an `EventSource` connection is via query string. Bearer header is preferred everywhere else; `?token=` is the fallback for streaming.

```python
if not raw_token:
    raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
```

No token at all → 401 immediately. The frontend's `apiFetch` listens for 401 and redirects to `/login`.

```python
payload = decode_token(raw_token)
user_id = payload.get("sub")
role = payload.get("role")
email = payload.get("email")
if not user_id or not role:
    raise HTTPException(401, "Malformed token")
```

Even a valid signature can carry a malformed payload (older token format, manual tampering before signing was possible). `sub` and `role` are the only mandatory claims for our authorization logic.

```python
pool = getattr(request.app.state, "pool", None)
if pool is not None:
    record = await db_mod.get_user_by_id(pool, user_id)
    if not record or not record.get("is_active", True):
        raise HTTPException(401, "User disabled or missing")
```

**The crucial DB check.** Without it, a token issued before the user was deactivated would still work for the full 8 hours. By looking up `is_active` on every request, an admin's `DELETE /admin/users/{id}` (which sets `is_active=False`) takes effect immediately.

The `pool is None` guard is for tests: unit tests can stub `request.app.state` without a real pool, and the dependency gracefully skips the DB step. Production always has a pool.

```python
return {"id": str(user_id), "role": role, "email": email}
```

The returned dict is what every protected route handler receives as its `user` parameter. It's intentionally a thin dict, not a SQLAlchemy model — keeps the dependency framework-agnostic and easy to mock in tests.

---

## `require_admin` (lines 138–141)

```python
async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return user
```

A trivial wrapper. Composed dependency: `require_admin` depends on `get_current_user`, which depends on `_bearer` and `Query`. FastAPI resolves the chain and injects only the final result. Routes use:

```python
@app.get("/admin/users")
async def list_users(user: dict = Depends(require_admin)):
    ...
```

Note: this is **403** (forbidden) not 401 (unauthenticated). The user *is* authenticated — they just don't have the right role.

---

## The four ownership assertions (lines 146–192)

These are the heart of multi-tenant isolation. All four follow the same pattern:

1. Admin shortcut → `return` immediately (no DB query).
2. Resolve the resource ID through one or more chained DB lookups to find the owning user.
3. Compare to `user["id"]` — match passes silently, mismatch raises 403.
4. Missing rows raise 404 (closed-system convention; see [isolation.md](isolation.md)).

### `assert_vendor_access` (the leaf)

```python
async def assert_vendor_access(pool, vendor_id: str, user: dict) -> None:
    if _is_admin(user):
        return
    owner = await db_mod.get_vendor_owner(pool, vendor_id)
    if owner is None:
        raise HTTPException(404, "Vendor not found")
    if owner != user["id"]:
        raise HTTPException(403, "Access denied")
```

**`owner is None` covers two cases**:
- Vendor doesn't exist → 404.
- Vendor exists but `user_id IS NULL` (legacy unowned row) → also 404.

The legacy row case is intentional: a vendor with no owner shouldn't be visible to any client. Admin still sees it (the `_is_admin` shortcut runs first).

### `assert_extraction_access`

```python
async def assert_extraction_access(pool, extraction_id: int, user: dict) -> None:
    if _is_admin(user):
        return
    ext = await db_mod.get_extraction(pool, int(extraction_id))
    if not ext:
        raise HTTPException(404, "Extraction not found")
    await assert_vendor_access(pool, ext["vendor_id"], user)
```

Resolve `extraction → vendor_id`, then delegate. Two DB hits for a non-admin client per protected request — acceptable for ~5 clients with low volume. If volume scales, add a denormalised `extractions.user_id` column or a join.

### `assert_job_access`

```python
async def assert_job_access(pool, job_id: int, user: dict) -> None:
    if _is_admin(user):
        return
    job = await db_mod.get_job(pool, int(job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    ext_id = job.get("extraction_id")
    if not ext_id:
        raise HTTPException(403, "Access denied")
    await assert_extraction_access(pool, int(ext_id), user)
```

Resolves `job → extraction → vendor → owner`. Three DB hits worst case. The `not ext_id` branch handles internal jobs not bound to an extraction — those are admin/system-only and clients always get 403.

### `assert_alias_access`

```python
async def assert_alias_access(pool, alias_id: int, user: dict) -> None:
    if _is_admin(user):
        return
    vendor_id = await db_mod.get_alias_vendor_id(pool, int(alias_id))
    if not vendor_id:
        raise HTTPException(404, "Alias not found")
    await assert_vendor_access(pool, vendor_id, user)
```

Used by `DELETE /vendors/aliases/{alias_id}`. Maps the bare alias ID back to a vendor (the alias_id alone doesn't carry the vendor in the URL).

---

## How a typical protected route uses all of this

```python
@app.get("/extractions/{extraction_id}")
async def get_extraction(
    extraction_id: int,
    request: Request,
    user: dict = Depends(get_current_user),    # ← 1. authenticate
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)   # ← 2. authorize
    return await db_mod.get_extraction(pool, extraction_id)     # ← 3. fetch
```

Three concepts, one route. **Authenticate** (who are you?). **Authorize** (can you see this?). **Fetch**. Every protected route follows this template.

---

## What's NOT in auth.py (and why)

- **No password reset.** Closed system, ~5 clients. Admin resets via DB or future admin endpoint.
- **No refresh token.** 8h TTL is enough for a workday; clients log in each morning.
- **No CSRF tokens.** API uses Bearer headers, not cookies, so CSRF doesn't apply (CSRF requires the browser to auto-attach credentials).
- **No rate limiting on `/auth/login`.** The global slowapi limit (`RATE_LIMIT_PER_MINUTE=30`) covers it; with ~5 clients, dedicated login throttling would be over-engineering. Add it if the user base grows.
- **No "remember me" / longer tokens.** Same reason as no refresh — 8h is the deliberate session length.

---

## Common pitfalls to avoid

1. **Forgetting `Depends(get_current_user)` on a new route.** The route will be public. There's no static check; only tests catch it.
2. **Calling DB queries before `assert_*_access`.** A query that fetches data and *then* checks ownership defeats the purpose — the data has already been read. Always assert first, fetch second.
3. **Trusting `vendor_id` from form data.** `POST /vendors/{vendor_id}/template` accepts `vendor_id` from the URL. Without `assert_vendor_access`, a client could write a template into another client's vendor by guessing the ID. The route handler MUST run the assertion before `upsert_template`.
4. **Hardcoding admin checks instead of using `require_admin`.** `if user["role"] == "admin"` scattered throughout routes is fragile. Use the dependency so the admin check is one line and consistent.
