//! spatial_memory.rs ← spatial_memory.py (reusable geometry from corrections).
//!
//! Two phases:
//!
//! * **Write** — [`save_from_corrections`] turns manual drag-box corrections
//!   into normalised regions keyed by vendor + layout.
//! * **Read** — [`apply_to_extraction`] looks those regions up for the current
//!   vendor + layout, reads the *current* document's text inside them, and
//!   overrides the extraction result with it.
//!
//! The critical rule (AGENTS.md §2–3): store **where** a field is, never
//! **what** its old value was. On reuse the text always comes from the
//! document being processed now — which is why the read path re-derives text
//! from page geometry rather than replaying a stored string.

use std::collections::HashMap;
use std::collections::HashSet;

use augocr_common::error::AppResult;
use augocr_common::layout_key::compute_layout_key;
use augocr_common::pyjson::{py_str, truncate_chars};
use serde_json::{json, Map, Value};
use sqlx::PgPool;

use crate::page::Word;

pub const LINE_ITEM_FIELD_PREFIX: &str = "line_item_";
pub const LINE_ITEM_CONTAINER: &str = "line_items";
pub const STRATEGY_SPATIAL_MEMORY: &str = "spatial_memory";
/// Only human drag-box corrections become reusable memory. Qwen anchors and
/// previously applied memory describe an anchor, not a value region.
const STRATEGY_MANUAL: &str = "manual";
/// Text shorter than this is treated as a stale/mis-registered region and the
/// model's own answer is kept instead.
const MIN_REUSABLE_TEXT_LEN: usize = 2;
/// Rows are banded into 10px strips before left-to-right ordering, so words
/// whose baselines wobble by a pixel or two still read as one line.
const READING_ORDER_BAND_PX: i64 = 10;

/// A row from `spatial_memory` that is ready to apply.
struct Memory {
    field_key: String,
    page_number: i64,
    normalized_box: [f64; 4],
}

/// Page dimensions and OCR engine, keyed by page number.
#[derive(Default)]
struct PageDims {
    dims: HashMap<i64, (i64, i64)>,
    sources: HashMap<i64, String>,
}

impl PageDims {
    fn from_rows(pages: &[Value]) -> Self {
        let mut out = Self::default();
        for p in pages {
            let Some(pn) = p.get("page_number").and_then(Value::as_i64) else {
                continue;
            };
            let w = p.get("width").and_then(Value::as_i64).unwrap_or(0);
            let h = p.get("height").and_then(Value::as_i64).unwrap_or(0);
            out.dims.insert(pn, (w, h));
            out.sources.insert(
                pn,
                p.get("source")
                    .and_then(Value::as_str)
                    .unwrap_or("paddleocr")
                    .to_string(),
            );
        }
        out
    }

    /// Usable dimensions for a page, or `None` when the page is unknown or
    /// has a non-positive extent.
    fn usable(&self, page_num: i64) -> Option<(i64, i64)> {
        match self.dims.get(&page_num) {
            Some(&(w, h)) if w > 0 && h > 0 => Some((w, h)),
            _ => None,
        }
    }

    fn source_engine(&self, page_num: i64) -> &'static str {
        match self.sources.get(&page_num).map(String::as_str) {
            Some("pypdfium") => "pypdfium",
            _ => "paddleocr",
        }
    }
}

// ── Geometry helpers ─────────────────────────────────────────────────────────

/// Read a pixel box in either accepted shape, with inverted boxes (drawn
/// right-to-left or bottom-to-top) flipped into `x0 <= x1, y0 <= y1`.
fn read_box(box_value: &Value) -> Option<[f64; 4]> {
    let (x0, y0, x1, y1) = match box_value {
        Value::Array(items) if items.len() == 4 => (
            items[0].as_f64()?,
            items[1].as_f64()?,
            items[2].as_f64()?,
            items[3].as_f64()?,
        ),
        Value::Object(map) => {
            // The review UI sends `left/top/right/bottom`; the pipeline sends
            // `x0/y0/x1/y1`. Both mean the same thing.
            let pick = |a: &str, b: &str| -> f64 {
                map.get(a)
                    .or_else(|| map.get(b))
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0)
            };
            (
                pick("x0", "left"),
                pick("y0", "top"),
                pick("x1", "right"),
                pick("y1", "bottom"),
            )
        }
        _ => return None,
    };
    Some([x0.min(x1), y0.min(y1), x0.max(x1), y0.max(y1)])
}

