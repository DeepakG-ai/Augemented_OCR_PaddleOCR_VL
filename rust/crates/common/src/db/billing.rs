//! billing.rs — usage counters, quotas, subscriptions, top-ups, top-up
//! requests, idempotency claims (db.py "LLM usage" / "Quota" / "Subscriptions"
//! / "Top-up requests" sections).
//!
//! Quota model (v2): effective_limit = active_subscription.page_limit +
//! SUM(topups); used = distinct (extraction_id, page_num) billed inside the
//! subscription window; pending_pages acts as an in-flight reservation locked
//! under FOR UPDATE so concurrent uploads serialise. Lazy expiry flips overdue
//! subscriptions to 'expired' on every read path, exactly as db.py did.

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use super::util;
use crate::error::{AppError, AppResult};

/// Lazy-expiry statement shared verbatim by every quota read path in db.py:
/// flips overdue active subscriptions to 'expired' and zeroes the legacy
/// users.subscription_limit mirror.
const EXPIRE_DUE_SQL: &str = "WITH expired AS (
        UPDATE subscriptions
           SET status = 'expired'
         WHERE user_id = $1 AND status = 'active' AND period_end < NOW()
         RETURNING user_id
     )
     UPDATE users
        SET subscription_limit = 0
      WHERE id IN (SELECT user_id FROM expired)";

/// Wrap an INSERT/UPDATE..RETURNING so its result row arrives as one jsonb
/// payload (the DML must sit as a top-level CTE member).
fn returning_row_sql(statement: &str) -> String {
    format!("WITH _r AS ({statement}) SELECT to_jsonb(_r.*) AS row FROM _r")
}

/// db.py `_row_subscription` stringified UUID columns after the fact; here
/// to_jsonb has already rendered them as strings, so this passes the row
/// through unchanged.
fn _row_subscription(row: Value) -> Value {
    row
}

/// Persist one LLM call's usage counters.
///
/// llama.cpp reports OpenAI-compatible usage bodies; counters are stored
/// as-is, with a computed total only when the server omits total_tokens
/// (zero). `billing_user_id` overrides the vendor-resolved owner (admin
/// uploads bill to the admin account); `api_key_id` enables per-key reports.
#[allow(clippy::too_many_arguments)]
pub async fn record_llm_usage(
    pool: &PgPool,
    doc_id: Option<&str>,
    document_id: Option<&str>,
    extraction_id: Option<i64>,
    vendor_id: Option<&str>,
    page_num: Option<i64>,
    total_pages: Option<i64>,
    call_type: &str,
    model: &str,
    prompt_tokens: i64,
    completion_tokens: i64,
    total_tokens: i64,
    duration_ms: Option<f64>,
    llm_url: &str,
    request_id: Option<&str>,
    billing_user_id: Option<&str>,
    api_key_id: Option<i64>,
) -> AppResult<Value> {
    let total = if total_tokens != 0 {
        total_tokens
    } else {
        prompt_tokens + completion_tokens
    };
    let billing_uuid: Option<Uuid> = util::uuid_or_none(billing_user_id);
    let sql = returning_row_sql(
        "INSERT INTO llm_usage
            (request_id, doc_id, extraction_id, vendor_id, user_id, page_num, total_pages,
             call_type, model, prompt_tokens, completion_tokens, total_tokens,
             duration_ms, llm_url, api_key_id)
         VALUES (
             $1, $2, $3, $4,
             COALESCE($5::uuid, (SELECT user_id FROM vendors WHERE id = $4)),
             $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
         )
         RETURNING id, ts, request_id, doc_id, extraction_id, vendor_id, user_id,
                   page_num, total_pages, call_type, model, prompt_tokens,
                   completion_tokens, total_tokens, duration_ms, llm_url, api_key_id",
    );
    let rec = sqlx::query(&sql)
        .bind(request_id)
        .bind(doc_id.or(document_id))
        .bind(extraction_id.map(|v| v as i32))
        .bind(vendor_id)
        .bind(billing_uuid)
        .bind(page_num.map(|v| v as i32))
        .bind(total_pages.map(|v| v as i32))
        .bind(call_type)
        .bind(model)
        .bind(prompt_tokens as i32)
        .bind(completion_tokens as i32)
        .bind(total as i32)
        .bind(duration_ms.map(|d| d as f32))
        .bind(llm_url)
        .bind(api_key_id.map(|v| v as i32))
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value).unwrap_or_else(|| json!({})))
}

/// Return summed token counts for all LLM calls belonging to one extraction.
pub async fn get_extraction_token_totals(pool: &PgPool, extraction_id: i64) -> AppResult<Value> {
    let sql = util::row_query(
        "SELECT
            COALESCE(SUM(prompt_tokens), 0)::BIGINT     AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0)::BIGINT AS completion_tokens,
            COALESCE(SUM(total_tokens), 0)::BIGINT      AS total_tokens,
            COUNT(*)::INT                               AS llm_calls
         FROM llm_usage
         WHERE extraction_id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value).unwrap_or_else(|| {
        json!({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0})
    }))
}

