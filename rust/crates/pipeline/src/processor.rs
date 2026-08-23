//! processor.rs ← processor.py (source document → page images for the VLM).
//!
//! Two inputs, two paths:
//!
//! * **Images** are decoded, resized and re-encoded entirely in-process with
//!   the `image` crate. Python needed Pillow for this; Rust does not need a
//!   sidecar, so the upload path has no external dependency at all.
//! * **PDFs** still need PDFium to rasterise. That lives in the PDF sidecar
//!   (`PDF_SERVICE_URL`) alongside the digital-text endpoint, because PDFium
//!   is a C++ library with no pure-Rust equivalent.
//!
//! The *decisions* stay here either way. [`vlm_budget_dims`] and
//! [`compute_target_dpi`] are the normative implementations of Qwen3-VL's
//! `_resize_pil` budget and the adaptive-DPI rule; the sidecar mirrors them so
//! that a page rendered remotely lands in exactly the coordinate space the
//! word boxes from `geometry.rs` assume. Getting this wrong shifts every
//! bounding box in the review UI, so both are covered by tests below.
//!
//! Sidecar contract:
//!
//! ```text
//! POST {pdf_service_url}/page-count   (body: raw PDF bytes)
//! 200  { "page_count": 12 }
//!
//! POST {pdf_service_url}/render       (body: raw PDF bytes)
//!      ?dpi=128&max_pages=5&max_long_side=1536&max_pixels=1720320&jpeg_quality=92
//! 200  { "pages": [ {page_number, image_b64, mime_type, width, height,
//!                    orig_width, orig_height, doc_total_pages} ],
//!        "skipped_pages": [3],
//!        "total_pages": 12 }
//! ```
//!
//! Render options travel as explicit query parameters rather than being read
//! from the sidecar's own environment: one process owns the configuration.

use std::io::Cursor;

use augocr_common::config::Config;
use augocr_common::error::{AppError, AppResult};
use base64::Engine as _;
use image::codecs::jpeg::JpegEncoder;
use image::ImageReader;
use serde::Deserialize;

use crate::page::RenderedPage;
use crate::pdf_extractor::PdfExtractorClient;

/// Qwen3-VL consumes images in 32-pixel patches. Dimensions that are not a
/// multiple of 32 make the model pad internally, which shifts every returned
/// bounding box a few pixels up — the review UI then draws boxes slightly
/// above their fields.
const PATCH_ALIGN: u32 = 32;

/// Return the JPEG dimensions for an image of `w`×`h` under the VLM budget.
///
/// Mirrors Python `_resize_to_vlm_budget`. Two constraints apply and the
/// tighter one wins: the long side may not exceed `max_long_side`, and the
/// total pixel area may not exceed `max_pixels`. The result is then rounded to
/// the nearest [`PATCH_ALIGN`] multiple (never below one full patch).
///
/// Returns the dimensions only; the caller decides whether a resample is
/// needed (`dims != (w, h)`).
pub fn vlm_budget_dims(w: u32, h: u32, max_long_side: u32, max_pixels: u32) -> (u32, u32) {
    // Python would raise ZeroDivisionError here; PIL never yields a
    // zero-sized image, but a malformed sidecar reply could.
    if w == 0 || h == 0 {
        return (PATCH_ALIGN, PATCH_ALIGN);
    }

    let area = u64::from(w) * u64::from(h);
    let scale_side = f64::from(max_long_side) / f64::from(w.max(h));
    let scale_side = scale_side.min(1.0);
    let scale_area = if area > u64::from(max_pixels) {
        (f64::from(max_pixels) / area as f64).sqrt()
    } else {
        1.0
    };
    let scale = scale_side.min(scale_area);

    let (nw, nh) = if scale < 1.0 {
        // Python `int()` truncates toward zero, then clamps to at least 1px.
        (
            ((f64::from(w) * scale) as u32).max(1),
            ((f64::from(h) * scale) as u32).max(1),
        )
    } else {
        (w, h)
    };

    (align_to_patch(nw), align_to_patch(nh))
}

/// `max(32, round(n / 32) * 32)` with Python's half-to-even rounding.
fn align_to_patch(n: u32) -> u32 {
    let aligned = (f64::from(n) / f64::from(PATCH_ALIGN)).round_ties_even() * f64::from(PATCH_ALIGN);
    (aligned as u32).max(PATCH_ALIGN)
}

