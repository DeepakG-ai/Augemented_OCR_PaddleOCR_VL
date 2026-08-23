//! docs.rs — documents, extractions, pages, corrections, review events
//! (db.py "Documents/Extractions/Pages" sections).
//!
//! Reads wrap Python's verbatim column lists with [`util::row_query`]; writes
//! are plain parameterised statements. The few mutations Python ran inside an
//! explicit transaction (vendor backfill on normalize, single-extraction
//! delete) keep that atomicity here.

use serde_json::{json, Value};
use sqlx::PgPool;
use uuid::Uuid;

use super::util;
use crate::error::{AppError, AppResult};
use crate::pyjson::truthy;

/// Verbatim column projection shared by all extraction reads (db.py
/// `_EXTRACTION_COLS`).
pub(crate) const EXTRACTION_COLS: &str = "
    e.id, e.document_id, e.vendor_id, v.name AS vendor_name, e.template_id,
    e.filename, e.total_pages, e.format_type, e.header_fields, e.line_item_fields,
    e.result, e.page_results, e.field_locations, e.ocr_data,
    e.corrected_result, e.correction_meta, e.export_object_key,
    e.progress, e.cancel_requested, e.status, e.error, e.duration_ms,
    e.universal_agent, e.created_at, e.updated_at
";

/// Wrap an INSERT/UPDATE..RETURNING so its result row arrives as one jsonb
/// payload (the DML must sit as a top-level CTE member).
fn returning_row_sql(statement: &str) -> String {
    format!("WITH _r AS ({statement}) SELECT to_jsonb(_r.*) AS row FROM _r")
}

/// Insert a document record and return the created row (db.py create_document).
#[allow(clippy::too_many_arguments)]
pub async fn create_document(
    pool: &PgPool,
    vendor_id: Option<&str>,
    filename: &str,
    mime_type: &str,
    size_bytes: i64,
    object_key: &str,
    source_type: &str,
    source_ref: Option<&str>,
    metadata: Option<&Value>,
) -> AppResult<Value> {
    // Python: json.dumps(metadata or {}) — falsy metadata becomes {}.
    let metadata_owned = match metadata {
        Some(v) if truthy(v) => v.clone(),
        _ => json!({}),
    };
    let sql = returning_row_sql(
        "INSERT INTO documents
            (vendor_id, source_type, source_ref, filename, mime_type, size_bytes, object_key, metadata)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
         RETURNING id, vendor_id, source_type, source_ref, filename, mime_type, size_bytes,
                   object_key, metadata, status, created_at, updated_at",
    );
    let rec = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(source_type)
        .bind(source_ref)
        .bind(filename)
        .bind(mime_type)
        .bind(size_bytes)
        .bind(object_key)
        .bind(metadata_owned)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rec.map(util::row_value).unwrap_or_else(|| json!({})))
}

pub async fn get_document(pool: &PgPool, document_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT id, vendor_id, source_type, source_ref, filename, mime_type, size_bytes,
                object_key, metadata, status, created_at, updated_at
         FROM documents
         WHERE id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(document_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value).filter(|doc| !doc.is_null()))
}

