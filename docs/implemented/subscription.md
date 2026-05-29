# Subscription & Quotas

This document details the design, mathematical calculations, and lifecycle of the user subscription page limits, page reservations, and top-up approval workflows.

---

## What it is

The subscription and quota system restricts how many document pages a client user is allowed to ingest and process. Page limits are defined within a subscription period. If a user runs out of pages or if their subscription expires, further uploads are blocked. Admins can manually grant page top-ups or approve top-up requests submitted by client users.

---

## Key States

- **active**: The subscription period is currently valid, the current date is within the start/end window, and the subscription has not been cancelled.
- **expired**: The subscription period has passed (`period_end < NOW()`). The user's page allowance drops to zero.
- **superseded**: A new subscription was created for the user. Any prior active subscription is marked as superseded.
- **cancelled**: The subscription was manually revoked by an admin before its end date.

---

## How it works

The quota system operates atomically to handle concurrent uploads from client agents or web sessions.

```
          [Document Upload Received]
                      │
                      ▼
             [reserve_quota() Txn]
                      │
        [Update Expired Subscriptions]
                      │
                      ▼
            [Select User FOR UPDATE]
        (Locks concurrent pending_pages)
                      │
                      ▼
         [Calculate Quota Snapshot]
    (base_limit + topups - used - pending)
                      │
         ┌────────────┴────────────┐
         ▼                         ▼
   (Page fits?)               (Over limit?)
         │                         │
         ├─ Yes ──► [Allow]        ├─ Yes (<= 10 pages grace?) ──► [Allow (grace)]
         │           (pending_pages│
         │            increased)   └─ No ────────────────────────► [Block (402)]
         ▼                         
 [Normalize Stage Done]
         │
         ▼
[release_quota_reservation()]
(Decrements pending_pages)
         │
         ├─ Success ──► [Record llm_usage] (Increments used pages)
         └─ Failure ──► [No llm_usage recorded] (Restores quota)
```

### 1. Subscription Configuration & Expiry
- **Subscription Creation**: Admins create subscriptions via `POST /admin/users/{user_id}/subscriptions`. Creating a new active subscription automatically updates any older active row to `status = 'superseded'` in the same transaction.
- **Lazy Expiry**: The system does not run background expiry daemons. Instead, whenever a user's quota is checked, or when the admin retrieves the user list, the database executes an inline UPDATE matching `period_end < NOW()` to flag active rows as `expired` and set `users.subscription_limit = 0`.

### 2. Quota Mathematics
A user's available quota is computed dynamically:
1. **Effective Limit**: The sum of the active subscription's `page_limit` and all approved `topups` attached to that subscription ID.
2. **Used Pages**: Calculated by counting unique `(extraction_id, page_num)` rows in the `llm_usage` table where the timestamp falls within the active subscription's start and end date window.
3. **Pending Pages**: Pages reserved for uploads currently in-flight but not yet fully processed.
4. **Remaining Pages**: `max(effective_limit - used - pending, 0)`.

### 3. The Atomic Reservation Lifecycle
1. **Reservation (`reserve_quota`)**:
   - On upload (`POST /ingest/rest` or `/v1/extract`), the server calls `reserve_quota()` in a transaction.
   - It acquires a row-lock (`SELECT FOR UPDATE`) on the `users` table for that user ID, serializing concurrent reservation requests.
   - It calculates available pages. If the incoming document fits (or falls within the `grace_pages = 10` boundary), the user's `pending_pages` column is incremented by the page count, and the upload is allowed.
2. **Release (`release_quota_reservation`)**:
   - Once the normalization stage completes (which splits the PDF into page images), the system knows the final page count.
   - It calls `release_quota_reservation()`, decrementing `pending_pages` by the reserved amount.
3. **Consumption (`llm_usage`)**:
   - As the LLM worker processes pages, successful page extraction completions write records to the `llm_usage` table. This permanently increments the `used` count in quota calculations.
   - If an extraction fails, cancelled or partially failed jobs are cleaned up without writing `llm_usage` records, returning pages to the user.

