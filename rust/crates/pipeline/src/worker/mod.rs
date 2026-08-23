//! worker ← worker.py (stage handlers for durable extraction jobs).
//!
//! Python kept all four stages in one 1,600-line module; here each stage owns
//! a file and this module holds what they share — the job context, the
//! cancellation sentinel, and the small predicates that decide whether a
//! pipeline may continue.
//!
//! The stages run as separate supervisord processes, each claiming jobs of one
//! type from the Postgres queue:
//!
//! ```text
//! normalize → ocr ──┐
//!           └→ llm ─┴→ postprocess
//! ```
//!
//! `normalize` fans out to `ocr` and `llm` in parallel; whichever finishes
//! last enqueues `postprocess` via [`maybe_enqueue_postprocess`], which is
//! guarded by a database-side readiness check so the job is created exactly
//! once.

pub mod llm;
pub mod normalize;
pub mod ocr;
pub mod postprocess;

use augocr_common::error::AppError;
use augocr_common::pyjson::truthy;
use serde_json::{json, Map, Value};
use sqlx::PgPool;

use crate::llm::PipelineContext;

pub const OCR_REVIEW_UNAVAILABLE_ERROR: &str = "ocr_failed_review_unavailable";
pub const OCR_REVIEW_UNAVAILABLE_MESSAGE: &str =
    "JSON extracted, but OCR failed. Review tools and spatial memory are unavailable.";
pub const LLM_FAILED_ERROR: &str = "llm_failed";
pub const LLM_FAILED_MESSAGE: &str =
    "LLM extraction failed on one or more pages. Retry or resume the extraction.";
pub const NO_INVOICE_ERROR: &str = "no_invoice_found";
pub const NO_INVOICE_MESSAGE: &str =
    "Universal agent: no tax invoice page was detected in this document — \
     all pages were classified as purchase order / e-way bill and were dropped.";

/// Extraction statuses that no longer accept progress updates.
pub const TERMINAL_EXTRACTION_STATUSES: [&str; 5] =
    ["done", "failed", "partial", "cancelled", "unverified"];

/// Why a stage handler stopped.
///
/// The distinction matters at the queue: a cancelled job is *cancelled*, not
/// *failed*, so it is neither retried nor counted against the user.
#[derive(Debug, thiserror::Error)]
pub enum JobError {
    /// The user asked to stop; the job ends without being marked failed.
    #[error("{0}")]
    Cancelled(String),
    #[error("{0}")]
    Failed(String),
}

impl From<AppError> for JobError {
    fn from(e: AppError) -> Self {
        JobError::Failed(e.to_string())
    }
}

impl From<crate::ocr_runner::OcrUnavailable> for JobError {
    fn from(e: crate::ocr_runner::OcrUnavailable) -> Self {
        JobError::Failed(e.to_string())
    }
}

pub type JobResult<T> = Result<T, JobError>;

/// Identifiers shared by every log line, trace and usage row of one job.
#[derive(Debug, Clone, Default)]
pub struct PipelineBase {
    pub extraction_id: Option<i64>,
    pub document_id: Option<i64>,
    pub job_id: Option<i64>,
    pub vendor_id: Option<String>,
    pub vendor_name: Option<String>,
    pub filename: Option<String>,
    pub billing_user_id: Option<String>,
    pub api_key_id: Option<i64>,
    pub reserved_pages: Option<i64>,
}