/// Return token totals grouped per document for manager reporting.
pub async fn get_llm_usage_document_summary(
    pool: &PgPool,
    limit: i64,
    vendor_id: Option<&str>,
) -> AppResult<Vec<Value>> {
    let capped = 1.max(limit.min(500));
    let sql = util::row_query(
        "SELECT
            COALESCE(doc_id, extraction_id::TEXT, 'unknown') AS doc_id,
            vendor_id,
            SUM(prompt_tokens)::BIGINT AS total_input_tokens,
            SUM(completion_tokens)::BIGINT AS total_output_tokens,
            SUM(total_tokens)::BIGINT AS grand_total,
            COUNT(*)::INT AS llm_calls,
            ROUND(AVG(duration_ms))::INT AS avg_call_ms,
            MIN(ts) AS first_seen_at,
            MAX(ts) AS last_seen_at
         FROM llm_usage
         WHERE ($1::TEXT IS NULL OR vendor_id = $1)
         GROUP BY COALESCE(doc_id, extraction_id::TEXT, 'unknown'), vendor_id
         ORDER BY MAX(ts) DESC
         LIMIT $2",
    );
    let rows = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(capped)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return token totals grouped by day, optionally scoped to one vendor or user.
pub async fn get_llm_usage_daily_summary(
    pool: &PgPool,
    limit: i64,
    vendor_id: Option<&str>,
    user_id: Option<&str>,
    date_from: Option<DateTime<Utc>>,
    date_to: Option<DateTime<Utc>>,
) -> AppResult<Vec<Value>> {
    let uid: Option<Uuid> = util::uuid_or_none(user_id);
    let capped = 1.max(limit.min(366));
    let sql = util::row_query(
        "SELECT
            DATE(lu.ts) AS day,
            SUM(lu.prompt_tokens)::BIGINT AS input_tokens,
            SUM(lu.completion_tokens)::BIGINT AS output_tokens,
            SUM(lu.total_tokens)::BIGINT AS total_tokens,
            COUNT(DISTINCT COALESCE(lu.doc_id, lu.extraction_id::TEXT, 'unknown'))::INT AS docs_processed,
            COUNT(*)::INT AS llm_calls,
            ROUND(AVG(lu.duration_ms))::INT AS avg_call_ms
         FROM llm_usage lu
         WHERE ($1::TEXT IS NULL OR lu.vendor_id = $1)
           AND ($2::UUID IS NULL OR lu.user_id = $2)
           AND ($4::TIMESTAMPTZ IS NULL OR lu.ts >= $4)
           AND ($5::TIMESTAMPTZ IS NULL OR lu.ts < $5)
         GROUP BY DATE(lu.ts)
         ORDER BY day DESC
         LIMIT $3",
    );
    let rows = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(uid)
        .bind(capped)
        .bind(date_from)
        .bind(date_to)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return daily LLM usage for one client user's vendors.
pub async fn get_client_daily_summary(
    pool: &PgPool,
    user_id: &str,
    limit: i64,
    date_from: Option<DateTime<Utc>>,
    date_to: Option<DateTime<Utc>>,
) -> AppResult<Vec<Value>> {
    get_llm_usage_daily_summary(pool, limit, None, Some(user_id), date_from, date_to).await
}

/// Return token/page usage aggregated per user for the admin client-breakdown view.
pub async fn get_usage_by_client(pool: &PgPool) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "WITH extraction_totals AS (
            SELECT
                COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) AS user_id,
                COUNT(e.id) FILTER (WHERE e.status = 'done')::INT AS total_extractions
            FROM vendors v
            LEFT JOIN extractions e ON e.vendor_id = v.id
            LEFT JOIN documents d ON d.id = e.document_id
            GROUP BY COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id)
        ),
        usage_totals AS (
            SELECT
                lu.user_id,
                COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
                COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS grand_total,
                COUNT(lu.id)::INT AS total_llm_calls,
                COUNT(DISTINCT (lu.extraction_id, lu.page_num)) FILTER (
                    WHERE lu.call_type = 'extraction'
                      AND lu.extraction_id IS NOT NULL
                      AND lu.page_num IS NOT NULL
                )::INT AS billable_pages
            FROM llm_usage lu
            GROUP BY lu.user_id
        )
        SELECT
            u.id::TEXT AS user_id,
            u.email,
            u.role,
            u.is_active,
            COALESCE(et.total_extractions, 0)::INT AS total_extractions,
            COALESCE(ut.billable_pages, 0)::INT AS billable_pages,
            COALESCE(ut.total_input_tokens, 0)::BIGINT AS total_input_tokens,
            COALESCE(ut.total_output_tokens, 0)::BIGINT AS total_output_tokens,
            COALESCE(ut.grand_total, 0)::BIGINT AS grand_total,
            COALESCE(ut.total_llm_calls, 0)::INT AS total_llm_calls
        FROM users u
        LEFT JOIN extraction_totals et ON et.user_id = u.id
        LEFT JOIN usage_totals ut ON ut.user_id = u.id
        ORDER BY grand_total DESC",
    );
    let rows = sqlx::query(&sql).fetch_all(pool).await.map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return per-document token usage for a specific client user (admin only).