/// Convert a pixel box to normalised 0–1 coordinates, rounded to 6 decimals.
///
/// Returns `None` for a non-positive page size.
fn normalize_box(pixel_box: &[f64; 4], page_width: i64, page_height: i64) -> Option<Value> {
    if page_width <= 0 || page_height <= 0 {
        return None;
    }
    // `round(x, 6)`. Python rounds the decimal representation; scaling by 1e6
    // agrees with it everywhere except ULP-level ties, which cannot matter for
    // a coordinate that is about to be multiplied by a pixel extent.
    let round6 = |v: f64| (v * 1e6).round_ties_even() / 1e6;
    Some(json!({
        "x0": round6(pixel_box[0] / page_width as f64),
        "y0": round6(pixel_box[1] / page_height as f64),
        "x1": round6(pixel_box[2] / page_width as f64),
        "y1": round6(pixel_box[3] / page_height as f64),
    }))
}

/// Convert a normalised box back to pixel coordinates.
fn denormalize_box(normalized: &[f64; 4], page_width: i64, page_height: i64) -> [i64; 4] {
    let scale = |v: f64, extent: i64| (v * extent as f64).round_ties_even() as i64;
    [
        scale(normalized[0], page_width),
        scale(normalized[1], page_height),
        scale(normalized[2], page_width),
        scale(normalized[3], page_height),
    ]
}

/// Read a stored `normalized_box`, accepting the dict or 4-array shape.
fn read_normalized(value: &Value) -> Option<[f64; 4]> {
    match value {
        Value::Object(map) => Some([
            map.get("x0")?.as_f64()?,
            map.get("y0")?.as_f64()?,
            map.get("x1")?.as_f64()?,
            map.get("y1")?.as_f64()?,
        ]),
        Value::Array(items) if items.len() == 4 => Some([
            items[0].as_f64()?,
            items[1].as_f64()?,
            items[2].as_f64()?,
            items[3].as_f64()?,
        ]),
        _ => None,
    }
}

/// Words that overlap the box on both axes, even partially.
fn words_in_box<'a>(words: &'a [Word], b: &[i64; 4]) -> Vec<&'a Word> {
    words
        .iter()
        .filter(|w| {
            let [wx0, wy0, wx1, wy1] = w.bbox;
            wx1 >= b[0] && wx0 <= b[2] && wy1 >= b[1] && wy0 <= b[3]
        })
        .collect()
}

/// Sort matched words top-to-bottom, then left-to-right.
///
/// Rust's integer division truncates toward zero, matching Python's
/// `int(y / 10)` for negative coordinates as well as positive ones. The sort
/// is stable in both languages, so equal keys keep input order.
fn reading_order(mut words: Vec<&Word>) -> Vec<&Word> {
    words.sort_by_key(|w| (w.bbox[1] / READING_ORDER_BAND_PX, w.bbox[0]));
    words
}

/// The current document's text inside a region, in reading order.
fn text_in_region(words: &[Word], pixel_box: &[i64; 4]) -> String {
    let matched = reading_order(words_in_box(words, pixel_box));
    let mut text = String::new();
    for w in matched {
        if !text.is_empty() {
            text.push(' ');
        }
        text.push_str(&w.text);
    }
    text.trim().to_string()
}

/// The `field_locations` entry recording where a value was re-read from.
fn spatial_location(page_num: i64, pixel_box: &[i64; 4], current_text: &str) -> Value {
    json!({
        "page": page_num,
        "box": pixel_box.to_vec(),
        "strategy": STRATEGY_SPATIAL_MEMORY,
        "confidence": "high",
        "matched_text": current_text,
    })
}

// ── Template field configuration ─────────────────────────────────────────────

/// Configured header field names from list-like template data.
fn configured_field_names(fields: Option<&Value>) -> HashSet<String> {
    let mut names = HashSet::new();
    let Some(Value::Array(list)) = fields else {
        return names;
    };
    for field in list {
        let name = match field {
            Value::String(s) => s.trim().to_string(),
            Value::Object(map) => {
                // `field_key or key or name or id` — a truthiness chain, so an
                // empty string falls through to the next candidate.
                let raw = ["field_key", "key", "name", "id"]
                    .iter()
                    .find_map(|k| map.get(*k).filter(|v| augocr_common::pyjson::truthy(v)));
                raw.map(|v| py_str(v).trim().to_string()).unwrap_or_default()
            }
            _ => String::new(),
        };
        if !name.is_empty() {
            names.insert(name);
        }
    }
    names
}

/// Header field names for this client/template, preferring the extraction's
/// own snapshot and falling back to the vendor's template row.
async fn load_configured_header_fields(
    pool: &PgPool,
    extraction: &Value,
    vendor_id: &str,
) -> HashSet<String> {
    let names = configured_field_names(extraction.get("header_fields"));
    if !names.is_empty() {
        return names;
    }
    // A missing template is not an error here — it just means no field is
    // eligible, which the callers already treat as "skip everything".
    match augocr_common::db::get_template(pool, vendor_id).await {
        Ok(template) => configured_field_names(
            template.as_ref().and_then(|t| t.get("header_fields")),
        ),
        Err(e) => {
            tracing::debug!("Could not load template fields for vendor={vendor_id}: {e}");
            HashSet::new()
        }
    }
}

