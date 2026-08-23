//! ocr_runner.rs ← ocr_runner.py (PaddleOCR over rendered page images).
//!
//! PaddleOCR has no Rust engine, so it keeps running as its own process and is
//! reached over HTTP at `OCR_SERVICE_URL`. That inverts one Python detail:
//! `OCR_WORKERS` no longer sizes a local `ThreadPoolExecutor`, it caps how many
//! pages are in flight to the sidecar at once. The observable behaviour is the
//! same — bounded parallelism, results in page order, and one hard failure if
//! any page fails.
//!
//! Sidecar contract:
//!
//! ```text
//! POST {ocr_service_url}/ocr   { "image_b64": "...", "page_number": 1 }
//! 200  { "page_number": 1,
//!        "words": [ {"text": "ACME", "box": [1,2,3,4], "score": 0.95} ] }
//!
//! POST {ocr_service_url}/warmup   { "workers": 1 }
//! 200  { "engines": 1, "elapsed_ms": 21840 }
//! ```
//!
//! The sidecar owns the normalisation Python did inline: text is stripped,
//! empty strings dropped, box coordinates rounded to ints and scores rounded
//! to 4 decimal places.
//!
//! Note the engine build costs 20–46s per sidecar process. [`OcrClient::warmup`]
//! is called at worker boot so no user request ever pays it.

use std::time::{Duration, Instant};

use augocr_common::config::Config;
use futures::stream::{self, StreamExt, TryStreamExt};
use serde::{Deserialize, Serialize};

use crate::page::{RenderedPage, Word};

/// One sidecar round trip. PaddleOCR is CPU-bound and a page can queue behind
/// another worker's engine build, so this is deliberately generous.
const OCR_TIMEOUT: Duration = Duration::from_secs(180);
/// Engine build is 20–46s; allow for a cold sidecar plus queueing.
const WARMUP_TIMEOUT: Duration = Duration::from_secs(300);

/// PaddleOCR could not initialise or could not run.
///
/// Mirrors Python's `OCRUnavailable`. Blank pages are never returned silently:
/// a page-level failure fails the whole document, because a blank OCR page is
/// indistinguishable from a legitimately empty one downstream.
#[derive(Debug, thiserror::Error)]
#[error("PaddleOCR failed: {detail}")]
pub struct OcrUnavailable {
    pub detail: String,
}

impl OcrUnavailable {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl From<OcrUnavailable> for augocr_common::error::AppError {
    fn from(e: OcrUnavailable) -> Self {
        // The OCR engine being down is a dependency failure, not a bad request.
        augocr_common::error::AppError::ServiceUnavailable(e.to_string())
    }
}

pub type OcrResult<T> = Result<T, OcrUnavailable>;

/// OCR output for one page: `{page_number, words}`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OcrPage {
    pub page_number: i64,
    #[serde(default)]
    pub words: Vec<Word>,
}

#[derive(Debug, Serialize)]
struct OcrRequest<'a> {
    image_b64: &'a str,
    page_number: i64,
}

#[derive(Debug, Serialize)]
struct WarmupRequest {
    workers: usize,
}

/// Client for the PaddleOCR sidecar.
///
/// Cheap to clone: [`reqwest::Client`] is an `Arc` internally and shares one
/// connection pool, so cloning per job is intended.
#[derive(Clone)]
pub struct OcrClient {
    http: reqwest::Client,
    base_url: String,
    /// Maximum pages in flight — `OCR_WORKERS`, never below 1.
    concurrency: usize,
}

