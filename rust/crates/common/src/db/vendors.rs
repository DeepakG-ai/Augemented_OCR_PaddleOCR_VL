//! vendors.rs — vendors + vendor_aliases (db.py "Vendor queries" sections).
//!
//! Ownership lookups used by auth plus the vendor CRUD family
//! (`create_vendor_by_name`, `upsert_vendor`, `delete_vendor`,
//! `assign_vendor_owner`) and the alias CRUD/detection family.

use serde_json::Value;
use sqlx::PgPool;
use uuid::Uuid;

use super::util;
use crate::config::Config;
use crate::error::{AppError, AppResult};

/// Verbatim vendor projection shared by every read/write (db.py `_VENDOR_COLS`).
const VENDOR_COLS: &str = "id, name, status, user_id, client_seq, created_at";

fn cfg() -> &'static Config {
    Config::global()
}

/// Return the user_id that owns this vendor, or None if missing/unowned.
pub async fn get_vendor_owner(pool: &PgPool, vendor_id: &str) -> AppResult<Option<String>> {
    let sql = util::row_query("SELECT user_id FROM vendors WHERE id = $1");
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let Some(row) = rec.map(util::row_value) else {
        return Ok(None);
    };
    Ok(row
        .get("user_id")
        .and_then(Value::as_str)
        // to_jsonb renders uuid columns as strings; an empty/missing value
        // means an unowned legacy row — same as Python's None.
        .filter(|s| !s.is_empty())
        .map(str::to_string))
}

/// Return the vendor_id that owns this alias row, or None if alias missing.
pub async fn get_alias_vendor_id(pool: &PgPool, alias_id: i64) -> AppResult<Option<String>> {
    let sql = util::row_query("SELECT vendor_id FROM vendor_aliases WHERE id = $1");
    let rec = sqlx::query(&sql)
        .bind(alias_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec
        .map(util::row_value)
        .and_then(|row| row.get("vendor_id").and_then(Value::as_str).map(str::to_string)))
}

/// Fetch one vendor by id. Cached under "vendor:{id}".
pub async fn get_vendor(pool: &PgPool, vendor_id: &str) -> AppResult<Option<Value>> {
    let key = format!("vendor:{vendor_id}");
    super::cached_read(&key, cfg().cache_ttl_vendor, move || async move {
        let sql = util::row_query(&format!(
            "SELECT {VENDOR_COLS} FROM vendors WHERE id = $1"
        ));
        let rec = sqlx::query(&sql)
            .bind(vendor_id)
            .fetch_optional(pool)
            .await
            .map_err(crate::error::AppError::from)?;
        Ok(rec.map(util::row_value))
    })
    .await
}

/// List vendors newest-first, optionally scoped to one owner's uuid. Cached
/// under "vendors:list:{uid|all}".
pub async fn list_vendors(pool: &PgPool, user_id: Option<&str>) -> AppResult<Vec<Value>> {
    // Python relies on the $1::UUID cast to reject malformed ids; mirror that
    // by failing fast instead of silently widening the scope.
    let scope = match user_id {
        Some(id) => Some(util::uuid_or_bad(id)?),
        None => None,
    };
    let key = format!("vendors:list:{}", user_id.filter(|s| !s.is_empty()).unwrap_or("all"));
    let cached = super::cached_read(&key, cfg().cache_ttl_vendor, move || async move {
        let sql = util::row_query(&format!(
            "SELECT {VENDOR_COLS} FROM vendors
             WHERE ($1::UUID IS NULL OR user_id = $1)
             ORDER BY created_at DESC"
        ));
        let recs = sqlx::query(&sql)
            .bind(scope)
            .fetch_all(pool)
            .await
            .map_err(crate::error::AppError::from)?;
        Ok(Some(Value::Array(
            recs.into_iter().map(util::row_value).collect(),
        )))
    })
    .await?;
    Ok(cached.and_then(|v| v.as_array().cloned()).unwrap_or_default())
}

/// Transfer ownership of a vendor to a user; returns the updated row or None
/// when the vendor does not exist. Invalidates caches only on success.
pub async fn assign_vendor_owner(
    pool: &PgPool,
    vendor_id: &str,
    user_id: &str,
) -> AppResult<Option<Value>> {
    let sql = util::row_query(&format!(
        "WITH upd AS (
            UPDATE vendors SET user_id = $1::UUID
            WHERE id = $2
            RETURNING {VENDOR_COLS}
         ) SELECT * FROM upd"
    ));
    let rec = sqlx::query(&sql)
        .bind(user_id)
        .bind(vendor_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let Some(row) = rec.map(util::row_value) else {
        return Ok(None);
    };
    super::invalidate_vendor_cache(Some(vendor_id)).await;
    Ok(Some(row))
}