/// Flip a document's lifecycle status.
pub async fn update_document_status(pool: &PgPool, document_id: i64, status: &str) -> AppResult<()> {
    sqlx::query(
        "UPDATE documents
         SET status = $2, updated_at = NOW()
         WHERE id = $1",
    )
    .bind(document_id as i32)
    .bind(status)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

/// Record the currently outstanding quota reservation on the document.
///
/// The worker release paths read metadata.reserved_pages to know how many pages
/// to return to the user's quota when the pipeline ends. Resume reserves only
/// the missing pages, so this must be updated to that smaller count (it was set
/// to the full page count at initial submission).
pub async fn set_document_reserved_pages(pool: &PgPool, document_id: i64, pages: i64) -> AppResult<()> {
    if document_id == 0 {
        return Ok(());
    }
    sqlx::query(
        "UPDATE documents
           SET metadata = COALESCE(metadata, '{}'::jsonb)
                          || jsonb_build_object('reserved_pages', $2::int),
               updated_at = NOW()
         WHERE id = $1",
    )
    .bind(document_id as i32)
    .bind(pages as i32)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

pub async fn get_extraction(pool: &PgPool, extraction_id: i64) -> AppResult<Option<Value>> {
    let inner = format!(
        "{EXTRACTION_COLS}
         FROM extractions e
         LEFT JOIN vendors v ON v.id = e.vendor_id
         WHERE e.id = $1"
    );
    let sql = util::row_query(&inner);
    let rec = sqlx::query(&sql)
        .bind(extraction_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Insert a queued extraction and return its full row. vendor/template/fields
/// may be empty when the document is submitted for auto-detection — the
/// normalize stage fills them in later (see update_extraction_vendor).
#[allow(clippy::too_many_arguments)]
pub async fn create_extraction(
    pool: &PgPool,
    vendor_id: Option<&str>,
    template_id: Option<i64>,
    filename: &str,
    total_pages: i64,
    format_type: Option<&str>,
    header_fields: &[&str],
    line_item_fields: &[&str],
    document_id: Option<i64>,
    universal_agent: bool,
) -> AppResult<Value> {
    let header_json = json!(header_fields);
    let line_json = json!(line_item_fields);
    let progress = json!({"stage": "queued", "message": "Queued for processing"});
    let id: i32 = sqlx::query_scalar(
        "INSERT INTO extractions
            (document_id, vendor_id, template_id, filename, total_pages,
             format_type, header_fields, line_item_fields, status, progress,
             universal_agent)
         VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, 'queued', $9::jsonb, $10)
         RETURNING id",
    )
    .bind(document_id.map(|v| v as i32))
    .bind(vendor_id)
    .bind(template_id.map(|v| v as i32))
    .bind(filename)
    .bind(total_pages as i32)
    .bind(format_type)
    .bind(header_json)
    .bind(line_json)
    .bind(progress)
    .bind(universal_agent)
    .fetch_one(pool)
    .await
    .map_err(AppError::from)?;
    Ok(get_extraction(pool, i64::from(id)).await?.unwrap_or_else(|| json!({})))
}

/// Persist a vendor detected during the normalize stage.
///
/// Sets the extraction's vendor/template/format/field lists AND the parent
/// document's vendor_id in one transaction so the two never drift apart.
pub async fn update_extraction_vendor(
    pool: &PgPool,
    extraction_id: i64,
    vendor_id: &str,
    template_id: Option<i64>,
    format_type: Option<&str>,
    header_fields: &[&str],
    line_item_fields: &[&str],
) -> AppResult<()> {
    let header_json = json!(header_fields);
    let line_json = json!(line_item_fields);
    let mut tx = pool.begin().await.map_err(AppError::from)?;
    sqlx::query(
        "UPDATE extractions
           SET vendor_id        = $2,
               template_id      = $3,
               format_type      = $4,
               header_fields    = $5::jsonb,
               line_item_fields = $6::jsonb,
               updated_at       = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(vendor_id)
    .bind(template_id.map(|v| v as i32))
    .bind(format_type)
    .bind(header_json)
    .bind(line_json)
    .execute(&mut *tx)
    .await
    .map_err(AppError::from)?;
    sqlx::query(
        "UPDATE documents d
           SET vendor_id  = $2,
               updated_at = NOW()
          FROM extractions e
         WHERE e.id = $1 AND d.id = e.document_id",
    )
    .bind(extraction_id as i32)
    .bind(vendor_id)
    .execute(&mut *tx)
    .await
    .map_err(AppError::from)?;
    tx.commit().await.map_err(AppError::from)?;
    Ok(())
}

/// Store final/partial results. When `page_results_partial` carries entries,
/// they are appended incrementally to the existing page_results array instead
/// of replacing it (streaming progress path).
#[allow(clippy::too_many_arguments)]
pub async fn update_extraction_result(
    pool: &PgPool,
    extraction_id: i64,
    result: Option<&Value>,
    page_results: Option<&Value>,
    status: &str,
    duration_ms: Option<i64>,
    error: Option<&str>,
    page_results_partial: Option<&[Value]>,
    progress: Option<&Value>,
) -> AppResult<()> {
    if let Some(partial) = page_results_partial.filter(|p| !p.is_empty()) {
        // Incremental append: add page results to existing array.
        for pr in partial {
            sqlx::query(
                "UPDATE extractions
                 SET page_results = COALESCE(page_results, '[]'::jsonb) || $2::jsonb,
                     status       = $3,
                     progress     = COALESCE($4::jsonb, progress),
                     updated_at   = NOW()
                 WHERE id = $1",
            )
            .bind(extraction_id as i32)
            .bind(json!([pr]))
            .bind(status)
            .bind(progress)
            .execute(pool)
            .await
            .map_err(AppError::from)?;
        }
    } else {
        sqlx::query(
            "UPDATE extractions
             SET result       = $2::jsonb,
                 page_results = $3::jsonb,
                 status       = $4,
                 duration_ms  = COALESCE($5, duration_ms),
                 error        = $6,
                 progress     = COALESCE($7::jsonb, progress),
                 updated_at   = NOW()
             WHERE id = $1",
        )
        .bind(extraction_id as i32)
        .bind(result)
        .bind(page_results)
        .bind(status)
        .bind(duration_ms.map(|d| d as i32))
        .bind(error)
        .bind(progress)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    }
    Ok(())
}

/// Extraction history for one vendor, optionally restricted to the documents
/// billed to `user_id`, newest first.
pub async fn list_extractions(
    pool: &PgPool,
    vendor_id: &str,
    limit: i64,
    user_id: Option<&str>,
) -> AppResult<Vec<Value>> {
    let uid: Option<Uuid> = util::uuid_or_none(user_id);
    let inner = format!(
        "{EXTRACTION_COLS}
         FROM extractions e
         LEFT JOIN vendors v ON v.id = e.vendor_id
         LEFT JOIN documents d ON d.id = e.document_id
         WHERE e.vendor_id = $1
           AND ($3::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $3)
         ORDER BY e.created_at DESC LIMIT $2"
    );
    let sql = util::row_query(&inner);
    let rows = sqlx::query(&sql)
        .bind(vendor_id)
        .bind(limit)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Global extraction history with vendor_name joined, newest first.
pub async fn list_all_extractions(
    pool: &PgPool,
    limit: i64,
    offset: i64,
    user_id: Option<&str>,
) -> AppResult<Vec<Value>> {
    let uid: Option<Uuid> = util::uuid_or_none(user_id);
    let inner = format!(
        "{EXTRACTION_COLS}
         FROM extractions e
         LEFT JOIN vendors v ON v.id = e.vendor_id
         LEFT JOIN documents d ON d.id = e.document_id
         WHERE ($3::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $3)
         ORDER BY e.created_at DESC LIMIT $1 OFFSET $2"
    );
    let sql = util::row_query(&inner);
    let rows = sqlx::query(&sql)
        .bind(limit)
        .bind(offset)
        .bind(uid)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Total number of extractions, optionally scoped to one billing user.
pub async fn count_all_extractions(pool: &PgPool, user_id: Option<&str>) -> AppResult<i64> {
    let uid: Option<Uuid> = util::uuid_or_none(user_id);
    let count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM extractions e
         LEFT JOIN vendors v ON v.id = e.vendor_id
         LEFT JOIN documents d ON d.id = e.document_id
         WHERE ($1::UUID IS NULL OR COALESCE((d.metadata->>'billing_user_id')::UUID, v.user_id) = $1)",
    )
    .bind(uid)
    .fetch_one(pool)
    .await
    .map_err(AppError::from)?;
    Ok(count)
}

/// Bulk-insert (upsert) page artifact metadata for an extraction.
pub async fn save_pages(pool: &PgPool, extraction_id: i64, pages: &[Value]) -> AppResult<()> {
    if pages.is_empty() {
        return Ok(());
    }
    for p in pages {
        // Python .get(key, default): a present-but-null value stays null;
        // the default applies only when the key is missing.
        let page_number = p
            .get("page_number")
            .and_then(util::int_or_none)
            .ok_or_else(|| AppError::BadRequest("page missing page_number".to_string()))?;
        let object_key = p
            .get("object_key")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::BadRequest("page missing object_key".to_string()))?;
        let mime_type = match p.get("mime_type") {
            Some(v) => v.as_str(),
            None => Some("image/jpeg"),
        };
        let width = match p.get("width") {
            Some(v) => util::int_or_none(v),
            None => Some(0),
        };
        let height = match p.get("height") {
            Some(v) => util::int_or_none(v),
            None => Some(0),
        };
        let orig_width = match p.get("orig_width") {
            Some(v) => util::int_or_none(v),
            None => width,
        };
        let orig_height = match p.get("orig_height") {
            Some(v) => util::int_or_none(v),
            None => height,
        };
        let source = p.get("source").and_then(Value::as_str);
        let char_count = p.get("char_count").and_then(util::int_or_none);
        let word_geometry = match p.get("word_geometry") {
            Some(v) if !v.is_null() => Some(v),
            _ => None,
        };

        sqlx::query(
            "INSERT INTO pages (extraction_id, page_number, object_key, mime_type, width, height, orig_width, orig_height,
                               source, char_count, word_geometry)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
             ON CONFLICT (extraction_id, page_number) DO UPDATE SET
                 object_key = EXCLUDED.object_key,
                 mime_type = EXCLUDED.mime_type,
                 width = EXCLUDED.width,
                 height = EXCLUDED.height,
                 orig_width = EXCLUDED.orig_width,
                 orig_height = EXCLUDED.orig_height,
                 source = EXCLUDED.source,
                 char_count = EXCLUDED.char_count,
                 word_geometry = EXCLUDED.word_geometry",
        )
        .bind(extraction_id as i32)
        .bind(page_number as i32)
        .bind(object_key)
        .bind(mime_type)
        .bind(width.map(|v| v as i32))
        .bind(height.map(|v| v as i32))
        .bind(orig_width.map(|v| v as i32))
        .bind(orig_height.map(|v| v as i32))
        .bind(source)
        .bind(char_count.map(|v| v as i32))
        .bind(word_geometry)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    }
    Ok(())
}

/// Return all pages for an extraction, ordered by page_number.
pub async fn get_pages(pool: &PgPool, extraction_id: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT page_number, object_key, mime_type, width, height, orig_width, orig_height,
                source, char_count, word_geometry
         FROM pages
         WHERE extraction_id = $1
         ORDER BY page_number ASC",
    );
    let rows = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Return artifact object keys for all rendered pages of an extraction.
pub async fn get_page_object_keys(pool: &PgPool, extraction_id: i64) -> AppResult<Vec<String>> {
    let sql = util::row_query(
        "SELECT object_key
         FROM pages
         WHERE extraction_id = $1
         ORDER BY page_number ASC",
    );
    let rows = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows
        .into_iter()
        .map(util::row_value)
        .filter_map(|r| r.get("object_key").and_then(Value::as_str).map(str::to_string))
        // Python filters falsy values, which includes "".
        .filter(|k| !k.is_empty())
        .collect())
}

/// Collect object-store keys owned by a vendor for cleanup on delete:
/// uploaded documents, rendered page artifacts, and export files.
pub async fn get_vendor_object_keys(pool: &PgPool, vendor_id: &str) -> AppResult<Value> {
    async fn object_key_rows(pool: &PgPool, inner_select: &str, vendor_id: &str) -> AppResult<Vec<Value>> {
        let sql = util::row_query(inner_select);
        let rows = sqlx::query(&sql)
            .bind(vendor_id)
            .fetch_all(pool)
            .await
            .map_err(AppError::from)?;
        Ok(rows.into_iter().map(util::row_value).collect())
    }

    let document_rows = object_key_rows(
        pool,
        "SELECT object_key
         FROM documents
         WHERE vendor_id = $1 AND object_key IS NOT NULL
         ORDER BY created_at ASC",
        vendor_id,
    )
    .await?;
    let page_rows = object_key_rows(
        pool,
        "SELECT p.object_key
         FROM pages p
         JOIN extractions e ON e.id = p.extraction_id
         WHERE e.vendor_id = $1 AND p.object_key IS NOT NULL
         ORDER BY p.extraction_id ASC, p.page_number ASC",
        vendor_id,
    )
    .await?;
    let export_rows = object_key_rows(
        pool,
        "SELECT export_object_key
         FROM extractions
         WHERE vendor_id = $1 AND export_object_key IS NOT NULL
         ORDER BY created_at ASC",
        vendor_id,
    )
    .await?;

    let keys_of = |rows: &[Value], field: &str| -> Vec<String> {
        rows.iter()
            .filter_map(|r| r.get(field).and_then(Value::as_str))
            .filter(|k| !k.is_empty())
            .map(str::to_string)
            .collect()
    };
    Ok(json!({
        "documents": keys_of(&document_rows, "object_key"),
        "pages": keys_of(&page_rows, "object_key"),
        "exports": keys_of(&export_rows, "export_object_key"),
    }))
}

/// Save field_locations to the extraction record.
pub async fn save_field_locations(
    pool: &PgPool,
    extraction_id: i64,
    field_locations: &Value,
) -> AppResult<()> {
    sqlx::query(
        "UPDATE extractions
         SET field_locations = $2::jsonb,
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(field_locations)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

/// Save PaddleOCR results (words + boxes per page) to the extraction record.
pub async fn save_ocr_data(pool: &PgPool, extraction_id: i64, ocr_data: &Value) -> AppResult<()> {
    sqlx::query(
        "UPDATE extractions
         SET ocr_data = $2::jsonb,
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(ocr_data)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

/// Lightweight check for postprocess prerequisites.
///
/// Hybrid extraction needs current-document geometry before postprocess because
/// spatial memory reads text from the current OCR/pdfium word boxes. If the
/// OCR branch failed, postprocess may still finalize the LLM JSON as partial
/// and explicitly disable OCR-backed review features.
pub async fn is_postprocess_ready(pool: &PgPool, extraction_id: i64) -> AppResult<bool> {
    let ready: Option<bool> = sqlx::query_scalar(
        "WITH latest_ocr AS (
            SELECT status
            FROM jobs
            WHERE extraction_id = $1
              AND job_type = 'ocr'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        )
        SELECT (
            e.status = 'processing'
            AND e.error IS NULL
            AND e.result IS NOT NULL
            AND NOT COALESCE((e.result->>'_all_pages_failed') = 'true', FALSE)
            AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(COALESCE(e.page_results, '[]'::jsonb)) AS pr
                WHERE pr ? '_error'
            )
            AND (
                e.ocr_data IS NOT NULL
                OR COALESCE((SELECT status = 'failed' FROM latest_ocr), FALSE)
            )
        ) AS ready
        FROM extractions e
        WHERE e.id = $1",
    )
    .bind(extraction_id as i32)
    .fetch_optional(pool)
    .await
    .map_err(AppError::from)?;
    Ok(ready.unwrap_or(false))
}

/// Return PaddleOCR results (words + boxes per page) for click-to-select.
pub async fn get_ocr_data(pool: &PgPool, extraction_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query("SELECT ocr_data FROM extractions WHERE id = $1");
    let rec = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?;
    let Some(row) = rec.map(util::row_value) else {
        return Ok(None);
    };
    Ok(row.get("ocr_data").cloned().filter(|v| !v.is_null()))
}

/// Persist user corrections to corrected_result (original result stays immutable).
///
/// Returns true if the extraction was found and updated, false otherwise.
/// An empty correction_meta object counts as falsy in Python and stores NULL.
pub async fn save_corrections(
    pool: &PgPool,
    extraction_id: i64,
    corrected_result: Option<&Value>,
    field_locations: &Value,
    correction_meta: Option<&Value>,
) -> AppResult<bool> {
    let meta = correction_meta.filter(|v| truthy(v));
    let res = sqlx::query(
        "UPDATE extractions
         SET corrected_result = $2::jsonb,
             field_locations  = $3::jsonb,
             correction_meta  = $4::jsonb,
             updated_at       = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(corrected_result)
    .bind(field_locations)
    .bind(meta)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(res.rows_affected() == 1)
}

/// Delete exactly one extraction record together with its pages and jobs.
pub async fn delete_extraction(pool: &PgPool, extraction_id: i64) -> AppResult<bool> {
    let mut tx = pool.begin().await.map_err(AppError::from)?;
    sqlx::query("DELETE FROM pages WHERE extraction_id = $1")
        .bind(extraction_id as i32)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;
    sqlx::query("DELETE FROM jobs WHERE extraction_id = $1")
        .bind(extraction_id as i32)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let res = sqlx::query("DELETE FROM extractions WHERE id = $1")
        .bind(extraction_id as i32)
        .execute(&mut *tx)
        .await
        .map_err(AppError::from)?;
    let deleted = res.rows_affected() == 1;
    tx.commit().await.map_err(AppError::from)?;
    Ok(deleted)
}

/// How many extractions still reference a document.
pub async fn count_extractions_for_document(pool: &PgPool, document_id: i64) -> AppResult<i64> {
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM extractions WHERE document_id = $1")
        .bind(document_id as i32)
        .fetch_optional(pool)
        .await
        .map_err(AppError::from)?
        .unwrap_or(0);
    Ok(count)
}

/// Delete exactly one document record.
pub async fn delete_document(pool: &PgPool, document_id: i64) -> AppResult<bool> {
    let res = sqlx::query("DELETE FROM documents WHERE id = $1")
        .bind(document_id as i32)
        .execute(pool)
        .await
        .map_err(AppError::from)?;
    Ok(res.rows_affected() == 1)
}

/// Return corrected_result if available, else the original result.
///
/// Sync by design: db.py's version operates on an already-loaded extraction
/// dict, not the database.
pub fn get_effective_result(extraction: &Value) -> Value {
    extraction
        .get("corrected_result")
        .filter(|v| truthy(v))
        .or_else(|| extraction.get("result").filter(|v| truthy(v)))
        .cloned()
        .unwrap_or_else(|| json!({}))
}

/// Append a review audit event (who changed what, with before/after + diff).
#[allow(clippy::too_many_arguments)]
pub async fn create_review_event(
    pool: &PgPool,
    extraction_id: i64,
    actor: &str,
    reason_code: &str,
    note: Option<&str>,
    before_result: &Value,
    after_result: &Value,
    before_locations: Option<&Value>,
    after_locations: Option<&Value>,
    diff: &Value,
) -> AppResult<i64> {
    let id: i32 = sqlx::query_scalar(
        "INSERT INTO review_events
            (extraction_id, actor, reason_code, note, before_result, after_result,
             before_locations, after_locations, diff)
         VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb)
         RETURNING id",
    )
    .bind(extraction_id as i32)
    .bind(actor)
    .bind(reason_code)
    .bind(note)
    .bind(before_result)
    .bind(after_result)
    .bind(before_locations)
    .bind(after_locations)
    .bind(diff)
    .fetch_one(pool)
    .await
    .map_err(AppError::from)?;
    Ok(i64::from(id))
}

/// Review audit trail for an extraction, newest first.
pub async fn list_review_events(pool: &PgPool, extraction_id: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(
        "SELECT id, extraction_id, actor, reason_code, note, before_result, after_result,
                before_locations, after_locations, diff, created_at
         FROM review_events
         WHERE extraction_id = $1
         ORDER BY created_at DESC",
    );
    let rows = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_all(pool)
        .await
        .map_err(AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}

/// Overwrite the live progress payload; optionally advance the status.
pub async fn update_extraction_progress(
    pool: &PgPool,
    extraction_id: i64,
    progress: &Value,
    status: Option<&str>,
) -> AppResult<()> {
    sqlx::query(
        "UPDATE extractions
         SET progress = $2::jsonb,
             status = COALESCE($3, status),
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(progress)
    .bind(status)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

/// Correct the page count discovered during normalize.
pub async fn set_total_pages(pool: &PgPool, extraction_id: i64, total_pages: i64) -> AppResult<()> {
    sqlx::query(
        "UPDATE extractions
         SET total_pages = $2,
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(total_pages as i32)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

/// Request cooperative cancellation of a running pipeline.
/// Returns false when the extraction row does not exist.
pub async fn set_cancel_requested(
    pool: &PgPool,
    extraction_id: i64,
    cancel_requested: bool,
) -> AppResult<bool> {
    let res = sqlx::query(
        "UPDATE extractions
         SET cancel_requested = $2,
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(cancel_requested)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(res.rows_affected() == 1)
}

/// Poll whether cancellation was requested for this extraction.
pub async fn is_cancel_requested(pool: &PgPool, extraction_id: i64) -> AppResult<bool> {
    let flag: Option<bool> =
        sqlx::query_scalar("SELECT cancel_requested FROM extractions WHERE id = $1")
            .bind(extraction_id as i32)
            .fetch_optional(pool)
            .await
            .map_err(AppError::from)?;
    Ok(flag.unwrap_or(false))
}

/// Store (or clear) the canonical-field mapped result for an extraction.
pub async fn update_extraction_mapped_result(
    pool: &PgPool,
    extraction_id: i64,
    mapped_result: Option<&Value>,
) -> AppResult<()> {
    sqlx::query(
        "UPDATE extractions SET mapped_result = $2::jsonb, updated_at = NOW()
         WHERE id = $1",
    )
    .bind(extraction_id as i32)
    .bind(mapped_result)
    .execute(pool)
    .await
    .map_err(AppError::from)?;
    Ok(())
}

/// Return the stored mapped_result for an extraction (normalized), or None.
pub async fn get_extraction_mapped_result(pool: &PgPool, extraction_id: i64) -> AppResult<Option<Value>> {
    let val: Option<Option<Value>> =
        sqlx::query_scalar("SELECT mapped_result FROM extractions WHERE id = $1")
            .bind(extraction_id as i32)
            .fetch_optional(pool)
            .await
            .map_err(AppError::from)?;
    Ok(val.flatten().map(_normalize_mapped_result))
}

/// Rename legacy 'items' key to 'line_items' in stored mapped results.
fn _normalize_mapped_result(val: Value) -> Value {
    match val {
        Value::Array(items) => {
            Value::Array(items.into_iter().map(_normalize_mapped_result).collect())
        }
        Value::Object(mut map) => {
            if map.contains_key("items") && !map.contains_key("line_items") {
                if let Some(items) = map.remove("items") {
                    map.insert("line_items".to_string(), items);
                }
            }
            Value::Object(map)
        }
        other => other,
    }
}

/// Set status plus optional progress/error/duration.
///
/// With `end_to_end` the stored duration_ms is recomputed as the full pipeline
/// wall-clock (created_at → now), overriding the LLM-stage-only value the llm
/// worker wrote — matching what the history page shows as end-to-end latency.
pub async fn set_extraction_status(
    pool: &PgPool,
    extraction_id: i64,
    status: &str,
    progress: Option<&Value>,
    error: Option<&str>,
    duration_ms: Option<i64>,
    end_to_end: bool,
) -> AppResult<()> {
    let duration_expr = if end_to_end {
        "ROUND(EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000)::INT".to_string()
    } else {
        "COALESCE($5, duration_ms)".to_string()
    };
    let sql = format!(
        "UPDATE extractions
         SET status = $2,
             progress = COALESCE($3::jsonb, progress),
             error = $4,
             duration_ms = {duration_expr},
             updated_at = NOW()
         WHERE id = $1"
    );
    let mut q = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .bind(status)
        .bind(progress)
        .bind(error);
    if !end_to_end {
        q = q.bind(duration_ms.map(|d| d as i32));
    }
    q.execute(pool).await.map_err(AppError::from)?;
    Ok(())
}