impl OcrClient {
    pub fn new(base_url: &str, concurrency: usize) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(OCR_TIMEOUT)
                // Keep sockets alive between pages; the sidecar is long-lived.
                .pool_idle_timeout(Duration::from_secs(90))
                .build()
                .unwrap_or_default(),
            base_url: base_url.trim_end_matches('/').to_string(),
            concurrency: concurrency.max(1),
        }
    }

    pub fn from_config(cfg: &Config) -> Self {
        Self::new(&cfg.ocr_service_url, cfg.ocr_workers)
    }

    /// Process-wide shared client built once from [`Config::global`].
    pub fn global() -> &'static Self {
        static CLIENT: std::sync::OnceLock<OcrClient> = std::sync::OnceLock::new();
        CLIENT.get_or_init(|| Self::from_config(Config::global()))
    }

    /// Ask the sidecar to build its OCR engines now.
    ///
    /// Python paid 20–46s of Paddle graph compilation on the first page; the
    /// stage workers call this at boot so a user request never does. A failure
    /// is logged and swallowed — exactly like the Python warmup, which only
    /// warned — because the first real page will surface a hard error anyway.
    pub async fn warmup(&self, workers: Option<usize>) {
        let count = workers.unwrap_or(self.concurrency).max(1);
        let started = Instant::now();
        tracing::info!("Warming PaddleOCR engines for {count} worker(s)...");

        let url = format!("{}/warmup", self.base_url);
        let sent = self
            .http
            .post(&url)
            .timeout(WARMUP_TIMEOUT)
            .json(&WarmupRequest { workers: count })
            .send()
            .await;

        match sent {
            Ok(resp) if resp.status().is_success() => {
                tracing::info!(
                    "PaddleOCR warmup complete: {count} engine(s) ready in {}ms",
                    started.elapsed().as_millis()
                );
            }
            Ok(resp) => {
                tracing::warn!(
                    "PaddleOCR warmup returned HTTP {} after {}ms — first page will pay the engine build",
                    resp.status(),
                    started.elapsed().as_millis()
                );
            }
            Err(e) => {
                tracing::warn!(
                    "PaddleOCR warmup could not reach {url} ({e}) — first page will pay the engine build"
                );
            }
        }
    }

    /// Run OCR on a single page image.
    async fn ocr_page(&self, image_b64: &str, page_number: i64) -> OcrResult<OcrPage> {
        let started = Instant::now();
        let url = format!("{}/ocr", self.base_url);
        let response = self
            .http
            .post(&url)
            .json(&OcrRequest {
                image_b64,
                page_number,
            })
            .send()
            .await
            .map_err(|e| OcrUnavailable::new(format!("page {page_number}: {e}")))?;

        let status = response.status();
        if !status.is_success() {
            let detail = response.text().await.unwrap_or_default();
            // Trim the body: sidecar tracebacks are long and this string ends
            // up in a job's error column.
            let detail: String = detail.chars().take(500).collect();
            return Err(OcrUnavailable::new(format!(
                "page {page_number}: HTTP {status}: {detail}"
            )));
        }

        let page: OcrPage = response
            .json()
            .await
            .map_err(|e| OcrUnavailable::new(format!("page {page_number}: malformed response: {e}")))?;

        tracing::info!(
            "PaddleOCR page {page_number}: {} words detected in {}ms",
            page.words.len(),
            started.elapsed().as_millis()
        );
        Ok(page)
    }

    /// Run OCR over every page, at most `concurrency` in flight.
    ///
    /// Results come back in input order. Any page failure fails the whole
    /// call, matching Python's rule that a blank page must never pass for a
    /// successful one — but unlike Python, which gathered every page before
    /// checking, this short-circuits on the first error and drops the
    /// remaining requests instead of paying for work whose result is discarded.
    pub async fn run_ocr_on_pages(&self, pages: &[RenderedPage]) -> OcrResult<Vec<OcrPage>> {
        if pages.is_empty() {
            return Ok(Vec::new());
        }

        let started = Instant::now();
        tracing::info!(
            "Starting PaddleOCR on {} page(s) ({} in flight)...",
            pages.len(),
            self.concurrency
        );

        // `buffered` preserves input order while bounding in-flight requests;
        // `try_collect` cancels the rest as soon as one page fails.
        let ocr_pages: Vec<OcrPage> = stream::iter(
            pages
                .iter()
                .map(|p| self.ocr_page(&p.image_b64, p.page_number)),
        )
        .buffered(self.concurrency)
        .try_collect()
        .await?;

        let total_words: usize = ocr_pages.iter().map(|p| p.words.len()).sum();
        tracing::info!(
            "PaddleOCR complete: {} page(s), {total_words} total words, {}ms",
            ocr_pages.len(),
            started.elapsed().as_millis()
        );
        Ok(ocr_pages)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn base_url_trailing_slash_is_normalised() {
        let c = OcrClient::new("http://ocr:9100/", 4);
        assert_eq!(c.base_url, "http://ocr:9100");
        assert_eq!(c.concurrency, 4);
    }

    #[test]
    fn concurrency_never_drops_below_one() {
        // `.buffered(0)` would stall forever, so a misconfigured
        // OCR_WORKERS=0 must still make progress.
        assert_eq!(OcrClient::new("http://ocr", 0).concurrency, 1);
    }

    #[tokio::test]
    async fn empty_page_list_short_circuits() {
        let c = OcrClient::new("http://127.0.0.1:1", 1);
        assert!(c.run_ocr_on_pages(&[]).await.expect("no pages").is_empty());
    }

    #[test]
    fn ocr_page_parses_the_sidecar_shape() {
        let page: OcrPage = serde_json::from_value(json!({
            "page_number": 2,
            "words": [{"text": "ACME", "box": [1, 2, 3, 4], "score": 0.95}]
        }))
        .expect("parse");
        assert_eq!(page.page_number, 2);
        assert_eq!(page.words[0].text, "ACME");
        assert_eq!(page.words[0].bbox, [1, 2, 3, 4]);
    }

    #[test]
    fn missing_words_defaults_to_empty_not_an_error() {
        let page: OcrPage = serde_json::from_value(json!({"page_number": 1})).expect("parse");
        assert!(page.words.is_empty());
    }

    #[test]
    fn unavailable_maps_to_service_unavailable() {
        let err: augocr_common::error::AppError = OcrUnavailable::new("page 1: boom").into();
        assert_eq!(err.status().as_u16(), 503);
        assert!(err.detail().contains("page 1: boom"));
    }
}