impl PipelineBase {
    /// Assemble from whichever of the three rows the caller has loaded.
    pub fn new(extraction: Option<&Value>, document: Option<&Value>, job: Option<&Value>) -> Self {
        let empty = json!({});
        let ext = extraction.unwrap_or(&empty);
        let doc = document.unwrap_or(&empty);
        let job = job.unwrap_or(&empty);
        let meta = doc.get("metadata").unwrap_or(&Value::Null);

        // `a or b or c` chains — first truthy wins.
        let first_i64 = |vals: [Option<&Value>; 3]| -> Option<i64> {
            vals.into_iter()
                .flatten()
                .filter(|v| truthy(v))
                .find_map(Value::as_i64)
        };
        let first_str = |vals: [Option<&Value>; 3]| -> Option<String> {
            vals.into_iter()
                .flatten()
                .filter(|v| truthy(v))
                .find_map(Value::as_str)
                .map(str::to_string)
        };

        Self {
            extraction_id: first_i64([ext.get("id"), job.get("extraction_id"), None]),
            document_id: first_i64([
                doc.get("id"),
                ext.get("document_id"),
                job.get("document_id"),
            ]),
            job_id: job.get("id").and_then(Value::as_i64),
            vendor_id: first_str([ext.get("vendor_id"), doc.get("vendor_id"), None]),
            vendor_name: first_str([ext.get("vendor_name"), None, None]),
            filename: first_str([doc.get("filename"), ext.get("filename"), None]),
            billing_user_id: meta
                .get("billing_user_id")
                .and_then(Value::as_str)
                .map(str::to_string),
            api_key_id: meta.get("api_key_id").and_then(Value::as_i64),
            reserved_pages: meta.get("reserved_pages").and_then(Value::as_i64),
        }
    }

    /// Override the extraction id when the job carries one the rows do not.
    pub fn with_extraction_id(mut self, extraction_id: i64) -> Self {
        self.extraction_id = Some(extraction_id);
        self
    }

    /// The subset the LLM client needs to attribute token usage.
    pub fn to_llm_context(&self) -> PipelineContext {
        PipelineContext {
            doc_id: None,
            document_id: self.document_id.map(|id| id.to_string()),
            extraction_id: self.extraction_id,
            vendor_id: self.vendor_id.clone(),
            request_id: None,
            job_id: self.job_id.map(|id| id.to_string()),
            billing_user_id: self.billing_user_id.clone(),
            api_key_id: self.api_key_id,
        }
    }
}

// ── Job payload helpers ──────────────────────────────────────────────────────

/// The MLflow trace context threaded through a job's payload, if any.
pub fn job_trace_context(job: &Value) -> Option<Value> {
    job.get("payload")?
        .get("trace_context")
        .filter(|v| v.is_object())
        .cloned()
}

/// A downstream job's payload, carrying this job's trace context forward.
pub fn payload_with_trace(job: &Value, payload: Value) -> Value {
    let mut out = payload.as_object().cloned().unwrap_or_default();
    if let Some(ctx) = job_trace_context(job) {
        out.insert("trace_context".into(), ctx);
    }
    Value::Object(out)
}

/// Total number of field locations across either payload shape.
pub fn field_location_count(field_locations: &Value) -> usize {
    match field_locations {
        Value::Array(list) => list
            .iter()
            .filter_map(Value::as_object)
            .map(Map::len)
            .sum(),
        Value::Object(map) => map.len(),
        _ => 0,
    }
}

/// Drop underscore-prefixed internal keys (`_page`, …) from line items before
/// the result is persisted — they are routing metadata, not extracted data.
pub fn strip_internal_keys(result: &Value) -> Value {
    match result {
        Value::Array(records) => Value::Array(records.iter().map(strip_internal_keys).collect()),
        Value::Object(obj) => {
            let mut out = obj.clone();
            if let Some(Value::Array(items)) = out.get("line_items") {
                let cleaned: Vec<Value> = items
                    .iter()
                    .map(|item| match item.as_object() {
                        Some(map) => Value::Object(
                            map.iter()
                                .filter(|(k, _)| !k.starts_with('_'))
                                .map(|(k, v)| (k.clone(), v.clone()))
                                .collect(),
                        ),
                        None => item.clone(),
                    })
                    .collect();
                out.insert("line_items".into(), Value::Array(cleaned));
            }
            // The v3 shape nests everything one level deeper.
            if let Some(fields) = out.get("fields").filter(|f| f.is_object()) {
                let cleaned = strip_internal_keys(fields);
                out.insert("fields".into(), cleaned);
            }
            Value::Object(out)
        }
        other => other.clone(),
    }
}

