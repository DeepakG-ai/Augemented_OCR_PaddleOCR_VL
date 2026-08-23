//! spatial.rs — spatial memory, Qwen layout boxes and gold correction
//! examples (db.py "Spatial memory queries", "Qwen layout boxes" and the
//! gold-example sections). None of these rows are cached in Python; reads go
//! straight to Postgres.

use serde_json::{json, Map, Value};
use sqlx::{PgConnection, PgPool};

use super::util;
use crate::error::AppResult;
use crate::pyjson::truthy;

/// Insert or update a spatial memory entry. Last write wins on conflict.
#[allow(clippy::too_many_arguments)] // mirrors the spatial_memory table's columns
pub async fn upsert_spatial_memory(
    pool: &PgPool,
    vendor_id: &str,
    layout_key: &str,
    field_key: &str,
    page_number: i32,
    normalized_box: &Value,
    source_engine: &str,
    created_from_extraction_id: Option<i32>,
) -> AppResult<Value> {
    let sql = util::row_query(
        "WITH up AS (
            INSERT INTO spatial_memory
                (vendor_id, layout_key, field_key, page_number, normalized_box,
                 source_engine, created_from_extraction_id, last_verified_at, is_active)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, NOW(), TRUE)
            ON CONFLICT (vendor_id, layout_key, field_key, page_number) DO UPDATE SET
                normalized_box = EXCLUDED.normalized_box,
                source_engine = EXCLUDED.source_engine,
                created_from_extraction_id = EXCLUDED.created_from_extraction_id,
                last_verified_at = NOW(),
                is_active = TRUE
            RETURNING id, vendor_id, layout_key, field_key, page_number,
                      normalized_box, source_engine, created_from_extraction_id,
                      last_verified_at, is_active
         ) SELECT * FROM up",
    );
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(layout_key)
        .bind(field_key)
        .bind(page_number)
        .bind(normalized_box)
        .bind(source_engine)
        .bind(created_from_extraction_id)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(util::row_value(rec))
}

/// Load all active spatial memory entries for a vendor+layout.
pub async fn get_spatial_memory_for_layout(
    pool: &PgPool,
    vendor_id: &str,
    layout_key: &str,
) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT id, vendor_id, layout_key, field_key, page_number,
                normalized_box, source_engine, created_from_extraction_id,
                last_verified_at, is_active
         FROM spatial_memory
         WHERE vendor_id = $1 AND layout_key = $2 AND is_active = TRUE
         ORDER BY field_key, page_number",
    );
    let recs = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(layout_key)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(recs.into_iter().map(util::row_value).collect())
}

/// Deactivate spatial memory entries. If field_key is None/empty, deactivate
/// all entries for the layout. Returns the number of rows updated.
pub async fn deactivate_spatial_memory(
    pool: &PgPool,
    vendor_id: &str,
    layout_key: &str,
    field_key: Option<&str>,
) -> AppResult<i64> {
    // Python's `if field_key:` treats an empty string as "whole layout".
    let result = if field_key.is_some_and(|k| !k.is_empty()) {
        sqlx::query(
            "UPDATE spatial_memory SET is_active = FALSE
             WHERE vendor_id = $1 AND layout_key = $2 AND field_key = $3",
        )
        .bind(vendor_id)
        .bind(layout_key)
        .bind(field_key)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?
    } else {
        sqlx::query(
            "UPDATE spatial_memory SET is_active = FALSE
             WHERE vendor_id = $1 AND layout_key = $2",
        )
        .bind(vendor_id)
        .bind(layout_key)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?
    };
    Ok(result.rows_affected() as i64)
}

