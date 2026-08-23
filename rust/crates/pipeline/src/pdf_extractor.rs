//! pdf_extractor.rs ← pdf_extractor.py (pypdfium2 digital-text helpers).
//!
//! pypdfium2 has no in-process Rust binding, so its capability is consumed
//! over HTTP from the PDF sidecar service (`PDF_SERVICE_URL`, see
//! `augocr_common::config`). The whole document is described in a single
//! round trip and this module owns the JSON contract:
//!
//! ```text
//! POST {pdf_service_url}/digital-text
//! Content-Type: application/pdf
//!
//! <raw PDF bytes>
//!
//! 200 OK
//! { "pages": [
//!     {
//!       "width_pts": 612.0,      // page MediaBox size in PDF points
//!       "height_pts": 792.0,
//!       "char_count": 1043,      // textpage.count_chars()
//!       "is_digital": true,      // classification below, mirrored exactly
//!       "words": [               // empty unless is_digital
//!         { "text": "Invoice", "box": [42, 30, 180, 58], "score": 1.0 }
//!       ],
//!       "error": null            // string on per-page sidecar failure
//!     }
//! ] }
//! ```
//!
//! `words` live in the native rendered-pixel space defined by
//! [`compute_scale`] (scale × PDF points, Y flipped to top-left origin) —
//! the same space Python's `extract_words` produced. The threshold
//! constants below are normative for the sidecar: it must implement the
//! identical digital-page classification so the client-side rescaling in
//! `geometry.rs` sees consistent geometry. A non-400 HTTP failure maps to
//! [`AppError::ServiceUnavailable`], a 400 to [`AppError::BadRequest`]
//! (mirroring the Python code letting `PdfiumError` escape for unreadable
//! documents), and a malformed body to [`AppError::Internal`].

use std::sync::OnceLock;
use std::time::Duration;

use augocr_common::config::Config;
use augocr_common::error::{AppError, AppResult};
use serde::Deserialize;

pub use crate::page::Word;

/// Pages with fewer printable characters than this are treated as scanned.
pub const DIGITAL_CHAR_THRESHOLD: usize = 50;
/// Character window inspected by the digital-page classification.
pub const DETECTION_SAMPLE_SIZE: usize = 500;
/// Longest rendered-image side targeted before the DPI is clamped.
pub const MAX_LONG_SIDE: u32 = 1344;
pub const DPI_FLOOR: u32 = 96;
pub const DPI_DEFAULT: u32 = 150;

/// Upper bound for one sidecar round trip covering every page of a document.
const SIDECAR_TIMEOUT: Duration = Duration::from_secs(120);

/// Return the scale factor used to render a PDF page at the sidecar's target DPI.
///
/// Mirrors Python `pdf_extractor.compute_scale`: pick a target DPI so the
/// long side stays within [`MAX_LONG_SIDE`], clamped to
/// `[DPI_FLOOR, DPI_DEFAULT]`. Must stay byte-identical with the sidecar's
/// implementation.
pub fn compute_scale(w_pts: f64, h_pts: f64) -> f64 {
    let max_pts = w_pts.max(h_pts);
    if max_pts <= 0.0 {
        return DPI_FLOOR as f64 / 72.0;
    }
    let target_dpi = (MAX_LONG_SIDE as f64 / (max_pts / 72.0)) as u32;
    let target_dpi = target_dpi.clamp(DPI_FLOOR, DPI_DEFAULT);
    target_dpi as f64 / 72.0
}


/// Per-page entry of the sidecar's `digital-text` response.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SidecarPageText {
    #[serde(default)]
    pub width_pts: f64,
    #[serde(default)]
    pub height_pts: f64,
    #[serde(default)]
    pub char_count: u64,
    #[serde(default)]
    pub is_digital: bool,
    #[serde(default)]
    pub words: Vec<Word>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
struct DigitalTextResponse {
    #[serde(default)]
    pages: Vec<SidecarPageText>,
}

/// Async client for the PDF sidecar's pypdfium2-equivalent capabilities.
#[derive(Clone)]
pub struct PdfExtractorClient {
    http: reqwest::Client,
    base_url: String,
}

