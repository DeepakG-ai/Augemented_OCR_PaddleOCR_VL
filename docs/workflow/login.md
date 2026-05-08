# Login Workflow

> Source files:
> - Frontend: [frontend/login.js](../../frontend/login.js)
> - Backend: [backend/main.py](../../backend/main.py) (`/auth/login`, `/auth/me`)
> - Auth helpers: [backend/auth.py](../../backend/auth.py)
>
> See [auth.md](auth.md) for what happens *after* login.

---

## End-to-end sequence

```
Browser                     FastAPI                    Postgres
   │                           │                          │
   │ 1. GET / (no token)       │                          │
   │ ─────────────────────────>│                          │
   │ 2. router() sees no token │                          │
   │    → window.location.hash │                          │
   │      = '#/login'          │                          │
   │ 3. renderLoginPage()      │                          │
   │ 4. user types email + pw  │                          │
   │ 5. submit form            │                          │
   │                           │                          │
   │ 6. POST /auth/login       │                          │
   │    {email, password}      │                          │
   │ ─────────────────────────>│                          │
   │                           │ 7. get_user_by_email     │
   │                           │ ─────────────────────────>│
   │                           │ <─ user row or NULL ─────│
   │                           │ 8. verify_password       │
   │                           │    (bcrypt.checkpw)      │
   │                           │ 9. create_access_token   │
   │                           │    (jwt.encode HS256)    │
   │ <──── 200 {token, user} ──│                          │
   │                           │                          │
   │ 10. localStorage.setItem  │                          │
   │     auth_token, auth_user │                          │
   │ 11. window.location.hash  │                          │
   │     = '#/vendors'         │                          │
   │ 12. router() runs again,  │                          │
   │     token present →       │                          │
   │     renderVendorsPage()   │                          │
   │ 13. apiFetch('/vendors')  │                          │
   │     adds Bearer header    │                          │
   │ ─────────────────────────>│                          │
   │                           │ 14. get_current_user     │
   │                           │    decodes JWT, looks up │
   │                           │    user, returns dict    │
   │                           │ 15. list_vendors(user_id)│
   │                           │ ─────────────────────────>│
   │ <──── 200 [vendors...] ───│                          │
```

---

## Frontend: `frontend/login.js` walkthrough

The whole file is ~88 lines. It's responsible for:
1. Rendering the login form HTML.
2. Submitting credentials.
3. Storing the token + user on success.
4. Providing helpers `getAuthToken()`, `getAuthUser()`, `logout()` used by other JS files.

### `renderLoginPage(app)` — lines 3–67

```javascript
async function renderLoginPage(app) {
    app.className = 'app';
    app.innerHTML = `<div ...>...form HTML...</div>`;
```

The form HTML uses inline styles instead of CSS classes from `styles.css`. Why? Because login is the only page rendered when no auth state exists, and we don't want it to depend on later-loaded styles or risk a flash of unstyled content if `styles.css` is delayed.

Three inputs: email, password, submit button. Everything else (themes, navigation, header) is *not* rendered on this page — login deliberately has no chrome.

```javascript
document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;
```

`.trim()` on email but *not* password — passwords can intentionally contain leading/trailing whitespace and we never silently strip them.

```javascript
    btn.disabled = true;
    btn.textContent = 'Signing in…';
```

Visual disable + label change happens immediately on submit, before the network request — prevents double-submit and gives feedback during the bcrypt round-trip (~250ms server-side).

```javascript
    try {
        const res = await fetch(`${API}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password }),
        });
```

Note: this is `fetch`, not the `apiFetch` wrapper. `apiFetch` would inject the Bearer token (which we don't have yet) and would redirect on 401 (which would loop forever — login is allowed to 401). So login bypasses the wrapper and uses raw fetch.

```javascript
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.detail || `Login failed (${res.status})`);
        }
```

`.catch(() => ({}))` defends against the server returning a non-JSON body (network proxy, gateway error). Falls back to a generic message.

```javascript
        const data = await res.json();
        localStorage.setItem('auth_token', data.access_token);
        localStorage.setItem('auth_user', JSON.stringify(data.user));
        window.location.hash = '#/vendors';
```

Token and user are persisted in `localStorage`. On hash change, `core.js`'s `router()` runs again, sees a token, and routes to the vendors page.

**Why `localStorage` and not `sessionStorage` or a cookie?**
- `localStorage` survives tab close — clients don't have to log in on every reload.
- `sessionStorage` would force re-login on tab close, annoying for the workflow.
- Cookies (httpOnly) would be more XSS-resistant but require server-set Set-Cookie semantics, CSRF tokens for mutations, and `withCredentials: true` everywhere. The closed-client deployment doesn't justify that complexity.

The trade-off: `localStorage` is readable by any JS running in the page. An XSS bug would leak the token. Mitigation lives in the CSP (not yet enforced — TODO if exposure changes).

```javascript
    } catch (err) {
        errBox.textContent = err.message || 'Login failed';
        errBox.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sign In';
    }