/// Fetch a single spatial memory entry by primary key (active or inactive).
pub async fn get_spatial_memory_by_id(pool: &PgPool, sm_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT id, vendor_id, layout_key, field_key, page_number,
                normalized_box, source_engine, created_from_extraction_id,
                last_verified_at, is_active
         FROM spatial_memory
         WHERE id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(sm_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Remove one field from every gold example of a vendor, deleting examples
/// whose diff became empty. Runs on the caller's connection so it can join a
/// transaction. Returns the number of updated rows.
async fn _delete_gold_correction_field_conn(
    conn: &mut PgConnection,
    vendor_id: &str,
    field_key: &str,
) -> AppResult<i64> {
    let key = field_key.trim();
    if vendor_id.is_empty() || key.is_empty() {
        return Ok(0);
    }
    use sqlx::Row;
    let rows = sqlx::query(
        "UPDATE gold_examples
            SET correction_diff = correction_diff - $2::TEXT
          WHERE vendor_id = $1
            AND correction_diff IS NOT NULL
            AND correction_diff ? $2::TEXT
        RETURNING id, correction_diff",
    )
    .bind(vendor_id)
    .bind(key)
    .fetch_all(&mut *conn)
    .await?;
    let mut empty_ids: Vec<i32> = Vec::new();
    for row in &rows {
        // JSONB decodes natively; Python's str-parse branch is unnecessary.
        let diff: Option<Value> = row.try_get("correction_diff")?;
        let is_empty_object =
            matches!(diff.as_ref(), Some(d) if d.as_object().is_some_and(Map::is_empty));
        if is_empty_object {
            empty_ids.push(row.try_get("id")?);
        }
    }
    if !empty_ids.is_empty() {
        sqlx::query("DELETE FROM gold_examples WHERE id = ANY($1::INT[])")
            .bind(&empty_ids)
            .execute(&mut *conn)
            .await?;
    }
    Ok(rows.len() as i64)
}

/// Hard-delete a spatial memory entry by ID. Returns the deleted row or None.
/// Optionally strips the same field from the vendor's gold corrections inside
/// the same transaction.
pub async fn delete_spatial_memory_by_id(
    pool: &PgPool,
    sm_id: i64,
    delete_gold_correction: bool,
) -> AppResult<Option<Value>> {
    let mut tx = pool.begin().await.map_err(crate::error::AppError::from)?;
    let sql = util::row_query(
        "WITH del AS (
            DELETE FROM spatial_memory
             WHERE id = $1
            RETURNING id, vendor_id, layout_key, field_key, page_number
         ) SELECT * FROM del",
    );
    let rec = sqlx::query(&sql)
        .bind(sm_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(crate::error::AppError::from)?;
    let Some(result) = rec.map(util::row_value) else {
        tx.commit().await.map_err(crate::error::AppError::from)?;
        return Ok(None);
    };
    let mut result = result;
    if delete_gold_correction {
        let vendor_id = result
            .get("vendor_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let field_key = result
            .get("field_key")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let deleted =
            _delete_gold_correction_field_conn(&mut tx, &vendor_id, &field_key).await?;
        result["gold_correction_fields_deleted"] = json!(deleted);
    }
    tx.commit().await.map_err(crate::error::AppError::from)?;
    Ok(Some(result))
}

/// List all active spatial memory entries for a vendor (all layouts).
pub async fn list_spatial_memory_for_vendor(
    pool: &PgPool,
    vendor_id: &str,
) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT id, vendor_id, layout_key, field_key, page_number,
                normalized_box, source_engine, created_from_extraction_id,
                last_verified_at, is_active
         FROM spatial_memory
         WHERE vendor_id = $1 AND is_active = TRUE
         ORDER BY layout_key, field_key, page_number",
    );
    let recs = sqlx::query(&sql)
        .bind(vendor_id)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(recs.into_iter().map(util::row_value).collect())
}

/// Admin: list all active spatial memory entries across all vendors with
/// vendor name and client email joined in.
pub async fn list_spatial_memory_all(
    pool: &PgPool,
    limit: i64,
    offset: i64,
) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT sm.id, sm.vendor_id, v.name AS vendor_name,
                sm.layout_key, sm.field_key, sm.page_number,
                sm.normalized_box, sm.source_engine,
                sm.created_from_extraction_id,
                sm.last_verified_at, sm.is_active,
                u.email AS client_email
         FROM spatial_memory sm
         LEFT JOIN vendors v ON v.id = sm.vendor_id
         LEFT JOIN users u ON u.id = v.user_id
         WHERE sm.is_active = TRUE
         ORDER BY u.email NULLS LAST, v.name, sm.field_key, sm.page_number
         LIMIT $1 OFFSET $2",
    );
    let recs = sqlx::query(&sql)
        .bind(limit)
        .bind(offset)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(recs.into_iter().map(util::row_value).collect())
}

/// Admin: total count of active spatial memory entries across all vendors.
pub async fn count_spatial_memory_all(pool: &PgPool) -> AppResult<i64> {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM spatial_memory WHERE is_active = TRUE")
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)
}

// -- Qwen layout boxes (auto-learned label geometry) ------------------------