pub async fn get_client_document_usage(
    pool: &PgPool,
    user_id: &str,
    limit: i64,
    date_from: Option<DateTime<Utc>>,
    date_to: Option<DateTime<Utc>>,
) -> AppResult<Vec<Value>> {
    let Some(user_uuid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(Vec::new());
    };
    let capped = 1.max(limit.min(500));
    let sql = util::row_query(
        "SELECT
            e.id AS extraction_id,
            e.filename,
            v.id AS vendor_id,
            v.name AS vendor_name,
            e.total_pages,
            e.status,
            e.created_at,
            COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
            COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
            COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS grand_total,
            COUNT(lu.id)::INT AS llm_calls,
            COUNT(DISTINCT lu.page_num) FILTER (
                WHERE lu.call_type = 'extraction' AND lu.page_num IS NOT NULL
            )::INT AS billable_pages,
            COALESCE(SUM(lu.duration_ms), 0)::REAL AS total_latency_ms
         FROM extractions e
         JOIN vendors v ON v.id = e.vendor_id
         JOIN documents d ON d.id = e.document_id
         LEFT JOIN llm_usage lu ON lu.extraction_id = e.id
         WHERE COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1::UUID
           AND ($3::TIMESTAMPTZ IS NULL OR e.created_at >= $3)
           AND ($4::TIMESTAMPTZ IS NULL OR e.created_at < $4)
         GROUP BY e.id, e.filename, v.id, v.name, e.total_pages, e.status, e.created_at
         ORDER BY e.created_at DESC
         LIMIT $2",
    );
    let rows = sqlx::query(&sql)
        .bind(user_uuid)
        .bind(capped)
        .bind(date_from)
        .bind(date_to)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return per-page token breakdown for a single extraction (admin drill-down).
pub async fn get_extraction_page_usage(pool: &PgPool, extraction_id: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT
            page_num,
            call_type,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            duration_ms,
            ts
         FROM llm_usage
         WHERE extraction_id = $1
         ORDER BY page_num ASC, ts ASC",
    );
    let rows = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return recent raw LLM usage rows, one row per model call.
pub async fn list_llm_usage_calls(
    pool: &PgPool,
    limit: i64,
    doc_id: Option<&str>,
    vendor_id: Option<&str>,
) -> AppResult<Vec<Value>> {
    let capped = 1.max(limit.min(1000));
    let sql = util::row_query(
        "SELECT id, ts, request_id, doc_id, extraction_id, vendor_id,
               page_num, total_pages, call_type, model, prompt_tokens,
               completion_tokens, total_tokens, duration_ms, llm_url
        FROM llm_usage
        WHERE ($1::TEXT IS NULL OR doc_id = $1)
          AND ($2::TEXT IS NULL OR vendor_id = $2)
        ORDER BY ts DESC
        LIMIT $3",
    );
    let rows = sqlx::query(&sql)
        .bind(doc_id)
        .bind(vendor_id)
        .bind(capped)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return aggregate usage counters for the dashboard, optionally scoped to one user.
pub async fn get_usage_stats(
    pool: &PgPool,
    user_id: Option<&str>,
    date_from: Option<DateTime<Utc>>,
    date_to: Option<DateTime<Utc>>,
) -> AppResult<Value> {
    let uid: Option<Uuid> = util::uuid_or_none(user_id);
    let ext_sql = util::row_query(
        "SELECT
            COUNT(*)::INT AS total_pdfs,
            COUNT(*) FILTER (WHERE e.status = 'done')::INT AS total_extractions,
            COALESCE(SUM(e.total_pages), 0)::BIGINT AS all_pages,
            COALESCE(SUM(e.total_pages) FILTER (WHERE e.status = 'done'), 0)::BIGINT AS total_pages
         FROM extractions e
         LEFT JOIN vendors v ON v.id = e.vendor_id
         LEFT JOIN documents d ON d.id = e.document_id
         WHERE ($1::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1)
           AND ($2::TIMESTAMPTZ IS NULL OR e.created_at >= $2)
           AND ($3::TIMESTAMPTZ IS NULL OR e.created_at < $3)",
    );
    let ext_rec = sqlx::query(&ext_sql)
        .bind(uid)
        .bind(date_from)
        .bind(date_to)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;

    let llm_sql = util::row_query(
        "SELECT
            COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT     AS total_input_tokens,
            COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
            COALESCE(SUM(lu.total_tokens), 0)::BIGINT      AS grand_total,
            COUNT(lu.id)::INT                               AS total_llm_calls,
            COUNT(DISTINCT (lu.extraction_id, lu.page_num)) FILTER (
                WHERE lu.call_type = 'extraction'
                  AND lu.extraction_id IS NOT NULL
                  AND lu.page_num IS NOT NULL
            )::INT AS billable_pages
         FROM llm_usage lu
         WHERE ($1::UUID IS NULL OR lu.user_id = $1)
           AND ($2::TIMESTAMPTZ IS NULL OR lu.ts >= $2)
           AND ($3::TIMESTAMPTZ IS NULL OR lu.ts < $3)",
    );
    let llm_rec = sqlx::query(&llm_sql)
        .bind(uid)
        .bind(date_from)
        .bind(date_to)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;

    let mut stats = ext_rec.map(util::row_value).unwrap_or_else(|| json!({}));
    let extra = llm_rec.map(util::row_value);
    if let (Some(base), Some(extra_obj)) =
        (stats.as_object_mut(), extra.as_ref().and_then(Value::as_object))
    {
        for (k, v) in extra_obj {
            base.insert(k.clone(), v.clone());
        }
    }
    let all_pages = stats.get("all_pages").map(util::int_or_zero).unwrap_or(0);
    let billable = stats.get("billable_pages").map(util::int_or_zero).unwrap_or(0);
    stats["unbilled_pages"] = json!((all_pages - billable).max(0));
    stats["failed_pages"] = json!(0);
    Ok(stats)
}

/// Atomically check quota and reserve pages for an in-flight upload.
///
/// SELECT … FOR UPDATE on the user row serialises concurrent uploads so two
/// simultaneous requests cannot both see the same usage snapshot; on success
/// pending_pages is incremented by incoming_pages. Behaviour matrix: fits →
/// ok; over but within grace while headroom remains → grace; otherwise
/// exceeded; no active subscription → no_subscription.
pub async fn reserve_quota(
    pool: &PgPool,
    user_id: &str,
    incoming_pages: i64,
    grace_pages: i64,
) -> AppResult<Value> {
    let blocked = || {
        json!({
            "allowed": false, "reason": "exceeded", "used": 0,
            "limit": 0, "remaining": 0, "pending": 0,
        })
    };
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(blocked());
    };

    let mut tx = pool.begin().await.map_err(AppError::from)?;
    // Lazy expiry inside the txn so concurrent uploads see the same view.
    sqlx::query(EXPIRE_DUE_SQL)
        .bind(uid)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;

    // Lock the user row so concurrent reserves serialise on the
    // pending_pages counter.
    let user_rec =
        sqlx::query("SELECT COALESCE(pending_pages, 0) AS pending FROM users WHERE id = $1 FOR UPDATE")
            .bind(uid)
            .fetch_optional(&mut *tx)
            .await
            .map_err(AppError::from)?;
    let Some(user_rec) = user_rec else {
        tx.commit().await.map_err(AppError::from)?;
        return Ok(blocked());
    };
    let pending = i64::from(user_rec.get::<i32, _>("pending"));

    let sub = sqlx::query(
        "SELECT id, page_limit, period_start, period_end
         FROM subscriptions
         WHERE user_id = $1 AND status = 'active'
         LIMIT 1",
    )
    .bind(uid)
    .fetch_optional(&mut *tx)
    .await
    .map_err(AppError::from)?;
    let Some(sub) = sub else {
        tx.commit().await.map_err(AppError::from)?;
        return Ok(json!({
            "allowed": false, "reason": "no_subscription", "used": 0,
            "limit": 0, "remaining": 0, "pending": pending,
        }));
    };
    let sub_id: i32 = sub.get("id");
    let base_limit = i64::from(sub.get::<i32, _>("page_limit"));
    let period_start: DateTime<Utc> = sub.get("period_start");
    let period_end: DateTime<Utc> = sub.get("period_end");

    let topup_total: i64 =
        sqlx::query_scalar("SELECT COALESCE(SUM(pages), 0) FROM topups WHERE subscription_id = $1")
            .bind(sub_id)
            .fetch_one(&mut *tx)
            .await
            .map_err(AppError::from)?;
    let effective_limit = base_limit + topup_total;

    let used: i64 = sqlx::query_scalar(
        "SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
         FROM llm_usage lu
         WHERE lu.user_id = $1
           AND lu.call_type = 'extraction'
           AND lu.page_num IS NOT NULL
           AND lu.ts >= $2 AND lu.ts < $3",
    )
    .bind(uid)
    .bind(period_start)
    .bind(period_end)
    .fetch_one(&mut *tx)
    .await
    .map_err(AppError::from)?;

    let committed = used + pending;
    let remaining = (effective_limit - committed).max(0);
    let would_exceed = committed + incoming_pages > effective_limit;

    let (reason, allowed) = if !would_exceed {
        ("ok", true)
    } else if committed < effective_limit && incoming_pages <= grace_pages {
        // Grace applies only when the user still has headroom (committed
        // strictly under the limit). At exactly limit==committed there is
        // no room left, so block — no grace.
        ("grace", true)
    } else {
        ("exceeded", false)
    };

    let mut grace_pages_used = 0i64;
    if reason == "grace" {
        grace_pages_used = (committed + incoming_pages - effective_limit).max(0);
    }

    if allowed {
        sqlx::query("UPDATE users SET pending_pages = pending_pages + $1 WHERE id = $2")
            .bind(incoming_pages as i32)
            .bind(uid)
            .execute(&mut *tx)
            .await
            .map_err(AppError::from)?;
    }
    tx.commit().await.map_err(AppError::from)?;

    Ok(json!({
        "allowed": allowed,
        "reason": reason,
        "used": used,
        "limit": effective_limit,
        "remaining": remaining,
        "pending": pending,
        "grace_pages_used": grace_pages_used,
    }))
}

/// Decrement pending_pages after the normalize worker finishes (success or
/// failure). GREATEST(0, …) keeps a double-release from driving it negative.
pub async fn release_quota_reservation(pool: &PgPool, user_id: &str, pages: i64) -> AppResult<()> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(());
    };
    sqlx::query("UPDATE users SET pending_pages = GREATEST(0, pending_pages - $1) WHERE id = $2")
        .bind(pages as i32)
        .bind(uid)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    Ok(())
}

