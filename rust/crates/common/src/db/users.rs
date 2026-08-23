//! users.rs — users + API keys (db.py "User queries" / "API keys" sections).
//!
//! Auth-critical lookups (`get_user_by_id`, `verify_api_key_hash`,
//! `touch_api_key`) plus the user CRUD family, subscription-limit updates and
//! the api-key CRUD family.

use serde_json::{json, Value};
use sqlx::PgPool;

use super::util;
use crate::config::Config;
use crate::error::AppResult;

fn cfg() -> &'static Config {
    Config::global()
}

pub async fn get_user_by_id(pool: &PgPool, user_id: &str) -> AppResult<Option<Value>> {
    let Some(user_uuid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(None);
    };
    let key = format!("user:{user_uuid}");
    super::cached_read(&key, cfg().cache_ttl_auth, move || async move {
        let sql = util::row_query(
            "SELECT id, email, role, is_active, created_at, subscription_limit
             FROM users WHERE id = $1",
        );
        let rec = sqlx::query(&sql)
            .bind(user_uuid)
            .fetch_optional(pool)
            .await
            .map_err(crate::error::AppError::from)?;
        Ok(rec.map(util::row_value))
    })
    .await
}

/// Look up an active API key by SHA-256 hash. Cached positive-only, TTL capped
/// at the key's own expiry so an entry can never outlive its key.
pub async fn verify_api_key_hash(pool: &PgPool, key_hash: &str) -> AppResult<Option<Value>> {
    use crate::cache::get_cache;

    let cache = get_cache();
    let ckey = format!("apikey:{key_hash}");
    if let Some(cached) = cache.get(&ckey).await {
        return Ok(Some(cached));
    }

    let sql = util::row_query(
        "SELECT id, user_id, is_active, expires_at
         FROM api_keys
         WHERE key_hash = $1
           AND (expires_at IS NULL OR expires_at > NOW())",
    );
    let rec = sqlx::query(&sql)
        .bind(key_hash)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;

    let Some(row) = rec.map(util::row_value) else {
        return Ok(None);
    };

    let mut ttl_secs = cfg().cache_ttl_auth;
    if let Some(expires_raw) = row.get("expires_at").cloned() {
        if !expires_raw.is_null() {
            // Column renders as ISO-8601 through to_jsonb. A malformed value
            // must never break the auth path — treat it as no expiry.
            if let Ok(expires_at) =
                chrono::DateTime::parse_from_rfc3339(expires_raw.as_str().unwrap_or_default())
            {
                let secs = (expires_at.with_timezone(&chrono::Utc) - chrono::Utc::now()).num_seconds();
                if secs <= 0 {
                    // Expired inside the race window — return but don't cache.
                    return Ok(Some(row));
                }
                ttl_secs = 1.max(cfg().cache_ttl_auth.min(secs));
            }
        }
    }
    cache.set(&ckey, &row, Some(ttl_secs)).await;
    Ok(Some(row))
}

/// Update last_used_at at most once per key per window. A marker key in the
/// cache suppresses repeat writes; with no Redis this writes every call.
pub async fn touch_api_key(pool: &PgPool, key_hash: &str) -> AppResult<()> {
    use crate::cache::get_cache;

    let cache = get_cache();
    let tkey = format!("apikey_touch:{key_hash}");
    if cache.get(&tkey).await.is_some() {
        return Ok(());
    }
    cache.set(&tkey, &json!(1), Some(cfg().cache_ttl_apikey_touch)).await;

    sqlx::query("UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = $1")
        .bind(key_hash)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(())
}

/// True when a cached user record says the account is active (default true),
/// mirroring Python's `record.get("is_active", True)` semantics.
pub fn user_is_active(record: &Value) -> bool {
    record.get("is_active").and_then(Value::as_bool).unwrap_or(true)
}

/// Create a user account. Email is stored lower-cased/trimmed; the limit
/// defaults to DEFAULT_SUBSCRIPTION_LIMIT when not supplied.
pub async fn create_user(
    pool: &PgPool,
    email: &str,
    hashed_pw: &str,
    role: &str,
    subscription_limit: Option<i32>,
) -> AppResult<Value> {
    let limit = subscription_limit.unwrap_or(cfg().default_subscription_limit as i32);
    let sql = util::row_query(
        "WITH ins AS (
            INSERT INTO users (email, hashed_pw, role, subscription_limit)
            VALUES ($1, $2, $3, $4)
            RETURNING id, email, role, is_active, created_at, subscription_limit
         ) SELECT * FROM ins",
    );
    let rec = sqlx::query(&sql)
        .bind(email.trim().to_lowercase())
        .bind(hashed_pw)
        .bind(role)
        .bind(limit)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(util::row_value(rec))
}