/// True when a review field is a reusable top-level template field.
///
/// Line-item cells and the container itself are never reusable, and with no
/// configured template nothing is: an unconstrained memory would let a stale
/// region overwrite an arbitrary key.
fn is_reusable_header_field(field_key: &str, configured: &HashSet<String>) -> bool {
    if field_key.trim().is_empty()
        || field_key == LINE_ITEM_CONTAINER
        || field_key.starts_with(LINE_ITEM_FIELD_PREFIX)
    {
        return false;
    }
    configured.contains(field_key)
}

// ── Phase 3: write path ──────────────────────────────────────────────────────

/// Flatten `field_locations` into `(field_key, location)` pairs.
///
/// The po-per-page shape is a *list* of maps. Python deliberately built a list
/// of pairs rather than merging the maps, so the same field on two pages (say
/// `po_number` on pages 1 and 2) yields two memories instead of one
/// overwriting the other.
fn location_items(field_locations: &Value) -> Vec<(&String, &Value)> {
    match field_locations {
        Value::Array(list) => list
            .iter()
            .filter_map(Value::as_object)
            .flat_map(|fl| fl.iter())
            .collect(),
        Value::Object(map) => map.iter().collect(),
        _ => Vec::new(),
    }
}

/// Persist spatial memory from manual review corrections.
///
/// Returns the number of rows written.
pub async fn save_from_corrections(
    pool: &PgPool,
    extraction_id: i64,
    field_locations: &Value,
) -> AppResult<usize> {
    let Some(extraction) = augocr_common::db::get_extraction(pool, extraction_id).await? else {
        tracing::warn!("Extraction {extraction_id} not found for spatial memory save");
        return Ok(0);
    };

    let Some(vendor_id) = extraction.get("vendor_id").and_then(Value::as_str) else {
        tracing::warn!("No vendor_id on extraction {extraction_id} — skipping spatial memory");
        return Ok(0);
    };
    let template_id = extraction.get("template_id").and_then(Value::as_i64);
    let lk = compute_layout_key(vendor_id, template_id);

    let items = location_items(field_locations);
    if items.is_empty() {
        return Ok(0);
    }

    let pages = augocr_common::db::get_pages(pool, extraction_id).await?;
    let page_dims = PageDims::from_rows(&pages);
    let configured = load_configured_header_fields(pool, &extraction, vendor_id).await;

    tracing::info!(
        "Spatial memory save: layout={lk}, {} locations submitted",
        items.len()
    );

    let mut saved = 0usize;
    for (field_key, loc) in items {
        if !is_reusable_header_field(field_key, &configured) {
            continue;
        }
        let Some(loc) = loc.as_object() else { continue };

        let strategy = loc
            .get("strategy")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        if strategy != STRATEGY_MANUAL {
            tracing::debug!(
                "Spatial memory skip: field={field_key} strategy={strategy} (not manual)"
            );
            continue;
        }

        let Some(box_value) = loc.get("box").filter(|v| augocr_common::pyjson::truthy(v)) else {
            continue;
        };
        let page_num = loc.get("page").and_then(Value::as_i64).unwrap_or(1);

        let Some((w, h)) = page_dims.usable(page_num) else {
            continue;
        };
        let Some(pixel_box) = read_box(box_value) else {
            continue;
        };
        let Some(normalized) = normalize_box(&pixel_box, w, h) else {
            continue;
        };

        let write = augocr_common::db::upsert_spatial_memory(
            pool,
            vendor_id,
            &lk,
            field_key,
            page_num as i32,
            &normalized,
            page_dims.source_engine(page_num),
            Some(extraction_id as i32),
        )
        .await;

        match write {
            Ok(_) => {
                saved += 1;
                tracing::info!(
                    "Spatial memory saved: vendor={vendor_id} layout={lk} field={field_key} page={page_num}"
                );
            }
            // One bad region must not abandon the rest of the corrections.
            Err(e) => tracing::warn!("Failed to save spatial memory for field={field_key}: {e}"),
        }
    }

    tracing::info!("Spatial memory save done: layout={lk}, {saved} saved");
    Ok(saved)
}

// ── Phase 4: read path ───────────────────────────────────────────────────────