/// Adaptive per-page DPI: scale so the long side lands on `max_long_side`,
/// never above `dpi` and never below `dpi_floor`.
///
/// Mirrors Python `_render_pdf_sync`. Returns `None` for a non-positive page
/// size — corrupt PDFs report 0×0 pages, and Python skipped those rather than
/// dividing by zero.
pub fn compute_target_dpi(max_pts: f64, dpi: u32, max_long_side: u32, dpi_floor: u32) -> Option<u32> {
    // `is_sign_negative` would accept NaN; an explicit finite-and-positive
    // test is what "a page we can render" actually means.
    if !max_pts.is_finite() || max_pts <= 0.0 {
        return None;
    }
    let fitted = (f64::from(max_long_side) / (max_pts / 72.0)) as u32;
    Some(fitted.min(dpi).max(dpi_floor))
}

// ── PDF rendering (sidecar) ──────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RenderResponse {
    #[serde(default)]
    pages: Vec<RenderedPage>,
    #[serde(default)]
    skipped_pages: Vec<i64>,
    #[serde(default)]
    total_pages: i64,
}

#[derive(Debug, Deserialize)]
struct PageCountResponse {
    #[serde(default)]
    page_count: i64,
}

impl PdfExtractorClient {
    /// Page count without rendering (Python `count_pdf_pages`).
    pub async fn page_count(&self, pdf_bytes: &[u8]) -> AppResult<i64> {
        let body: PageCountResponse = self.post_pdf("page-count", pdf_bytes, &[]).await?;
        Ok(body.page_count)
    }

    /// Render pages to JPEG (Python `pdf_to_images`).
    async fn render(
        &self,
        pdf_bytes: &[u8],
        dpi: u32,
        max_pages: Option<usize>,
        cfg: &Config,
    ) -> AppResult<RenderResponse> {
        let mut params: Vec<(&str, String)> = vec![
            ("dpi", dpi.to_string()),
            ("max_long_side", cfg.max_long_side_px.to_string()),
            ("max_pixels", cfg.max_pixels.to_string()),
            ("jpeg_quality", cfg.jpeg_quality.to_string()),
            ("dpi_floor", cfg.dpi_floor.to_string()),
        ];
        if let Some(limit) = max_pages {
            params.push(("max_pages", limit.to_string()));
        }
        self.post_pdf("render", pdf_bytes, &params).await
    }
}

/// Render every page of a PDF to a base64 JPEG (Python `pdf_to_images`).
///
/// `max_pages` caps rendering from page 1, mirroring the Python argument. A
/// page that fails to render is skipped and logged rather than failing the
/// document — but if *every* attempted page fails, that surfaces as a hard
/// error instead of a silent empty success.
pub async fn pdf_to_images(
    pdf_bytes: &[u8],
    dpi: Option<u32>,
    max_pages: Option<usize>,
) -> AppResult<Vec<RenderedPage>> {
    let cfg = Config::global();
    let dpi = dpi.unwrap_or(cfg.dpi_default);
    let response = PdfExtractorClient::global()
        .render(pdf_bytes, dpi, max_pages, cfg)
        .await?;

    let render_count = match max_pages {
        Some(limit) => (response.total_pages as usize).min(limit),
        None => response.total_pages as usize,
    };

    if !response.skipped_pages.is_empty() {
        tracing::warn!(
            "PDF render partial — skipped {}/{render_count} page(s): {:?}",
            response.skipped_pages.len(),
            response.skipped_pages
        );
    }

    if response.pages.is_empty() && render_count > 0 {
        return Err(AppError::BadRequest(format!(
            "PDF_RENDER_FAILED: All {render_count} page(s) failed to render (skipped={:?})",
            response.skipped_pages
        )));
    }

    tracing::info!(
        "PDF render complete — {}/{} pages OK",
        response.pages.len(),
        response.total_pages
    );
    Ok(response.pages)
}

// ── Single image normalisation (in-process) ──────────────────────────────────

/// Normalise one uploaded image into the single-element page list the rest of
/// the pipeline expects (Python `image_file_to_b64`).
///
/// Converts to RGB (dropping alpha and palette modes), applies the VLM budget
/// resize, and re-encodes as JPEG. Unlike the PDF path this runs entirely in
/// this process.
///
/// This is CPU-bound work — decode plus a Lanczos3 resample of a multi-
/// megapixel image is tens of milliseconds — so it runs on the blocking pool
/// rather than stalling a runtime worker thread, which is what Python's
/// `run_in_executor` was doing.
pub async fn image_file_to_b64(file_bytes: Vec<u8>) -> AppResult<Vec<RenderedPage>> {
    let cfg = Config::global();
    let max_long_side = cfg.max_long_side_px;
    let max_pixels = cfg.max_pixels;
    let quality = cfg.jpeg_quality;

    tokio::task::spawn_blocking(move || {
        normalise_image(&file_bytes, max_long_side, max_pixels, quality)
    })
    .await
    .map_err(|e| AppError::Internal(format!("image normalisation task failed: {e}")))?
}

