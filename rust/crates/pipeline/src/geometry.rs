//! geometry.rs ← geometry.py (unified per-page text geometry service).
//!
//! Produces one [`PageGeometry`] per page regardless of whether the source is
//! the PDF sidecar's digital text layer (Python pypdfium2) or PaddleOCR.
//! Word boxes always live in the final rendered-image pixel space (post
//! Qwen3-VL resize) — the same coordinates the frontend and PaddleOCR consume.
//!
//! Port notes versus Python:
//! * `PdfDocument` handling collapses into a single
//!   [`pdf_extractor::PdfExtractorClient::digital_text`] round trip; the
//!   sidecar's per-page `error` field replaces Python's per-page
//!   try/except (failed pages are marked scanned, exactly like the except
//!   branch set `words=[], char_count=0, is_digital=False`).
//! * Native-scale words are rescaled to final image space client-side using
//!   [`pdf_extractor::compute_scale`], which both sides implement
//!   identically.

use std::collections::HashMap;

use augocr_common::error::AppResult;
use serde::{Deserialize, Serialize};

use crate::ocr_runner::OcrPage;
use crate::page::{PageSize, Word};
use crate::pdf_extractor::{compute_scale, PdfExtractorClient, SidecarPageText};

pub const SOURCE_DIGITAL: &str = "pypdfium";
pub const SOURCE_SCANNED: &str = "paddleocr";

/// Unified per-page geometry entry:
/// `{page_number, source, char_count, word_count, words}` in Python.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PageGeometry {
    pub page_number: i64,
    pub source: String,
    pub char_count: i64,
    pub word_count: usize,
    pub words: Vec<Word>,
}

impl PageGeometry {
    fn new(page_number: i64, source: &str, char_count: i64, words: Vec<Word>) -> Self {
        Self {
            page_number,
            source: source.to_string(),
            char_count,
            word_count: words.len(),
            words,
        }
    }
}

fn py_round(value: f64) -> i64 {
    // Python round() is half-to-even; keep pixel-exact parity.
    value.round_ties_even() as i64
}

fn rescale_words(words: &[Word], scale_x: f64, scale_y: f64) -> Vec<Word> {
    words
        .iter()
        .map(|w| Word {
            text: w.text.clone(),
            bbox: [
                py_round(w.bbox[0] as f64 * scale_x),
                py_round(w.bbox[1] as f64 * scale_y),
                py_round(w.bbox[2] as f64 * scale_x),
                py_round(w.bbox[3] as f64 * scale_y),
            ],
            score: w.score,
        })
        .collect()
}

/// Extract digital words for one sidecar-reported page, rescaled to the final
/// image space. Returns `(words, char_count, is_digital)`.
fn digital_words_for_page(
    page: &SidecarPageText,
    page_index: usize,
    final_width: i64,
    final_height: i64,
) -> (Vec<Word>, i64, bool) {
    if !page.is_digital {
        return (Vec::new(), page.char_count as i64, false);
    }

    let scale = compute_scale(page.width_pts, page.height_pts);
    if page.words.is_empty() {
        // Page has chars but no extractable word geometry — treat as scanned
        tracing::debug!(
            "Page {} reported digital ({} chars) but yielded 0 words — routing to OCR",
            page_index + 1,
            page.char_count
        );
        return (Vec::new(), page.char_count as i64, false);
    }

    let native_w = ((page.width_pts * scale) as i64).max(1);
    let native_h = ((page.height_pts * scale) as i64).max(1);
    let sx = if final_width > 0 {
        final_width as f64 / native_w as f64
    } else {
        1.0
    };
    let sy = if final_height > 0 {
        final_height as f64 / native_h as f64
    } else {
        1.0
    };
    (
        rescale_words(&page.words, sx, sy),
        page.char_count as i64,
        true,
    )
}

/// Compute per-page unified geometry for a PDF.
///
/// `page_sizes` carries each page's final rendered image dimensions; the
/// returned list is aligned with it. `source` is [`SOURCE_DIGITAL`] for
/// digital pages (words filled in) or [`SOURCE_SCANNED`] for pages needing
/// OCR (words empty until OCR runs).
pub async fn compute_pdf_geometry(
    pdf_bytes: &[u8],
    page_sizes: &[PageSize],
) -> AppResult<Vec<PageGeometry>> {
    let pdf_pages = PdfExtractorClient::global().digital_text(pdf_bytes).await?;
    let mut results = Vec::with_capacity(page_sizes.len());
    for (index, meta) in page_sizes.iter().enumerate() {
        if index >= pdf_pages.len() {
            tracing::warn!(
                "geometry: page_sizes has {} entries but PDF only has {} pages — stopping early (index {index})",
                page_sizes.len(),
                pdf_pages.len()
            );
            break;
        }
        let page = &pdf_pages[index];
        let (words, char_count, is_digital) = match &page.error {
            Some(err) => {
                tracing::warn!(
                    "Digital word extraction failed for page {}: {err} — marking as scanned",
                    meta.page_number
                );
                (Vec::new(), 0, false)
            }
            None => digital_words_for_page(page, index, meta.width, meta.height),
        };
        results.push(PageGeometry::new(
            meta.page_number,
            if is_digital {
                SOURCE_DIGITAL
            } else {
                SOURCE_SCANNED
            },
            char_count,
            words,
        ));
    }
    Ok(results)
}