/// Next per-owner display number (client_seq is an INT column).
async fn _next_client_seq(pool: &PgPool, user_id: Option<Uuid>) -> AppResult<i32> {
    sqlx::query_scalar::<_, i32>(
        "SELECT COALESCE(MAX(client_seq), 0) + 1 FROM vendors
         WHERE user_id IS NOT DISTINCT FROM $1",
    )
    .bind(user_id)
    .fetch_one(pool)
    .await
    .map_err(crate::error::AppError::from)
}

/// Create a vendor from a name alone — the id is issued server-side from a
/// global sequence. Name is unique per owner (case-insensitive): re-submitting
/// an existing name returns that vendor instead of creating a duplicate.
pub async fn create_vendor_by_name(
    pool: &PgPool,
    name: &str,
    user_id: Option<&str>,
) -> AppResult<Value> {
    let uid = util::uuid_or_none(user_id);
    let existing_sql = util::row_query(&format!(
        "SELECT {VENDOR_COLS} FROM vendors
         WHERE user_id IS NOT DISTINCT FROM $1 AND lower(name) = lower($2)"
    ));
    if let Some(rec) = sqlx::query(&existing_sql)
        .bind(uid)
        .bind(name)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?
    {
        return Ok(util::row_value(rec));
    }
    let new_id = sqlx::query_scalar::<_, i64>("SELECT nextval('vendors_global_id_seq')")
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?
        .to_string();
    let seq = _next_client_seq(pool, uid).await?;
    let insert_sql = util::row_query(&format!(
        "WITH ins AS (
            INSERT INTO vendors (id, name, user_id, client_seq)
            VALUES ($1, $2, $3, $4)
            RETURNING {VENDOR_COLS}
         ) SELECT * FROM ins"
    ));
    match sqlx::query(&insert_sql)
        .bind(&new_id)
        .bind(name)
        .bind(uid)
        .bind(seq)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(rec)) => {
            let created = util::row_value(rec);
            super::invalidate_vendor_cache(created.get("id").and_then(Value::as_str)).await;
            Ok(created)
        }
        // INSERT..RETURNING without ON CONFLICT always yields exactly one row.
        Ok(None) => Err(AppError::Internal("vendors INSERT returned no rows".into())),
        Err(sqlx::Error::Database(ref db_err))
            if db_err.code().as_deref() == Some("23505") =>
        {
            // Concurrent create of the same name lost the race — return winner.
            let rec = sqlx::query(&existing_sql)
                .bind(uid)
                .bind(name)
                .fetch_optional(pool)
                .await
                .map_err(crate::error::AppError::from)?
                .ok_or_else(|| AppError::NotFound("vendor lost in concurrent create".into()))?;
            Ok(util::row_value(rec))
        }
        Err(e) => Err(e.into()),
    }
}

/// Upsert by explicit id. Used by the template side-door where the vendor id
/// already exists (issued earlier by create_vendor_by_name). Backfills
/// client_seq if the row is created here.
pub async fn upsert_vendor(
    pool: &PgPool,
    vendor_id: &str,
    name: &str,
    user_id: Option<&str>,
) -> AppResult<Value> {
    let uid = util::uuid_or_none(user_id);
    let seq = _next_client_seq(pool, uid).await?;
    let sql = util::row_query(&format!(
        "WITH up AS (
            INSERT INTO vendors (id, name, user_id, client_seq)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                user_id = COALESCE(EXCLUDED.user_id, vendors.user_id),
                client_seq = COALESCE(vendors.client_seq, EXCLUDED.client_seq)
            RETURNING {VENDOR_COLS}
         ) SELECT * FROM up"
    ));
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(name)
        .bind(uid)
        .bind(seq)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let result = util::row_value(rec);
    super::invalidate_vendor_cache(result.get("id").and_then(Value::as_str)).await;
    Ok(result)
}

/// Delete a vendor and dependent rows even on schemas without FK cascades.
pub async fn delete_vendor(pool: &PgPool, vendor_id: &str) -> AppResult<bool> {
    let mut tx = pool.begin().await.map_err(crate::error::AppError::from)?;
    // Some deployments still have vendor foreign keys without ON DELETE
    // CASCADE, so delete dependents explicitly before removing the vendor.
    for stmt in [
        "DELETE FROM gold_examples WHERE vendor_id = $1",
        "DELETE FROM extractions WHERE vendor_id = $1",
        "DELETE FROM documents WHERE vendor_id = $1",
        "DELETE FROM vendor_aliases WHERE vendor_id = $1",
        "DELETE FROM templates WHERE vendor_id = $1",
        "DELETE FROM spatial_memory WHERE vendor_id = $1",
        "DELETE FROM qwen_layout_boxes WHERE vendor_id = $1",
    ] {
        sqlx::query(stmt)
            .bind(vendor_id)
            .execute(&mut *tx)
            .await
            .map_err(crate::error::AppError::from)?;
    }
    let result = sqlx::query("DELETE FROM vendors WHERE id = $1")
        .bind(vendor_id)
        .execute(&mut *tx)
        .await
        .map_err(crate::error::AppError::from)?;
    tx.commit().await.map_err(crate::error::AppError::from)?;
    let deleted = result.rows_affected() == 1;
    if deleted {
        super::invalidate_vendor_cache(Some(vendor_id)).await;
    }
    Ok(deleted)
}

