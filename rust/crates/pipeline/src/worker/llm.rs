//! llm stage ← `worker._process_llm`.
//!
//! Builds the vendor's prompts, runs every page through the vision model, and
//! persists the merged result. Page 1 also learns layout boxes for the review
//! UI when the caller asked for them (the web UI does; the JSON API does not).
//!
//! This stage never writes the terminal `done` status — postprocess does, once
//! field locations exist — so the SSE stream stays open until the review data
//! is actually ready.

use std::time::Instant;

use augocr_common::pyjson::truthy;
use serde_json::{json, Map, Value};
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use super::{
    extraction_id_of, job_trace_context, load_pages, maybe_enqueue_postprocess,
    release_quota_quietly, strip_internal_keys, JobError, JobResult, PipelineBase,
    LLM_FAILED_ERROR, LLM_FAILED_MESSAGE, NO_INVOICE_ERROR, NO_INVOICE_MESSAGE,
};
use crate::extractor::{self, ExtractRequest, PageObserver, PromptSpec};
use crate::llm::LlmClient;
use crate::page_logger;

pub async fn run(pool: &PgPool, job: &Value) -> JobResult<()> {
    let extraction_id = extraction_id_of(job)?;
    let extraction = augocr_common::db::get_extraction(pool, extraction_id)
        .await?
        .ok_or_else(|| JobError::Failed("Extraction not found for LLM job".to_string()))?;
    let document = match job.get("document_id").and_then(Value::as_i64) {
        Some(id) => augocr_common::db::get_document(pool, id).await?,
        None => None,
    };
    let base = PipelineBase::new(Some(&extraction), document.as_ref(), Some(job))
        .with_extraction_id(extraction_id);

    // Layout boxes come from document metadata: the UI wants them, the JSON
    // API does not (and paying for them would be waste).
    let include_layout_boxes = document
        .as_ref()
        .and_then(|d| d.get("metadata"))
        .and_then(|m| m.get("include_layout_boxes"))
        .is_none_or(truthy);
    let vendor_id = extraction
        .get("vendor_id")
        .and_then(Value::as_str)
        .ok_or_else(|| JobError::Failed("extraction has no vendor_id".to_string()))?
        .to_string();
    tracing::info!(
        "── LLM started ── ext={extraction_id} vendor={vendor_id} \
         include_layout_boxes={include_layout_boxes}"
    );

    // ── Resolve field configuration ──
    let tmpl = augocr_common::db::get_template(pool, &vendor_id).await?;
    let header_fields = pick_fields(&extraction, tmpl.as_ref(), "header_fields");
    let line_item_fields = pick_fields(&extraction, tmpl.as_ref(), "line_item_fields");
    // The template is the source of truth; a stale extraction row is a
    // fallback only.
    let format_type = tmpl
        .as_ref()
        .and_then(|t| t.get("format_type"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| extraction.get("format_type").and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .unwrap_or("single_po_multipage")
        .to_string();
    let universal_agent = truthy(extraction.get("universal_agent").unwrap_or(&Value::Null));

    let model = LlmClient::global().model().to_string();
    tracing::info!(
        "LLM config: model={model} format={format_type} universal_agent={universal_agent} \
         headers={header_fields:?} line_items={line_item_fields:?}"
    );

    // ── Build prompts fresh from the current DB fields ──
    let gold_examples = augocr_common::db::get_gold_examples(pool, &vendor_id, None).await?;
    let instructions = tmpl
        .as_ref()
        .and_then(|t| t.get("prompt_instructions"))
        .and_then(Value::as_str);
    let rules: Vec<String> = tmpl
        .as_ref()
        .and_then(|t| t.get("extraction_rules"))
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();

    let mut spec = PromptSpec {
        header_fields: &header_fields,
        line_item_fields: &line_item_fields,
        instructions,
        rules: &rules,
        format_type: &format_type,
        gold_examples: &gold_examples,
        include_boxes: false,
        universal_invoice: universal_agent,
    };
    // Pages 2+: fields only.
    let system_prompt = extractor::build_system_prompt(&spec);
    // Page 1 additionally requests bounding boxes, but only when the caller
    // wants layout data.
    let system_prompt_page1 = if include_layout_boxes {
        spec.include_boxes = true;
        let p1 = extractor::build_system_prompt(&spec);
        spec.include_boxes = false;
        Some(p1)
    } else {
        None
    };
    if !gold_examples.is_empty() {
        tracing::info!(
            "LLM prompt includes {} value-redacted correction hint set(s) for vendor={vendor_id}",
            gold_examples.len()
        );
    }

    // ── Run the model ──
    let pages = load_pages(pool, extraction_id).await?;
    let cancel = CancellationToken::new();
    let started = Instant::now();

    augocr_common::db::update_extraction_progress(
        pool,
        extraction_id,
        &json!({
            "stage": "llm",
            "message": format!("Starting vision extraction on {} page(s)", pages.len()),
            "total_pages": pages.len(),
        }),
        Some("processing"),
    )
    .await?;

    let observer = ProgressObserver {
        pool,
        extraction_id,
        job_id: base.job_id,
        cancel: cancel.clone(),
    };
    let payload = job.get("payload").unwrap_or(&Value::Null);
    let existing_page_results = payload
        .get("existing_page_results")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let llm_ctx = base.to_llm_context();
    let output = extractor::extract_document(ExtractRequest {
        pages: &pages,
        spec: spec.clone(),
        system_prompt: &system_prompt,
        system_prompt_page1: system_prompt_page1.as_deref(),
        format_type: &format_type,
        include_page1_boxes: include_layout_boxes,
        start_from_page: payload
            .get("start_from_page")
            .and_then(Value::as_i64)
            .unwrap_or(1),
        existing_page_results,
        pipeline_context: Some(&llm_ctx),
        pool: Some(pool),
        cancel: Some(&cancel),
        observer: Some(&observer),
    })
    .await;

    let elapsed_ms = started.elapsed().as_millis() as i64;
    tracing::info!(
        "Document extracted: {} page results, cancelled={} ({elapsed_ms}ms)",
        output.page_results.len(),
        output.cancelled
    );

    // ── Learn page-1 layout boxes ──
    if include_layout_boxes {
        if let Some(template_id) = tmpl.as_ref().and_then(|t| t.get("id")).and_then(Value::as_i64) {
            learn_layout_boxes(
                pool,
                &output.page_results,
                &line_item_fields,
                &vendor_id,
                template_id,
                extraction_id,
            )
            .await?;
        }
    }

    persist_outcome(pool, extraction_id, job, &base, &pages, output, elapsed_ms).await
}

/// Decide the extraction's status, write the result, and route what happens next.
async fn persist_outcome(
    pool: &PgPool,
    extraction_id: i64,
    job: &Value,
    base: &PipelineBase,
    pages: &[crate::page::RenderedPage],
    mut output: extractor::ExtractOutcome,
    elapsed_ms: i64,
) -> JobResult<()> {
    let failed_pages: Vec<Value> = output
        .page_results
        .iter()
        .filter_map(|pr| {
            let error = pr.get("_error")?.as_str()?;
            Some(json!({
                "page": pr.get("_page").cloned().unwrap_or(Value::Null),
                "error": error,
                "error_type": page_logger::classify_error_type(error),
            }))
        })
        .collect();

    let no_invoice = output.no_invoice_pages;
    let mut user_cancelled = false;
    let status = if no_invoice {
        // Universal Agent found zero tax-invoice pages — nothing extractable.
        "failed"
    } else if output.cancelled {
        user_cancelled = augocr_common::db::is_cancel_requested(pool, extraction_id).await?;
        if user_cancelled {
            if output.page_results.is_empty() {
                "cancelled"
            } else {
                "partial"
            }
        } else {
            "failed"
        }
    } else if !failed_pages.is_empty() {
        "failed"
    } else {
        // NOT "done": postprocess sets that once field locations exist, which
        // keeps the SSE stream open until the review data is ready.
        "processing"
    };

    let result = output
        .result
        .as_ref()
        .map(strip_internal_keys)
        .unwrap_or(Value::Null);
    let extracted = output.page_results.len() - failed_pages.len();
    let field_count = page_logger::count_result_fields(&result);

    let log_status = if output.cancelled {
        if user_cancelled {
            if output.page_results.is_empty() {
                "cancelled"
            } else {
                "partial"
            }
        } else {
            "failed"
        }
    } else if !failed_pages.is_empty() || no_invoice {
        "failed"
    } else {
        "done"
    };

    if !output.dropped_pages.is_empty() || no_invoice {
        tracing::info!(
            "Universal agent summary ext={extraction_id}: dropped non-invoice page(s): {:?}",
            output.dropped_pages
        );
    }

    let extraction = augocr_common::db::get_extraction(pool, extraction_id)
        .await?
        .unwrap_or(json!({}));
    let mut record: Map<String, Value> = json!({
        "extraction_id": extraction_id,
        "filename": extraction.get("filename").cloned().unwrap_or(Value::Null),
        "vendor_id": extraction.get("vendor_id").cloned().unwrap_or(Value::Null),
        "attempt_number": job.get("attempts").and_then(Value::as_i64).unwrap_or(1),
        "total_pages": pages.len(),
        "billable_pages": pages.len(),
        "digital_pages": pages.iter().filter(|p| p.source.as_deref() == Some("pypdfium")).count(),
        "scanned_pages": pages.iter().filter(|p| p.source.as_deref() == Some("paddleocr")).count(),
        "qwen_extracted_pages": extracted,
        "qwen_failed_pages": failed_pages.len(),
        "qwen_skipped_pages": pages.len().saturating_sub(output.page_results.len()),
        "field_count": field_count,
        "empty_result": field_count == 0,
        "duration_ms": elapsed_ms,
        "status": log_status,
        "errors": if failed_pages.is_empty() { Value::Null } else { json!(failed_pages) },
    })
    .as_object()
    .cloned()
    .unwrap_or_default();
    page_logger::append_log(&mut record).await;

    let error = if no_invoice {
        Some(NO_INVOICE_ERROR)
    } else if status == "failed" {
        Some(LLM_FAILED_ERROR)
    } else {
        None
    };
    let message = if no_invoice {
        NO_INVOICE_MESSAGE
    } else if status == "processing" {
        "LLM extraction complete, awaiting field mapping"
    } else if status == "failed" {
        LLM_FAILED_MESSAGE
    } else {
        status
    };

    output.result = Some(result.clone());
    augocr_common::db::update_extraction_result(
        pool,
        extraction_id,
        Some(&result),
        Some(&json!(output.page_results)),
        status,
        Some(elapsed_ms),
        error,
        None,
        Some(&json!({
            "stage": "llm",
            "message": message,
            "last_completed_page": output.last_completed_page,
            "total_pages": pages.len(),
            "failed_pages": failed_pages.iter()
                .map(|p| p.get("page").cloned().unwrap_or(Value::Null))
                .collect::<Vec<_>>(),
            "dropped_pages": output.dropped_pages,
        })),
    )
    .await?;
    tracing::info!("LLM result persisted: status={status} elapsed={elapsed_ms}ms");

    // Cancellation and page failures both end the pipeline before postprocess.
    // Release the reservation now, or `pending_pages` blocks the user's next
    // upload until it expires.
    if output.cancelled || status == "failed" {
        release_quota_quietly(
            pool,
            base.document_id,
            base.billing_user_id.as_deref(),
            if user_cancelled { "llm cancel" } else { "llm failed" },
            extraction_id,
        )
        .await;
        if user_cancelled {
            return Err(JobError::Cancelled(
                "Cancelled during LLM extraction".to_string(),
            ));
        }
        // An internal failure leaves the extraction failed; a resume can
        // retry just the missing pages.
        return Ok(());
    }

    if status == "processing" {
        maybe_enqueue_postprocess(
            pool,
            extraction_id,
            job.get("document_id").and_then(Value::as_i64),
            job_trace_context(job),
        )
        .await?;
    }
    Ok(())
}

/// Save the label boxes page 1 reported, for the review UI to reuse.
///
/// Qwen3-VL emits relative 0–1000 coordinates, so normalising is a plain
/// divide — `processor` pre-aligns images to multiples of 32 precisely so the
/// model never re-pads internally and the grid stays exact.
async fn learn_layout_boxes(
    pool: &PgPool,
    page_results: &[Value],
    line_item_fields: &[String],
    vendor_id: &str,
    template_id: i64,
    extraction_id: i64,
) -> JobResult<()> {
    // Learn from page 1 — but never from a page the Universal Agent
    // classified as non-invoice (an e-way bill cover page, say). Results are
    // page-ordered, so this is page 1 when it is an invoice, else the first
    // invoice page.
    let Some(p1) = page_results.iter().find(|pr| {
        pr.get("_error").is_none() && pr.get("_invoice") != Some(&json!(false))
    }) else {
        return Ok(());
    };
    let Some(boxes) = p1.get("boxes").and_then(Value::as_object) else {
        return Ok(());
    };
    if boxes.is_empty() {
        return Ok(());
    }

    let mut learned = Map::new();
    for (field_key, raw_box) in boxes {
        let Some(coords) = raw_box.as_array().filter(|a| a.len() == 4) else {
            continue;
        };
        let parsed: Option<Vec<f64>> = coords
            .iter()
            .map(|c| c.as_f64().filter(|f| f.is_finite()))
            .collect();
        let Some(c) = parsed else { continue };

        let nx0 = (c[0] / 1000.0).max(0.0);
        let ny0 = (c[1] / 1000.0).max(0.0);
        let nx1 = (c[2] / 1000.0).min(1.0);
        let ny1 = (c[3] / 1000.0).min(1.0);
        // A degenerate box would draw an invisible or inverted highlight.
        if nx1 <= nx0 || ny1 <= ny0 {
            continue;
        }

        let field_type = if line_item_fields.iter().any(|f| f == field_key) {
            "line_item_column"
        } else {
            "header"
        };
        learned.insert(
            field_key.clone(),
            json!({
                "normalized_box": {"x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1},
                "field_type": field_type,
            }),
        );
    }

    if learned.is_empty() {
        return Ok(());
    }
    let count = learned.len();
    augocr_common::db::upsert_qwen_layout_boxes(
        pool,
        vendor_id,
        template_id as i32,
        Some(extraction_id as i32),
        &Value::Object(learned),
    )
    .await?;
    tracing::info!(
        "Page 1 boxes: saved {count} field(s) for vendor={vendor_id} template={template_id}"
    );
    Ok(())
}

/// `extraction[key] or template[key] or []` — the template is the fallback.
fn pick_fields(extraction: &Value, tmpl: Option<&Value>, key: &str) -> Vec<String> {
    let from = |v: Option<&Value>| -> Option<Vec<String>> {
        let list = v?.as_array()?;
        if list.is_empty() {
            return None;
        }
        Some(list.iter().filter_map(Value::as_str).map(str::to_string).collect())
    };
    from(extraction.get(key))
        .or_else(|| from(tmpl.and_then(|t| t.get(key))))
        .unwrap_or_default()
}

/// Streams per-page progress to the database and watches for cancellation.
struct ProgressObserver<'a> {
    pool: &'a PgPool,
    extraction_id: i64,
    job_id: Option<i64>,
    cancel: CancellationToken,
}

#[async_trait::async_trait]
impl PageObserver for ProgressObserver<'_> {
    async fn on_page_done(&self, page_num: i64, total_pages: i64, result: &Value) {
        let progress = json!({
            "stage": "llm",
            "message": format!("Extracting page {page_num}/{total_pages}"),
            "page": page_num,
            "total_pages": total_pages,
        });

        // Progress reporting must never abort an extraction that is otherwise
        // succeeding, so every failure here is logged and swallowed.
        if let Some(job_id) = self.job_id {
            if let Err(e) =
                augocr_common::db::update_job_progress(self.pool, job_id, &progress).await
            {
                tracing::warn!("on_page_done: job progress update failed page {page_num}: {e}");
            }
        }
        if let Err(e) = augocr_common::db::update_extraction_progress(
            self.pool,
            self.extraction_id,
            &progress,
            Some("processing"),
        )
        .await
        {
            tracing::warn!("on_page_done: extraction progress update failed page {page_num}: {e}");
        }
        // Persist the page immediately so a resume never re-bills a page that
        // already succeeded.
        if let Err(e) = augocr_common::db::update_extraction_result(
            self.pool,
            self.extraction_id,
            None,
            None,
            "processing",
            None,
            None,
            Some(std::slice::from_ref(result)),
            Some(&progress),
        )
        .await
        {
            tracing::warn!("on_page_done: partial result save failed page {page_num}: {e}");
        }

        match augocr_common::db::is_cancel_requested(self.pool, self.extraction_id).await {
            Ok(true) => self.cancel.cancel(),
            Ok(false) => {}
            Err(e) => tracing::warn!("on_page_done: cancel check failed page {page_num}: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pick_fields_prefers_the_extraction_then_the_template() {
        let extraction = json!({"header_fields": ["from_extraction"]});
        let tmpl = json!({"header_fields": ["from_template"]});
        assert_eq!(
            pick_fields(&extraction, Some(&tmpl), "header_fields"),
            vec!["from_extraction"]
        );

        // An empty list is falsy in Python, so it falls through.
        let empty = json!({"header_fields": []});
        assert_eq!(
            pick_fields(&empty, Some(&tmpl), "header_fields"),
            vec!["from_template"]
        );
        assert_eq!(
            pick_fields(&json!({}), Some(&tmpl), "header_fields"),
            vec!["from_template"]
        );
        assert!(pick_fields(&json!({}), None, "header_fields").is_empty());
    }
}
