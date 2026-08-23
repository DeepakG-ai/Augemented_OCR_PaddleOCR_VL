//! postprocess stage ← `worker._process_postprocess`.
//!
//! The terminal stage. It builds `field_locations` for the review UI, applies
//! spatial memory from past corrections, runs the ERP field mapping, and only
//! then writes the `done` status and releases the page reservation.
//!
//! Because this stage owns the terminal status, it refuses to run on an
//! extraction that already failed — otherwise a broken document would be
//! reported as successfully extracted.

use std::time::Instant;

use augocr_common::pyjson::truthy;
use serde_json::{json, Map, Value};
use sqlx::PgPool;

use super::{
    extraction_id_of, field_location_count, release_quota_quietly, result_summary,
    stop_if_cancelled, strip_internal_keys, JobError, JobResult, PipelineBase,
    LLM_FAILED_ERROR, OCR_REVIEW_UNAVAILABLE_ERROR, OCR_REVIEW_UNAVAILABLE_MESSAGE,
};
use crate::page::PageSize;
use crate::{page_logger, qwen_layout_apply, spatial_memory};

pub async fn run(pool: &PgPool, job: &Value) -> JobResult<()> {
    let extraction_id = extraction_id_of(job)?;
    let extraction = augocr_common::db::get_extraction(pool, extraction_id)
        .await?
        .ok_or_else(|| JobError::Failed("Extraction not found for postprocess job".to_string()))?;

    let document_id = extraction
        .get("document_id")
        .and_then(Value::as_i64)
        .or_else(|| job.get("document_id").and_then(Value::as_i64));
    let document = match document_id {
        Some(id) => augocr_common::db::get_document(pool, id).await?,
        None => None,
    };
    let base = PipelineBase::new(Some(&extraction), document.as_ref(), Some(job))
        .with_extraction_id(extraction_id);
    let include_layout_boxes = document
        .as_ref()
        .and_then(|d| d.get("metadata"))
        .and_then(|m| m.get("include_layout_boxes"))
        .is_none_or(truthy);
    tracing::info!(
        "── POSTPROCESS started ── ext={extraction_id} include_layout_boxes={include_layout_boxes}"
    );

    let mut result = extraction.get("result").cloned().unwrap_or(Value::Null);
    if !truthy(&result) {
        return Err(JobError::Failed(
            "Postprocess prerequisites not satisfied (no result)".to_string(),
        ));
    }

    let ocr_data = extraction.get("ocr_data").cloned().unwrap_or(Value::Null);
    let page_results = extraction.get("page_results").cloned().unwrap_or(Value::Null);
    let vendor_id = extraction
        .get("vendor_id")
        .and_then(Value::as_str)
        .map(str::to_string);
    let template_id = extraction.get("template_id").and_then(Value::as_i64);

    // ── Gate: never finalise a failed extraction as done ──
    if let Some(reason) = super::postprocess_failure_reason(&extraction) {
        tracing::warn!(
            "Postprocess blocked for failed extraction: ext={extraction_id} status={:?} error={:?}",
            extraction.get("status"),
            extraction.get("error")
        );
        let error = extraction
            .get("error")
            .and_then(Value::as_str)
            .filter(|e| !e.is_empty())
            .unwrap_or(LLM_FAILED_ERROR)
            .to_string();
        augocr_common::db::set_extraction_status(
            pool,
            extraction_id,
            "failed",
            Some(&json!({"stage": "postprocess", "message": reason})),
            Some(&error),
            None,
            false,
        )
        .await?;
        return Err(JobError::Failed(reason));
    }

    let status = extraction.get("status").and_then(Value::as_str).unwrap_or("");
    if status == "done" {
        tracing::info!("Postprocess skipped: extraction already done ext={extraction_id}");
        return Ok(());
    }
    if status != "processing" {
        return Err(JobError::Failed(format!(
            "Postprocess prerequisites not satisfied (status={status})"
        )));
    }

    // ── JSON-only success when OCR failed outright ──
    let latest_ocr_job =
        augocr_common::db::get_latest_job_for_extraction_type(pool, extraction_id, "ocr").await?;
    let ocr_failed = latest_ocr_job
        .as_ref()
        .and_then(|j| j.get("status"))
        .and_then(Value::as_str)
        == Some("failed");

    if !truthy(&ocr_data) && ocr_failed {
        let ocr_error = latest_ocr_job
            .as_ref()
            .and_then(|j| j.get("error"))
            .and_then(Value::as_str)
            .filter(|e| !e.is_empty())
            .unwrap_or("OCR failed")
            .to_string();
        return finalize_json_only(
            pool,
            extraction_id,
            &base,
            vendor_id.as_deref(),
            &result,
            &ocr_error,
        )
        .await;
    }

    // ── Build field_locations from the learned layout ──
    let qwen_boxes = match (include_layout_boxes, vendor_id.as_deref(), template_id) {
        (true, Some(vid), Some(tid)) => {
            augocr_common::db::get_qwen_layout_boxes(pool, vid, tid as i32).await?
        }
        _ => json!({}),
    };
    let qwen_boxes = qwen_boxes.as_object().cloned().unwrap_or_default();

    let mut field_locations = if qwen_boxes.is_empty() {
        if include_layout_boxes {
            tracing::warn!("Postprocess: no layout boxes - empty field_locations");
        } else {
            tracing::info!("Postprocess: layout boxes skipped (API JSON-only mode)");
        }
        json!({})
    } else {
        tracing::info!(
            "Postprocess: qwen_layout_apply ({} learned fields)",
            qwen_boxes.len()
        );
        let pages = page_sizes_for(pool, extraction_id).await?;
        let page_result_list = page_results.as_array().cloned().unwrap_or_default();
        let started = Instant::now();
        let locs = qwen_layout_apply::build_field_locations_from_layout(
            &qwen_boxes,
            &pages,
            &result,
            &page_result_list,
        );
        tracing::info!(
            "Field locations built (qwen_layout): {} mappings ({}ms)",
            field_location_count(&locs),
            started.elapsed().as_millis()
        );
        locs
    };

    // ── Apply spatial memory from prior corrections ──
    // This re-reads the current document's text inside saved regions; it never
    // replays an old value.
    let started = Instant::now();
    let geometry = ocr_data.as_array().cloned().unwrap_or_default();
    let applied = spatial_memory::apply_to_extraction(
        pool,
        extraction_id,
        result,
        field_locations,
        Some(&geometry),
    )
    .await;
    result = applied.result;
    field_locations = applied.field_locations;
    tracing::info!(
        "Spatial memory: {} region(s) applied ({}ms)",
        applied.count,
        started.elapsed().as_millis()
    );

    if applied.count > 0 {
        augocr_common::db::update_extraction_result(
            pool,
            extraction_id,
            Some(&strip_internal_keys(&result)),
            Some(&page_results),
            "processing",
            None,
            None,
            None,
            None,
        )
        .await?;
        tracing::info!(
            "Spatial memory: {} field(s) applied for extraction {extraction_id}",
            applied.count
        );
    }

    augocr_common::db::save_field_locations(pool, extraction_id, &field_locations).await?;
    tracing::info!(
        "Field locations saved: {} mappings",
        field_location_count(&field_locations)
    );

    // Renames merged extraction fields to the program's canonical fields. The
    // raw `result` is untouched; the mapped copy is what API clients read.
    apply_erp_field_mapping(pool, extraction_id, vendor_id.as_deref(), &result).await;

    if stop_if_cancelled(pool, extraction_id, "postprocess", "Cancelled before completion").await? {
        return Err(JobError::Cancelled(
            "Cancelled before completion".to_string(),
        ));
    }

    let mut progress = Map::new();
    progress.insert("stage".into(), json!("postprocess"));
    progress.insert("message".into(), json!("Field mapping complete"));
    if !include_layout_boxes {
        progress.insert("layout_boxes_available".into(), json!(false));
        progress.insert("layout_boxes_reason".into(), json!("api_json_only"));
    }
    augocr_common::db::set_extraction_status(
        pool,
        extraction_id,
        "done",
        Some(&Value::Object(progress)),
        None,
        None,
        // Replace the LLM-stage-only duration with true end-to-end pipeline
        // time, so history-page latency matches the pipeline timer.
        true,
    )
    .await?;
    release_quota_quietly(
        pool,
        base.document_id,
        base.billing_user_id.as_deref(),
        "postprocess",
        extraction_id,
    )
    .await;

    log_completion(pool, extraction_id, &extraction, &result).await;
    Ok(())
}