```

Error path shows the server's `detail` message inline. The `finally` block restores the button regardless of success or failure.

### `getAuthToken()` — lines 69–71

```javascript
function getAuthToken() {
    return localStorage.getItem('auth_token');
}
```

Trivial reader. Used by `apiFetch` and SSE setup.

### `getAuthUser()` — lines 73–79

```javascript
function getAuthUser() {
    try {
        return JSON.parse(localStorage.getItem('auth_user') || 'null');
    } catch (e) {
        return null;
    }
}
```

The `try/catch` covers a corrupt `auth_user` value (manual edit, partial write). Defensive: if the JSON is malformed, we return `null` rather than crashing the page render.

### `logout()` — lines 81–87

```javascript
function logout() {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('auth_user');
    // Full reload clears all in-memory state (loadedFile, activeExtractionId, etc.)
    window.location.replace(window.location.pathname + '#/login');
    window.location.reload();
}
```

**Why a full reload?** The SPA shares dozens of top-level `let` variables across scripts (`loadedFile`, `activeExtractionId`, `reviewFieldLocations`, etc. — see [frontend.md](frontend.md)). A simple `navigate('#/login')` would leave them populated; the next user to log in on the same browser would see the previous user's in-memory state until they navigated to those pages.

`location.replace()` swaps the URL without adding a history entry (so back-button doesn't return to the logged-in app), and `location.reload()` blanks the JS context entirely.

---

## Backend: `POST /auth/login` (in `backend/main.py`)

```python
@app.post("/auth/login")
async def auth_login(payload: LoginRequest, request: Request):
    pool = request.app.state.pool
    user = await db_mod.get_user_by_email(pool, payload.email)

    if not user or not user.get("is_active", True):
        raise HTTPException(401, "Invalid email or password")

    if not auth.verify_password(payload.password, user["hashed_pw"]):
        raise HTTPException(401, "Invalid email or password")

    token = auth.create_access_token(
        user_id=user["id"], role=user["role"], email=user["email"]
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user["id"], "email": user["email"], "role": user["role"]},
    }
```

**Constant-error-message principle**: missing user, inactive user, and wrong password all return the same string. This prevents email enumeration — an attacker can't tell from the response whether `unknown@example.com` exists or is just blocked.

**No timing attack mitigation**: `verify_password` is only called when the user exists, so the response time differs slightly between "user not found" and "user found but wrong password". For a closed system with 5 known users this is acceptable. If exposure changes, add a dummy `verify_password` call in the not-found branch to equalize timing.

**`is_active` check**: an admin who calls `DELETE /admin/users/{id}` (which sets `is_active=False`) immediately blocks future logins. Existing tokens issued to the now-inactive user also fail because `get_current_user` re-checks `is_active` on every request (see [auth.md](auth.md)).

**Returned user object**: only `id`, `email`, `role`. No password hash, no metadata. The frontend stores this in `auth_user` for header display ("admin@example.com · ADMIN").

---

## Backend: `GET /auth/me` (in `backend/main.py`)

```python
@app.get("/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    return user
```

Trivial. Returns whatever `get_current_user` returned — used by frontend to verify a token is still valid (e.g. on app boot, before showing the cached user info).

---

## Bootstrap admin (in `backend/main.py` lifespan)

On API startup:

```python
admin_email = os.getenv("ADMIN_EMAIL")
admin_pw    = os.getenv("ADMIN_PASSWORD")
if admin_email and admin_pw:
    existing = await db_mod.get_user_by_email(pool, admin_email)
    if not existing:
        await db_mod.create_user(
            pool, admin_email, auth.hash_password(admin_pw), role="admin",
        )
        logger.info("Bootstrap admin created: %s", admin_email)
```

Idempotent: only creates the admin if no user with that email exists. On redeploy or restart, this is a no-op. If you want to *change* the admin password, you must do it via DB or future admin endpoint — re-running with new env vars won't update an existing row.

**Why this exists**: there's no other path to create the first user. Bootstrapping via env vars means a fresh deployment always has at least one admin who can then create clients via `POST /admin/users`.

---

## Cross-cutting: how every other request uses the token

Every protected page in the app loads data via `apiFetch` from `core.js`:

```javascript
async function apiFetch(path, opts = {}) {
    const token = localStorage.getItem('auth_token');
    if (token) {
        opts.headers = { ...(opts.headers || {}), 'Authorization': `Bearer ${token}` };
    }
    const res = await fetch(`${API}${path}`, opts);
    if (res.status === 401) {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('auth_user');
        if (window.location.hash !== '#/login') window.location.hash = '#/login';
        throw new Error('HTTP 401: not authenticated');
    }
    if (!res.ok) { const b = await res.text(); throw new Error(`HTTP ${res.status}: ${b}`); }
    return res;
}
```

Three concerns in nine lines:
1. **Inject** the token as `Authorization: Bearer <token>` if present.
2. **Detect 401** (token missing/expired/user disabled). Clear storage and bounce to login.
3. **Throw on any other non-2xx** so callers can `try/catch`.

The 401 path is critical: an expired token would otherwise produce broken UI states. Centralising the redirect here means every call site is automatically resilient.

---

## Common login failure modes

| Symptom | Likely cause |
|---|---|
| 401 with `"Invalid email or password"` | Wrong credentials, OR `is_active=False`, OR no user with that email |
| 500 with `"SECRET_KEY is not configured"` | Env var missing on the API container |
| Login succeeds but `/vendors` returns 401 | Token cached but server's `SECRET_KEY` rotated; user must re-login |
| Login succeeds, redirects, but no vendors visible | Client has 0 vendors; or all their vendors have `user_id=NULL` (legacy data needs backfill) |
| Form submit does nothing | JS error; check browser console (often `API` is undefined → check `meta[name="api-base"]` in `index.html`) |
| Logout button doesn't appear | `auth_user` in localStorage is corrupt → `JSON.parse` returned null → header skips the user block |

---

## What this workflow does NOT do

- **No "remember me" checkbox.** The 8-hour TTL is the only session length.
- **No password complexity enforcement on login** (only on creation, server-side).
- **No 2FA / OTP.** Not in scope for the closed-client deployment.
- **No SSO.** Explicit choice — see the original plan in `unified-kindling-wren`: "no SSO, no Clerk, no OAuth — JWT + bcrypt, in-house".
- **No social login.** Same reason.
- **No password reset flow.** Admin-mediated only.