/// The synchronous body of [`image_file_to_b64`], separated so it is testable
/// without a runtime.
fn normalise_image(
    file_bytes: &[u8],
    max_long_side: u32,
    max_pixels: u32,
    jpeg_quality: u8,
) -> AppResult<Vec<RenderedPage>> {
    // `with_guessed_format` sniffs content rather than trusting the extension,
    // matching `Image.open`, which ignores the filename entirely.
    let decoded = ImageReader::new(Cursor::new(file_bytes))
        .with_guessed_format()
        .map_err(|e| {
            AppError::BadRequest(format!("IMAGE_DECODE_FAILED: Could not open image: {e}"))
        })?
        .decode()
        .map_err(|e| {
            AppError::BadRequest(format!(
                "IMAGE_DECODE_FAILED: Could not open or decode image: {e}"
            ))
        })?;

    let (w, h) = (decoded.width(), decoded.height());
    let (nw, nh) = vlm_budget_dims(w, h, max_long_side, max_pixels);

    // `.convert("RGB")` — JPEG has no alpha channel, so this must happen
    // whether or not the image is resized.
    let rgb = if (nw, nh) == (w, h) {
        decoded.into_rgb8()
    } else {
        // Lanczos3 is Pillow's LANCZOS: the sharpest downscale available.
        image::imageops::resize(&decoded.into_rgb8(), nw, nh, image::imageops::FilterType::Lanczos3)
    };
    tracing::debug!("Image normalised {w}x{h} → {nw}x{nh}");

    // Rough JPEG size estimate to avoid repeated buffer growth on large pages.
    let mut jpeg = Vec::with_capacity((nw as usize * nh as usize / 4).max(8 * 1024));
    JpegEncoder::new_with_quality(&mut jpeg, jpeg_quality)
        .encode_image(&rgb)
        .map_err(|e| {
            AppError::BadRequest(format!(
                "IMAGE_DECODE_FAILED: Could not encode image as JPEG: {e}"
            ))
        })?;

    Ok(vec![RenderedPage {
        page_number: 1,
        image_b64: base64::engine::general_purpose::STANDARD.encode(&jpeg),
        mime_type: "image/jpeg".to_string(),
        width: i64::from(rgb.width()),
        height: i64::from(rgb.height()),
        orig_width: i64::from(w),
        orig_height: i64::from(h),
        doc_total_pages: 1,
        // An uploaded image always needs OCR; the worker records that when it
        // stores the page row.
        source: None,
    }])
}

#[cfg(test)]
mod tests {
    use super::*;

    const MAX_SIDE: u32 = 1536;
    const MAX_PIXELS: u32 = 1536 * 1120; // 1,720,320

    #[test]
    fn budget_leaves_small_images_alone_but_still_aligns() {
        // 640x480 is under both budgets; only patch alignment applies.
        // 640/32 = 20 exactly; 480/32 = 15 exactly.
        assert_eq!(vlm_budget_dims(640, 480, MAX_SIDE, MAX_PIXELS), (640, 480));
    }

    #[test]
    fn long_side_constraint_wins_for_tall_narrow_pages() {
        // 400x4000: area 1.6M is under budget, but the long side is not.
        // scale = 1536/4000 = 0.384 → 153x1536 → aligned 160x1536.
        assert_eq!(vlm_budget_dims(400, 4000, MAX_SIDE, MAX_PIXELS), (160, 1536));
    }

    #[test]
    fn area_constraint_wins_for_large_square_pages() {
        // 2000x2000: long-side scale 0.768, area scale sqrt(1720320/4e6)
        // = 0.6558 — the area constraint is tighter. 2000*0.6558 = 1311.6,
        // truncated to 1311, then patch-aligned up to 1312.
        let (w, h) = vlm_budget_dims(2000, 2000, MAX_SIDE, MAX_PIXELS);
        assert_eq!((w, h), (1312, 1312));
        // Patch alignment rounds to the *nearest* multiple of 32, so the final
        // area can land marginally over the raw budget (1312² = 1,721,344 vs
        // 1,720,320). Python behaves identically — the budget bounds the
        // pre-alignment scale, not the aligned result. What must hold is that
        // alignment never moves a dimension by a whole patch.
        assert!(u64::from(w) * u64::from(h) <= u64::from(MAX_PIXELS) + u64::from(w) * 32);
    }

    #[test]
    fn patch_alignment_rounds_half_to_even_like_python() {
        // 48/32 = 1.5 → banker's rounding gives 2 → 64.
        assert_eq!(align_to_patch(48), 64);
        // 16/32 = 0.5 → rounds to 0 → floored at one full patch.
        assert_eq!(align_to_patch(16), 32);
        // 112/32 = 3.5 → rounds to 4 → 128.
        assert_eq!(align_to_patch(112), 128);
        // 80/32 = 2.5 → rounds to 2 (even) → 64.
        assert_eq!(align_to_patch(80), 64);
        assert_eq!(align_to_patch(0), 32);
    }