/// Finish a document whose JSON extracted fine but whose OCR failed.
///
/// The result is real and worth keeping, so this is a success — but review
/// tooling and spatial memory need word geometry that does not exist, so the
/// UI is told review is unavailable rather than shown an empty overlay.
async fn finalize_json_only(
    pool: &PgPool,
    extraction_id: i64,
    base: &PipelineBase,
    vendor_id: Option<&str>,
    result: &Value,
    ocr_error: &str,
) -> JobResult<()> {
    tracing::warn!(
        "Postprocess finalizing JSON-only success with OCR warning: \
         ext={extraction_id} ocr_error={ocr_error}"
    );
    augocr_common::db::save_field_locations(pool, extraction_id, &json!({})).await?;
    apply_erp_field_mapping(pool, extraction_id, vendor_id, result).await;

    augocr_common::db::set_extraction_status(
        pool,
        extraction_id,
        "done",
        Some(&json!({
            "stage": "postprocess",
            "message": OCR_REVIEW_UNAVAILABLE_MESSAGE,
            "warning_code": OCR_REVIEW_UNAVAILABLE_ERROR,
            "review_available": false,
            "ocr_error": ocr_error,
        })),
        None,
        None,
        true,
    )
    .await?;
    release_quota_quietly(
        pool,
        base.document_id,
        base.billing_user_id.as_deref(),
        "postprocess json-only",
        extraction_id,
    )
    .await;
    Ok(())
}