/// Words per page, from unified page geometry.
fn words_by_page(page_geometry: &[Value]) -> HashMap<i64, Vec<Word>> {
    let mut out = HashMap::with_capacity(page_geometry.len());
    for entry in page_geometry {
        let pn = entry.get("page_number").and_then(Value::as_i64).unwrap_or(0);
        let words = entry
            .get("words")
            .and_then(|w| serde_json::from_value::<Vec<Word>>(w.clone()).ok())
            .unwrap_or_default();
        out.insert(pn, words);
    }
    out
}

/// Load and validate the memory rows for a layout.
async fn load_memories(pool: &PgPool, vendor_id: &str, layout_key: &str) -> AppResult<Vec<Memory>> {
    let rows = augocr_common::db::get_spatial_memory_for_layout(pool, vendor_id, layout_key).await?;
    Ok(rows
        .iter()
        .filter_map(|m| {
            Some(Memory {
                field_key: m.get("field_key")?.as_str()?.to_string(),
                page_number: m.get("page_number")?.as_i64()?,
                normalized_box: read_normalized(m.get("normalized_box")?)?,
            })
        })
        .collect())
}

/// Outcome of applying memory to one extraction.
pub struct Applied {
    pub result: Value,
    pub field_locations: Value,
    pub count: usize,
}

/// Apply saved regions to an extraction result.
///
/// For every memory matching the current vendor + layout: convert the
/// normalised box to pixels using *current* page dimensions, read the words
/// inside it, and override the field. When a region yields no usable text the
/// model's original answer is kept.
///
/// Any database failure degrades to returning the inputs unchanged — raw
/// model output is always better than failing the whole extraction, which is
/// what the Python blanket `try/except` was for.
pub async fn apply_to_extraction(
    pool: &PgPool,
    extraction_id: i64,
    result: Value,
    field_locations: Value,
    page_geometry: Option<&[Value]>,
) -> Applied {
    match apply_inner(pool, extraction_id, &result, &field_locations, page_geometry).await {
        Ok(Some(applied)) => applied,
        Ok(None) => Applied {
            result,
            field_locations,
            count: 0,
        },
        Err(e) => {
            tracing::error!(
                "Spatial memory application failed due to database or query error \
                 (falling back to raw OCR/LLM results): {e}"
            );
            Applied {
                result,
                field_locations,
                count: 0,
            }
        }
    }
}

/// `Ok(None)` means "nothing to do, keep the inputs as they are".
async fn apply_inner(
    pool: &PgPool,
    extraction_id: i64,
    result: &Value,
    field_locations: &Value,
    page_geometry: Option<&[Value]>,
) -> AppResult<Option<Applied>> {
    let Some(extraction) = augocr_common::db::get_extraction(pool, extraction_id).await? else {
        return Ok(None);
    };
    // A list of locations only makes sense alongside a list result.
    if field_locations.is_array() && !result.is_array() {
        tracing::debug!("Skipping spatial memory apply: field_locations is list but result is not.");
        return Ok(None);
    }
    let Some(vendor_id) = extraction.get("vendor_id").and_then(Value::as_str) else {
        return Ok(None);
    };

    let template_id = extraction.get("template_id").and_then(Value::as_i64);
    let lk = compute_layout_key(vendor_id, template_id);
    let per_page = result.is_array();

    let memories = load_memories(pool, vendor_id, &lk).await?;
    tracing::info!(
        "Spatial memory loaded: layout={lk}, {} regions{}",
        memories.len(),
        if per_page { " (po_per_page)" } else { "" }
    );
    if memories.is_empty() {
        return Ok(None);
    }

    let configured = load_configured_header_fields(pool, &extraction, vendor_id).await;
    let pages = augocr_common::db::get_pages(pool, extraction_id).await?;
    let page_dims = PageDims::from_rows(&pages);

    // `page_geometry` defaults to the extraction's stored `ocr_data`.
    let stored_geometry;
    let geometry: &[Value] = match page_geometry {
        Some(g) => g,
        None => {
            stored_geometry = extraction
                .get("ocr_data")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            &stored_geometry
        }
    };
    let words = words_by_page(geometry);

    if per_page {
        Ok(Some(apply_per_page(
            result,
            field_locations,
            &memories,
            &configured,
            &page_dims,
            &words,
            extraction_id,
            vendor_id,
            &lk,
        )))
    } else {
        Ok(Some(apply_single(
            result,
            field_locations,
            &memories,
            &configured,
            &page_dims,
            &words,
            extraction_id,
            vendor_id,
            &lk,
        )))
    }
}

