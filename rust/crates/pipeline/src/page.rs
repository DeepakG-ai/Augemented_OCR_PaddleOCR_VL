//! page.rs — the vocabulary types every stage shares.
//!
//! In Python these were bare dicts passed between `processor`, `geometry`,
//! `ocr_runner` and `extractor`, which made the field contract implicit and
//! unchecked. They are named types here so a stage boundary is a compile
//! error rather than a `KeyError` at 3am. Field names and JSON shapes are
//! unchanged, so persisted artifacts and the frontend keep working.

use serde::{Deserialize, Serialize};

fn default_score() -> f64 {
    1.0
}

/// One extracted word: text plus an integer pixel-space box `[x0, y0, x1, y1]`
/// with a top-left origin.
///
/// Produced identically by digital text extraction (pypdfium) and by OCR
/// (PaddleOCR), which is why it lives here rather than in either module.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Word {
    pub text: String,
    #[serde(rename = "box")]
    pub bbox: [i64; 4],
    #[serde(default = "default_score")]
    pub score: f64,
}

impl Word {
    pub fn new(text: impl Into<String>, bbox: [i64; 4], score: f64) -> Self {
        Self {
            text: text.into(),
            bbox,
            score,
        }
    }
}

/// A page rendered to a JPEG for the vision model — the element type Python's
/// `processor.pdf_to_images` / `image_file_to_b64` returned.
///
/// `image_b64` is the dominant memory cost of a document (roughly 0.5 MB per
/// page), so this type is moved rather than cloned; use [`RenderedPage::size`]
/// when a consumer only needs the geometry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RenderedPage {
    pub page_number: i64,
    pub image_b64: String,
    pub mime_type: String,
    /// Final (post VLM-budget resize) pixel dimensions — the coordinate space
    /// every word box lives in.
    pub width: i64,
    pub height: i64,
    /// Pre-resize dimensions, kept for diagnostics.
    pub orig_width: i64,
    pub orig_height: i64,
    pub doc_total_pages: i64,
    /// Which engine produced this page's word geometry (`pypdfium` or
    /// `paddleocr`). The renderer leaves it unset; the worker fills it in when
    /// reloading pages from the database, which is what the billing log's
    /// digital-vs-scanned split counts.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

impl RenderedPage {
    /// Narrow view for consumers that need geometry but not the image bytes.
    pub fn size(&self) -> PageSize {
        PageSize {
            page_number: self.page_number,
            width: self.width,
            height: self.height,
        }
    }
}

/// Final rendered-image dimensions for one page — the `page_sizes` argument
/// Python threaded into `geometry.compute_pdf_geometry`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PageSize {
    pub page_number: i64,
    #[serde(default)]
    pub width: i64,
    #[serde(default)]
    pub height: i64,
}

/// Collect the geometry of a page list without touching the image payloads.
pub fn page_sizes(pages: &[RenderedPage]) -> Vec<PageSize> {
    pages.iter().map(RenderedPage::size).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn word_json_shape_matches_python_dicts() {
        let w = Word::new("Invoice", [1, 2, 3, 4], 0.95);
        let v = serde_json::to_value(&w).expect("serialize");
        assert_eq!(v, json!({"text": "Invoice", "box": [1, 2, 3, 4], "score": 0.95}));

        // `score` defaults to 1.0 when absent, like `w.get("score", 1.0)`.
        let parsed: Word =
            serde_json::from_value(json!({"text": "x", "box": [0, 0, 1, 1]})).expect("parse");
        assert_eq!(parsed.score, 1.0);
    }

    #[test]
    fn size_view_drops_the_image_payload() {
        let page = RenderedPage {
            page_number: 3,
            image_b64: "AAAA".into(),
            mime_type: "image/jpeg".into(),
            width: 1344,
            height: 1000,
            orig_width: 2550,
            orig_height: 1900,
            doc_total_pages: 5,
            source: None,
        };
        assert_eq!(
            page.size(),
            PageSize {
                page_number: 3,
                width: 1344,
                height: 1000
            }
        );
        assert_eq!(page_sizes(std::slice::from_ref(&page)).len(), 1);
    }
}