impl PdfExtractorClient {
    pub fn new(base_url: &str) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(SIDECAR_TIMEOUT)
                .build()
                .unwrap_or_default(),
            base_url: base_url.trim_end_matches('/').to_string(),
        }
    }

    pub fn from_config(cfg: &Config) -> Self {
        Self::new(&cfg.pdf_service_url)
    }

    /// Process-wide shared client, built once from [`Config::global`]
    /// (`reqwest::Client` is internally connection-pooled).
    pub fn global() -> &'static Self {
        static CLIENT: OnceLock<PdfExtractorClient> = OnceLock::new();
        CLIENT.get_or_init(|| Self::from_config(Config::global()))
    }

    /// POST raw PDF bytes to one sidecar endpoint and decode the JSON reply.
    ///
    /// The single place the sidecar's failure modes are mapped onto
    /// [`AppError`]: an unreachable or erroring service is a dependency
    /// failure, a 400 means the document itself is unreadable (Python let
    /// `PdfiumError` escape for those), and a body that will not parse is our
    /// own bug rather than the user's.
    pub(crate) async fn post_pdf<T: serde::de::DeserializeOwned>(
        &self,
        endpoint: &str,
        pdf_bytes: &[u8],
        params: &[(&str, String)],
    ) -> AppResult<T> {
        let url = format!("{}/{endpoint}", self.base_url);
        let response = self
            .http
            .post(&url)
            .query(params)
            .header(reqwest::header::CONTENT_TYPE, "application/pdf")
            .body(pdf_bytes.to_vec())
            .send()
            .await
            .map_err(|e| {
                AppError::ServiceUnavailable(format!("PDF sidecar unreachable ({url}): {e}"))
            })?;

        let status = response.status();
        if !status.is_success() {
            let detail = response.text().await.unwrap_or_default();
            // Sidecar tracebacks are long and this string reaches a job's
            // error column and the API response.
            let detail: String = detail.chars().take(500).collect();
            if status.as_u16() == 400 {
                return Err(AppError::BadRequest(format!(
                    "invalid PDF document rejected by PDF sidecar: {detail}"
                )));
            }
            return Err(AppError::ServiceUnavailable(format!(
                "PDF sidecar {endpoint} request failed: HTTP {status}: {detail}"
            )));
        }

        response.json().await.map_err(|e| {
            AppError::Internal(format!(
                "PDF sidecar returned a malformed {endpoint} payload: {e}"
            ))
        })
    }

    /// Describe every page of `pdf_bytes` in one round trip: point sizes,
    /// char counts, digital classification and native-scale words.
    pub async fn digital_text(&self, pdf_bytes: &[u8]) -> AppResult<Vec<SidecarPageText>> {
        let payload: DigitalTextResponse = self.post_pdf("digital-text", pdf_bytes, &[]).await?;
        Ok(payload.pages)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scale_zero_and_negative_points_fall_to_dpi_floor() {
        assert_eq!(compute_scale(0.0, 0.0), DPI_FLOOR as f64 / 72.0);
        assert_eq!(compute_scale(-5.0, -1.0), DPI_FLOOR as f64 / 72.0);
    }

    #[test]
    fn scale_us_letter_targets_truncated_dpi() {
        // 792pt = 11in → 1344/11 = 122.18… → int() truncates to 122.
        assert_eq!(compute_scale(612.0, 792.0), 122.0 / 72.0);
    }

    #[test]
    fn scale_small_page_caps_at_default_dpi() {
        // 36pt = 0.5in → 1344/0.5 = 2688, clamped down to DPI_DEFAULT.
        assert_eq!(compute_scale(36.0, 24.0), DPI_DEFAULT as f64 / 72.0);
    }

    #[test]
    fn scale_huge_page_floors_at_min_dpi() {
        // 20000pt ≈ 277.8in → 1344/277.8 = 4.84 → truncated 4, lifted to DPI_FLOOR.
        assert_eq!(compute_scale(20000.0, 10000.0), DPI_FLOOR as f64 / 72.0);
    }
}