### 4. Client Top-up Requests
1. Clients submit requests via `POST /me/topup-requests` specifying the pages and period.
2. Admins review pending requests in the Admin Dashboard (`/admin/topup-requests`).
3. Approving a request runs `approve_topup_atomically`: it transitions the request status to `approved`, creates a `topup` row attached to the active subscription, and adds the pages. Rejections transition the status to `rejected`.

---

## Rules & Hard Constraints

- **Use-it-or-lose-it**: Top-ups are attached to a specific subscription period. When the subscription expires, all unused top-up pages are lost.
- **Single Active Period**: A user can have at most one subscription marked `active` at any given time.
- **Grace-Overage Threshold**: If a user has remaining pages but a multi-page upload exceeds the limit, it is allowed only if the overage is `≤ 10` pages (grace threshold).
- **Concurrency Protection**: The `FOR UPDATE` lock on the `users` table is mandatory during `reserve_quota` to prevent concurrent uploads from double-allocating pages.
- **Failed Job Restoration**: If an LLM run fails, the quota is not charged (no `llm_usage` row is created), preventing customers from paying for model timeouts.

---

## All Scenarios in Plain English

### Scenario 1 — Normal ingestion and quota deduction
- A client has a subscription with a limit of 100 pages. They have processed 80 pages.
- They upload a 10-page PDF.
- `reserve_quota` locks the user row, calculates 20 pages remaining, allows the upload, and sets `pending_pages = 10`.
- The PDF is normalized. The reservation is released (`pending_pages = 0`).
- The LLM processes 10 pages. 10 rows are added to `llm_usage`.
- The client's remaining balance is now 10 pages.

### Scenario 2 — Quota exceeded block
- A client has 5 pages remaining. They upload a 20-page PDF.
- `reserve_quota` calculates that the upload exceeds the limit by 15 pages.
- Since 15 is greater than the grace threshold (10), the reservation is rejected, and the server returns HTTP 402.

### Scenario 3 — Grace threshold allowance
- A client has 5 pages remaining. They upload an 8-page PDF.
- The limit is exceeded by 3 pages. Since 3 <= `grace_pages` (10), the upload is allowed.

### Scenario 4 — Top-up request approval
- A client has 0 pages remaining. They request 50 pages via the UI.
- The admin approves the request.
- The system atomically marks the request approved and creates a top-up of 50 pages attached to the active subscription.
- The client can now upload files immediately.

---

## Error Responses

| Situation | HTTP Code | Error Message |
|---|---|---|
| Ingest file with no active subscription | 402 / 409 | `"User has no active subscription. Create a subscription period before uploading."` |
| Ingest file exceeding limit | 402 | `"Subscription quota exceeded"` |
| Add top-up when user has no active subscription | 409 | `"User has no active subscription. Create a subscription period before adding top-ups."` |
| Create top-up request as Admin | 403 | `"Admins do not submit top-up requests"` |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_subscriptions.py`](../../tests/test_subscriptions.py) | `test_supersedes_prior_active_in_same_txn` | Verifies older subscriptions are automatically superseded on new subscription insert. |
| | `test_active_subscription_combines_base_and_topups` | Verifies available limit combines subscription and topups. |
| | `test_no_active_subscription_blocks_with_reason` | Verifies `reserve_quota` blocks uploads if no subscription is active. |
| | `test_exceeded_when_over_effective_limit_and_above_grace` | Verifies uploads exceeding the limit plus grace threshold are blocked. |
| [`test_topup_requests.py`](../../tests/test_topup_requests.py) | `test_approve_returns_updated_row` | Verifies top-up request approval marks status and creates top-ups. |
| | `test_query_guards_on_pending_status` | Verifies the update statement guards against double-resolution of top-up requests. |
| | `test_string_pages_coerced_to_int` | Verifies string inputs for pages (e.g. `"1500"`) are coerced to integer values. |

---

## Quick Reference

| Operation / Check | Source / Location | Columns / Variables | Notes |
|---|---|---|---|
| Subscription details | `subscriptions` table | `page_limit`, `period_start`, `period_end` | Keyed by `user_id` |
| Top-up values | `topups` table | `pages` | Attached via `subscription_id` |
| Quota Reservation | `users` table | `pending_pages` | Incremented/decremented dynamically |
| Usage details | `llm_usage` table | `extraction_id`, `page_num`, `call_type` | Filtered by subscription dates |