/// Page dimensions for the layout mapper.
async fn page_sizes_for(pool: &PgPool, extraction_id: i64) -> JobResult<Vec<PageSize>> {
    let rows = augocr_common::db::get_pages(pool, extraction_id).await?;
    Ok(rows
        .iter()
        .filter_map(|p| {
            Some(PageSize {
                page_number: p.get("page_number").and_then(Value::as_i64)?,
                width: p.get("width").and_then(Value::as_i64).unwrap_or(0),
                height: p.get("height").and_then(Value::as_i64).unwrap_or(0),
            })
        })
        .collect())
}

/// Apply the configured ERP mapping, or clear a stale mapped result.
///
/// Never fatal: a missing or broken mapping must not sink an extraction whose
/// raw result is perfectly good.
async fn apply_erp_field_mapping(
    pool: &PgPool,
    extraction_id: i64,
    vendor_id: Option<&str>,
    result: &Value,
) {
    match erp_mapping_inner(pool, extraction_id, vendor_id, result).await {
        Ok(true) => tracing::info!("ERP field mapping applied: ext={extraction_id}"),
        Ok(false) => {}
        Err(e) => tracing::warn!("ERP field mapping failed ext={extraction_id}: {e}"),
    }
}

async fn erp_mapping_inner(
    pool: &PgPool,
    extraction_id: i64,
    vendor_id: Option<&str>,
    result: &Value,
) -> augocr_common::error::AppResult<bool> {
    let mapping = match vendor_id {
        Some(vid) => augocr_common::db::get_field_mapping(pool, vid).await?,
        None => None,
    };
    let has_map = mapping.as_ref().is_some_and(|m| {
        truthy(m.get("header_map").unwrap_or(&Value::Null))
            || truthy(m.get("line_map").unwrap_or(&Value::Null))
    });

    let Some(mapping) = mapping.filter(|_| has_map) else {
        augocr_common::db::update_extraction_mapped_result(pool, extraction_id, None).await?;
        return Ok(false);
    };

    let schema = match mapping.get("schema_id").and_then(Value::as_i64) {
        Some(id) => augocr_common::db::get_schema_by_id(pool, id).await?,
        None => None,
    };
    let mapped = augocr_common::field_mapper::apply_mapping(result, &mapping, schema.as_ref());
    augocr_common::db::update_extraction_mapped_result(pool, extraction_id, Some(&mapped)).await?;
    Ok(true)
}

/// Write the completion summary line and structured result block.
///
/// Purely observability, so every failure is swallowed — a summary that
/// cannot be written must not undo a finished extraction.
async fn log_completion(pool: &PgPool, extraction_id: i64, fallback: &Value, result: &Value) {
    let tokens = match augocr_common::db::get_extraction_token_totals(pool, extraction_id).await {
        Ok(t) => t,
        Err(e) => {
            tracing::warn!("post: result summary failed ext={extraction_id}: {e}");
            return;
        }
    };
    let final_row = augocr_common::db::get_extraction(pool, extraction_id)
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| fallback.clone());

    let duration_ms = final_row.get("duration_ms").and_then(Value::as_f64);
    // Read these before the macro: inside `tracing::info!`, the name `Value`
    // resolves to tracing's own `Value` trait, not `serde_json::Value`.
    let total_pages = final_row.get("total_pages").cloned().unwrap_or(Value::Null);
    let token_count = |key: &str| tokens.get(key).and_then(Value::as_i64).unwrap_or(0);
    let (tok_in, tok_out, calls) = (
        token_count("prompt_tokens"),
        token_count("completion_tokens"),
        token_count("llm_calls"),
    );
    tracing::info!(
        "── EXTRACTION COMPLETE ── status=done pages={total_pages} tok_in={tok_in} \
         tok_out={tok_out} calls={calls} ms={duration_ms:?} summary={}",
        result_summary(result)
    );

    let mut record: Map<String, Value> = json!({
        "extraction_id": extraction_id,
        "status": "done",
        "filename": final_row.get("filename").cloned().unwrap_or(Value::Null),
        "vendor_id": final_row
            .get("vendor_name")
            .filter(|v| truthy(v))
            .or_else(|| final_row.get("vendor_id"))
            .cloned()
            .unwrap_or(Value::Null),
        "total_pages": final_row.get("total_pages").cloned().unwrap_or(Value::Null),
        "field_count": page_logger::count_result_fields(result),
        "duration_ms": duration_ms,
    })
    .as_object()
    .cloned()
    .unwrap_or_default();
    page_logger::append_log(&mut record).await;
}