/// Compact result description for logs and traces — never the full payload,
/// which can be megabytes.
pub fn result_summary(result: &Value) -> Value {
    match result {
        Value::Array(records) => return json!({"record_count": records.len()}),
        Value::Object(_) => {}
        other => {
            let kind = match other {
                Value::Null => "NoneType",
                Value::Bool(_) => "bool",
                Value::Number(_) => "int",
                Value::String(_) => "str",
                _ => "unknown",
            };
            return json!({"type": kind});
        }
    }

    let fields = match result.get("fields") {
        Some(f) if f.is_object() => f,
        _ => result,
    };
    let line_items = fields
        .get("line_items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let header: Map<String, Value> = fields
        .as_object()
        .map(|obj| {
            obj.iter()
                .filter(|(k, _)| {
                    k.as_str() != "line_items" && k.as_str() != "boxes" && !k.starts_with('_')
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect()
        })
        .unwrap_or_default();

    json!({
        "fields": Value::Object(header),
        "line_items_count": line_items.len(),
        "line_items_sample": line_items.into_iter().take(5).collect::<Vec<_>>(),
    })
}

// ── Pipeline-continuation predicates ─────────────────────────────────────────

/// True when any page result carries an `_error`.
pub fn page_results_have_errors(page_results: &Value) -> bool {
    page_results
        .as_array()
        .is_some_and(|list| list.iter().any(|pr| truthy(pr.get("_error").unwrap_or(&Value::Null))))
}

/// True when the whole document failed — either the merge marker is set or
/// every page carries an error.
pub fn all_pages_failed(result: &Value, page_results: &Value) -> bool {
    if truthy(result.get("_all_pages_failed").unwrap_or(&Value::Null)) {
        return true;
    }
    match page_results.as_array() {
        Some(list) if !list.is_empty() => list.iter().all(|pr| pr.get("_error").is_some()),
        _ => false,
    }
}

/// Why postprocess must not run, or `None` when the extraction is healthy.
///
/// Postprocess writes the terminal `done` status, so letting it run on a
/// failed extraction would mark a broken document as successfully extracted.
pub fn postprocess_failure_reason(extraction: &Value) -> Option<String> {
    let error = extraction.get("error").and_then(Value::as_str);
    if error == Some(LLM_FAILED_ERROR) {
        return Some(LLM_FAILED_MESSAGE.to_string());
    }
    let result = extraction.get("result").unwrap_or(&Value::Null);
    let page_results = extraction.get("page_results").unwrap_or(&Value::Null);
    if all_pages_failed(result, page_results) || page_results_have_errors(page_results) {
        return Some(LLM_FAILED_MESSAGE.to_string());
    }
    let status = extraction.get("status").and_then(Value::as_str).unwrap_or("");
    if matches!(status, "failed" | "partial" | "cancelled" | "unverified") {
        return Some(
            error
                .filter(|e| !e.is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| format!("Extraction is {status}")),
        );
    }
    None
}

// ── Shared database operations ───────────────────────────────────────────────

/// Stop the pipeline if the user requested cancellation.
///
/// Returns `true` when the job should stop. Work already done is preserved:
/// an extraction with partial output becomes `partial` rather than
/// `cancelled`, so the user keeps the pages that succeeded.
pub async fn stop_if_cancelled(
    pool: &PgPool,
    extraction_id: i64,
    stage: &str,
    message: &str,
) -> JobResult<bool> {
    if !augocr_common::db::is_cancel_requested(pool, extraction_id).await? {
        return Ok(false);
    }
    let Some(row) = augocr_common::db::get_extraction(pool, extraction_id).await? else {
        return Ok(true);
    };
    let has_output = truthy(row.get("result").unwrap_or(&Value::Null))
        || truthy(row.get("page_results").unwrap_or(&Value::Null));
    let status = if has_output { "partial" } else { "cancelled" };

    augocr_common::db::set_extraction_status(
        pool,
        extraction_id,
        status,
        Some(&json!({"stage": stage, "message": message})),
        None,
        None,
        false,
    )
    .await?;
    release_quota_quietly(
        pool,
        row.get("document_id").and_then(Value::as_i64),
        None,
        "cancel",
        extraction_id,
    )
    .await;
    Ok(true)
}

/// Release a page reservation, logging rather than failing on error.
///
/// A quota release that fails must never take down the job that triggered it:
/// the reservation expires on its own, but a lost extraction does not come
/// back.
pub async fn release_quota_quietly(
    pool: &PgPool,
    document_id: Option<i64>,
    user_id: Option<&str>,
    context: &str,
    extraction_id: i64,
) {
    if let Err(e) = augocr_common::db::release_quota_once(pool, document_id, user_id).await {
        tracing::warn!("quota release failed ({context}) ext={extraction_id}: {e}");
    }
}

/// Enqueue postprocess if, and only if, the extraction is ready for it.
///
/// The readiness check is a single cheap query rather than loading the
/// extraction row, which carries multi-megabyte JSONB columns.
pub async fn maybe_enqueue_postprocess(
    pool: &PgPool,
    extraction_id: i64,
    document_id: Option<i64>,
    trace_context: Option<Value>,
) -> JobResult<()> {
    if !augocr_common::db::is_postprocess_ready(pool, extraction_id).await? {
        return Ok(());
    }
    let mut payload = Map::new();
    payload.insert("extraction_id".into(), json!(extraction_id));
    if let Some(ctx) = trace_context {
        payload.insert("trace_context".into(), ctx);
    }
    augocr_common::db::ensure_job(
        pool,
        Some(extraction_id),
        document_id,
        "postprocess",
        Some(&Value::Object(payload)),
        100,
        3,
    )
    .await?;
    Ok(())
}

/// Update progress, unless the extraction has already failed.
///
/// Returns `false` when the caller must stop — a failed extraction has been
/// marked as such and must not be walked forward.
pub async fn update_progress_if_not_failed(
    pool: &PgPool,
    extraction_id: i64,
    progress: &Value,
) -> JobResult<bool> {
    let Some(current) = augocr_common::db::get_extraction(pool, extraction_id).await? else {
        return Ok(false);
    };
    if let Some(reason) = postprocess_failure_reason(&current) {
        let error = current
            .get("error")
            .and_then(Value::as_str)
            .filter(|e| !e.is_empty())
            .unwrap_or(LLM_FAILED_ERROR)
            .to_string();
        augocr_common::db::set_extraction_status(
            pool,
            extraction_id,
            "failed",
            Some(&json!({"stage": progress.get("stage"), "message": reason})),
            Some(&error),
            None,
            false,
        )
        .await?;
        return Ok(false);
    }
    augocr_common::db::update_extraction_progress(pool, extraction_id, progress, Some("processing"))
        .await?;
    Ok(true)
}

/// Load every page of an extraction, with its image bytes, from object storage.
pub async fn load_pages(
    pool: &PgPool,
    extraction_id: i64,
) -> JobResult<Vec<crate::page::RenderedPage>> {
    use base64::Engine as _;

    let store = augocr_common::object_store::get_store().await?;
    let rows = augocr_common::db::get_pages(pool, extraction_id).await?;
    let total = rows.len() as i64;

    let mut pages = Vec::with_capacity(rows.len());
    for row in &rows {
        let Some(object_key) = row.get("object_key").and_then(Value::as_str) else {
            continue;
        };
        let raw = store.get_bytes(store.artifacts_bucket(), object_key).await?;
        let width = row.get("width").and_then(Value::as_i64).unwrap_or(0);
        let height = row.get("height").and_then(Value::as_i64).unwrap_or(0);
        pages.push(crate::page::RenderedPage {
            page_number: row.get("page_number").and_then(Value::as_i64).unwrap_or(0),
            image_b64: base64::engine::general_purpose::STANDARD.encode(&raw),
            mime_type: row
                .get("mime_type")
                .and_then(Value::as_str)
                .unwrap_or("image/jpeg")
                .to_string(),
            width,
            height,
            // The stored page is already at final size; there is no pre-resize
            // dimension to recover, so they mirror the final one.
            orig_width: width,
            orig_height: height,
            doc_total_pages: total,
            source: row
                .get("source")
                .and_then(Value::as_str)
                .map(str::to_string),
        });
    }
    Ok(pages)
}

// ── Dispatch ─────────────────────────────────────────────────────────────────

/// Route a claimed job to its stage handler.
pub async fn process_job(pool: &PgPool, stage: &str, job: &Value) -> JobResult<()> {
    match stage {
        "normalize" => normalize::run(pool, job).await,
        "ocr" => ocr::run(pool, job).await,
        "llm" => llm::run(pool, job).await,
        "postprocess" => postprocess::run(pool, job).await,
        other => Err(JobError::Failed(format!("Unknown worker stage: {other}"))),
    }
}

/// `job["extraction_id"]`, or a hard failure — every stage needs one.
pub fn extraction_id_of(job: &Value) -> JobResult<i64> {
    job.get("extraction_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| JobError::Failed("job has no extraction_id".to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pipeline_base_prefers_the_first_truthy_source() {
        let extraction = json!({
            "id": 7, "document_id": 3, "vendor_id": "v1", "vendor_name": "Acme",
            "filename": "from_extraction.pdf"
        });
        let document = json!({
            "id": 3, "filename": "from_document.pdf",
            "metadata": {"billing_user_id": "u1", "api_key_id": 9, "reserved_pages": 4}
        });
        let job = json!({"id": 42, "extraction_id": 7, "document_id": 3});

        let base = PipelineBase::new(Some(&extraction), Some(&document), Some(&job));
        assert_eq!(base.extraction_id, Some(7));
        assert_eq!(base.document_id, Some(3));
        assert_eq!(base.job_id, Some(42));
        assert_eq!(base.vendor_id.as_deref(), Some("v1"));
        assert_eq!(
            base.filename.as_deref(),
            Some("from_document.pdf"),
            "document filename wins over the extraction's"
        );
        assert_eq!(base.billing_user_id.as_deref(), Some("u1"));
        assert_eq!(base.api_key_id, Some(9));
        assert_eq!(base.reserved_pages, Some(4));
    }

    #[test]
    fn pipeline_base_falls_back_through_the_job() {
        let job = json!({"id": 1, "extraction_id": 55, "document_id": 66});
        let base = PipelineBase::new(None, None, Some(&job));
        assert_eq!(base.extraction_id, Some(55));
        assert_eq!(base.document_id, Some(66));
        assert_eq!(base.vendor_id, None);
        assert_eq!(base.billing_user_id, None);
    }

    #[test]
    fn pipeline_base_ignores_falsy_ids() {
        // Python's `or` chain skips 0 and "" as well as null.
        let extraction = json!({"id": 0, "vendor_id": ""});
        let job = json!({"extraction_id": 12, "id": 3});
        let base = PipelineBase::new(Some(&extraction), None, Some(&job));
        assert_eq!(base.extraction_id, Some(12));
        assert_eq!(base.vendor_id, None);
    }

    #[test]
    fn trace_context_round_trips_into_downstream_payloads() {
        let job = json!({"payload": {"trace_context": {"trace_id": "abc"}}});
        assert_eq!(job_trace_context(&job), Some(json!({"trace_id": "abc"})));

        let out = payload_with_trace(&job, json!({"extraction_id": 5}));
        assert_eq!(out["extraction_id"], 5);
        assert_eq!(out["trace_context"]["trace_id"], "abc");

        // A non-object trace context is ignored rather than propagated.
        let bad = json!({"payload": {"trace_context": "nope"}});
        assert_eq!(job_trace_context(&bad), None);
        assert_eq!(payload_with_trace(&bad, json!({"a": 1})), json!({"a": 1}));
    }

    #[test]
    fn field_location_count_handles_both_shapes() {
        assert_eq!(field_location_count(&json!({"a": {}, "b": {}})), 2);
        assert_eq!(
            field_location_count(&json!([{"a": {}}, {"b": {}, "c": {}}, "junk"])),
            3
        );
        assert_eq!(field_location_count(&json!(null)), 0);
    }

    #[test]
    fn strip_internal_keys_cleans_line_items_at_both_levels() {
        let result = json!({
            "po_number": "P1",
            "line_items": [{"sku": "a", "_page": 1}, "loose"]
        });
        let out = strip_internal_keys(&result);
        assert_eq!(out["line_items"][0], json!({"sku": "a"}));
        assert_eq!(out["line_items"][1], "loose");
        assert_eq!(out["po_number"], "P1");

        let v3 = json!({"fields": {"line_items": [{"sku": "a", "_page": 2}]}});
        let out = strip_internal_keys(&v3);
        assert_eq!(out["fields"]["line_items"][0], json!({"sku": "a"}));

        let list = json!([{"line_items": [{"_page": 1, "q": 2}]}]);
        assert_eq!(strip_internal_keys(&list)[0]["line_items"][0], json!({"q": 2}));
    }

    #[test]
    fn result_summary_is_compact_for_each_shape() {
        let s = result_summary(&json!([{"a": 1}, {"b": 2}]));
        assert_eq!(s, json!({"record_count": 2}));

        assert_eq!(result_summary(&json!("junk")), json!({"type": "str"}));
        assert_eq!(result_summary(&json!(null)), json!({"type": "NoneType"}));

        let items: Vec<Value> = (0..9).map(|i| json!({"n": i})).collect();
        let s = result_summary(&json!({
            "po_number": "P1", "boxes": {"x": 1}, "_page": 1, "line_items": items
        }));
        assert_eq!(s["fields"], json!({"po_number": "P1"}), "metadata excluded");
        assert_eq!(s["line_items_count"], 9);
        assert_eq!(
            s["line_items_sample"].as_array().map(Vec::len),
            Some(5),
            "sample is capped"
        );
    }

    #[test]
    fn failure_predicates_detect_each_way_a_document_can_break() {
        assert!(page_results_have_errors(&json!([{"_error": "boom"}])));
        assert!(!page_results_have_errors(&json!([{"fields": {}}])));
        assert!(!page_results_have_errors(&json!(null)));
        // An empty-string error is falsy in Python.
        assert!(!page_results_have_errors(&json!([{"_error": ""}])));

        assert!(all_pages_failed(&json!({"_all_pages_failed": true}), &json!(null)));
        assert!(all_pages_failed(&json!({}), &json!([{"_error": "a"}, {"_error": "b"}])));
        assert!(!all_pages_failed(&json!({}), &json!([{"_error": "a"}, {"ok": 1}])));
        assert!(!all_pages_failed(&json!({}), &json!([])));
    }

    #[test]
    fn postprocess_is_blocked_for_every_failure_mode() {
        let llm_failed = json!({"error": LLM_FAILED_ERROR, "status": "processing"});
        assert_eq!(
            postprocess_failure_reason(&llm_failed).as_deref(),
            Some(LLM_FAILED_MESSAGE)
        );

        let page_failed = json!({"status": "processing", "page_results": [{"_error": "boom"}]});
        assert_eq!(
            postprocess_failure_reason(&page_failed).as_deref(),
            Some(LLM_FAILED_MESSAGE)
        );

        let cancelled = json!({"status": "cancelled", "error": "user stopped"});
        assert_eq!(
            postprocess_failure_reason(&cancelled).as_deref(),
            Some("user stopped")
        );

        // A terminal status with no recorded error still blocks, with a
        // generated message.
        let partial = json!({"status": "partial"});
        assert_eq!(
            postprocess_failure_reason(&partial).as_deref(),
            Some("Extraction is partial")
        );

        let healthy = json!({"status": "processing", "result": {"po_number": "P1"}});
        assert_eq!(postprocess_failure_reason(&healthy), None);
    }

    #[test]
    fn llm_context_carries_the_billing_identifiers() {
        let base = PipelineBase {
            extraction_id: Some(7),
            document_id: Some(3),
            job_id: Some(42),
            vendor_id: Some("v1".into()),
            billing_user_id: Some("u1".into()),
            api_key_id: Some(9),
            ..Default::default()
        };
        let ctx = base.to_llm_context();
        assert_eq!(ctx.extraction_id, Some(7));
        assert_eq!(ctx.document_id.as_deref(), Some("3"));
        assert_eq!(ctx.job_id.as_deref(), Some("42"));
        assert_eq!(ctx.billing_user_id.as_deref(), Some("u1"));
        assert_eq!(ctx.api_key_id, Some(9));
    }

    #[test]
    fn extraction_id_of_requires_the_field() {
        assert_eq!(extraction_id_of(&json!({"extraction_id": 5})).ok(), Some(5));
        assert!(extraction_id_of(&json!({})).is_err());
    }
}
