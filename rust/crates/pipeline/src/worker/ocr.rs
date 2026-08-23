//! ocr stage ← `worker._process_ocr`.
//!
//! Runs PaddleOCR over the scanned pages only, then merges the result with the
//! digital word geometry `normalize` already stored, producing one unified
//! `ocr_data` blob covering every page.
//!
//! A document whose pages are all digital skips OCR entirely — the geometry is
//! already on the page rows, so this stage just reshapes and saves it.

use std::time::Instant;

use serde_json::{json, Value};
use sqlx::PgPool;

use super::{
    extraction_id_of, job_trace_context, load_pages, maybe_enqueue_postprocess, stop_if_cancelled,
    update_progress_if_not_failed, JobError, JobResult, LLM_FAILED_ERROR,
};
use crate::geometry::{self, PageGeometry};
use crate::ocr_runner::OcrClient;
use crate::page::Word;

pub async fn run(pool: &PgPool, job: &Value) -> JobResult<()> {
    let extraction_id = extraction_id_of(job)?;
    if augocr_common::db::get_extraction(pool, extraction_id)
        .await?
        .is_none()
    {
        return Err(JobError::Failed(
            "Extraction not found for OCR job".to_string(),
        ));
    }
    tracing::info!("── OCR started ── ext={extraction_id}");

    let all_page_rows = augocr_common::db::get_pages(pool, extraction_id).await?;

    // Which pages need OCR? The normalize stage puts the list in the payload;
    // older jobs (and rows with a NULL source) fall back to the pages table.
    let scanned_page_numbers: Vec<i64> = match job
        .get("payload")
        .and_then(|p| p.get("scanned_page_numbers"))
        .and_then(Value::as_array)
    {
        Some(list) => list.iter().filter_map(Value::as_i64).collect(),
        None => all_page_rows
            .iter()
            .filter(|p| p.get("source").and_then(Value::as_str) != Some(geometry::SOURCE_DIGITAL))
            .filter_map(|p| p.get("page_number").and_then(Value::as_i64))
            .collect(),
    };

    if scanned_page_numbers.is_empty() {
        skip_ocr_all_digital(pool, extraction_id, job, &all_page_rows).await?;
    } else if !run_paddleocr(pool, extraction_id, job, &all_page_rows, &scanned_page_numbers).await?
    {
        // The extraction failed while we were working; nothing more to do.
        return Ok(());
    }

    if stop_if_cancelled(pool, extraction_id, "ocr", "Cancelled during OCR").await? {
        return Err(JobError::Cancelled("Cancelled during OCR".to_string()));
    }
    tracing::info!("── OCR completed ── ext={extraction_id}");

    // The LLM stage may have failed while OCR was running. Postprocess writes
    // the terminal `done` status, so it must not run on a failed extraction.
    let current = augocr_common::db::get_extraction(pool, extraction_id)
        .await?
        .unwrap_or(json!({}));
    if let Some(reason) = super::postprocess_failure_reason(&current) {
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
            Some(&json!({"stage": "ocr", "message": reason})),
            Some(&error),
            None,
            false,
        )
        .await?;
        return Ok(());
    }

    maybe_enqueue_postprocess(
        pool,
        extraction_id,
        job.get("document_id").and_then(Value::as_i64),
        job_trace_context(job),
    )
    .await
}

/// Every page has a digital text layer — reuse it and skip PaddleOCR.
async fn skip_ocr_all_digital(
    pool: &PgPool,
    extraction_id: i64,
    job: &Value,
    all_page_rows: &[Value],
) -> JobResult<()> {
    tracing::info!(
        "All {} page(s) are digital — skipping PaddleOCR",
        all_page_rows.len()
    );
    let unified = base_geometry(all_page_rows, geometry::SOURCE_DIGITAL);
    save_geometry(pool, extraction_id, &unified).await?;

    if let Some(job_id) = job.get("id").and_then(Value::as_i64) {
        augocr_common::db::update_job_progress(
            pool,
            job_id,
            &json!({
                "stage": "ocr",
                "pages_processed": 0,
                "message": "All pages digital — OCR skipped",
            }),
        )
        .await?;
    }
    update_progress_if_not_failed(
        pool,
        extraction_id,
        &json!({"stage": "ocr", "message": "All pages digital — OCR skipped"}),
    )
    .await?;
    Ok(())
}