/// Merge OCR results (scanned pages) into the base unified geometry.
///
/// `base_geometry` already contains digital word data for pypdfium pages and
/// empty entries for paddleocr pages; this fills the paddleocr entries from
/// freshly computed OCR output. Later OCR entries win on duplicate page
/// numbers, matching Python's dict comprehension.
///
/// Takes the OCR pages by value and moves each word list into place — a page
/// carries thousands of `Word`s and this runs on every scanned document.
/// Page numbers in `base_geometry` come from `page_sizes` and are therefore
/// unique, so moving a word list out of the map cannot starve a later entry.
pub fn merge_scanned_into_geometry(
    mut base_geometry: Vec<PageGeometry>,
    ocr_pages: Vec<OcrPage>,
) -> Vec<PageGeometry> {
    let mut ocr_by_page: HashMap<i64, Vec<Word>> = HashMap::with_capacity(ocr_pages.len());
    for page in ocr_pages {
        ocr_by_page.insert(page.page_number, page.words);
    }
    for entry in base_geometry.iter_mut() {
        if entry.source == SOURCE_SCANNED {
            if let Some(words) = ocr_by_page.remove(&entry.page_number) {
                entry.word_count = words.len();
                entry.words = words;
            }
        }
    }
    base_geometry
}

/// Return an empty scanned-source geometry entry for a non-PDF image.
///
/// Non-PDF images always need OCR, so the placeholder marks the page scanned
/// with zero words until OCR fills them in.
pub fn image_page_geometry(page_number: i64) -> PageGeometry {
    PageGeometry::new(page_number, SOURCE_SCANNED, 0, Vec::new())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn word(text: &str, bbox: [i64; 4]) -> Word {
        Word::new(text, bbox, 1.0)
    }

    fn ocr_page(page_number: i64, words: Vec<Word>) -> OcrPage {
        OcrPage { page_number, words }
    }

    #[test]
    fn rescale_rounds_half_to_even_like_python() {
        let words = vec![word("a", [21, 5, 43, 9]), word("b", [3, 7, 11, 13])];
        let out = rescale_words(&words, 0.5, 0.5);
        assert_eq!(out[0].bbox, [10, 2, 22, 4]);
        assert_eq!(out[1].bbox, [2, 4, 6, 6]);
        assert_eq!(out[1].score, 1.0);
    }

    #[test]
    fn rescale_non_uniform_axes_and_identity_scale() {
        let words = vec![word("x", [100, 50, 200, 75])];
        let doubled = rescale_words(&words, 2.0, 3.0);
        assert_eq!(doubled[0].bbox, [200, 150, 400, 225]);
        let same = rescale_words(&words, 1.0, 1.0);
        assert_eq!(same[0].bbox, [100, 50, 200, 75]);
    }

    #[test]
    fn merge_fills_only_scanned_entries_with_ocr_words() {
        let base = vec![
            PageGeometry::new(1, SOURCE_DIGITAL, 900, vec![word("digital", [1, 2, 3, 4])]),
            PageGeometry::new(2, SOURCE_SCANNED, 12, Vec::new()),
            PageGeometry::new(3, SOURCE_SCANNED, 5, Vec::new()),
        ];
        let ocr = vec![ocr_page(2, vec![word("ocr", [9, 9, 20, 20])])];
        let merged = merge_scanned_into_geometry(base, ocr);
        assert_eq!(merged.len(), 3);
        assert_eq!(merged[0].source, SOURCE_DIGITAL);
        assert_eq!(merged[0].word_count, 1);
        assert_eq!(merged[1].words.len(), 1);
        assert_eq!(merged[1].words[0].text, "ocr");
        assert_eq!(merged[1].word_count, 1);
        // No OCR result for page 3 → stays an empty scanned entry.
        assert!(merged[2].words.is_empty());
        assert_eq!(merged[2].word_count, 0);
    }

    #[test]
    fn merge_prefers_last_ocr_entry_for_duplicate_pages() {
        let base = vec![PageGeometry::new(1, SOURCE_SCANNED, 3, Vec::new())];
        let ocr = vec![
            ocr_page(1, vec![word("first", [0, 0, 1, 1])]),
            ocr_page(1, vec![word("second", [2, 2, 3, 3])]),
        ];
        let merged = merge_scanned_into_geometry(base, ocr);
        assert_eq!(merged[0].words[0].text, "second");
    }

    #[test]
    fn image_page_is_placeholder_scanned() {
        let geo = image_page_geometry(7);
        assert_eq!(geo.page_number, 7);
        assert_eq!(geo.source, SOURCE_SCANNED);
        assert_eq!(geo.char_count, 0);
        assert_eq!(geo.word_count, 0);
        assert!(geo.words.is_empty());
    }
}