/// Record one grace overage or hard-block event for admin visibility.
///
/// event_type: 'grace_used' | 'exceeded'. Never raises — best-effort logging;
/// must not crash the upload path.
#[allow(clippy::too_many_arguments)]
pub async fn insert_quota_grace_event(
    pool: &PgPool,
    user_id: &str,
    event_type: &str,
    grace_pages_used: i64,
    incoming_pages: i64,
    used_before: i64,
    limit_at_time: i64,
    filename: Option<&str>,
) -> AppResult<()> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(());
    };
    let res = sqlx::query(
        "INSERT INTO quota_grace_events
            (user_id, event_type, grace_pages_used, incoming_pages,
             used_before, limit_at_time, filename)
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(uid)
    .bind(event_type)
    .bind(grace_pages_used as i32)
    .bind(incoming_pages as i32)
    .bind(used_before as i32)
    .bind(limit_at_time as i32)
    .bind(filename)
    .execute(pool)
    .await;
    if let Err(err) = res {
        tracing::warn!(error = %err, "insert_quota_grace_event failed");
    }
    Ok(())
}

/// Return recent quota grace/exceeded events with the user's email.
pub async fn get_admin_quota_events(pool: &PgPool, limit: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT qge.id, qge.event_ts, qge.event_type, qge.grace_pages_used,
                qge.incoming_pages, qge.used_before, qge.limit_at_time,
                qge.filename, u.email
         FROM quota_grace_events qge
         JOIN users u ON u.id = qge.user_id
         ORDER BY qge.event_ts DESC
         LIMIT $1",
    );
    let rows = sqlx::query(&sql)
        .bind(limit)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Idempotently release a document's page reservation.
///
/// Reads metadata.reserved_pages and decrements the billing user's
/// pending_pages, clearing reserved_pages in the SAME transaction. A second
/// call finds reserved_pages already gone and is a no-op, so a double-release
/// can't steal pending quota from another in-flight upload. Returns the pages
/// actually released.
pub async fn release_quota_once(
    pool: &PgPool,
    document_id: Option<i64>,
    user_id: Option<&str>,
) -> AppResult<i64> {
    let Some(document_id) = document_id else {
        return Ok(0);
    };
    let mut tx = pool.begin().await.map_err(AppError::from)?;
    let row = sqlx::query("SELECT metadata FROM documents WHERE id = $1 FOR UPDATE")
        .bind(document_id as i32)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let Some(row) = row else {
        tx.commit().await.map_err(AppError::from)?;
        return Ok(0);
    };
    let meta = row
        .try_get::<Option<Value>, _>("metadata")
        .map_err(AppError::Database)?
        .unwrap_or_else(|| json!({}));
    let raw_pages = meta.get("reserved_pages");
    let pages = raw_pages.and_then(util::int_or_none).unwrap_or(0);
    if pages <= 0 {
        tx.commit().await.map_err(AppError::from)?;
        return Ok(0);
    }
    let meta_billing_uid = meta
        .get("billing_user_id")
        .and_then(Value::as_str)
        .and_then(|s| Uuid::parse_str(s.trim()).ok());
    let uid = util::uuid_or_none(user_id).or(meta_billing_uid);
    if let Some(uid) = uid {
        sqlx::query("UPDATE users SET pending_pages = GREATEST(0, pending_pages - $1) WHERE id = $2")
            .bind(pages as i32)
            .bind(uid)
            .execute(&mut *tx)
            .await
            .map_err(AppError::from)?;
    }
    sqlx::query(
        "UPDATE documents
           SET metadata = COALESCE(metadata, '{}'::jsonb) - 'reserved_pages',
               updated_at = NOW()
         WHERE id = $1",
    )
    .bind(document_id as i32)
    .execute(&mut *tx)
    .await
    .map_err(AppError::from)?;
    tx.commit().await.map_err(AppError::from)?;
    Ok(pages)
}

/// Flip any active subscription past its period_end to status='expired'.
/// Returns the number of touched rows (the legacy-limit mirror reset). Safe
/// to call repeatedly.
pub async fn expire_due_subscriptions(pool: &PgPool) -> AppResult<i64> {
    let res = sqlx::query(
        "WITH expired AS (
            UPDATE subscriptions
               SET status = 'expired'
             WHERE status = 'active' AND period_end < NOW()
             RETURNING user_id
        )
        UPDATE users
           SET subscription_limit = 0
         WHERE id IN (SELECT user_id FROM expired)",
    )
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(res.rows_affected() as i64)
}

/// Create a new active subscription. Any prior active subscription for the
/// same user is marked 'superseded' in the same transaction so the partial
/// unique index (one active per user) is respected; the legacy
/// users.subscription_limit mirror is kept in sync.
pub async fn create_subscription(
    pool: &PgPool,
    user_id: &str,
    page_limit: i64,
    period_start: DateTime<Utc>,
    period_end: DateTime<Utc>,
    note: Option<&str>,
    created_by: Option<&str>,
) -> AppResult<Option<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(None);
    };
    if page_limit < 0 {
        return Err(AppError::BadRequest("page_limit must be non-negative".to_string()));
    }
    if period_end <= period_start {
        return Err(AppError::BadRequest(
            "period_end must be after period_start".to_string(),
        ));
    }
    let creator_uuid: Option<Uuid> = created_by.and_then(|s| util::uuid_or_none(Some(s)));

    let mut tx = pool.begin().await.map_err(AppError::from)?;
    sqlx::query(
        "UPDATE subscriptions
           SET status = 'superseded'
         WHERE user_id = $1 AND status = 'active'",
    )
    .bind(uid)
    .execute(&mut *tx)
    .await
    .map_err(AppError::from)?;

    let sql = returning_row_sql(
        "INSERT INTO subscriptions
            (user_id, page_limit, period_start, period_end, status, note, created_by)
         VALUES ($1, $2, $3, $4, 'active', $5, $6)
         RETURNING id, user_id, page_limit, period_start, period_end,
                   status, note, created_by, created_at",
    );
    let row = sqlx::query(&sql)
        .bind(uid)
        .bind(page_limit as i32)
        .bind(period_start)
        .bind(period_end)
        .bind(note)
        .bind(creator_uuid)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;

    // Keep the legacy users.subscription_limit in sync so any code path
    // that still reads it sees the new base.
    sqlx::query("UPDATE users SET subscription_limit = $1 WHERE id = $2")
        .bind(page_limit as i32)
        .bind(uid)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;
    tx.commit().await.map_err(AppError::from)?;

    // legacy subscription_limit mirror changed
    super::invalidate_user_cache(Some(&uid.to_string())).await;
    Ok(row.map(util::row_value).map(_row_subscription))
}