/// Shared per-memory step: validate, re-read the region, return the new text.
///
/// `None` means this memory contributes nothing (unconfigured field, unusable
/// page, or text too short to trust).
fn resolve_memory(
    mem: &Memory,
    configured: &HashSet<String>,
    page_dims: &PageDims,
    words: &HashMap<i64, Vec<Word>>,
) -> Option<([i64; 4], String)> {
    if !configured.contains(&mem.field_key) {
        tracing::debug!(
            "Spatial memory skip: field={} not in template (or template unavailable)",
            mem.field_key
        );
        return None;
    }
    let (w, h) = page_dims.usable(mem.page_number)?;
    let pixel_box = denormalize_box(&mem.normalized_box, w, h);
    let empty: Vec<Word> = Vec::new();
    let page_words = words.get(&mem.page_number).unwrap_or(&empty);
    let current_text = text_in_region(page_words, &pixel_box);

    if current_text.chars().count() < MIN_REUSABLE_TEXT_LEN {
        tracing::debug!(
            "Spatial memory skip: field={} page={} text too short ({} chars)",
            mem.field_key,
            mem.page_number,
            current_text.chars().count()
        );
        return None;
    }
    Some((pixel_box, current_text))
}

#[allow(clippy::too_many_arguments)]
fn apply_single(
    result: &Value,
    field_locations: &Value,
    memories: &[Memory],
    configured: &HashSet<String>,
    page_dims: &PageDims,
    words: &HashMap<i64, Vec<Word>>,
    extraction_id: i64,
    vendor_id: &str,
    layout_key: &str,
) -> Applied {
    let mut result_obj = result.as_object().cloned().unwrap_or_default();
    let mut locs = field_locations.as_object().cloned().unwrap_or_default();
    let mut applied = 0usize;

    for mem in memories {
        let Some((pixel_box, current_text)) = resolve_memory(mem, configured, page_dims, words)
        else {
            continue;
        };
        let preview = truncate_chars(&current_text, 50);
        match result_obj.insert(mem.field_key.clone(), json!(current_text)) {
            Some(old) => tracing::info!(
                "Spatial memory applied: field={} old='{}' new='{preview}' (from region on page {})",
                mem.field_key,
                truncate_chars(&py_str(&old), 50),
                mem.page_number
            ),
            None => tracing::info!(
                "Spatial memory added: field={} value='{preview}' (from region on page {})",
                mem.field_key,
                mem.page_number
            ),
        }
        locs.insert(
            mem.field_key.clone(),
            spatial_location(mem.page_number, &pixel_box, &current_text),
        );
        applied += 1;
    }

    if applied > 0 {
        tracing::info!(
            "Spatial memory: {applied} field(s) applied for extraction {extraction_id} \
             (vendor={vendor_id}, layout={layout_key})"
        );
    }
    Applied {
        result: Value::Object(result_obj),
        field_locations: Value::Object(locs),
        count: applied,
    }
}