/// Fetch a user by exact (post-normalisation) email, including the password
/// hash. Uncached on purpose — the cached variant must never carry secrets.
pub async fn get_user_by_email(pool: &PgPool, email: &str) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT id, email, hashed_pw, role, is_active, created_at, subscription_limit
         FROM users WHERE email = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(email.trim().to_lowercase())
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Admin listing: every user joined with their active subscription, top-up
/// total and pages billed inside the current period. Uncached (admin-only).
pub async fn list_users(pool: &PgPool) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT
            u.id, u.email, u.role, u.is_active, u.created_at, u.subscription_limit,
            COALESCE(u.pending_pages, 0)::INT AS pending_pages,
            s.id AS sub_id,
            s.page_limit AS base_limit,
            s.period_start,
            s.period_end,
            s.status AS sub_status,
            COALESCE(
                (SELECT SUM(t.pages) FROM topups t WHERE t.subscription_id = s.id),
                0
            )::INT AS topup_total,
            COALESCE(
                (
                    SELECT COUNT(DISTINCT (lu.extraction_id, lu.page_num))
                    FROM llm_usage lu
                    WHERE lu.user_id = u.id
                      AND lu.call_type = 'extraction'
                      AND lu.page_num IS NOT NULL
                      AND lu.ts >= s.period_start AND lu.ts < s.period_end
                ),
                0
            )::INT AS used
        FROM users u
        LEFT JOIN subscriptions s ON s.user_id = u.id AND s.status = 'active'
        ORDER BY u.created_at DESC",
    );
    let recs = sqlx::query(&sql)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(recs.into_iter().map(util::row_value).collect())
}

/// Soft-disable a login (is_active = FALSE). False when the id is malformed
/// or no row matched.
pub async fn deactivate_user(pool: &PgPool, user_id: &str) -> AppResult<bool> {
    let Some(user_uuid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(false);
    };
    let result = sqlx::query("UPDATE users SET is_active = FALSE WHERE id = $1")
        .bind(user_uuid)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_user_cache(Some(&user_uuid.to_string())).await;
    Ok(result.rows_affected() == 1)
}

/// Re-enable a previously deactivated login. False when the id is malformed
/// or no row matched.
pub async fn reactivate_user(pool: &PgPool, user_id: &str) -> AppResult<bool> {
    let Some(user_uuid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(false);
    };
    let result = sqlx::query("UPDATE users SET is_active = TRUE WHERE id = $1")
        .bind(user_uuid)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_user_cache(Some(&user_uuid.to_string())).await;
    Ok(result.rows_affected() == 1)
}

/// Permanently remove the account row. False when the id is malformed or no
/// row was deleted.
pub async fn hard_delete_user(pool: &PgPool, user_id: &str) -> AppResult<bool> {
    let Some(user_uuid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(false);
    };
    let result = sqlx::query("DELETE FROM users WHERE id = $1")
        .bind(user_uuid)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_user_cache(Some(&user_uuid.to_string())).await;
    Ok(result.rows_affected() == 1)
}

/// Replace a user's password hash. False when the id is malformed or no row
/// matched.
pub async fn reset_user_password(pool: &PgPool, user_id: &str, hashed_pw: &str) -> AppResult<bool> {
    let Some(user_uuid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(false);
    };
    let result = sqlx::query("UPDATE users SET hashed_pw = $1 WHERE id = $2")
        .bind(hashed_pw)
        .bind(user_uuid)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_user_cache(Some(&user_uuid.to_string())).await;
    Ok(result.rows_affected() == 1)
}