/// Mark a subscription as cancelled (admin action). Only affects rows
/// belonging to this user; idempotent if already cancelled.
pub async fn cancel_subscription(pool: &PgPool, user_id: &str, subscription_id: i64) -> AppResult<bool> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(false);
    };
    let mut tx = pool.begin().await.map_err(AppError::from)?;
    let res = sqlx::query(
        "UPDATE subscriptions
           SET status = 'cancelled'
         WHERE id = $1 AND user_id = $2 AND status = 'active'",
    )
    .bind(subscription_id as i32)
    .bind(uid)
    .execute(&mut *tx)
    .await
    .map_err(AppError::from)?;
    let is_cancelled = res.rows_affected() == 1;
    if is_cancelled {
        sqlx::query("UPDATE users SET subscription_limit = 0 WHERE id = $1")
            .bind(uid)
            .execute(&mut *tx)
            .await
            .map_err(AppError::from)?;
    }
    tx.commit().await.map_err(AppError::from)?;
    if is_cancelled {
        // legacy subscription_limit mirror changed
        super::invalidate_user_cache(Some(&uid.to_string())).await;
    }
    Ok(is_cancelled)
}

/// Return the user's currently active subscription, or None.
/// Auto-expires stale rows before reading (lazy expiry keeps reads correct
/// even if a background job hasn't run).
pub async fn get_active_subscription(pool: &PgPool, user_id: &str) -> AppResult<Option<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(None);
    };
    sqlx::query(EXPIRE_DUE_SQL)
        .bind(uid)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    let sql = util::row_query(
        "SELECT id, user_id, page_limit, period_start, period_end,
                status, note, created_by, created_at
         FROM subscriptions
         WHERE user_id = $1 AND status = 'active'
         LIMIT 1",
    );
    let rec = sqlx::query(&sql)
        .bind(uid)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value).map(_row_subscription))
}

/// All subscriptions for this user, newest first.
pub async fn list_user_subscriptions(pool: &PgPool, user_id: &str) -> AppResult<Vec<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(Vec::new());
    };
    let sql = util::row_query(
        "SELECT id, user_id, page_limit, period_start, period_end,
                status, note, created_by, created_at
         FROM subscriptions
         WHERE user_id = $1
         ORDER BY created_at DESC",
    );
    let rows = sqlx::query(&sql)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows
        .into_iter()
        .map(|rec| _row_subscription(util::row_value(rec)))
        .collect())
}

/// Attach pages to the user's current active subscription.
/// Returns None if there is no active subscription (admin must create a
/// subscription period first).
pub async fn add_topup(
    pool: &PgPool,
    user_id: &str,
    pages: i64,
    note: Option<&str>,
    created_by: Option<&str>,
) -> AppResult<Option<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(None);
    };
    if pages <= 0 {
        return Err(AppError::BadRequest("topup pages must be positive".to_string()));
    }
    let creator_uuid: Option<Uuid> = created_by.and_then(|s| util::uuid_or_none(Some(s)));

    let mut tx = pool.begin().await.map_err(AppError::from)?;
    sqlx::query(EXPIRE_DUE_SQL)
        .bind(uid)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let sub = sqlx::query("SELECT id FROM subscriptions WHERE user_id = $1 AND status = 'active' LIMIT 1")
        .bind(uid)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let Some(sub) = sub else {
        tx.commit().await.map_err(AppError::from)?;
        return Ok(None);
    };
    let sub_id: i32 = sub.get("id");

    let sql = returning_row_sql(
        "INSERT INTO topups (user_id, subscription_id, pages, note, created_by)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING id, user_id, subscription_id, pages, note, created_by, created_at",
    );
    let row = sqlx::query(&sql)
        .bind(uid)
        .bind(sub_id)
        .bind(pages as i32)
        .bind(note)
        .bind(creator_uuid)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;
    tx.commit().await.map_err(AppError::from)?;
    Ok(row.map(util::row_value).map(_row_subscription))
}

/// Top-ups attached to one subscription, newest first.
pub async fn list_topups_for_subscription(pool: &PgPool, subscription_id: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT id, user_id, subscription_id, pages, note, created_by, created_at
         FROM topups
         WHERE subscription_id = $1
         ORDER BY created_at DESC",
    );
    let rows = sqlx::query(&sql)
        .bind(subscription_id as i32)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows
        .into_iter()
        .map(|rec| _row_subscription(util::row_value(rec)))
        .collect())
}

/// All top-ups ever granted to a user, newest first.
pub async fn list_topups_for_user(pool: &PgPool, user_id: &str) -> AppResult<Vec<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(Vec::new());
    };
    let sql = util::row_query(
        "SELECT id, user_id, subscription_id, pages, note, created_by, created_at
         FROM topups
         WHERE user_id = $1
         ORDER BY created_at DESC",
    );
    let rows = sqlx::query(&sql)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows
        .into_iter()
        .map(|rec| _row_subscription(util::row_value(rec)))
        .collect())
}

fn blank_quota() -> Value {
    json!({
        "has_active_subscription": false,
        "subscription_id": null,
        "period_start": null,
        "period_end": null,
        "base_limit": 0,
        "topup_total": 0,
        "effective_limit": 0,
        "used": 0,
        "pending": 0,
        "remaining": 0,
        "status": "none",
    })
}