// -- Vendor alias queries ---------------------------------------------------

/// Insert a vendor alias pattern. Returns the row or None on conflict.
pub async fn insert_vendor_alias(
    pool: &PgPool,
    vendor_id: &str,
    pattern: &str,
    weight: i32,
    source: &str,
) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "WITH ins AS (
            INSERT INTO vendor_aliases (vendor_id, pattern, weight, source)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (vendor_id, pattern) DO NOTHING
            RETURNING id, vendor_id, pattern, weight, source, created_at
         ) SELECT * FROM ins",
    );
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(pattern.trim())
        .bind(weight)
        .bind(source)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let Some(row) = rec.map(util::row_value) else {
        return Ok(None);
    };
    super::invalidate_alias_cache(vendor_id).await;
    Ok(Some(row))
}

/// List vendor aliases with vendor names joined in, optionally filtered to one
/// vendor. Cached under "aliases:list:{vid|all}".
pub async fn list_vendor_aliases(pool: &PgPool, vendor_id: Option<&str>) -> AppResult<Vec<Value>> {
    let scope = vendor_id.filter(|v| !v.is_empty()).map(str::to_string);
    let key = format!("aliases:list:{}", scope.as_deref().unwrap_or("all"));
    let cached = super::cached_read(&key, cfg().cache_ttl_alias, move || async move {
        let recs = if let Some(vid) = scope.as_deref() {
            let sql = util::row_query(
                "SELECT va.id, va.vendor_id, v.name AS vendor_name, va.pattern, va.weight, va.source, va.created_at
                 FROM vendor_aliases va
                 JOIN vendors v ON v.id = va.vendor_id
                 WHERE va.vendor_id = $1
                 ORDER BY va.weight DESC, va.created_at ASC",
            );
            sqlx::query(&sql)
                .bind(vid)
                .fetch_all(pool)
                .await
                .map_err(crate::error::AppError::from)?
        } else {
            let sql = util::row_query(
                "SELECT va.id, va.vendor_id, v.name AS vendor_name, va.pattern, va.weight, va.source, va.created_at
                 FROM vendor_aliases va
                 JOIN vendors v ON v.id = va.vendor_id
                 ORDER BY va.vendor_id, va.weight DESC, va.created_at ASC",
            );
            sqlx::query(&sql)
                .fetch_all(pool)
                .await
                .map_err(crate::error::AppError::from)?
        };
        Ok(Some(Value::Array(
            recs.into_iter().map(util::row_value).collect(),
        )))
    })
    .await?;
    Ok(cached.and_then(|v| v.as_array().cloned()).unwrap_or_default())
}

/// Delete a single vendor alias by id; invalidates the alias cache for its
/// vendor when a row was removed.
pub async fn delete_vendor_alias(pool: &PgPool, alias_id: i64) -> AppResult<bool> {
    let vendor_id = sqlx::query_scalar::<_, String>(
        "DELETE FROM vendor_aliases WHERE id = $1 RETURNING vendor_id",
    )
    .bind(alias_id)
    .fetch_optional(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    let Some(vendor_id) = vendor_id else {
        return Ok(false);
    };
    super::invalidate_alias_cache(&vendor_id).await;
    Ok(true)
}

/// Load vendor aliases for detection (excluding the reserved `_auto` vendor),
/// optionally scoped to a single user's vendors. Cached under
/// "aliases:detect:{uid|all}".
pub async fn get_all_aliases_for_detection(
    pool: &PgPool,
    user_id: Option<&str>,
) -> AppResult<Vec<Value>> {
    let scope = match user_id {
        Some(id) => Some(util::uuid_or_bad(id)?),
        None => None,
    };
    let key = format!("aliases:detect:{}", user_id.filter(|s| !s.is_empty()).unwrap_or("all"));
    let cached = super::cached_read(&key, cfg().cache_ttl_alias, move || async move {
        let sql = util::row_query(
            "SELECT va.vendor_id, v.name AS vendor_name, va.pattern, va.weight, va.source
             FROM vendor_aliases va
             JOIN vendors v ON v.id = va.vendor_id
             WHERE va.vendor_id <> '_auto'
               AND ($1::UUID IS NULL OR v.user_id = $1)
             ORDER BY va.vendor_id, va.weight DESC",
        );
        let recs = sqlx::query(&sql)
            .bind(scope)
            .fetch_all(pool)
            .await
            .map_err(crate::error::AppError::from)?;
        Ok(Some(Value::Array(
            recs.into_iter().map(util::row_value).collect(),
        )))
    })
    .await?;
    Ok(cached.and_then(|v| v.as_array().cloned()).unwrap_or_default())
}
