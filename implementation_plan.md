# Production Hardening: Corrected Implementation Plan

Two-wave deployment strategy. Wave 1 contains high-confidence, safe-to-ship fixes for go-live. Wave 2 contains deeper structural work requiring schema changes and coordinated deployments.

---

## User Review Required

> [!IMPORTANT]
> **Wave 1 is self-contained.** Every fix is designed to be correct on its own without requiring any other Wave 1 fix to be deployed simultaneously. No database schema migrations. No breaking API changes.

> [!WARNING]
> **Wave 2 is NOT included in this plan.** It requires schema changes (`quota_released` flag, reservation identity table, `stale_at` timestamp) and coordinated agent+server deployments. Those should be designed, reviewed, and deployed separately after go-live stabilization.

> [!CAUTION]
> **`reserve_quota` is NOT being restructured.** The previous plan's optimization (moving the `COUNT(DISTINCT ...)` scan outside the transaction) was correctly identified as unsafe — it breaks serialization guarantees. The only change to `reserve_quota` is the one-character M1 grace inequality fix.

---

## Wave 1: Safe Blockers (9 Fixes)

---

### Fix 1 — Remove Startup Limit Zeroing (C1)

**File:** [db.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/db.py#L574-L578)
**Risk:** Critical — silently resets legitimate 1000-page limits on every Docker restart.

**Change:** Delete lines 574–578 entirely.

```diff
-        # Retroactively reset clients who got the legacy 1000 default limit back to 0
-        await conn.execute("""
-            UPDATE users SET subscription_limit = 0
-            WHERE role = 'client' AND subscription_limit = 1000;
-        """)
```

**Rationale:** This migration was a one-time legacy cleanup that should have been run once and removed. It now silently destroys any admin-set limit of exactly 1000 on every server restart.

**Side effects:** None. The column default is already `0` (set by the preceding `ALTER TABLE` migration at line 568–570). New clients still get `0`.

---

### Fix 2 — Grace Limit Inequality (M1)

**File:** [db.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/db.py#L3390)
**Risk:** Low — edge case at exactly the limit boundary.

**Change:** Line 3390 — change `<` to `<=`:

```diff
-            elif committed < effective_limit and incoming_pages <= grace_pages:
+            elif committed <= effective_limit and incoming_pages <= grace_pages:
```

**Rationale:** When `committed == effective_limit` exactly, a small document (≤ `grace_pages`) should be allowed through the grace path. The current `<` blocks it.

---

### Fix 3 — Atomic Top-Up Approval Transaction (H3)

**File:** [db.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/db.py#L4591-L4612) — new function
**File:** [main.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L1345-L1382) — rewrite endpoint

**Risk:** High — current code does `add_topup` then `resolve_topup_request` as two separate pool operations. If `add_topup` succeeds but `resolve_topup_request` fails (or vice versa under the old plan's reversed ordering), the system enters an inconsistent state. The CAS guard (`WHERE status = 'pending'`) also makes rollback impossible once the status is changed.

#### 3a. New function in `db.py` — `approve_topup_atomically`

Add after [resolve_topup_request](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/db.py#L4591):

```python
async def approve_topup_atomically(
    pool: asyncpg.Pool,
    request_id: int,
    resolved_by: str,
    resolution_note: str | None = None,
) -> dict:
    """Approve a pending top-up request and apply pages in a single transaction.

    Uses SELECT ... FOR UPDATE on the topup_request row to serialize concurrent
    admin approvals. All-or-nothing: either both the status update AND the
    topup insertion succeed, or neither does.

    Returns: {"request": {...}, "topup": {...}}
    Raises:
        ValueError: if request not found, not pending, or no active subscription.
    """
    admin_uid = _uuid_or_none(resolved_by)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Lock the request row — serializes concurrent approvals
            req = await conn.fetchrow(
                "SELECT * FROM topup_requests WHERE id = $1 FOR UPDATE",
                request_id,
            )
            if not req:
                raise ValueError("not_found")
            if req["status"] != "pending":
                raise ValueError(f"already_{req['status']}")

            user_id = req["user_id"]
            pages = req["requested_pages"]

            # 2. Expire stale subscriptions for this user (same pattern as reserve_quota)
            await conn.execute(
                """
                WITH expired AS (
                    UPDATE subscriptions
                       SET status = 'expired'
                     WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
                     RETURNING user_id
                )
                UPDATE users
                   SET subscription_limit = 0
                 WHERE id IN (SELECT user_id FROM expired)
                """,
                user_id,
            )

            # 3. Verify active subscription exists
            sub = await conn.fetchrow(
                "SELECT id FROM subscriptions WHERE user_id = $1 AND status = 'active' LIMIT 1",
                user_id,
            )
            if not sub:
                raise ValueError("no_active_subscription")

            # 4. Insert the topup row
            topup_row = await conn.fetchrow(
                """
                INSERT INTO topups (user_id, subscription_id, pages, note, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                user_id, sub["id"], pages,
                f"Approved top-up request #{request_id}"
                + (f": {resolution_note}" if resolution_note else ""),
                admin_uid,
            )

            # 5. Mark request as approved
            resolved_row = await conn.fetchrow(
                """
                UPDATE topup_requests
                   SET status = 'approved',
                       resolution_note = $1,
                       resolved_by = $2,
                       resolved_at = NOW()
                 WHERE id = $3
                 RETURNING *
                """,
                resolution_note, admin_uid, request_id,
            )

    return {
        "request": _row_topup_request(resolved_row),
        "topup": dict(topup_row),
    }
```

#### 3b. Rewrite `admin_approve_topup_request` in `main.py`

Replace lines 1345–1382:

```python
@app.post("/admin/topup-requests/{request_id}/approve", status_code=200)
@limiter.limit(f"{RATE_LIMIT}/minute")
async def admin_approve_topup_request(
    request: Request,
    request_id: int,
    body: TopupRequestResolve,
    user: dict = Depends(require_admin),
):
    """Admin: approve a pending top-up request and automatically apply the top-up."""
    pool = request.app.state.pool
    try:
        result = await db_mod.approve_topup_atomically(
            pool,
            request_id=request_id,
            resolved_by=user["id"],
            resolution_note=body.resolution_note,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "not_found":
            raise HTTPException(404, detail="Top-up request not found")
        if msg == "no_active_subscription":
            raise HTTPException(
                409,
                detail="User has no active subscription. Create a subscription period before approving.",
            )
        if msg.startswith("already_"):
            raise HTTPException(409, detail=f"Request is already {msg.replace('already_', '')}")
        raise HTTPException(400, detail=msg)

    logger.info(
        "Admin %s approved topup request #%d (%d pages) [atomic]",
        user["id"], request_id, result["request"]["requested_pages"],
    )
    return result
```

**Why this is correct:** One `conn.transaction()` block. The `SELECT ... FOR UPDATE` on the request row serializes concurrent admin clicks. If the subscription expired between the admin clicking "approve" and the transaction running, the expiry CTE fires first and the `sub` check returns `None`, raising `ValueError("no_active_subscription")` — the entire transaction rolls back, the request stays `pending`, and the admin gets a clear error message. No orphaned states possible.

---

### Fix 4 — Resume Endpoint: Correct Operation Ordering (H6)

**File:** [main.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L2900-L2982)
**Risk:** High — current code uses non-atomic `get_user_billable_pages`, and the inflight-jobs check happens after reservation, leaking reserved pages on 409.

**Change:** Rewrite `queue_resume_extraction` with this ordering:
1. Check inflight jobs **first** (no reservation yet)
2. `reserve_quota` only after inflight check passes
3. Wrap `enqueue_job` + `set_extraction_status` in try/except to release on failure

```python
@app.post("/jobs/extractions/{extraction_id}/resume", response_model=ExtractionJobStartOut)
@limiter.limit("10/minute")
async def queue_resume_extraction(
    request: Request, extraction_id: int, user: dict = Depends(get_current_user),
):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        raise HTTPException(404, detail="Extraction not found")
    if extraction["status"] not in ("partial", "cancelled", "failed", "cancelling"):
        raise HTTPException(400, detail=f"Cannot resume extraction with status '{extraction['status']}'")

    # 1. Check inflight jobs FIRST — before any reservation
    jobs = await db_mod.list_jobs_for_extraction(pool, extraction_id)
    inflight_jobs = [job for job in jobs if job["status"] in ("queued", "running", "cancelling")]
    if inflight_jobs:
        raise HTTPException(409, detail="Cannot resume while prior jobs are still draining")

    pages = await db_mod.get_pages(pool, extraction_id)
    if not pages:
        raise HTTPException(400, detail="No rendered pages available for this extraction")

    existing_page_results = extraction.get("page_results") or []
    completed_page_nums = {pr.get("_page") for pr in existing_page_results
                           if "_error" not in pr and pr.get("_page") is not None}
    all_page_nums = {p["page_number"] for p in pages}
    missing_pages = sorted(all_page_nums - completed_page_nums)

    incoming_pages = len(missing_pages)
    if incoming_pages <= 0:
        raise HTTPException(400, detail="All pages are already successfully extracted. Nothing to resume.")

    start_from = missing_pages[0]

    # 2. Reserve quota AFTER inflight check
    quota_reserved = False
    if user.get("role") != "admin":
        try:
            quota = await db_mod.reserve_quota(pool, user["id"], incoming_pages)
        except Exception as usage_exc:
            logger.error("Subscription quota check failed — blocking resume: %s", usage_exc)
            raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please retry.")
        if not quota["allowed"]:
            _user_record = await db_mod.get_user_by_id(pool, user["id"])
            page_logger.log_limit_alert(
                user_id=user["id"],
                email=(_user_record or {}).get("email"),
                total_extracted_pages=quota["used"],
                subscription_limit=quota["limit"],
                alert_type="exceeded",
                filename=extraction.get("filename"),
            )
            overage = max(quota["used"] - quota["limit"], 0)
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "QUOTA_EXCEEDED",
                    "message": (
                        f"Resuming this document ({incoming_pages} pages) would exceed your "
                        f"subscription limit of {quota['limit']} pages. "
                        f"You have {quota['remaining']} pages remaining. "
                        "Contact your administrator to increase your limit."
                    ),
                    "subscription_limit": quota["limit"],
                    "total_extracted_pages": quota["used"],
                    "remaining": quota["remaining"],
                    "overage": overage,
                },
            )
        quota_reserved = True

    # 3. Enqueue job — release reservation on failure
    try:
        await db_mod.set_cancel_requested(pool, extraction_id, False)
        job = await db_mod.enqueue_job(
            pool,
            extraction_id=extraction_id,
            document_id=extraction.get("document_id"),
            job_type="llm",
            payload={
                "extraction_id": extraction_id,
                "start_from_page": start_from,
                "existing_page_results": existing_page_results,
                "reserved_pages": incoming_pages,
            },
        )
        if job is None:
            raise HTTPException(409, detail="A resume job is already queued or running for this extraction")
        await db_mod.set_extraction_status(
            pool,
            extraction_id,
            "queued",
            progress={"stage": "resume", "message": f"Queued resume from page {start_from}"},
        )
    except Exception:
        # Release reserved pages if job submission or status update fails
        if quota_reserved:
            try:
                await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
            except Exception:
                pass
        raise

    return ExtractionJobStartOut(job_id=job["id"], extraction_id=extraction_id, status=job["status"])
```

**Key differences from the broken plan:**
- Inflight check **before** reservation → no leaked pages on 409
- `quota_reserved` guard → `release_quota_reservation` only called if reservation actually happened
- Bare `raise` in except block → preserves original traceback
- No `user["email"]` assumption → reads from DB with `get_user_by_id` (safe for API-key auth paths where JWT may not carry email)

---

### Fix 5 — Idempotency Cleanup Guards (H2)

**File:** [main.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L4411-L4551)
**Risk:** High — if `count_pdf_pages` throws before `incoming_pages` is assigned, the except block references an undefined variable.

**Change:** At the top of the `/v1/extract` route's `if extraction_id is None:` block (~line 4411), initialize:

```python
        incoming_pages = 0
        quota_reserved = False
```

Then after `reserve_quota` succeeds (~line 4434), set:

```python
                quota_reserved = True
```

Then change the except block (line 4545–4551) to:

```python
        except Exception:
            if user.get("role") != "admin" and quota_reserved:
                try:
                    await db_mod.release_quota_reservation(pool, user["id"], incoming_pages)
                except Exception:
                    pass
            if claim and claim.get("claim_id") and not _job_submitted:
                try:
                    await db_mod.delete_idempotency_claim(pool, user["id"], idempotency_key)
                except Exception as del_exc:
                    logger.warning("Failed to delete orphaned idempotency claim: %s", del_exc)
            raise
```

**Key fixes:**
- `incoming_pages = 0` at top prevents `NameError` if `count_pdf_pages` throws
- `quota_reserved` flag prevents releasing quota that was never reserved (e.g., admin users, or errors before the `reserve_quota` call)
- Idempotency claim cleanup prevents permanent key poisoning
- Bare `raise` (not `raise exc`) preserves the original traceback

---

### Fix 6 — Stale Scheduler Unlock (H5)

**File:** [db.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/db.py#L4164-L4173)
**Risk:** High — if the desktop agent crashes after calling `/running` but before `/ran`, the `is_executing` flag stays `TRUE` forever, permanently blocking manual uploads.

**Change:** Modify `get_user_is_executing` to auto-clear stale locks:

```python
async def get_user_is_executing(pool: asyncpg.Pool, user_id: str) -> bool:
    uid = _uuid_or_none(user_id)
    if uid is None:
        return False
    async with pool.acquire() as conn:
        # Auto-clear stale execution locks older than 10 minutes
        await conn.execute(
            """
            UPDATE user_schedules
               SET is_executing = FALSE
             WHERE user_id = $1
               AND is_executing = TRUE
               AND updated_at < NOW() - INTERVAL '10 minutes'
            """,
            uid,
        )
        row = await conn.fetchrow(
            "SELECT EXISTS(SELECT 1 FROM user_schedules WHERE user_id = $1 AND is_executing = TRUE) AS running",
            uid,
        )
    return bool(row["running"]) if row else False
```

**Rationale:** No schema change needed — `updated_at` is already maintained by `set_schedule_executing`. The 10-minute window is generous (typical schedule runs complete in 1–3 minutes). If the agent genuinely takes longer than 10 minutes, it will re-acquire the lock on its next `/running` call.

---

### Fix 7 — Spatial Memory `po_per_page` Flattening Fix (M3)

**File:** [spatial_memory.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/spatial_memory.py#L209-L224)
**Risk:** Medium — `dict.update()` silently overwrites same-named fields from different pages.

**Change:** Replace lines 209–224 with tuple-based processing:

```python
    # Handle list-format field_locations (po_per_page) — use tuples to preserve
    # duplicate field keys across pages (e.g. "po_number" on page 1 and page 2).
    items_to_process: list[tuple[str, Any]] = []
    if isinstance(field_locations, list):
        for fl in field_locations:
            if isinstance(fl, dict):
                for k, v in fl.items():
                    items_to_process.append((k, v))
    elif isinstance(field_locations, dict):
        items_to_process = list(field_locations.items())
    else:
        return 0

    if not items_to_process:
        return 0

    configured_header_fields = await _load_configured_header_fields(pool, extraction, vendor_id)
    logger.info("Spatial memory save: layout=%s, %d locations submitted", lk, len(items_to_process))
    saved = 0
    for field_key, loc in items_to_process:
```

The rest of the loop body (lines 225 onward: `_is_reusable_header_field`, box validation, `upsert_spatial_memory`) remains unchanged — it already operates on `(field_key, loc)` pairs.

---

### Fix 8 — LLM Usage Recording on Parse Failure (M7)

**File:** [extractor.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/extractor.py#L524-L530)
**Risk:** Medium — tokens consumed but not billed when the LLM returns unparseable JSON.

**Change:** Add `await _record_usage_if_needed()` before the final `raise ValueError` at line 530:

```diff
         # All recovery attempts failed
+        await _record_usage_if_needed()
+
         plog.error(
             f"page {page_num}/{total_pages} JSON parse failed (all fallbacks exhausted)",
             logger=__name__,
             raw=raw[:200],
         )
         raise ValueError(f"LLM returned invalid JSON: {raw[:200]}")
```

**Rationale:** The LLM consumed tokens regardless of whether we can parse the output. This call is already wrapped in its own try/except (lines 465–474) with a recovery buffer, so it cannot crash the caller.

---

### Fix 9 — Settings.js API Base URL (M8)

**File:** [settings.js](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/frontend/settings.js#L59)
**Risk:** Medium — SSE stream fails silently in split-deployment (frontend on CDN, API on separate host).

**Change:** Line 59:

```diff
-                const resp = await fetch('/api/config/stream', {
+                const resp = await fetch(`${API || ''}/api/config/stream`, {
```

**Rationale:** All other `fetch()` calls in the frontend use the `API` constant. This is the only one hardcoded to a root-relative path.

---

### Fix 10 — Upload-Preview `max_pages` Bounds (H4)

**File:** [main.py](file:///c:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL/backend/main.py#L3147-L3159)
**Risk:** Medium — unbounded `max_pages` allows memory/CPU exhaustion rendering hundreds of scanned pages.

**Change:** Add validation after line 3157 (`_require_pdf`), before line 3159:

```python
    max_pages = max(1, min(max_pages, 50))
```

**Rationale:** Silently clamping is safer than returning 400 for this preview-only endpoint. Users sending `max_pages=0` get `1`, users sending `max_pages=999` get `50`. No workflow breaks.

---

## Deferred to Wave 2 (Post-Launch)

The following issues are **real bugs** but require schema migrations, coordinated deployments, or deeper refactoring that should not be rushed into a go-live:

| ID | Issue | Why Deferred |
|----|-------|--------------|
| H1 | Worker `pending_pages` release on partial/failed | Without a `quota_released` boolean on the extraction row, double-release from concurrent jobs is possible. Needs schema migration. |
| H8 | `reserve_quota` lock duration optimization | Moving the `COUNT(DISTINCT ...)` outside the lock is unsafe without a pre-aggregated counter table. Needs schema design. |
| H5+ | Agent crash → permanent `is_executing` lock | Fix 6 (stale unlock) handles this server-side. But the agent-side `try/finally` with `/running`+`/ran` calls requires a coordinated agent deployment. |
| M2 | ERP mapping failure propagation | Raising 400 after template is already saved creates half-saved state. Correct fix is either transactional template+mapping save, or returning a `warning` field in the 200 response. Both need design review. |
| M9 | Legacy `subscription_limit` endpoint sync | Writing to both `users.subscription_limit` and `subscriptions.page_limit` silently does nothing if no active subscription exists. Better to return HTTP 409 forcing the admin to use the subscription-period endpoint. Needs UI changes. |
| M10 | JWT auth centralization | Correct but large refactor. Risk of breaking existing auth flows if done under time pressure. |
| H7 | Bcrypt `asyncio.to_thread` | Correct but requires updating every `hash_password`/`verify_password` call site. Test thoroughly before go-live. |
| M4 | Gold correction `doc_i_` prefix stripping | Needs verification that no downstream code depends on prefixed keys. |
| M5 | 3-schedule cap enforcement | Low risk, but needs UI error handling for the 400 response. |
| M6 | Idempotency key race `UniqueViolationError` | Edge case under concurrent expired-key submissions. Low probability. |
| M11 | Redundant `get_user_by_id` calls for alerts | Performance optimization, not a correctness issue. |

---

## Verification Plan

### Automated Tests

Run the existing test suite to verify no regressions:

```powershell
.venv\Scripts\activate
pytest tests/test_page_limits.py tests/test_subscriptions.py tests/test_topup_requests.py tests/test_scheduler.py tests/test_scheduler_edge_cases.py -v
```

### Manual Verification

| Fix | Test |
|-----|------|
| **C1** | Set a test client's `subscription_limit` to exactly `1000` in the DB. Restart the server. Verify limit remains `1000`. |
| **M1** | Create a user with exactly `500/500` used pages. Upload a 5-page document (≤ `grace_pages=10`). Verify it's allowed with `reason='grace'`. |
| **H3** | Open two browser tabs on the admin panel. Click "Approve" on the same top-up request simultaneously. Verify only one succeeds; the other gets HTTP 409. |
| **H6** | Resume a partial extraction. Verify `pending_pages` is correctly reserved. Kill the server before the job completes. Restart. Verify no permanent page leak. |
| **H2** | Send a `/v1/extract` request with an idempotency key and a corrupt PDF (causes `count_pdf_pages` to throw). Verify the idempotency key is cleaned up and can be reused. |
| **H5** | Set `is_executing = TRUE` and `updated_at = NOW() - INTERVAL '15 minutes'` on a schedule row. Attempt a manual upload. Verify it succeeds (stale lock auto-cleared). |
| **M3** | Correct a `po_per_page` extraction where the same field (e.g., `po_number`) appears on pages 1 and 3. Verify both spatial memory entries are saved (not just page 3). |
| **M7** | Trigger an LLM extraction where the model returns invalid JSON. Verify the `llm_usage` row is created with correct token counts despite the parse failure. |
| **M8** | Serve frontend on port 3000, API on port 8000 with `API='http://localhost:8000'`. Open Settings. Verify SSE stream connects (status shows "Connected"). |
| **H4** | `POST /upload-preview` with `max_pages=0`. Verify it renders 1 page (clamped). With `max_pages=999`, verify it renders 50. |

### What Is NOT Covered

> [!WARNING]
> **Worker quota release (H1)** is not fixed in Wave 1. This means `pending_pages` can still leak on `partial`/`failed` extractions until the worker-side fix with a `quota_released` guard is deployed in Wave 2. **Mitigation:** Monitor `pending_pages > 0` on users with no `queued`/`running` jobs and reset manually via admin tools if needed.