/// Compute the current page quota for a user using the v2 model.
///
/// For users with no active subscription we still return a shape so callers
/// don't need null-checks; effective_limit/remaining are 0. Timestamp bounds
/// are serialized RFC3339 (Python returned datetime objects).
pub async fn get_user_quota_v2(pool: &PgPool, user_id: &str) -> AppResult<Value> {
    let blank = blank_quota();
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(blank);
    };
    sqlx::query(EXPIRE_DUE_SQL)
        .bind(uid)
        .execute(pool)
        .await
        .map_err(AppError::from)?;

    let sub = sqlx::query(
        "SELECT id, page_limit, period_start, period_end
         FROM subscriptions
         WHERE user_id = $1 AND status = 'active'
         LIMIT 1",
    )
    .bind(uid)
    .fetch_optional(pool)
    .await
    .map_err(AppError::from)?;

    let pending_row = sqlx::query("SELECT COALESCE(pending_pages, 0) AS pending FROM users WHERE id = $1")
        .bind(uid)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    let pending = pending_row
        .map(|r| i64::from(r.get::<i32, _>("pending")))
        .unwrap_or(0);

    let Some(sub) = sub else {
        let mut out = blank;
        out["pending"] = json!(pending);
        return Ok(out);
    };

    let sub_id: i32 = sub.get("id");
    let base_limit = i64::from(sub.get::<i32, _>("page_limit"));
    let period_start: DateTime<Utc> = sub.get("period_start");
    let period_end: DateTime<Utc> = sub.get("period_end");

    let topup_total: i64 =
        sqlx::query_scalar("SELECT COALESCE(SUM(pages), 0) FROM topups WHERE subscription_id = $1")
            .bind(sub_id)
            .fetch_one(pool)
            .await
            .map_err(AppError::from)?;

    let used: i64 = sqlx::query_scalar(
        "SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
         FROM llm_usage lu
         WHERE lu.user_id = $1
           AND lu.call_type = 'extraction'
           AND lu.page_num IS NOT NULL
           AND lu.ts >= $2 AND lu.ts < $3",
    )
    .bind(uid)
    .bind(period_start)
    .bind(period_end)
    .fetch_one(pool)
    .await
    .map_err(AppError::from)?;

    let effective_limit = base_limit + topup_total;
    let remaining = (effective_limit - used - pending).max(0);
    Ok(json!({
        "has_active_subscription": true,
        "subscription_id": sub_id,
        "period_start": period_start,
        "period_end": period_end,
        "base_limit": base_limit,
        "topup_total": topup_total,
        "effective_limit": effective_limit,
        "used": used,
        "pending": pending,
        "remaining": remaining,
        "status": "active",
    }))
}

/// Return current billable pages, effective subscription limit and remaining
/// pages for a user (db.py `get_user_billable_pages`).
///
/// Built on the v2 quota model:
/// * `billable_pages` counts `DISTINCT (extraction_id, page_num)` inside the
///   active subscription's window, falling back to all-time usage when there
///   is no active subscription so admins still see total history.
/// * `subscription_limit` is the active subscription base plus topups, or 0.
/// * Period bounds and `topup_total` ride along so the UI does not need a
///   second round trip for warning banners.
///
/// Note the fallback branch reports `remaining = -used`, mirroring Python:
/// with a zero limit every page consumed is a page over.
pub async fn get_user_billable_pages(pool: &PgPool, user_id: &str) -> AppResult<Value> {
    let blank = json!({
        "billable_pages": 0,
        "subscription_limit": 0,
        "remaining": 0,
        "base_limit": 0,
        "topup_total": 0,
        "period_start": null,
        "period_end": null,
        "has_active_subscription": false,
    });
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(blank);
    };

    let quota = get_user_quota_v2(pool, user_id).await?;
    if quota["has_active_subscription"] == Value::Bool(true) {
        let used = util::int_or_zero(&quota["used"]);
        let effective_limit = util::int_or_zero(&quota["effective_limit"]);
        return Ok(json!({
            "billable_pages": used,
            "subscription_limit": effective_limit,
            "remaining": effective_limit - used,
            "base_limit": quota["base_limit"],
            "topup_total": quota["topup_total"],
            "period_start": quota["period_start"],
            "period_end": quota["period_end"],
            "has_active_subscription": true,
        }));
    }

    // No active subscription — report lifetime usage so the admin UI stays useful.
    let used: i64 = sqlx::query_scalar(
        "SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
         FROM llm_usage lu
         WHERE lu.user_id = $1
           AND lu.call_type = 'extraction'
           AND lu.page_num IS NOT NULL",
    )
    .bind(uid)
    .fetch_one(pool)
    .await
    .map_err(AppError::from)?;

    let mut out = blank;
    out["billable_pages"] = json!(used);
    out["remaining"] = json!(-used);
    Ok(out)
}

/// Return all subscriptions + all topups for a user (admin history view).
/// Topups carry their subscription's period dates so the UI can show them on
/// the same timeline.
pub async fn get_user_history(pool: &PgPool, user_id: &str) -> AppResult<Value> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(json!({"subscriptions": [], "topups": []}));
    };
    sqlx::query(EXPIRE_DUE_SQL)
        .bind(uid)
        .execute(pool)
        .await
        .map_err(AppError::from)?;

    let subs_sql = util::row_query(
        "SELECT s.id, s.page_limit, s.period_start, s.period_end,
                s.status, s.note, s.created_at,
                admin.email AS created_by_email,
                COALESCE((SELECT SUM(pages) FROM topups WHERE subscription_id = s.id), 0)::INT
                    AS topup_total,
                (
                    SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
                    FROM llm_usage lu
                    WHERE lu.user_id = s.user_id
                      AND lu.call_type = 'extraction'
                      AND lu.page_num IS NOT NULL
                      AND lu.ts >= s.period_start AND lu.ts < s.period_end
                )::INT AS pages_used
         FROM subscriptions s
         LEFT JOIN users admin ON admin.id = s.created_by
         WHERE s.user_id = $1
         ORDER BY s.created_at DESC",
    );
    let subs = sqlx::query(&subs_sql)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;

    let tops_sql = util::row_query(
        "SELECT t.id, t.subscription_id, t.pages, t.note, t.created_at,
                admin.email AS created_by_email,
                s.period_start AS sub_period_start,
                s.period_end   AS sub_period_end,
                s.status       AS sub_status
         FROM topups t
         JOIN subscriptions s ON s.id = t.subscription_id
         LEFT JOIN users admin ON admin.id = t.created_by
         WHERE t.user_id = $1
         ORDER BY t.created_at DESC",
    );
    let tops = sqlx::query(&tops_sql)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;

    Ok(json!({
        "subscriptions": subs.into_iter().map(util::row_value).collect::<Vec<_>>(),
        "topups": tops.into_iter().map(util::row_value).collect::<Vec<_>>(),
    }))
}

/// Admin: per-client row with vendor_count and extraction_count.
pub async fn get_client_vendor_summary(pool: &PgPool) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT u.id::TEXT AS user_id, u.email, u.role, u.is_active,
                COUNT(DISTINCT v.id)::INT         AS vendor_count,
                COUNT(DISTINCT e.id)::INT         AS extraction_count,
                SUM(CASE WHEN e.status = 'done'   THEN 1 ELSE 0 END)::INT AS completed_extractions
         FROM users u
         LEFT JOIN vendors    v ON v.user_id = u.id
         LEFT JOIN extractions e ON e.vendor_id = v.id
         WHERE u.role = 'client'
         GROUP BY u.id, u.email, u.role, u.is_active
         ORDER BY u.email",
    );
    let rows = sqlx::query(&sql).fetch_all(pool).await.map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Vendor list for one client with extraction count, page total, and last run.