/// po_per_page: `result[i]` is page `i + 1`, so a memory saved for page 2 only
/// touches `result[1]`.
#[allow(clippy::too_many_arguments)]
fn apply_per_page(
    result: &Value,
    field_locations: &Value,
    memories: &[Memory],
    configured: &HashSet<String>,
    page_dims: &PageDims,
    words: &HashMap<i64, Vec<Word>>,
    extraction_id: i64,
    vendor_id: &str,
    layout_key: &str,
) -> Applied {
    let mut records = result.as_array().cloned().unwrap_or_default();

    // Normalise field_locations into a per-record list.
    let mut fl_list: Vec<Map<String, Value>> = match field_locations {
        Value::Array(list) => list
            .iter()
            .map(|fl| fl.as_object().cloned().unwrap_or_default())
            .collect(),
        other => {
            let shared = other.as_object().cloned().unwrap_or_default();
            vec![shared; records.len()]
        }
    };
    fl_list.resize_with(records.len(), Map::new);

    let mut applied = 0usize;
    for mem in memories {
        // Page numbers are 1-based; index 0 or below is out of range.
        let Ok(idx) = usize::try_from(mem.page_number - 1) else {
            continue;
        };
        if idx >= records.len() {
            continue;
        }
        if !records[idx].is_object() {
            continue;
        }
        let Some((pixel_box, current_text)) = resolve_memory(mem, configured, page_dims, words)
        else {
            continue;
        };

        let preview = truncate_chars(&current_text, 50);
        // Guarded by the `is_object` check above.
        if let Some(record) = records[idx].as_object_mut() {
            match record.insert(mem.field_key.clone(), json!(current_text)) {
                Some(old) => tracing::info!(
                    "Spatial memory applied (po_per_page): field={} page={} old='{}' new='{preview}'",
                    mem.field_key,
                    mem.page_number,
                    truncate_chars(&py_str(&old), 50)
                ),
                None => tracing::info!(
                    "Spatial memory added (po_per_page): field={} page={} value='{preview}'",
                    mem.field_key,
                    mem.page_number
                ),
            }
        }
        fl_list[idx].insert(
            mem.field_key.clone(),
            spatial_location(mem.page_number, &pixel_box, &current_text),
        );
        applied += 1;
    }

    if applied > 0 {
        tracing::info!(
            "Spatial memory: {applied} field(s) applied (po_per_page) for extraction \
             {extraction_id} (vendor={vendor_id}, layout={layout_key})"
        );
    }
    Applied {
        result: Value::Array(records),
        field_locations: Value::Array(fl_list.into_iter().map(Value::Object).collect()),
        count: applied,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(text: &str, bbox: [i64; 4]) -> Word {
        Word::new(text, bbox, 1.0)
    }

    fn configured(names: &[&str]) -> HashSet<String> {
        names.iter().map(|s| (*s).to_string()).collect()
    }

    fn dims(pages: &[(i64, i64, i64)]) -> PageDims {
        let rows: Vec<Value> = pages
            .iter()
            .map(|(pn, w, h)| json!({"page_number": pn, "width": w, "height": h}))
            .collect();
        PageDims::from_rows(&rows)
    }

    #[test]
    fn read_box_accepts_both_shapes_and_flips_inverted_boxes() {
        assert_eq!(read_box(&json!([10, 20, 30, 40])), Some([10.0, 20.0, 30.0, 40.0]));
        // Drawn right-to-left / bottom-to-top.
        assert_eq!(read_box(&json!([30, 40, 10, 20])), Some([10.0, 20.0, 30.0, 40.0]));
        assert_eq!(
            read_box(&json!({"x0": 1, "y0": 2, "x1": 3, "y1": 4})),
            Some([1.0, 2.0, 3.0, 4.0])
        );
        // The review UI's alternate key names.
        assert_eq!(
            read_box(&json!({"left": 3, "top": 4, "right": 1, "bottom": 2})),
            Some([1.0, 2.0, 3.0, 4.0])
        );
        assert_eq!(read_box(&json!([1, 2, 3])), None);
        assert_eq!(read_box(&json!("nope")), None);
    }

    #[test]
    fn normalize_and_denormalize_round_trip() {
        let normalized = normalize_box(&[100.0, 200.0, 500.0, 400.0], 1000, 2000).expect("norm");
        assert_eq!(normalized["x0"], 0.1);
        assert_eq!(normalized["y0"], 0.1);
        assert_eq!(normalized["x1"], 0.5);
        assert_eq!(normalized["y1"], 0.2);

        let back = denormalize_box(&read_normalized(&normalized).expect("read"), 1000, 2000);
        assert_eq!(back, [100, 200, 500, 400]);
    }

    #[test]
    fn normalize_rejects_zero_sized_pages() {
        assert!(normalize_box(&[0.0, 0.0, 1.0, 1.0], 0, 100).is_none());
        assert!(normalize_box(&[0.0, 0.0, 1.0, 1.0], 100, 0).is_none());
    }

    #[test]
    fn words_in_box_matches_partial_overlap() {
        let words = vec![
            w("inside", [10, 10, 20, 20]),
            w("straddling", [95, 10, 130, 20]),
            w("outside", [500, 500, 510, 510]),
            w("touching", [100, 20, 140, 30]),
        ];
        let found: Vec<&str> = words_in_box(&words, &[0, 0, 100, 25])
            .iter()
            .map(|w| w.text.as_str())
            .collect();
        assert_eq!(found, vec!["inside", "straddling", "touching"]);
    }

    #[test]
    fn reading_order_bands_rows_then_sorts_left_to_right() {
        // y=10 and y=15 fall in the same 10px band, so x decides.
        let words = [
            w("second", [200, 15, 260, 25]),
            w("first", [10, 10, 60, 20]),
            w("third", [10, 40, 60, 50]),
        ];
        let ordered: Vec<&str> = reading_order(words.iter().collect())
            .iter()
            .map(|w| w.text.as_str())
            .collect();
        assert_eq!(ordered, vec!["first", "second", "third"]);
    }

    #[test]
    fn text_in_region_joins_in_reading_order() {
        let words = vec![
            w("CORP", [200, 10, 260, 20]),
            w("ACME", [10, 12, 60, 22]),
            w("elsewhere", [900, 900, 950, 950]),
        ];
        assert_eq!(text_in_region(&words, &[0, 0, 300, 30]), "ACME CORP");
        assert_eq!(text_in_region(&words, &[0, 0, 5, 5]), "");
    }

    #[test]
    fn configured_field_names_reads_strings_and_dicts() {
        let fields = json!([
            "  po_number  ",
            {"field_key": "invoice_date"},
            {"key": "total"},
            {"name": "vendor"},
            {"id": 7},
            {"field_key": "", "key": "fallback"},
            {},
            42,
        ]);
        let names = configured_field_names(Some(&fields));
        assert!(names.contains("po_number"), "strings are trimmed");
        assert!(names.contains("invoice_date"));
        assert!(names.contains("total"));
        assert!(names.contains("vendor"));
        assert!(names.contains("7"), "non-string ids go through str()");
        assert!(names.contains("fallback"), "empty field_key falls through");
        assert_eq!(names.len(), 6);
    }

    #[test]
    fn configured_field_names_ignores_non_lists() {
        assert!(configured_field_names(None).is_empty());
        assert!(configured_field_names(Some(&json!({"a": 1}))).is_empty());
        assert!(configured_field_names(Some(&json!(null))).is_empty());
    }

    #[test]
    fn reusable_header_field_excludes_line_items_and_unconfigured() {
        let cfg = configured(&["po_number", "total"]);
        assert!(is_reusable_header_field("po_number", &cfg));
        assert!(!is_reusable_header_field("line_items", &cfg));
        assert!(!is_reusable_header_field("line_item_0_qty", &cfg));
        assert!(!is_reusable_header_field("  ", &cfg));
        assert!(!is_reusable_header_field("unlisted", &cfg));
        // With no template loaded, nothing is reusable.
        assert!(!is_reusable_header_field("po_number", &HashSet::new()));
    }

    #[test]
    fn location_items_keeps_duplicate_keys_across_pages() {
        // po_number appears on both pages; both must survive as separate
        // memories rather than one overwriting the other.
        let fl = json!([
            {"po_number": {"page": 1}},
            {"po_number": {"page": 2}},
        ]);
        let items = location_items(&fl);
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].1["page"], 1);
        assert_eq!(items[1].1["page"], 2);

        assert_eq!(location_items(&json!({"a": 1})).len(), 1);
        assert!(location_items(&json!("nonsense")).is_empty());
    }

    #[test]
    fn page_dims_rejects_unknown_and_zero_sized_pages() {
        let d = dims(&[(1, 1000, 2000), (2, 0, 500)]);
        assert_eq!(d.usable(1), Some((1000, 2000)));
        assert_eq!(d.usable(2), None, "zero width is unusable");
        assert_eq!(d.usable(9), None, "unknown page");
    }

    #[test]
    fn page_dims_source_engine_defaults_to_paddleocr() {
        let rows = vec![
            json!({"page_number": 1, "width": 10, "height": 10, "source": "pypdfium"}),
            json!({"page_number": 2, "width": 10, "height": 10, "source": "paddleocr"}),
            json!({"page_number": 3, "width": 10, "height": 10}),
        ];
        let d = PageDims::from_rows(&rows);
        assert_eq!(d.source_engine(1), "pypdfium");
        assert_eq!(d.source_engine(2), "paddleocr");
        assert_eq!(d.source_engine(3), "paddleocr", "missing source");
        assert_eq!(d.source_engine(9), "paddleocr", "unknown page");
    }

    fn memory(field: &str, page: i64, nbox: [f64; 4]) -> Memory {
        Memory {
            field_key: field.to_string(),
            page_number: page,
            normalized_box: nbox,
        }
    }

    fn word_map(pages: &[(i64, Vec<Word>)]) -> HashMap<i64, Vec<Word>> {
        pages.iter().cloned().collect()
    }

    #[test]
    fn resolve_memory_requires_configuration_dimensions_and_text() {
        let cfg = configured(&["po_number"]);
        let d = dims(&[(1, 1000, 1000), (2, 0, 0)]);
        let words = word_map(&[(1, vec![w("PO-123", [100, 100, 300, 140])])]);

        // Happy path.
        let mem = memory("po_number", 1, [0.05, 0.05, 0.4, 0.2]);
        let (bx, text) = resolve_memory(&mem, &cfg, &d, &words).expect("resolved");
        assert_eq!(bx, [50, 50, 400, 200]);
        assert_eq!(text, "PO-123");

        // Field not in the template.
        let mem = memory("unlisted", 1, [0.05, 0.05, 0.4, 0.2]);
        assert!(resolve_memory(&mem, &cfg, &d, &words).is_none());

        // Zero-sized page.
        let mem = memory("po_number", 2, [0.05, 0.05, 0.4, 0.2]);
        assert!(resolve_memory(&mem, &cfg, &d, &words).is_none());

        // Region contains no words → text too short, keep the model's answer.
        let mem = memory("po_number", 1, [0.9, 0.9, 0.99, 0.99]);
        assert!(resolve_memory(&mem, &cfg, &d, &words).is_none());
    }

    #[test]
    fn resolve_memory_rejects_single_character_text() {
        let cfg = configured(&["po_number"]);
        let d = dims(&[(1, 1000, 1000)]);
        let words = word_map(&[(1, vec![w("X", [100, 100, 120, 140])])]);
        let mem = memory("po_number", 1, [0.05, 0.05, 0.4, 0.2]);
        assert!(
            resolve_memory(&mem, &cfg, &d, &words).is_none(),
            "one character is below the staleness guard"
        );
    }

    #[test]
    fn apply_single_overrides_and_adds_fields() {
        let cfg = configured(&["po_number", "total"]);
        let d = dims(&[(1, 1000, 1000)]);
        let words = word_map(&[(
            1,
            vec![w("PO-999", [100, 100, 300, 140]), w("42.00", [100, 300, 300, 340])],
        )]);
        let memories = vec![
            memory("po_number", 1, [0.05, 0.05, 0.4, 0.2]),
            memory("total", 1, [0.05, 0.25, 0.4, 0.4]),
        ];
        let result = json!({"po_number": "WRONG", "other": "kept"});

        let out = apply_single(
            &result,
            &json!({}),
            &memories,
            &cfg,
            &d,
            &words,
            1,
            "v1",
            "v1:default",
        );
        assert_eq!(out.count, 2);
        assert_eq!(out.result["po_number"], "PO-999", "existing field overridden");
        assert_eq!(out.result["total"], "42.00", "missing field added");
        assert_eq!(out.result["other"], "kept", "untouched field preserved");
        assert_eq!(out.field_locations["po_number"]["strategy"], STRATEGY_SPATIAL_MEMORY);
        assert_eq!(out.field_locations["po_number"]["box"], json!([50, 50, 400, 200]));
        assert_eq!(out.field_locations["total"]["matched_text"], "42.00");
    }

    #[test]
    fn apply_per_page_touches_only_its_own_page() {
        let cfg = configured(&["po_number"]);
        let d = dims(&[(1, 1000, 1000), (2, 1000, 1000)]);
        let words = word_map(&[
            (1, vec![w("PO-A", [100, 100, 300, 140])]),
            (2, vec![w("PO-B", [100, 100, 300, 140])]),
        ]);
        let memories = vec![memory("po_number", 2, [0.05, 0.05, 0.4, 0.2])];
        let result = json!([{"po_number": "OLD-1"}, {"po_number": "OLD-2"}]);

        let out = apply_per_page(
            &result,
            &json!({}),
            &memories,
            &cfg,
            &d,
            &words,
            1,
            "v1",
            "v1:default",
        );
        assert_eq!(out.count, 1);
        assert_eq!(out.result[0]["po_number"], "OLD-1", "page 1 untouched");
        assert_eq!(out.result[1]["po_number"], "PO-B", "page 2 re-read");
        let locs = out.field_locations.as_array().expect("list");
        assert_eq!(locs.len(), 2);
        assert!(locs[0].as_object().expect("map").is_empty());
        assert_eq!(locs[1]["po_number"]["page"], 2);
    }

    #[test]
    fn apply_per_page_ignores_out_of_range_pages() {
        let cfg = configured(&["po_number"]);
        let d = dims(&[(1, 1000, 1000), (5, 1000, 1000)]);
        let words = word_map(&[(5, vec![w("PO-E", [100, 100, 300, 140])])]);
        // Page 5 has no matching record in a 1-record result.
        let memories = vec![memory("po_number", 5, [0.05, 0.05, 0.4, 0.2])];
        let result = json!([{"po_number": "ONLY"}]);

        let out = apply_per_page(
            &result, &json!({}), &memories, &cfg, &d, &words, 1, "v1", "lk",
        );
        assert_eq!(out.count, 0);
        assert_eq!(out.result[0]["po_number"], "ONLY");
    }

    #[test]
    fn apply_per_page_pads_locations_to_match_records() {
        let cfg = configured(&["po_number"]);
        let d = dims(&[(1, 1000, 1000)]);
        let words = word_map(&[(1, vec![w("PO-A", [100, 100, 300, 140])])]);
        let memories = vec![memory("po_number", 1, [0.05, 0.05, 0.4, 0.2])];
        let result = json!([{"a": 1}, {"b": 2}, {"c": 3}]);

        // A single shared location map is fanned out to every record.
        let out = apply_per_page(
            &result,
            &json!({"seed": {"page": 1}}),
            &memories,
            &cfg,
            &d,
            &words,
            1,
            "v1",
            "lk",
        );
        let locs = out.field_locations.as_array().expect("list");
        assert_eq!(locs.len(), 3);
        assert_eq!(locs[2]["seed"]["page"], 1);
        assert_eq!(locs[0]["po_number"]["matched_text"], "PO-A");
    }
}