/// Update the page quota ceiling for a user (admin-only at runtime). False
/// when the id is malformed or no row matched.
pub async fn update_user_subscription_limit(
    pool: &PgPool,
    user_id: &str,
    new_limit: i32,
) -> AppResult<bool> {
    let Some(uid) = util::uuid_or_none(Some(user_id)) else {
        return Ok(false);
    };
    let result = sqlx::query("UPDATE users SET subscription_limit = $1 WHERE id = $2")
        .bind(new_limit)
        .bind(uid)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    // subscription_limit is part of the cached user row.
    super::invalidate_user_cache(Some(&uid.to_string())).await;
    Ok(result.rows_affected() == 1)
}

// -- API Key CRUD -----------------------------------------------------------

/// Fetch the encrypted raw key material for admin reveal.
pub async fn get_api_key_encrypted(pool: &PgPool, key_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT id, label, encrypted_key FROM api_keys WHERE id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(key_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Insert a new API key row and return the created record.
pub async fn create_api_key(
    pool: &PgPool,
    user_id: &str,
    label: &str,
    key_hash: &str,
    prefix: &str,
    encrypted_key: Option<&str>,
    expires_at: Option<chrono::DateTime<chrono::Utc>>,
) -> AppResult<Value> {
    let uid = util::uuid_or_bad(user_id)?;
    let sql = util::row_query(
        "WITH ins AS (
            INSERT INTO api_keys (user_id, label, key_hash, prefix, encrypted_key, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, user_id, label, key_hash, prefix, is_active, created_at, last_used_at, expires_at
         ) SELECT * FROM ins",
    );
    let rec = sqlx::query(&sql)
        .bind(uid)
        .bind(label)
        .bind(key_hash)
        .bind(prefix)
        .bind(encrypted_key)
        .bind(expires_at)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    // An INSERT..RETURNING always yields a row; the empty-object branch only
    // mirrors Python's defensive `if row else {}`.
    Ok(rec.map(util::row_value).unwrap_or_else(|| json!({})))
}

/// List every API key with owner email and lifetime token/page/document usage
/// aggregates. Uncached (admin-only).
pub async fn list_api_keys(pool: &PgPool) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT ak.id, ak.user_id, ak.label, ak.prefix, ak.is_active,
                ak.created_at, ak.last_used_at, ak.expires_at,
                u.email AS owner_email,
                COALESCE(SUM(lu.prompt_tokens), 0)::BIGINT AS total_input_tokens,
                COALESCE(SUM(lu.completion_tokens), 0)::BIGINT AS total_output_tokens,
                COALESCE(SUM(lu.total_tokens), 0)::BIGINT AS total_tokens,
                COALESCE(COUNT(DISTINCT (lu.extraction_id, lu.page_num)) FILTER (WHERE lu.call_type = 'extraction' AND lu.page_num IS NOT NULL), 0)::BIGINT AS total_pages,
                COUNT(DISTINCT lu.doc_id)::INT AS total_documents
         FROM api_keys ak
         JOIN users u ON u.id = ak.user_id
         LEFT JOIN llm_usage lu ON lu.api_key_id = ak.id
         GROUP BY ak.id, ak.user_id, ak.label, ak.prefix, ak.is_active,
                  ak.created_at, ak.last_used_at, ak.expires_at, u.email
         ORDER BY ak.created_at DESC",
    );
    let recs = sqlx::query(&sql)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(recs.into_iter().map(util::row_value).collect())
}

/// Soft-disable an API key (auth checks fail immediately after invalidation).
pub async fn deactivate_api_key(pool: &PgPool, key_id: i64) -> AppResult<bool> {
    let result = sqlx::query("UPDATE api_keys SET is_active = FALSE WHERE id = $1")
        .bind(key_id)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_api_key_cache().await;
    Ok(result.rows_affected() == 1)
}

/// Re-enable a previously deactivated API key.
pub async fn activate_api_key(pool: &PgPool, key_id: i64) -> AppResult<bool> {
    let result = sqlx::query("UPDATE api_keys SET is_active = TRUE WHERE id = $1")
        .bind(key_id)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_api_key_cache().await;
    Ok(result.rows_affected() == 1)
}

/// Hard-delete an API key row. False when no row was removed.
pub async fn delete_api_key(pool: &PgPool, key_id: i64) -> AppResult<bool> {
    let result = sqlx::query("DELETE FROM api_keys WHERE id = $1")
        .bind(key_id)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    super::invalidate_api_key_cache().await;
    Ok(result.rows_affected() == 1)
}