pub async fn get_client_vendors_with_stats(pool: &PgPool, user_id: &str) -> AppResult<Vec<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(Vec::new());
    };
    let sql = util::row_query(
        "SELECT v.id AS vendor_id, v.name AS vendor_name, v.status,
                COUNT(DISTINCT e.id)::INT              AS extraction_count,
                COALESCE(SUM(e.total_pages), 0)::INT  AS total_pages_processed,
                MAX(e.created_at)                      AS last_extraction_at,
                SUM(CASE WHEN e.status = 'done'   THEN 1 ELSE 0 END)::INT AS completed,
                SUM(CASE WHEN e.status = 'failed' THEN 1 ELSE 0 END)::INT AS failed
         FROM vendors v
         LEFT JOIN extractions e ON e.vendor_id = v.id
         WHERE v.user_id = $1
         GROUP BY v.id, v.name, v.status
         ORDER BY v.name",
    );
    let rows = sqlx::query(&sql)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Per-day extraction counts for one vendor (most recent N days).
pub async fn get_vendor_extraction_daily(pool: &PgPool, vendor_id: &str, limit: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT date_trunc('day', e.created_at)::DATE::TEXT AS day,
                COUNT(*)::INT                               AS extractions,
                SUM(CASE WHEN e.status = 'done'   THEN 1 ELSE 0 END)::INT AS completed,
                SUM(CASE WHEN e.status = 'failed' THEN 1 ELSE 0 END)::INT AS failed,
                COALESCE(SUM(e.total_pages), 0)::INT        AS total_pages
         FROM extractions e
         WHERE e.vendor_id = $1
           AND e.created_at >= NOW() - ($2 * INTERVAL '1 day')
         GROUP BY 1
         ORDER BY 1 DESC",
    );
    // `$2 * INTERVAL` resolves server-side to double precision.
    let rows = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(limit as f64)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Per page_number aggregates across all done extractions for a vendor.
pub async fn get_vendor_page_stats(pool: &PgPool, vendor_id: &str) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT p.page_number,
                COUNT(*)::INT                                   AS times_processed,
                AVG(NULLIF(lu.total_tokens,  0))::INT          AS avg_tokens,
                AVG(NULLIF(lu.duration_ms,   0))::INT          AS avg_latency_ms
         FROM pages p
         JOIN extractions e
              ON e.id = p.extraction_id
             AND e.vendor_id = $1
             AND e.status = 'done'
         LEFT JOIN llm_usage lu
              ON lu.extraction_id = e.id
             AND lu.page_num = p.page_number
             AND lu.call_type = 'extraction'
         GROUP BY p.page_number
         ORDER BY p.page_number",
    );
    let rows = sqlx::query(&sql)
        .bind(vendor_id)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Reserve an idempotency claim for (user, key): returns
/// {"status":"claimed","claim_id"} on first sight, {"status":"conflict"} when
/// the same key was used for a different file within 24h, or
/// {"status":"duplicate","extraction_id","extraction_status"} when identical.
/// Expired claims are deleted and re-claimed atomically.
pub async fn claim_idempotency(
    pool: &PgPool,
    user_id: &str,
    idempotency_key: &str,
    file_sha256: &str,
) -> AppResult<Value> {
    let uid: Option<Uuid> = util::uuid_or_none(Some(user_id));
    let inserted: Result<i32, sqlx::Error> = sqlx::query_scalar(
        "INSERT INTO idempotency_claims (user_id, idempotency_key, file_sha256)
         VALUES ($1, $2, $3) RETURNING id",
    )
    .bind(uid)
    .bind(idempotency_key)
    .bind(file_sha256)
    .fetch_one(pool)
    .await;

    match inserted {
        Ok(id) => Ok(json!({"status": "claimed", "claim_id": i64::from(id)})),
        Err(sqlx::Error::Database(db_err)) if db_err.code().as_deref() == Some("23505") => {
            let existing_sql = util::row_query(
                "SELECT ic.id, ic.file_sha256, ic.extraction_id, e.status
                 FROM idempotency_claims ic
                 LEFT JOIN extractions e ON e.id = ic.extraction_id
                 WHERE ic.user_id = $1 AND ic.idempotency_key = $2
                   AND ic.created_at > NOW() - INTERVAL '24 hours'",
            );
            let existing = sqlx::query(&existing_sql)
                .bind(uid)
                .bind(idempotency_key)
                .fetch_optional(pool)
                .await
                .map_err(AppError::from)?
                .map(util::row_value);

            let Some(existing) = existing else {
                // Expired claim: delete old and insert new inside one transaction.
                let mut tx = pool.begin().await.map_err(AppError::from)?;
                sqlx::query("DELETE FROM idempotency_claims WHERE user_id = $1 AND idempotency_key = $2")
                    .bind(uid)
                    .bind(idempotency_key)
                    .execute(&mut *tx)
                    .await
                    .map_err(AppError::from)?;
                let id: i32 = sqlx::query_scalar(
                    "INSERT INTO idempotency_claims (user_id, idempotency_key, file_sha256)
                     VALUES ($1, $2, $3) RETURNING id",
                )
                .bind(uid)
                .bind(idempotency_key)
                .bind(file_sha256)
                .fetch_one(&mut *tx)
                .await
                .map_err(AppError::from)?;
                tx.commit().await.map_err(AppError::from)?;
                return Ok(json!({"status": "claimed", "claim_id": i64::from(id)}));
            };

            let existing_sha = existing.get("file_sha256").and_then(Value::as_str);
            if existing_sha != Some(file_sha256) {
                return Ok(json!({"status": "conflict"}));
            }
            Ok(json!({
                "status": "duplicate",
                "extraction_id": existing.get("extraction_id").cloned().unwrap_or(Value::Null),
                "extraction_status": existing.get("status").cloned().unwrap_or(Value::Null),
            }))
        }
        Err(e) => Err(e.into()),
    }
}

/// Bind the artifacts produced under a claim so duplicates can be detected.
pub async fn bind_idempotency_claim(
    pool: &PgPool,
    claim_id: i64,
    extraction_id: i64,
    document_id: i64,
) -> AppResult<()> {
    sqlx::query("UPDATE idempotency_claims SET extraction_id=$2, document_id=$3 WHERE id=$1")
        .bind(claim_id as i32)
        .bind(extraction_id as i32)
        .bind(document_id as i32)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    Ok(())
}

/// Drop a user's idempotency claim (upload failed before completion).
pub async fn delete_idempotency_claim(
    pool: &PgPool,
    user_id: &str,
    idempotency_key: &str,
) -> AppResult<()> {
    sqlx::query("DELETE FROM idempotency_claims WHERE user_id=$1 AND idempotency_key=$2")
        .bind(util::uuid_or_none(Some(user_id)))
        .bind(idempotency_key)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    Ok(())
}

