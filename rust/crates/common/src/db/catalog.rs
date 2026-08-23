//! catalog.rs — templates, ERP field mappings and output schemas
//! (db.py "Template queries" / "Field mapping queries" /
//! "Output schema helpers" sections).
//!
//! The mapped-result helpers on `extractions` belong to the extraction domain
//! (docs.rs) and are intentionally not here.

use serde_json::{json, Value};
use sqlx::PgPool;

use super::util;
use crate::config::Config;
use crate::error::{AppError, AppResult};

/// Verbatim template projection (db.py `_TEMPLATE_COLS`).
const TEMPLATE_COLS: &str = "
    id, vendor_id, format_type, header_fields, line_item_fields,
    prompt_instructions, extraction_rules, system_prompt, prompt_hash,
    created_at, updated_at
";

/// Verbatim field-mapping projection (db.py `_FIELD_MAPPING_COLS`).
const FIELD_MAPPING_COLS: &str = "
    id, vendor_id, template_id, schema_id, header_map, line_map,
    header_snapshot, line_snapshot, pending_notices, created_at, updated_at
";

fn cfg() -> &'static Config {
    Config::global()
}

// -- Template queries -------------------------------------------------------

/// Fetch a vendor's extraction template. Cached under "template:{vendor_id}".
pub async fn get_template(pool: &PgPool, vendor_id: &str) -> AppResult<Option<Value>> {
    let key = format!("template:{vendor_id}");
    super::cached_read(&key, cfg().cache_ttl_template, move || async move {
        let sql = util::row_query(&format!(
            "SELECT {TEMPLATE_COLS} FROM templates WHERE vendor_id = $1"
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

/// Insert or update the single template row per vendor; returns the full row.
#[allow(clippy::too_many_arguments)] // mirrors the templates table's columns
pub async fn upsert_template(
    pool: &PgPool,
    vendor_id: &str,
    format_type: &str,
    header_fields: &[String],
    line_item_fields: &[String],
    instructions: Option<&str>,
    rules: &[String],
    system_prompt: &str,
    prompt_hash: &str,
) -> AppResult<Value> {
    let sql = util::row_query(&format!(
        "WITH up AS (
            INSERT INTO templates
                (vendor_id, format_type, header_fields, line_item_fields,
                 prompt_instructions, extraction_rules, system_prompt, prompt_hash, updated_at)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6::jsonb, $7, $8, NOW())
            ON CONFLICT (vendor_id) DO UPDATE SET
                format_type         = EXCLUDED.format_type,
                header_fields       = EXCLUDED.header_fields,
                line_item_fields    = EXCLUDED.line_item_fields,
                prompt_instructions = EXCLUDED.prompt_instructions,
                extraction_rules    = EXCLUDED.extraction_rules,
                system_prompt       = EXCLUDED.system_prompt,
                prompt_hash         = EXCLUDED.prompt_hash,
                updated_at          = NOW()
            RETURNING {TEMPLATE_COLS}
         ) SELECT * FROM up"
    ));
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(format_type)
        .bind(json!(header_fields))
        .bind(json!(line_item_fields))
        .bind(instructions)
        .bind(json!(rules))
        .bind(system_prompt)
        .bind(prompt_hash)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let row = util::row_value(rec);
    super::invalidate_template_cache(vendor_id).await;
    Ok(row)
}

/// All templates joined with vendor name for the saved-templates page,
/// optionally scoped to a user's vendors. Cached under
/// "templates:list:{uid|all}".
pub async fn list_all_templates(pool: &PgPool, user_id: Option<&str>) -> AppResult<Vec<Value>> {
    // Python relies on the $1::UUID cast to reject malformed ids; mirror that
    // by failing fast instead of silently widening the scope.
    let scope = match user_id {
        Some(id) => Some(util::uuid_or_bad(id)?),
        None => None,
    };
    let key = format!("templates:list:{}", user_id.filter(|s| !s.is_empty()).unwrap_or("all"));
    let cached = super::cached_read(&key, cfg().cache_ttl_template, move || async move {
        let sql = util::row_query(
            "SELECT t.id, t.vendor_id, v.name AS vendor_name, t.format_type,
                    t.header_fields, t.line_item_fields, t.prompt_instructions,
                    t.extraction_rules, t.prompt_hash, t.created_at, t.updated_at
             FROM templates t
             JOIN vendors v ON v.id = t.vendor_id
             WHERE ($1::UUID IS NULL OR v.user_id = $1)
             ORDER BY t.updated_at DESC"
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

// -- Field mapping queries --------------------------------------------------

/// Return the ERP field mapping for a vendor, or None if not configured.
/// Cached under "mapping:{vendor_id}".
pub async fn get_field_mapping(pool: &PgPool, vendor_id: &str) -> AppResult<Option<Value>> {
    let key = format!("mapping:{vendor_id}");
    super::cached_read(&key, cfg().cache_ttl_mapping, move || async move {
        let sql =
            util::row_query(&format!("SELECT {FIELD_MAPPING_COLS} FROM field_mappings WHERE vendor_id = $1"));
        let rec = sqlx::query(&sql)
            .bind(vendor_id)
            .fetch_optional(pool)
            .await
            .map_err(crate::error::AppError::from)?;
        Ok(rec.map(util::row_value))
    })
    .await
}

/// Insert or update a vendor's ERP field mapping (one per vendor).
///
/// Binds follow the numbered placeholders ($8 appears before $3 in the SQL
/// text), so they are supplied in numeric order.
#[allow(clippy::too_many_arguments)]
pub async fn upsert_field_mapping(
    pool: &PgPool,
    vendor_id: &str,
    template_id: Option<i32>,
    header_map: &Value,
    line_map: &Value,
    header_snapshot: &[String],
    line_snapshot: &[String],
    pending_notices: &Value,
    schema_id: Option<i32>,
) -> AppResult<Value> {
    let sql = util::row_query(&format!(
        "WITH up AS (
            INSERT INTO field_mappings
                (vendor_id, template_id, schema_id, header_map, line_map,
                 header_snapshot, line_snapshot, pending_notices, updated_at)
            VALUES ($1, $2, $8, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, NOW())
            ON CONFLICT (vendor_id) DO UPDATE SET
                template_id     = EXCLUDED.template_id,
                schema_id       = EXCLUDED.schema_id,
                header_map      = EXCLUDED.header_map,
                line_map        = EXCLUDED.line_map,
                header_snapshot = EXCLUDED.header_snapshot,
                line_snapshot   = EXCLUDED.line_snapshot,
                pending_notices = EXCLUDED.pending_notices,
                updated_at      = NOW()
            RETURNING {FIELD_MAPPING_COLS}
         ) SELECT * FROM up"
    ));
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(template_id)
        .bind(header_map)
        .bind(line_map)
        .bind(json!(header_snapshot))
        .bind(json!(line_snapshot))
        .bind(pending_notices)
        .bind(schema_id)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let row = util::row_value(rec);
    super::invalidate_mapping_cache(vendor_id).await;
    Ok(row)
}

/// Overwrite the pending rename notices for a vendor's mapping.
pub async fn set_field_mapping_notices(
    pool: &PgPool,
    vendor_id: &str,
    pending_notices: &Value,
) -> AppResult<()> {
    sqlx::query(
        "UPDATE field_mappings SET pending_notices = $2::jsonb, updated_at = NOW() \
         WHERE vendor_id = $1",
    )
    .bind(vendor_id)
    .bind(pending_notices)
    .execute(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    super::invalidate_mapping_cache(vendor_id).await;
    Ok(())
}

// -- Output schema helpers --------------------------------------------------

/// Normalise an output_schemas row: the four TEXT[] list columns arrive as
/// JSON arrays via to_jsonb (never strings); NULL degrades to [] exactly as
/// Python's `_schema_row` did.
fn _schema_row(rec: sqlx::postgres::PgRow) -> AppResult<Value> {
    let mut d = util::row_value(rec);
    for key in [
        "header_fields",
        "line_fields",
        "header_fields_snapshot",
        "line_fields_snapshot",
    ] {
        let replacement = match d.get(key) {
            None | Some(Value::Null) => Some(json!([])),
            // Defensive parity with Python: a legacy text-encoded column is
            // parsed as JSON; garbage raises instead of silently passing.
            Some(Value::String(s)) => match serde_json::from_str::<Value>(s) {
                Ok(v) => Some(v),
                Err(e) => {
                    return Err(AppError::Internal(format!(
                        "output_schemas.{key} holds invalid JSON text: {e}"
                    )))
                }
            },
            _ => None,
        };
        if let Some(v) = replacement {
            d[key] = v;
        }
    }
    Ok(d)
}

/// List every output schema, system schemas first then by id ascending.
pub async fn get_all_schemas(pool: &PgPool) -> AppResult<Vec<Value>> {
    let sql =
        util::row_query("SELECT * FROM output_schemas ORDER BY is_system DESC, id ASC");
    let recs = sqlx::query(&sql)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    recs.into_iter().map(_schema_row).collect()
}

/// Fetch one output schema by primary key, or None.
pub async fn get_schema_by_id(pool: &PgPool, schema_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query("SELECT * FROM output_schemas WHERE id = $1");
    let rec = sqlx::query(&sql)
        .bind(schema_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    match rec {
        Some(row) => Ok(Some(_schema_row(row)?)),
        None => Ok(None),
    }
}

/// Fetch one output schema by slug, or None.
pub async fn get_schema_by_slug(pool: &PgPool, slug: &str) -> AppResult<Option<Value>> {
    let sql = util::row_query("SELECT * FROM output_schemas WHERE slug = $1");
    let rec = sqlx::query(&sql)
        .bind(slug)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    match rec {
        Some(row) => Ok(Some(_schema_row(row)?)),
        None => Ok(None),
    }
}

/// Collapse runs of non [a-z0-9] characters to single underscores and trim
/// them, falling back to "schema" — equivalent to Python's
/// `re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "schema"`.
fn slugify(name: &str) -> String {
    let mut out = String::new();
    for ch in name.to_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            out.push(ch);
        } else if !out.is_empty() && !out.ends_with('_') {
            out.push('_');
        }
    }
    while out.ends_with('_') {
        out.pop();
    }
    if out.is_empty() {
        "schema".to_string()
    } else {
        out
    }
}

/// Create a custom output schema with a unique slug derived from its name.
pub async fn create_schema(
    pool: &PgPool,
    name: &str,
    header_fields: &[String],
    line_fields: &[String],
) -> AppResult<Value> {
    let base_slug = slugify(name);
    let mut slug = base_slug.clone();
    let candidates =
        std::iter::once(String::new()).chain((2..100).map(|i| format!("_{i}")));
    for suffix in candidates {
        let candidate = format!("{base_slug}{suffix}");
        let taken: Option<i32> = sqlx::query_scalar("SELECT 1 FROM output_schemas WHERE slug = $1")
            .bind(&candidate)
            .fetch_optional(pool)
            .await
            .map_err(crate::error::AppError::from)?;
        if taken.is_none() {
            slug = candidate;
            break;
        }
    }
    let sql = util::row_query(
        "WITH ins AS (
            INSERT INTO output_schemas
                (name, slug, is_system, header_fields, line_fields,
                 header_fields_snapshot, line_fields_snapshot)
            VALUES ($1, $2, FALSE, $3::text[], $4::text[], $3::text[], $4::text[])
            RETURNING *
         ) SELECT * FROM ins",
    );
    let rec = sqlx::query(&sql)
        .bind(name)
        .bind(&slug)
        .bind(header_fields)
        .bind(line_fields)
        .fetch_one(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    _schema_row(rec)
}

/// Rename a schema and replace both field lists, refreshing their snapshots.
/// Errors when the schema does not exist (Python raised ValueError).
pub async fn update_schema(
    pool: &PgPool,
    schema_id: i64,
    name: &str,
    header_fields: &[String],
    line_fields: &[String],
) -> AppResult<Value> {
    let sql = util::row_query(
        "WITH upd AS (
            UPDATE output_schemas
               SET name = $2,
                   header_fields = $3::text[], line_fields = $4::text[],
                   header_fields_snapshot = $3::text[], line_fields_snapshot = $4::text[],
                   updated_at = NOW()
             WHERE id = $1
            RETURNING *
         ) SELECT * FROM upd",
    );
    let rec = sqlx::query(&sql)
        .bind(schema_id)
        .bind(name)
        .bind(header_fields)
        .bind(line_fields)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    match rec {
        Some(row) => _schema_row(row),
        None => Err(AppError::NotFound(format!("Schema {schema_id} not found"))),
    }
}

/// Delete a non-system schema. Errors when missing (Python ValueError) and
/// refuses to delete system schemas (Python ValueError; surfaced as Conflict).
pub async fn delete_schema(pool: &PgPool, schema_id: i64) -> AppResult<()> {
    let sql = util::row_query("SELECT is_system FROM output_schemas WHERE id = $1");
    let rec = sqlx::query(&sql)
        .bind(schema_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let Some(row) = rec.map(util::row_value) else {
        return Err(AppError::NotFound(format!("Schema {schema_id} not found")));
    };
    let is_system = row.get("is_system").and_then(Value::as_bool).unwrap_or(false);
    if is_system {
        return Err(AppError::Conflict("System schemas cannot be deleted".into()));
    }
    sqlx::query("DELETE FROM output_schemas WHERE id = $1")
        .bind(schema_id)
        .execute(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(())
}

/// Restore header_fields / line_fields from their last-saved snapshot.
pub async fn reset_schema(pool: &PgPool, schema_id: i64) -> AppResult<Value> {
    let sql = util::row_query(
        "WITH upd AS (
            UPDATE output_schemas
               SET header_fields = header_fields_snapshot,
                   line_fields   = line_fields_snapshot,
                   updated_at    = NOW()
             WHERE id = $1
            RETURNING *
         ) SELECT * FROM upd",
    );
    let rec = sqlx::query(&sql)
        .bind(schema_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    match rec {
        Some(row) => _schema_row(row),
        None => Err(AppError::NotFound(format!("Schema {schema_id} not found"))),
    }
}

/// Return the output schema assigned to a vendor's field mapping, or None.
pub async fn get_schema_for_vendor(pool: &PgPool, vendor_id: &str) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT s.* FROM output_schemas s
         JOIN field_mappings fm ON fm.schema_id = s.id
         WHERE fm.vendor_id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    match rec {
        Some(row) => Ok(Some(_schema_row(row)?)),
        None => Ok(None),
    }
}