/// Return all Qwen-learned label boxes for (vendor, template), keyed by
/// field_key as a JSON object.
pub async fn get_qwen_layout_boxes(
    pool: &PgPool,
    vendor_id: &str,
    template_id: i32,
) -> AppResult<Value> {
    let sql = util::row_query(
        "SELECT id, vendor_id, template_id, field_key, field_type,
                normalized_box, page_number, created_from_extraction_id,
                created_at, updated_at
         FROM qwen_layout_boxes
         WHERE vendor_id = $1 AND template_id = $2
         ORDER BY field_key",
    );
    let recs = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(template_id)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let mut out = Map::new();
    for rec in recs {
        let row = util::row_value(rec);
        // field_key is NOT NULL — a missing key cannot happen; default keeps
        // the mapping total like Python's dict indexing would require.
        let field_key = row
            .get("field_key")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        out.insert(field_key, row);
    }
    Ok(Value::Object(out))
}

/// Upsert one row per learned field. `learned` shape:
/// `{"field_key": {"normalized_box": {"x0":..,"y0":..,"x1":..,"y1":..},
///  "field_type": "header"|"line_item_column"}}`.
/// Accepts the legacy 4-element `"box"` list format and JSON-null boxes
/// (a missing field marker). Returns the number of rows written.
pub async fn upsert_qwen_layout_boxes(
    pool: &PgPool,
    vendor_id: &str,
    template_id: i32,
    extraction_id: Option<i32>,
    learned: &Value,
) -> AppResult<i64> {
    let Some(entries) = learned.as_object() else {
        return Ok(0);
    };
    if entries.is_empty() {
        return Ok(0);
    }
    let mut written: i64 = 0;
    let mut tx = pool.begin().await.map_err(crate::error::AppError::from)?;
    for (field_key, entry) in entries {
        // Support both old "box" (list) and new "normalized_box" (dict);
        // null signifies a not-found field so the agent stops re-searching.
        let raw_box = if entry.get("normalized_box").is_some() {
            entry.get("normalized_box")
        } else {
            entry.get("box")
        };
        let field_type = entry.get("field_type").and_then(Value::as_str);
        if field_type != Some("header") && field_type != Some("line_item_column") {
            continue;
        }
        // Ensure stored as dict format {x0, y0, x1, y1}.
        let nbox = match raw_box {
            Some(Value::Array(items)) if items.len() == 4 => json!({
                "x0": items[0], "y0": items[1], "x1": items[2], "y1": items[3],
            }),
            other => other.cloned().unwrap_or(Value::Null),
        };
        sqlx::query(
            "INSERT INTO qwen_layout_boxes
                (vendor_id, template_id, field_key, field_type,
                 normalized_box, page_number, created_from_extraction_id,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, 1, $6, NOW(), NOW())
            ON CONFLICT (vendor_id, template_id, field_key) DO UPDATE SET
                field_type = EXCLUDED.field_type,
                normalized_box = EXCLUDED.normalized_box,
                created_from_extraction_id = EXCLUDED.created_from_extraction_id,
                updated_at = NOW()",
        )
        .bind(vendor_id)
        .bind(template_id)
        .bind(field_key)
        .bind(field_type)
        .bind(nbox)
        .bind(extraction_id)
        .execute(&mut *tx)
        .await?;
        written += 1;
    }
    tx.commit().await.map_err(crate::error::AppError::from)?;
    Ok(written)
}

// -- Gold correction examples ------------------------------------------------

/// Store a human-verified correction as audit history. An absent/empty
/// correction diff is stored as SQL NULL. Prompt builders must redact field
/// values before using this data.
pub async fn save_gold_example(
    pool: &PgPool,
    vendor_id: &str,
    extraction_id: i32,
    original_result: &Value,
    corrected_result: &Value,
    correction_diff: Option<&Value>,
) -> AppResult<i64> {
    let diff_param = correction_diff.filter(|v| truthy(v));
    let sql = util::row_query(
        "WITH ins AS (
            INSERT INTO gold_examples
                (vendor_id, extraction_id, original_result, corrected_result, correction_diff)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)
            RETURNING id
         ) SELECT id FROM ins",
    );
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(extraction_id)
        .bind(original_result)
        .bind(corrected_result)
        .bind(diff_param)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(util::row_value(rec)
        .get("id")
        .and_then(Value::as_i64)
        .unwrap_or_default())
}