/// Submit a user-initiated top-up request for admin review.
pub async fn create_topup_request(
    pool: &PgPool,
    user_id: &str,
    requested_pages: i64,
    requested_period: &str,
    note: Option<&str>,
) -> AppResult<Value> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Err(AppError::BadRequest("Invalid user_id".to_string()));
    };
    let sql = returning_row_sql(
        "INSERT INTO topup_requests (user_id, requested_pages, requested_period, note)
         VALUES ($1, $2, $3, $4)
         RETURNING *",
    );
    let rec = sqlx::query(&sql)
        .bind(uid)
        .bind(requested_pages as i32)
        .bind(requested_period)
        .bind(note)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value).unwrap_or_else(|| json!({})))
}

/// List all top-up requests, optionally filtered by status, with user emails.
pub async fn list_topup_requests(pool: &PgPool, status: Option<&str>) -> AppResult<Vec<Value>> {
    let rows = match status.filter(|s| !s.is_empty()) {
        Some(status) => {
            let sql = util::row_query(
                "SELECT tr.*, u.email AS user_email,
                        ru.email AS resolved_by_email
                 FROM topup_requests tr
                 JOIN users u ON u.id = tr.user_id
                 LEFT JOIN users ru ON ru.id = tr.resolved_by
                 WHERE tr.status = $1
                 ORDER BY tr.created_at DESC",
            );
            sqlx::query(&sql)
                .bind(status)
                .fetch_all(pool)
                .await
                .map_err(AppError::from)?
        }
        None => {
            let sql = util::row_query(
                "SELECT tr.*, u.email AS user_email,
                        ru.email AS resolved_by_email
                 FROM topup_requests tr
                 JOIN users u ON u.id = tr.user_id
                 LEFT JOIN users ru ON ru.id = tr.resolved_by
                 ORDER BY tr.created_at DESC",
            );
            sqlx::query(&sql)
                .fetch_all(pool)
                .await
                .map_err(AppError::from)?
        }
    };
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// A user's own top-up requests, newest first.
pub async fn list_topup_requests_for_user(pool: &PgPool, user_id: &str) -> AppResult<Vec<Value>> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(Vec::new());
    };
    let sql = util::row_query(
        "SELECT tr.*, ru.email AS resolved_by_email
         FROM topup_requests tr
         LEFT JOIN users ru ON ru.id = tr.resolved_by
         WHERE tr.user_id = $1
         ORDER BY tr.created_at DESC",
    );
    let rows = sqlx::query(&sql)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Fetch one top-up request with joined user emails.
pub async fn get_topup_request(pool: &PgPool, request_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT tr.*, u.email AS user_email,
                ru.email AS resolved_by_email
         FROM topup_requests tr
         JOIN users u ON u.id = tr.user_id
         LEFT JOIN users ru ON ru.id = tr.resolved_by
         WHERE tr.id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(request_id as i32)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Resolve a still-pending top-up request ('approved' | 'rejected').
/// Returns None when the request does not exist or was already resolved.
pub async fn resolve_topup_request(
    pool: &PgPool,
    request_id: i64,
    resolved_by: &str,
    status: &str,
    resolution_note: Option<&str>,
) -> AppResult<Option<Value>> {
    let admin_uid: Option<Uuid> = util::uuid_or_none(Some(resolved_by));
    let sql = returning_row_sql(
        "UPDATE topup_requests
           SET status = $1,
               resolution_note = $2,
               resolved_by = $3,
               resolved_at = NOW()
         WHERE id = $4 AND status = 'pending'
         RETURNING *",
    );
    let rec = sqlx::query(&sql)
        .bind(status)
        .bind(resolution_note)
        .bind(admin_uid)
        .bind(request_id as i32)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Approve a pending top-up request and apply its pages in one transaction.
///
/// SELECT … FOR UPDATE on the request row serialises concurrent admin
/// approvals so a request can produce at most one top-up. All-or-nothing.
/// Fails with BadRequest("not_found"), BadRequest("already_<status>") or
/// BadRequest("no_active_subscription") — mirroring Python's ValueError.
pub async fn approve_topup_atomically(
    pool: &PgPool,
    request_id: i64,
    resolved_by: &str,
    resolution_note: Option<&str>,
) -> AppResult<Value> {
    let admin_uid: Option<Uuid> = util::uuid_or_none(Some(resolved_by));

    let mut tx = pool.begin().await.map_err(AppError::from)?;
    let req = sqlx::query("SELECT * FROM topup_requests WHERE id = $1 FOR UPDATE")
        .bind(request_id as i32)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let Some(req) = req else {
        return Err(AppError::BadRequest("not_found".to_string()));
    };
    let req_status: String = req.get("status");
    if req_status != "pending" {
        return Err(AppError::BadRequest(format!("already_{req_status}")));
    }

    let user_id: Uuid = req.get("user_id");
    let pages: i32 = req.get("requested_pages");

    // Expire any stale subscriptions for this user (same rule as reserve_quota).
    sqlx::query(EXPIRE_DUE_SQL)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;

    let sub = sqlx::query("SELECT id FROM subscriptions WHERE user_id = $1 AND status = 'active' LIMIT 1")
        .bind(user_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let Some(sub) = sub else {
        return Err(AppError::BadRequest("no_active_subscription".to_string()));
    };
    let sub_id: i32 = sub.get("id");

    let mut note = format!("Approved top-up request #{request_id}");
    if let Some(resolution_note) = resolution_note {
        note.push_str(": ");
        note.push_str(resolution_note);
    }

    let topup_sql = returning_row_sql(
        "INSERT INTO topups (user_id, subscription_id, pages, note, created_by)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING id, user_id, subscription_id, pages, note, created_by, created_at",
    );
    let topup_row = sqlx::query(&topup_sql)
        .bind(user_id)
        .bind(sub_id)
        .bind(pages)
        .bind(note)
        .bind(admin_uid)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;

    let resolved_sql = returning_row_sql(
        "UPDATE topup_requests
           SET status = 'approved',
               resolution_note = $1,
               resolved_by = $2,
               resolved_at = NOW()
         WHERE id = $3
         RETURNING *",
    );
    let resolved_row = sqlx::query(&resolved_sql)
        .bind(resolution_note)
        .bind(admin_uid)
        .bind(request_id as i32)
        .fetch_optional(&mut *tx)
        .await
        .map_err(AppError::from)?;

    tx.commit().await.map_err(AppError::from)?;

    Ok(json!({
        "request": resolved_row.map(util::row_value).unwrap_or(Value::Null),
        "topup": topup_row.map(|r| _row_subscription(util::row_value(r))).unwrap_or(Value::Null),
    }))
}

/// Count unresolved top-up requests for the admin notification badge.
pub async fn get_pending_topup_request_count(pool: &PgPool) -> AppResult<i64> {
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM topup_requests WHERE status = 'pending'")
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?
        .unwrap_or(0);
    Ok(count)
}
