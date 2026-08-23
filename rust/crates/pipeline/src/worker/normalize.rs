//! normalize stage ← `worker._process_normalize`.
//!
//! Downloads the source PDF, renders every page to a JPEG, classifies each
//! page as digital or scanned, stores the artifacts, and fans out to the `ocr`
//! and `llm` stages.
//!
//! When the document arrived without a vendor, this stage also detects one
//! from page-1 text. That detection ends the pipeline if it fails: an
//! extraction with no vendor (or a vendor with no usable template) cannot
//! produce anything, so no downstream job is queued.

use std::time::Instant;

use augocr_common::pyjson::truthy;
use base64::Engine as _;
use serde_json::{json, Map, Value};
use sqlx::PgPool;

use super::{
    extraction_id_of, job_trace_context, payload_with_trace, release_quota_quietly,
    stop_if_cancelled, JobError, JobResult, PipelineBase,
};
use crate::page::{page_sizes, RenderedPage};
use crate::{geometry, processor, vendor_detector};

pub async fn run(pool: &PgPool, job: &Value) -> JobResult<()> {
    let extraction_id = extraction_id_of(job)?;
    let document_id = job
        .get("document_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| JobError::Failed("normalize job has no document_id".to_string()))?;

    let document = augocr_common::db::get_document(pool, document_id)
        .await?
        .ok_or_else(|| JobError::Failed("Document not found for normalize job".to_string()))?;
    let base = PipelineBase::new(None, Some(&document), Some(job))
        .with_extraction_id(extraction_id);
    let filename = base.filename.clone().unwrap_or_default();
    tracing::info!("── NORMALIZE started ── ext={extraction_id} file={filename}");

    augocr_common::db::update_document_status(pool, document_id, "processing").await?;
    augocr_common::db::set_extraction_status(
        pool,
        extraction_id,
        "processing",
        Some(&json!({"stage": "normalize", "message": "Rendering document pages"})),
        None,
        None,
        false,
    )
    .await?;
    if let Some(job_id) = base.job_id {
        augocr_common::db::update_job_progress(
            pool,
            job_id,
            &json!({"stage": "normalize", "message": "Downloading original document"}),
        )
        .await?;
    }

    // ── Download ──
    let store = augocr_common::object_store::get_store().await?;
    let object_key = document
        .get("object_key")
        .and_then(Value::as_str)
        .ok_or_else(|| JobError::Failed("document has no object_key".to_string()))?;
    let started = Instant::now();
    let raw = store
        .get_bytes(store.documents_bucket(), object_key)
        .await?;
    tracing::info!(
        "Downloaded {filename} ({} bytes, {}ms)",
        raw.len(),
        started.elapsed().as_millis()
    );

    // Defence in depth: every entry point runs the PDF guard already, so a
    // non-PDF reaching the worker means a new upload path skipped it. Fail
    // loudly rather than silently treating it as an image.
    let is_pdf = filename.to_lowercase().ends_with(".pdf") && raw.starts_with(b"%PDF");
    if !is_pdf {
        return Err(JobError::Failed(format!(
            "Worker received non-PDF document: '{filename}'. Only PDFs are supported — \
             check that the upload endpoint enforces _require_pdf."
        )));
    }

    // ── Render ──
    let started = Instant::now();
    let rendered_pages = processor::pdf_to_images(&raw, None, None).await?;
    tracing::info!(
        "Rendered {} PDF page(s) ({}ms)",
        rendered_pages.len(),
        started.elapsed().as_millis()
    );
    if rendered_pages.is_empty() {
        return Err(JobError::Failed(format!(
            "Document '{filename}' produced 0 renderable pages — file may be corrupt, \
             password-protected, or contain only unrenderable content."
        )));
    }

    // ── Classify each page: digital text layer or scanned? ──
    let geo_by_page = classify_pages(&raw, &rendered_pages, extraction_id).await;

    // ── Store page artifacts ──
    let started = Instant::now();
    let (page_rows, scanned_page_numbers) =
        store_page_artifacts(&store, extraction_id, &rendered_pages, &geo_by_page).await?;
    let total_pages = page_rows.len();
    let digital_pages = total_pages - scanned_page_numbers.len();

    augocr_common::db::save_pages(pool, extraction_id, &page_rows).await?;
    tracing::info!(
        "Saved {total_pages} page artifacts ({digital_pages} digital, {} scanned, {}ms)",
        scanned_page_numbers.len(),
        started.elapsed().as_millis()
    );

    augocr_common::db::set_total_pages(pool, extraction_id, total_pages as i64).await?;
    augocr_common::db::update_extraction_progress(
        pool,
        extraction_id,
        &json!({
            "stage": "normalize",
            "message": format!(
                "Rendered {total_pages} page(s) ({digital_pages} digital, {} scanned)",
                scanned_page_numbers.len()
            ),
            "total_pages": total_pages,
            "digital_pages": digital_pages,
            "scanned_pages": scanned_page_numbers.len(),
        }),
        Some("processing"),
    )
    .await?;
    augocr_common::db::update_document_status(pool, document_id, "normalized").await?;
    tracing::info!(
        "── NORMALIZE completed ── ext={extraction_id} ({total_pages} pages: \
         {digital_pages} digital, {} scanned)",
        scanned_page_numbers.len()
    );

    if stop_if_cancelled(pool, extraction_id, "normalize", "Cancelled during page rendering").await?
    {
        return Err(JobError::Cancelled(
            "Cancelled during page rendering".to_string(),
        ));
    }

    // ── Vendor detection (auto-detect path) ──
    let extraction_row = augocr_common::db::get_extraction(pool, extraction_id).await?;
    let needs_vendor = extraction_row
        .as_ref()
        .is_some_and(|row| !truthy(row.get("vendor_id").unwrap_or(&Value::Null)));
    if needs_vendor {
        let applied = detect_and_apply_vendor(
            pool,
            &document,
            extraction_id,
            &rendered_pages,
            &geo_by_page,
        )
        .await?;
        if !applied {
            // The helper already marked everything failed and released the
            // reservation; queueing downstream work now would resurrect a
            // pipeline that cannot produce anything.
            return Ok(());
        }
    }

    // ── Fan out: OCR (scanned pages) and LLM run in parallel ──
    augocr_common::db::ensure_job(
        pool,
        Some(extraction_id),
        Some(document_id),
        "ocr",
        Some(&payload_with_trace(
            job,
            json!({
                "extraction_id": extraction_id,
                "scanned_page_numbers": scanned_page_numbers,
            }),
        )),
        100,
        3,
    )
    .await?;
    augocr_common::db::ensure_job(
        pool,
        Some(extraction_id),
        Some(document_id),
        "llm",
        Some(&payload_with_trace(
            job,
            json!({"extraction_id": extraction_id}),
        )),
        100,
        3,
    )
    .await?;
    let _ = job_trace_context(job);
    Ok(())
}

/// Per-page geometry keyed by page number.
///
/// A failure of the text layer is not fatal: the pages are marked scanned so
/// OCR reads them instead, which is exactly what Python's `except` branch did.
async fn classify_pages(
    raw: &[u8],
    rendered_pages: &[RenderedPage],
    extraction_id: i64,
) -> std::collections::HashMap<i64, geometry::PageGeometry> {
    let sizes = page_sizes(rendered_pages);
    let started = Instant::now();
    let page_geometry = match geometry::compute_pdf_geometry(raw, &sizes).await {
        Ok(geo) => {
            let sources: std::collections::BTreeSet<&str> =
                geo.iter().map(|g| g.source.as_str()).collect();
            tracing::info!(
                "PDF geometry computed: {} pages, sources={sources:?} ({}ms)",
                geo.len(),
                started.elapsed().as_millis()
            );
            geo
        }
        Err(e) => {
            tracing::warn!(
                "PDF text geometry failed for extraction {extraction_id}: {e}; \
                 treating pages as scanned"
            );
            rendered_pages
                .iter()
                .map(|p| geometry::image_page_geometry(p.page_number))
                .collect()
        }
    };

    let digital = page_geometry
        .iter()
        .filter(|g| g.source == geometry::SOURCE_DIGITAL)
        .count();
    tracing::info!(
        "Page classification: {digital} digital, {} scanned",
        page_geometry.len() - digital
    );
    page_geometry
        .into_iter()
        .map(|g| (g.page_number, g))
        .collect()
}

/// Upload each rendered page and build its database row.
///
/// Returns the rows plus the page numbers that still need OCR.
async fn store_page_artifacts(
    store: &augocr_common::object_store::ObjectStoreImpl,
    extraction_id: i64,
    rendered_pages: &[RenderedPage],
    geo_by_page: &std::collections::HashMap<i64, geometry::PageGeometry>,
) -> JobResult<(Vec<Value>, Vec<i64>)> {
    let mut page_rows = Vec::with_capacity(rendered_pages.len());
    let mut scanned_page_numbers = Vec::new();

    for page in rendered_pages {
        let suffix = if page.mime_type.ends_with("jpeg") {
            ".jpg"
        } else {
            ".bin"
        };
        let object_key = format!(
            "extractions/{extraction_id}/pages/page_{}{suffix}",
            page.page_number
        );
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(&page.image_b64)
            .map_err(|e| JobError::Failed(format!("rendered page is not valid base64: {e}")))?;
        store
            .put_bytes(
                store.artifacts_bucket(),
                &object_key,
                bytes,
                &page.mime_type,
            )
            .await?;

        let geo = geo_by_page.get(&page.page_number);
        let source = geo.map(|g| g.source.as_str());
        // An unknown source is treated as scanned: OCR-ing a digital page
        // wastes time, but skipping OCR on a scanned one loses the page.
        if source.is_none() || source == Some(geometry::SOURCE_SCANNED) {
            scanned_page_numbers.push(page.page_number);
        }

        page_rows.push(json!({
            "page_number": page.page_number,
            "object_key": object_key,
            "mime_type": page.mime_type,
            "width": page.width,
            "height": page.height,
            "orig_width": page.orig_width,
            "orig_height": page.orig_height,
            "source": source,
            "char_count": geo.map(|g| g.char_count),
            "word_geometry": geo.map(|g| serde_json::to_value(&g.words).unwrap_or(Value::Null)),
        }));
    }
    Ok((page_rows, scanned_page_numbers))
}

/// Detect the vendor from page-1 text and persist it on the extraction.
///
/// This used to run inside the ingest request; running it here keeps uploads
/// fast and independent of the OCR service.
///
/// Returns `true` when a vendor matched **and** has a usable template. On
/// `false` the extraction and document are already marked failed with a
/// specific error code and the quota reservation is released, so the caller
/// must not queue downstream jobs.
async fn detect_and_apply_vendor(
    pool: &PgPool,
    document: &Value,
    extraction_id: i64,
    rendered_pages: &[RenderedPage],
    geo_by_page: &std::collections::HashMap<i64, geometry::PageGeometry>,
) -> JobResult<bool> {
    let document_id = document.get("id").and_then(Value::as_i64);
    let metadata = document.get("metadata").unwrap_or(&Value::Null);
    // Detection is scoped to one client to avoid cross-tenant alias
    // collisions. Newer ingest paths write `detect_user_id`; fall back to
    // `billing_user_id` for jobs that predate it.
    let detect_user_id = metadata
        .get("detect_user_id")
        .or_else(|| metadata.get("billing_user_id"))
        .and_then(Value::as_str)
        .map(str::to_string);

    // Reuse page-1 digital geometry; only OCR page 1 when it is scanned.
    let mut page_words = geo_by_page
        .get(&1)
        .map(|g| g.words.clone())
        .unwrap_or_default();

    if page_words.is_empty() {
        if let Some(page1) = rendered_pages.iter().find(|p| p.page_number == 1) {
            match crate::ocr_runner::OcrClient::global()
                .run_ocr_on_pages(std::slice::from_ref(page1))
                .await
            {
                Ok(pages) => {
                    page_words = pages.into_iter().next().map(|p| p.words).unwrap_or_default();
                }
                Err(e) => {
                    // OCR was needed to read a scanned page 1 but was
                    // unavailable. That is infrastructure, NOT an unknown
                    // vendor — say so, or the user is wrongly told to create a
                    // vendor that may already exist.
                    tracing::warn!("vendor-detect page-1 OCR failed ext={extraction_id}: {e}");
                    return fail_detection(
                        pool,
                        extraction_id,
                        document_id,
                        "ocr_unavailable",
                        "OCR was unavailable while reading page 1 for vendor detection. \
                         Please retry.",
                    )
                    .await;
                }
            }
        }
    }

    let word_count = page_words.len();
    let matched =
        vendor_detector::detect_vendor(pool, &page_words, detect_user_id.as_deref()).await?;

    let Some(matched) = matched else {
        tracing::warn!(
            "Unknown vendor for ext={extraction_id} ({word_count} page-1 words) — marking failed"
        );
        return fail_detection(
            pool,
            extraction_id,
            document_id,
            "unknown_vendor",
            "Unknown vendor — no alias matched the document.",
        )
        .await;
    };

    // A detected vendor still needs a template with at least one field, or
    // the extraction cannot produce anything. Preselected vendors hit this
    // gate at ingest; auto-detected ones must hit the same gate here so we
    // never queue ocr/llm for a vendor that cannot extract.
    let Some(tmpl) = augocr_common::db::get_template(pool, &matched.vendor_id).await? else {
        tracing::warn!(
            "Detected vendor {} for ext={extraction_id} has no template — marking failed",
            matched.vendor_id
        );
        return fail_detection(
            pool,
            extraction_id,
            document_id,
            "no_template",
            &format!(
                "Detected vendor '{}' but it has no template. Create a template with at \
                 least one field, then retry.",
                matched.vendor_name
            ),
        )
        .await;
    };

    let header_fields = string_list(tmpl.get("header_fields"));
    let line_item_fields = string_list(tmpl.get("line_item_fields"));
    if header_fields.is_empty() && line_item_fields.is_empty() {
        tracing::warn!(
            "Detected vendor {} for ext={extraction_id} has a template with no fields — \
             marking failed",
            matched.vendor_id
        );
        return fail_detection(
            pool,
            extraction_id,
            document_id,
            "no_fields",
            &format!(
                "Detected vendor '{}' but its template has no fields. Add at least one \
                 header or line item field, then retry.",
                matched.vendor_name
            ),
        )
        .await;
    }

    let header_refs: Vec<&str> = header_fields.iter().map(String::as_str).collect();
    let line_refs: Vec<&str> = line_item_fields.iter().map(String::as_str).collect();
    augocr_common::db::update_extraction_vendor(
        pool,
        extraction_id,
        &matched.vendor_id,
        tmpl.get("id").and_then(Value::as_i64),
        Some(
            tmpl.get("format_type")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("single_po_multipage"),
        ),
        &header_refs,
        &line_refs,
    )
    .await?;

    tracing::info!(
        "Vendor detected ext={extraction_id} vendor={} ({}, score={:.2})",
        matched.vendor_id,
        matched.vendor_name,
        matched.score
    );
    augocr_common::db::update_extraction_progress(
        pool,
        extraction_id,
        &json!({
            "stage": "normalize",
            "message": format!("Detected vendor: {}", matched.vendor_name),
            "vendor_detected": true,
        }),
        Some("processing"),
    )
    .await?;
    Ok(true)
}

/// Mark the extraction and document failed with a specific reason, release the
/// reservation, and report "do not continue".
async fn fail_detection(
    pool: &PgPool,
    extraction_id: i64,
    document_id: Option<i64>,
    reason: &str,
    message: &str,
) -> JobResult<bool> {
    augocr_common::db::set_extraction_status(
        pool,
        extraction_id,
        "failed",
        Some(&json!({"stage": "normalize", "message": message})),
        Some(reason),
        None,
        false,
    )
    .await?;
    if let Some(doc_id) = document_id {
        augocr_common::db::update_document_status(pool, doc_id, "failed").await?;
    }
    release_quota_quietly(pool, document_id, None, reason, extraction_id).await;
    Ok(false)
}

/// A JSON array of strings as a `Vec<String>`, skipping non-string entries.
fn string_list(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Page rows carry `word_geometry` as JSON; this keeps the shape in one place.
#[allow(dead_code)]
fn page_row_words(row: &Map<String, Value>) -> Vec<crate::page::Word> {
    row.get("word_geometry")
        .and_then(|w| serde_json::from_value(w.clone()).ok())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn string_list_reads_template_field_arrays() {
        let v = json!(["po_number", "date", 7, null]);
        assert_eq!(string_list(Some(&v)), vec!["po_number", "date"]);
        assert!(string_list(Some(&json!(null))).is_empty());
        assert!(string_list(None).is_empty());
        assert!(string_list(Some(&json!("not a list"))).is_empty());
    }
}