    #[test]
    fn every_budget_result_is_patch_aligned() {
        for (w, h) in [(37, 41), (1, 1), (5000, 17), (1536, 1120), (999, 1001)] {
            let (nw, nh) = vlm_budget_dims(w, h, MAX_SIDE, MAX_PIXELS);
            assert_eq!(nw % PATCH_ALIGN, 0, "{w}x{h} → {nw} not patch aligned");
            assert_eq!(nh % PATCH_ALIGN, 0, "{w}x{h} → {nh} not patch aligned");
            assert!(nw >= PATCH_ALIGN && nh >= PATCH_ALIGN);
        }
    }

    #[test]
    fn zero_sized_input_does_not_divide_by_zero() {
        assert_eq!(vlm_budget_dims(0, 0, MAX_SIDE, MAX_PIXELS), (32, 32));
        assert_eq!(vlm_budget_dims(100, 0, MAX_SIDE, MAX_PIXELS), (32, 32));
    }

    #[test]
    fn target_dpi_matches_python_clamping() {
        // US Letter: 792pt = 11in → 1536/11 = 139.6 → int 139, capped at dpi=128.
        assert_eq!(compute_target_dpi(792.0, 128, MAX_SIDE, 96), Some(128));
        // A tiny label: 1536/(72/72) = 1536 → capped at dpi.
        assert_eq!(compute_target_dpi(72.0, 128, MAX_SIDE, 96), Some(128));
        // A huge plan sheet: 1536/(7200/72) = 15 → lifted to the floor.
        assert_eq!(compute_target_dpi(7200.0, 128, MAX_SIDE, 96), Some(96));
        // Corrupt page dimensions are skipped, not divided by.
        assert_eq!(compute_target_dpi(0.0, 128, MAX_SIDE, 96), None);
        assert_eq!(compute_target_dpi(-1.0, 128, MAX_SIDE, 96), None);
        assert_eq!(compute_target_dpi(f64::NAN, 128, MAX_SIDE, 96), None);
    }

    /// Encode a solid-colour PNG so the decode path has real bytes to chew on.
    fn png_fixture(w: u32, h: u32) -> Vec<u8> {
        let img = image::RgbaImage::from_pixel(w, h, image::Rgba([200, 40, 40, 255]));
        let mut out = Vec::new();
        image::DynamicImage::ImageRgba8(img)
            .write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png)
            .expect("encode png fixture");
        out
    }

    #[test]
    fn normalise_image_resizes_and_reports_original_dims() {
        let png = png_fixture(2000, 1000);
        let pages = normalise_image(&png, MAX_SIDE, MAX_PIXELS, 92).expect("normalise");
        assert_eq!(pages.len(), 1);
        let page = &pages[0];
        assert_eq!(page.page_number, 1);
        assert_eq!(page.mime_type, "image/jpeg");
        assert_eq!((page.orig_width, page.orig_height), (2000, 1000));
        assert_eq!(page.doc_total_pages, 1);

        let (ew, eh) = vlm_budget_dims(2000, 1000, MAX_SIDE, MAX_PIXELS);
        assert_eq!((page.width, page.height), (i64::from(ew), i64::from(eh)));

        // The payload must be a real JPEG, not just non-empty base64.
        let raw = base64::engine::general_purpose::STANDARD
            .decode(&page.image_b64)
            .expect("valid base64");
        assert_eq!(&raw[..2], &[0xFF, 0xD8], "JPEG SOI marker");
        let decoded = image::load_from_memory(&raw).expect("re-decodable jpeg");
        assert_eq!((decoded.width(), decoded.height()), (ew, eh));
    }

    #[test]
    fn normalise_image_strips_alpha_without_resizing_when_in_budget() {
        // 320x320 is inside both budgets and already patch-aligned.
        let png = png_fixture(320, 320);
        let pages = normalise_image(&png, MAX_SIDE, MAX_PIXELS, 92).expect("normalise");
        assert_eq!((pages[0].width, pages[0].height), (320, 320));
        assert_eq!((pages[0].orig_width, pages[0].orig_height), (320, 320));
    }

    #[test]
    fn corrupt_image_is_a_bad_request_not_a_panic() {
        let err = normalise_image(b"not an image at all", MAX_SIDE, MAX_PIXELS, 92)
            .expect_err("must fail");
        assert_eq!(err.status().as_u16(), 400);
        assert!(err.detail().contains("IMAGE_DECODE_FAILED"), "{}", err.detail());
    }
}