/// OCR the scanned pages and merge them into the base geometry.
///
/// Returns `false` when the extraction already failed and the stage should
/// stop without doing the work.
async fn run_paddleocr(
    pool: &PgPool,
    extraction_id: i64,
    job: &Value,
    all_page_rows: &[Value],
    scanned_page_numbers: &[i64],
) -> JobResult<bool> {
    let can_continue = update_progress_if_not_failed(
        pool,
        extraction_id,
        &json!({
            "stage": "ocr",
            "message": format!("Running OCR on {} scanned page(s)", scanned_page_numbers.len()),
        }),
    )
    .await?;
    if !can_continue {
        return Ok(false);
    }

    // Load only the scanned page images; a digital page's bytes would be
    // downloaded and then thrown away.
    let all_pages = load_pages(pool, extraction_id).await?;
    let scanned: Vec<crate::page::RenderedPage> = all_pages
        .into_iter()
        .filter(|p| scanned_page_numbers.contains(&p.page_number))
        .collect();

    let started = Instant::now();
    let ocr_pages = OcrClient::global().run_ocr_on_pages(&scanned).await?;
    let total_words: usize = ocr_pages.iter().map(|p| p.words.len()).sum();
    tracing::info!(
        "PaddleOCR completed: {} scanned pages, {total_words} words ({}ms)",
        scanned_page_numbers.len(),
        started.elapsed().as_millis()
    );
    let pages_processed = ocr_pages.len();

    // Digital pages keep the geometry normalize stored; scanned ones get the
    // words we just read.
    let base = base_geometry(all_page_rows, geometry::SOURCE_SCANNED);
    let unified = geometry::merge_scanned_into_geometry(base, ocr_pages);
    save_geometry(pool, extraction_id, &unified).await?;

    if let Some(job_id) = job.get("id").and_then(Value::as_i64) {
        augocr_common::db::update_job_progress(
            pool,
            job_id,
            &json!({
                "stage": "ocr",
                "pages_processed": pages_processed,
                "scanned_pages": scanned_page_numbers.len(),
            }),
        )
        .await?;
    }
    Ok(true)
}

/// Rebuild per-page geometry from the stored page rows.
///
/// `default_source` mirrors Python's differing `p.get("source", …)` defaults
/// between the all-digital and mixed paths.
fn base_geometry(page_rows: &[Value], default_source: &str) -> Vec<PageGeometry> {
    page_rows
        .iter()
        .filter_map(|p| {
            let words: Vec<Word> = p
                .get("word_geometry")
                .and_then(|w| serde_json::from_value(w.clone()).ok())
                .unwrap_or_default();
            Some(PageGeometry {
                page_number: p.get("page_number").and_then(Value::as_i64)?,
                source: p
                    .get("source")
                    .and_then(Value::as_str)
                    .unwrap_or(default_source)
                    .to_string(),
                char_count: p.get("char_count").and_then(Value::as_i64).unwrap_or(0),
                word_count: words.len(),
                words,
            })
        })
        .collect()
}

async fn save_geometry(
    pool: &PgPool,
    extraction_id: i64,
    unified: &[PageGeometry],
) -> JobResult<()> {
    let started = Instant::now();
    let payload = serde_json::to_value(unified)
        .map_err(|e| JobError::Failed(format!("could not serialize page geometry: {e}")))?;
    augocr_common::db::save_ocr_data(pool, extraction_id, &payload).await?;
    let total_words: usize = unified.iter().map(|p| p.words.len()).sum();
    tracing::info!(
        "Unified geometry saved: {} pages, {total_words} words ({}ms)",
        unified.len(),
        started.elapsed().as_millis()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_geometry_reads_stored_page_rows() {
        let rows = vec![
            json!({
                "page_number": 1, "source": "pypdfium", "char_count": 900,
                "word_geometry": [{"text": "ACME", "box": [1, 2, 3, 4], "score": 1.0}]
            }),
            json!({"page_number": 2, "source": "paddleocr", "char_count": 0}),
        ];
        let geo = base_geometry(&rows, "paddleocr");
        assert_eq!(geo.len(), 2);
        assert_eq!(geo[0].source, "pypdfium");
        assert_eq!(geo[0].char_count, 900);
        assert_eq!(geo[0].word_count, 1);
        assert_eq!(geo[0].words[0].text, "ACME");
        // A page with no stored geometry yields an empty, non-null word list.
        assert_eq!(geo[1].word_count, 0);
    }

    #[test]
    fn base_geometry_applies_the_default_source() {
        let rows = vec![json!({"page_number": 1})];
        assert_eq!(base_geometry(&rows, "pypdfium")[0].source, "pypdfium");
        assert_eq!(base_geometry(&rows, "paddleocr")[0].source, "paddleocr");
    }

    #[test]
    fn base_geometry_skips_rows_without_a_page_number() {
        let rows = vec![json!({"source": "paddleocr"}), json!({"page_number": 2})];
        let geo = base_geometry(&rows, "paddleocr");
        assert_eq!(geo.len(), 1);
        assert_eq!(geo[0].page_number, 2);
    }

    #[test]
    fn base_geometry_tolerates_malformed_word_payloads() {
        // A corrupt JSONB blob must not fail the whole stage; the page simply
        // has no words and OCR can refill it.
        let rows = vec![json!({"page_number": 1, "word_geometry": "not a list"})];
        assert_eq!(base_geometry(&rows, "paddleocr")[0].word_count, 0);
    }
}