/// Remove a field from all prompt correction examples for a vendor. Removing
/// every occurrence prevents get_gold_examples from falling back to an older
/// correction for the same field after the latest one is deleted.
pub async fn delete_gold_correction_field(
    pool: &PgPool,
    vendor_id: &str,
    field_key: &str,
) -> AppResult<i64> {
    let mut tx = pool.begin().await.map_err(crate::error::AppError::from)?;
    let removed = _delete_gold_correction_field_conn(&mut tx, vendor_id, field_key).await?;
    tx.commit().await.map_err(crate::error::AppError::from)?;
    Ok(removed)
}

/// Retrieve the latest correction per field for value-redacted prompt hints.
///
/// gold_examples stays append-only for audit history; this consolidates each
/// field to its latest saved correction and returns a single object under
/// "correction_diff". Returns an empty vec when there are no corrections.
/// Callers must not expose the raw values to the model.
pub async fn get_gold_examples(
    pool: &PgPool,
    vendor_id: &str,
    limit: Option<i64>,
) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT DISTINCT ON (field.key)
               field.key AS field_key,
               field.value AS correction,
               ge.id,
               ge.extraction_id,
               ge.created_at
        FROM gold_examples ge
        CROSS JOIN LATERAL jsonb_each(ge.correction_diff) AS field(key, value)
        WHERE ge.vendor_id = $1 AND ge.correction_diff IS NOT NULL
        ORDER BY field.key, ge.created_at DESC, ge.id DESC",
    );
    let recs = sqlx::query(&sql)
        .bind(vendor_id)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;

    // serde_json uses preserve_order, so insertion order matches the Python
    // dict semantics that the [:limit] slice depends on.
    let mut latest = Map::new();
    for rec in recs {
        let row = util::row_value(rec);
        let field_key = row
            .get("field_key")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let correction = row.get("correction").cloned().unwrap_or(Value::Null);
        latest.insert(field_key, correction);
    }

    if let Some(l) = limit.filter(|l| *l > 0) {
        latest = latest.into_iter().take(l.max(0) as usize).collect();
    }

    if latest.is_empty() {
        Ok(Vec::new())
    } else {
        Ok(vec![json!({ "correction_diff": Value::Object(latest) })])
    }
}

/// Delete qwen_layout_boxes rows for fields no longer in the template. An
/// empty valid_field_keys clears every box for the (vendor, template).
pub async fn delete_stale_qwen_layout_boxes(
    pool: &PgPool,
    vendor_id: &str,
    template_id: i64,
    valid_field_keys: &[String],
) -> AppResult<i64> {
    let result = if !valid_field_keys.is_empty() {
        sqlx::query(
            "DELETE FROM qwen_layout_boxes
             WHERE vendor_id = $1 AND template_id = $2
               AND field_key != ALL($3::text[])",
        )
        .bind(vendor_id)
        .bind(template_id)
        .bind(valid_field_keys)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?
    } else {
        sqlx::query("DELETE FROM qwen_layout_boxes WHERE vendor_id = $1 AND template_id = $2")
            .bind(vendor_id)
            .bind(template_id)
            .execute(pool)
            .await
            .map_err(crate::error::AppError::from)?
    };
    Ok(result.rows_affected() as i64)
}

/// Delete spatial_memory rows for fields no longer in the template. An empty
/// valid_field_keys clears every entry for the vendor.
pub async fn delete_stale_spatial_memory(
    pool: &PgPool,
    vendor_id: &str,
    valid_field_keys: &[String],
) -> AppResult<i64> {
    let result = if !valid_field_keys.is_empty() {
        sqlx::query(
            "DELETE FROM spatial_memory
             WHERE vendor_id = $1
               AND field_key != ALL($2::text[])",
        )
        .bind(vendor_id)
        .bind(valid_field_keys)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?
    } else {
        sqlx::query("DELETE FROM spatial_memory WHERE vendor_id = $1")
            .bind(vendor_id)
            .execute(pool)
            .await
            .map_err(crate::error::AppError::from)?
    };
    Ok(result.rows_affected() as i64)
}

/// Latest saved gold correction by field for UI warnings.
pub async fn get_latest_gold_correction_fields(
    pool: &PgPool,
    vendor_id: &str,
) -> AppResult<Value> {
    let examples = get_gold_examples(pool, vendor_id, None).await?;
    Ok(examples
        .first()
        .and_then(|e| e.get("correction_diff"))
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({})))
}
