# Multi-Tenant Data Isolation

> **The single most important concept in this codebase.** Every line of auth code, every route guard, every DB filter exists to enforce one rule: a client can only see data belonging to vendors they own.

---

## The model

```
┌─────────────┐        ┌──────────────────┐
│   users     │        │     vendors      │
│             │ 1   N  │                  │
│  id  ───────┼───────>│  user_id (FK)    │
│  email      │        │  id   (PK)       │
│  hashed_pw  │        │  name            │
│  role       │        │  status          │
│  is_active  │        │  created_at      │
└─────────────┘        └──────────────────┘
                              │
                              │  vendor_id is the central scoping unit.
                              │  Every downstream resource either has
                              │  vendor_id directly OR chains to one
                              │  through extractions.
                              ▼
        ┌─────────────────────┴─────────────────────┐
        │                                           │
        ▼                                           ▼
┌──────────────────┐                       ┌─────────────────────┐
│ Vendor-scoped:   │                       │ Extraction-chained: │
│                  │                       │                     │
│ - templates      │                       │ - extractions       │
│ - vendor_aliases │                       │   ├── pages         │
│ - spatial_memory │                       │   ├── jobs          │
│ - gold_examples  │                       │   ├── reviews       │
│ - qwen_layout_   │                       │   ├── llm_usage     │
│   boxes          │                       │   ├── deliveries    │
│                  │                       │   └── field_locs    │
└──────────────────┘                       └─────────────────────┘
```

**Why vendor as the unit?** It was already the central concept — templates, aliases, spatial memory, every extraction was already keyed by `vendor_id`. Adding a single column (`vendors.user_id`) gives us tenant isolation for free without touching the rest of the schema.

---

## Two roles

```python
role = 'admin'    # bypasses every assertion, sees all data
role = 'client'   # restricted to their own vendors
```

A `CHECK (role IN ('admin', 'client'))` constraint enforces the value at the DB level (see [db.md](db.md) — `users_role_check`). No third role exists. No hierarchy.

---

## How isolation is enforced (3 mechanisms)

### Mechanism 1 — Filter by user_id on list routes

```python
# backend/main.py — GET /vendors
user_id = None if user["role"] == "admin" else user["id"]
vendors = await db_mod.list_vendors(pool, user_id=user_id)
```

```sql
-- backend/db.py — list_vendors
SELECT id, name, status, created_at FROM vendors
WHERE ($1::UUID IS NULL OR user_id = $1)
ORDER BY created_at DESC
```

The `($1 IS NULL OR user_id = $1)` pattern means:
- Admin (`user_id = NULL`) → no filter → sees all rows
- Client (`user_id = '<uuid>'`) → filter applied → sees only their rows

Same pattern in `get_all_aliases_for_detection`, list extractions, list templates.

### Mechanism 2 — Assert ownership on individual resource routes

For routes that take an ID (e.g. `GET /extractions/{extraction_id}`), the route handler calls one of the four `assert_*_access` helpers from [auth.py](../../backend/auth.py):

| Helper | Resolves | Final check |
|---|---|---|
| `assert_vendor_access(pool, vendor_id, user)` | Direct: vendor → user_id | Returns owner_id, compares to user["id"] |
| `assert_extraction_access(pool, extraction_id, user)` | extraction → vendor_id → user_id | Delegates to `assert_vendor_access` |
| `assert_job_access(pool, job_id, user)` | job → extraction_id → vendor_id → user_id | Delegates to `assert_extraction_access` |
| `assert_alias_access(pool, alias_id, user)` | alias → vendor_id → user_id | Delegates to `assert_vendor_access` |

Every helper does:
1. **Admin shortcut**: `if user["role"] == "admin": return` — no DB hit.
2. **Resolve the ID chain** to find the owning user.
3. **Compare**: `if owner != user["id"]: raise HTTPException(403)`.
4. **Not-found vs forbidden**: missing rows → 404, present-but-wrong-owner → 403. (Per the closed-system model — the user knows IDs exist; we just deny access.)

### Mechanism 3 — Admin-only routes

```python
@app.get("/admin/users")
async def list_users(user=Depends(require_admin)):
    ...
```

`require_admin` is a thin wrapper around `get_current_user` that raises 403 if `role != 'admin'`.

Used for:
- `/admin/users` — create/list/deactivate users
- `/admin/stats` — system-wide counts
- `/admin/usage` — billing across all clients

---

## Vendor creation: the one tricky path

`POST /vendors` is asymmetric:

```python
@app.post("/vendors")
async def create_vendor(body: VendorCreate, user=Depends(get_current_user)):
    if user["role"] == "admin":
        owner_id = body.user_id      # Admin MUST specify the owner
        if not owner_id:
            raise HTTPException(400, "Admin must specify user_id")
    else:
        owner_id = user["id"]        # Client always owns what they create
        # body.user_id is ignored — clients cannot transfer ownership
    
    await upsert_vendor(pool, body.id, body.name, user_id=owner_id)
```

**Why this asymmetry?**
- A client should never be able to create a vendor for another user.
- But an admin needs to onboard new clients — that means creating a vendor for someone else.

**Upsert semantics:**

```sql
INSERT INTO vendors (id, name, user_id) VALUES ($1, $2, $3)
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
-- user_id is NOT updated on conflict — first creator owns it forever.
```

This is critical: if Client A creates `vendor_id=ACME`, no one (not even an admin via this path) can silently steal ownership by re-running upsert. To transfer, an admin would need a separate explicit endpoint.

The template-save path (`POST /vendors/{vendor_id}/template`) also internally calls `upsert_vendor`. To prevent a client from creating a vendor they don't own through this side door, the route runs `assert_vendor_access(pool, vendor_id, user)` **before** the upsert when the vendor already exists.

---

## Vendor detection (auto-detect during ingest)

Detection is also tenant-scoped — see [vendor_detection.md](vendor_detection.md) for the full algorithm.

```python
# backend/main.py — POST /ingest/ui
detect_uid = None if user["role"] == "admin" else user["id"]
match = await vendor_detector.detect_vendor(pool, page_words, user_id=detect_uid)
```

Defense in depth: even though detection is already scoped, we re-assert after:

```python
await assert_vendor_access(pool, match.vendor_id, user)
```

This guards against any future bug in the scoping query.

---

## SSE auth (the special case)

Server-Sent Events use `EventSource` which **cannot set custom headers**. So we accept the JWT as a query param:

```python
# backend/auth.py — get_current_user
async def get_current_user(
    request: Request,
    credentials = Depends(_bearer),
    token: str | None = Query(None),  # ← from ?token=...
):
    raw_token = credentials.credentials if credentials else token
    ...
```

Frontend:
```javascript
// frontend/extract.js
const token = localStorage.getItem('auth_token');
const es = new EventSource(`${API}/jobs/${jobId}/stream?token=${encodeURIComponent(token)}`);
```

Then `assert_job_access` enforces ownership the same way as a Bearer-header call.

---

## What happens when ownership is missing

### Legacy data (vendors with `user_id = NULL`)
After the migration ran, any pre-existing vendor row has `user_id = NULL`. These rows:
- **Will not appear** in any client's `GET /vendors` (the filter `user_id = $1` excludes NULL).
- **Will return 404** when a client queries them by ID (`get_vendor_owner` returns None → 404 from `assert_vendor_access`).
- **Are visible to admin** (admin bypasses filters).

To migrate: an admin runs `UPDATE vendors SET user_id = '<some-user-id>' WHERE id = '<vendor-id>'`. There is no admin UI for this yet — direct DB or a future admin endpoint.

### Orphan extractions (vendor deleted)
`vendors.id` is referenced by `extractions.vendor_id` without `ON DELETE CASCADE`. Deleting a vendor with active extractions raises a foreign-key violation. The user-facing flow is to either:
- Delete extractions first, or
- Use the `DELETE /vendors/{vendor_id}` endpoint, which does its own cascade in code (see [db.md](db.md) — `delete_vendor_cascade`).

---

## The closed-system assumption

This system is built for **~5 known clients**, not the open internet. That shapes several decisions:

- 8-hour JWT TTL with no refresh token (clients log in once a day).
- `403` instead of `404` for present-but-not-yours resources (the client already knows IDs exist; obfuscating them buys nothing).
- No password reset flow yet (admin manually resets via DB or admin endpoint).
- No rate limiting per user (only the global slowapi limit).

If the system were ever to be exposed to untrusted users, every one of these assumptions would need re-evaluation.

---

## Verification checklist

When auditing isolation, ask of every new route:

- [ ] Does it have `user: dict = Depends(get_current_user)`?
- [ ] If it takes a vendor_id / extraction_id / job_id / alias_id, does it call the matching `assert_*_access` BEFORE doing any work?
- [ ] If it returns a list, does it filter by `user_id` (with admin bypass via `None`)?
- [ ] If it's admin-only, does it use `Depends(require_admin)` instead of `get_current_user`?
- [ ] If it accepts a vendor_id from form data (e.g. `/ingest/ui`), is the assertion run after detection / before processing?
