//! qwen_layout_apply.rs ← qwen_layout_apply.py.
//!
//! Turns saved `qwen_layout_boxes` into pixel-space `field_locations` for the
//! review UI. Two kinds of entry come out:
//!
//! * header fields → strategy `qwen_anchor`
//! * line-item columns → expanded to `line_item_{row}_{col}` keys with
//!   strategy `qwen_column_header`
//!
//! There is no OCR or word snapping here: the box Qwen returned *is* the box
//! shown. Boxes arrive normalised to 0–1 and are multiplied by each page's
//! rendered pixel size.
//!
//! Inputs stay as [`Value`] rather than becoming typed structs on purpose.
//! They are model output replayed from the database, so any individual entry
//! can be malformed; Python skipped bad entries and kept the rest, and a
//! `Deserialize` impl over the whole map would instead discard everything on
//! the first bad box.

use augocr_common::pyjson::{py_str, truncate_chars};
use serde_json::{json, Map, Value};

use crate::page::PageSize;

pub const STRATEGY_ANCHOR: &str = "qwen_anchor";
pub const STRATEGY_COLUMN: &str = "qwen_column_header";
pub const STRATEGY_COLUMN_MISSING: &str = "qwen_column_header_missing";
pub const FIELD_TYPE_LINE_ITEM_COLUMN: &str = "line_item_column";
/// Longest `matched_text` preview stored on a location entry.
const MATCHED_TEXT_LIMIT: usize = 50;

/// Scale a normalised (0–1) box to pixel space.
///
/// Python used `int(round(v))`, i.e. half-to-even rounding, so a box edge on
/// an exact `.5` pixel lands on the same integer in both implementations.
fn denormalize_box(nbox: &[f64; 4], page_width: i64, page_height: i64) -> Value {
    let scale = |v: f64, extent: i64| -> i64 { (v * extent as f64).round_ties_even() as i64 };
    json!([
        scale(nbox[0], page_width),
        scale(nbox[1], page_height),
        scale(nbox[2], page_width),
        scale(nbox[3], page_height),
    ])
}

/// Read a normalised box in either accepted shape: `{x0, y0, x1, y1}` or a
/// 4-element array. Anything else is unusable and yields `None`.
fn read_normalized_box(box_info: &Value) -> Option<[f64; 4]> {
    let nbox = box_info.get("normalized_box")?;
    match nbox {
        Value::Object(map) => Some([
            map.get("x0")?.as_f64()?,
            map.get("y0")?.as_f64()?,
            map.get("x1")?.as_f64()?,
            map.get("y1")?.as_f64()?,
        ]),
        Value::Array(items) if items.len() == 4 => {
            let mut out = [0.0; 4];
            for (slot, item) in out.iter_mut().zip(items) {
                *slot = item.as_f64()?;
            }
            Some(out)
        }
        _ => None,
    }
}

/// `box_info["page_number"]`, defaulting to page 1.
fn box_page_number(box_info: &Value) -> i64 {
    box_info
        .get("page_number")
        .and_then(Value::as_i64)
        .unwrap_or(1)
}

/// Build one `field_location` entry, or `None` when the page is unknown, has
/// no usable dimensions, or the box is malformed.
fn make_loc(
    box_info: &Value,
    page_num: i64,
    pages: &[PageSize],
    strategy: &str,
    matched_text: &str,
) -> Option<Value> {
    let page = pages.iter().find(|p| p.page_number == page_num)?;
    if page.width <= 0 || page.height <= 0 {
        return None;
    }
    let nbox = read_normalized_box(box_info)?;
    // Key order is preserved on the wire (serde_json `preserve_order`) and
    // matches the Python dict literal.
    Some(json!({
        "page": page_num,
        "box": denormalize_box(&nbox, page.width, page.height),
        "matched_text": matched_text,
        "word_boxes": [],
        "strategy": strategy,
        "confidence": "high",
    }))
}

/// The placeholder emitted when a line-item column has no usable header box.
/// Note the distinct key order and the absent `word_boxes`, both as in Python.
fn missing_column_loc(row_page: i64, col_name: &str) -> Value {
    json!({
        "page": row_page,
        "box": null,
        "strategy": STRATEGY_COLUMN_MISSING,
        "confidence": "low",
        "matched_text": col_name,
    })
}

/// `str(field_value)[:50]`, or `""` when the field is absent or null.
fn matched_text_for(record: &Value, field_key: &str) -> String {
    match record.get(field_key) {
        Some(v) if !v.is_null() => truncate_chars(&py_str(v), MATCHED_TEXT_LIMIT),
        _ => String::new(),
    }
}

/// Split the saved boxes into header boxes and line-item column boxes,
/// preserving insertion order within each group.
fn split_boxes(qwen_boxes: &Map<String, Value>) -> (Vec<(&String, &Value)>, Map<String, Value>) {
    let mut headers = Vec::new();
    let mut columns = Map::new();
    for (key, box_info) in qwen_boxes {
        let field_type = box_info
            .get("field_type")
            .and_then(Value::as_str)
            .unwrap_or("header");
        if field_type == FIELD_TYPE_LINE_ITEM_COLUMN {
            columns.insert(key.clone(), box_info.clone());
        } else {
            headers.push((key, box_info));
        }
    }
    (headers, columns)
}

/// Line-item rows of `record`, or an empty slice when the field is missing or
/// is not a list.
fn line_items_of(record: &Value) -> &[Value] {
    match record.get("line_items") {
        Some(Value::Array(items)) => items.as_slice(),
        _ => &[],
    }
}

/// Map each row index of the merged `line_items` to the page it came from.
///
/// Counts how many line items each per-page result contributed, then assigns
/// row indices sequentially in page order.
fn build_row_page_map(page_results: &[Value]) -> Vec<i64> {
    // Row indices are dense and assigned in order, so a Vec indexed by row is
    // both simpler and cheaper than Python's dict.
    let mut row_page: Vec<i64> = Vec::new();
    if page_results.is_empty() {
        return row_page;
    }

    // `sorted(..., key=lambda p: p.get("_page", 0))` — stable, so results with
    // equal (or missing) page numbers keep their original order.
    let mut ordered: Vec<&Value> = page_results.iter().collect();
    ordered.sort_by_key(|pr| pr.get("_page").and_then(Value::as_i64).unwrap_or(0));

    for pr in ordered {
        if pr.get("_error").is_some() {
            continue;
        }
        // `pr.get("fields", pr)` — fall back to the record itself only when
        // the key is absent, not when it is present but null.
        let fields = match pr.get("fields") {
            Some(f) => f,
            None => pr,
        };
        if !fields.is_object() {
            continue;
        }
        let page_num = pr.get("_page").and_then(Value::as_i64).unwrap_or(1);
        row_page.extend(std::iter::repeat_n(page_num, line_items_of(fields).len()));
    }
    row_page
}

/// Header + line-item locations for a single result record.
fn build_single_result_locs(
    record: &Value,
    headers: &[(&String, &Value)],
    columns: &Map<String, Value>,
    page_num: i64,
    pages: &[PageSize],
) -> Map<String, Value> {
    let mut locs = Map::new();
    if !record.is_object() {
        return locs;
    }

    for (field_key, box_info) in headers {
        let matched = matched_text_for(record, field_key);
        if let Some(entry) = make_loc(box_info, page_num, pages, STRATEGY_ANCHOR, &matched) {
            locs.insert((*field_key).clone(), entry);
        }
    }

    for (row_idx, row) in line_items_of(record).iter().enumerate() {
        let Some(row_obj) = row.as_object() else {
            continue;
        };
        for (col_name, cell_value) in row_obj {
            if cell_value.is_null() {
                continue;
            }
            let Some(col_box_info) = columns.get(col_name) else {
                continue;
            };
            if let Some(entry) =
                make_loc(col_box_info, page_num, pages, STRATEGY_COLUMN, col_name)
            {
                locs.insert(format!("line_item_{row_idx}_{col_name}"), entry);
            }
        }
    }
    locs
}

/// Map saved `qwen_layout_boxes` to pixel-space `field_locations`.
///
/// Returns an object of `{field_key: location}`, or an array of such objects
/// when `result` is an array (the po-per-page shape).
pub fn build_field_locations_from_layout(
    qwen_boxes: &Map<String, Value>,
    pages: &[PageSize],
    result: &Value,
    page_results: &[Value],
) -> Value {
    if qwen_boxes.is_empty() {
        return if result.is_array() { json!([]) } else { json!({}) };
    }

    let (headers, columns) = split_boxes(qwen_boxes);

    // ── po_per_page: one location map per result record ──
    if let Value::Array(records) = result {
        let final_list: Vec<Value> = records
            .iter()
            .enumerate()
            .map(|(i, record)| {
                let locs =
                    build_single_result_locs(record, &headers, &columns, i as i64 + 1, pages);
                Value::Object(locs)
            })
            .collect();
        let mapped: usize = final_list
            .iter()
            .map(|d| d.as_object().map_or(0, Map::len))
            .sum();
        tracing::info!(
            "qwen_layout_apply (multi): mapped {mapped} fields across {} records",
            records.len()
        );
        return Value::Array(final_list);
    }

    // ── Standard single / multipage result ──
    let empty = json!({});
    let result = if result.is_object() { result } else { &empty };
    let mut locs = Map::new();

    for (field_key, box_info) in &headers {
        let page_num = box_page_number(box_info);
        let matched = matched_text_for(result, field_key);
        if let Some(entry) = make_loc(box_info, page_num, pages, STRATEGY_ANCHOR, &matched) {
            locs.insert((*field_key).clone(), entry);
        }
    }

    let row_page_map = build_row_page_map(page_results);
    for (row_idx, row) in line_items_of(result).iter().enumerate() {
        let Some(row_obj) = row.as_object() else {
            continue;
        };
        let row_page = row_page_map.get(row_idx).copied().unwrap_or(1);
        for (col_name, cell_value) in row_obj {
            if cell_value.is_null() {
                continue;
            }
            let comp_key = format!("line_item_{row_idx}_{col_name}");
            // The row's page decides placement; the column header's box
            // supplies the coordinates.
            let entry = columns
                .get(col_name)
                .and_then(|info| make_loc(info, row_page, pages, STRATEGY_COLUMN, col_name))
                .unwrap_or_else(|| missing_column_loc(row_page, col_name));
            locs.insert(comp_key, entry);
        }
    }

    let line_item_cells = locs.keys().filter(|k| k.starts_with("line_item_")).count();
    tracing::info!(
        "qwen_layout_apply: mapped {} field_locations ({} headers, {line_item_cells} line-item cells)",
        locs.len(),
        locs.len() - line_item_cells
    );
    Value::Object(locs)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pages() -> Vec<PageSize> {
        vec![
            PageSize {
                page_number: 1,
                width: 1000,
                height: 2000,
            },
            PageSize {
                page_number: 2,
                width: 1000,
                height: 2000,
            },
        ]
    }

    fn header_box(x0: f64, y0: f64, x1: f64, y1: f64, page: i64) -> Value {
        json!({
            "normalized_box": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "field_type": "header",
            "page_number": page,
        })
    }

    fn column_box(col: &str) -> (String, Value) {
        (
            col.to_string(),
            json!({
                "normalized_box": [0.1, 0.2, 0.3, 0.4],
                "field_type": FIELD_TYPE_LINE_ITEM_COLUMN,
                "page_number": 1,
            }),
        )
    }

    fn boxes(entries: Vec<(String, Value)>) -> Map<String, Value> {
        entries.into_iter().collect()
    }

    #[test]
    fn empty_boxes_return_the_shape_matching_the_result() {
        let empty = Map::new();
        assert_eq!(
            build_field_locations_from_layout(&empty, &pages(), &json!({"a": 1}), &[]),
            json!({})
        );
        assert_eq!(
            build_field_locations_from_layout(&empty, &pages(), &json!([{"a": 1}]), &[]),
            json!([])
        );
    }

    #[test]
    fn header_box_is_denormalised_to_pixels() {
        let qb = boxes(vec![("po_number".into(), header_box(0.1, 0.2, 0.5, 0.25, 1))]);
        let out = build_field_locations_from_layout(&qb, &pages(), &json!({"po_number": "P1"}), &[]);
        let loc = &out["po_number"];
        assert_eq!(loc["box"], json!([100, 400, 500, 500]));
        assert_eq!(loc["page"], 1);
        assert_eq!(loc["strategy"], STRATEGY_ANCHOR);
        assert_eq!(loc["confidence"], "high");
        assert_eq!(loc["matched_text"], "P1");
        assert_eq!(loc["word_boxes"], json!([]));
    }

    #[test]
    fn normalized_box_accepts_the_array_shape_too() {
        let qb = boxes(vec![(
            "total".into(),
            json!({"normalized_box": [0.0, 0.5, 1.0, 0.75], "page_number": 1}),
        )]);
        let out = build_field_locations_from_layout(&qb, &pages(), &json!({"total": 12.5}), &[]);
        assert_eq!(out["total"]["box"], json!([0, 1000, 1000, 1500]));
        // `str(12.5)` — a number renders without quotes.
        assert_eq!(out["total"]["matched_text"], "12.5");
    }

    #[test]
    fn matched_text_uses_python_str_and_truncates_at_50_chars() {
        let long = "x".repeat(80);
        let qb = boxes(vec![
            ("a".into(), header_box(0.0, 0.0, 0.1, 0.1, 1)),
            ("b".into(), header_box(0.0, 0.0, 0.1, 0.1, 1)),
            ("c".into(), header_box(0.0, 0.0, 0.1, 0.1, 1)),
        ]);
        let result = json!({"a": long, "b": true, "c": null});
        let out = build_field_locations_from_layout(&qb, &pages(), &result, &[]);
        assert_eq!(out["a"]["matched_text"].as_str().map(str::len), Some(50));
        assert_eq!(out["b"]["matched_text"], "True");
        // A null field yields an empty preview, not the string "None".
        assert_eq!(out["c"]["matched_text"], "");
    }

    #[test]
    fn unknown_page_or_zero_dimensions_drops_the_entry() {
        let qb = boxes(vec![("po_number".into(), header_box(0.1, 0.2, 0.5, 0.25, 9))]);
        let out = build_field_locations_from_layout(&qb, &pages(), &json!({"po_number": "P"}), &[]);
        assert_eq!(out, json!({}), "page 9 does not exist");

        let zero = vec![PageSize {
            page_number: 1,
            width: 0,
            height: 0,
        }];
        let qb = boxes(vec![("po_number".into(), header_box(0.1, 0.2, 0.5, 0.25, 1))]);
        let out = build_field_locations_from_layout(&qb, &zero, &json!({"po_number": "P"}), &[]);
        assert_eq!(out, json!({}), "a zero-sized page cannot host a box");
    }

    #[test]
    fn malformed_box_is_skipped_without_losing_siblings() {
        let qb = boxes(vec![
            ("good".into(), header_box(0.1, 0.1, 0.2, 0.2, 1)),
            ("bad_shape".into(), json!({"normalized_box": [1, 2], "page_number": 1})),
            ("no_box".into(), json!({"page_number": 1})),
            (
                "bad_type".into(),
                json!({"normalized_box": {"x0": "a", "y0": 0, "x1": 1, "y1": 1}, "page_number": 1}),
            ),
        ]);
        let out = build_field_locations_from_layout(&qb, &pages(), &json!({}), &[]);
        let obj = out.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        assert!(obj.contains_key("good"));
    }

    #[test]
    fn line_item_columns_expand_per_row_and_column() {
        let qb = boxes(vec![
            ("po_number".into(), header_box(0.1, 0.1, 0.2, 0.2, 1)),
            column_box("qty"),
            column_box("desc"),
        ]);
        let result = json!({
            "po_number": "P1",
            "line_items": [
                {"qty": 2, "desc": "widget"},
                {"qty": 5, "desc": null},
            ]
        });
        let out = build_field_locations_from_layout(&qb, &pages(), &result, &[]);
        assert_eq!(out["line_item_0_qty"]["strategy"], STRATEGY_COLUMN);
        assert_eq!(out["line_item_0_qty"]["matched_text"], "qty");
        assert_eq!(out["line_item_0_desc"]["box"], json!([100, 400, 300, 800]));
        assert_eq!(out["line_item_1_qty"]["page"], 1);
        // A null cell contributes no location at all.
        assert!(out.get("line_item_1_desc").is_none());
    }

    #[test]
    fn column_without_a_saved_box_gets_the_missing_placeholder() {
        let qb = boxes(vec![column_box("qty")]);
        let result = json!({"line_items": [{"qty": 1, "unmapped": "x"}]});
        let out = build_field_locations_from_layout(&qb, &pages(), &result, &[]);
        let missing = &out["line_item_0_unmapped"];
        assert_eq!(missing["strategy"], STRATEGY_COLUMN_MISSING);
        assert_eq!(missing["confidence"], "low");
        assert_eq!(missing["box"], Value::Null);
        assert_eq!(missing["matched_text"], "unmapped");
        // The placeholder deliberately carries no `word_boxes` key.
        assert!(missing.get("word_boxes").is_none());
    }

    #[test]
    fn row_page_map_assigns_rows_to_their_source_pages() {
        let page_results = vec![
            json!({"_page": 2, "line_items": [{"a": 1}]}),
            json!({"_page": 1, "line_items": [{"a": 1}, {"a": 2}]}),
        ];
        // Page 1 contributes rows 0-1, page 2 contributes row 2.
        assert_eq!(build_row_page_map(&page_results), vec![1, 1, 2]);
    }

    #[test]
    fn row_page_map_skips_failed_pages_and_reads_nested_fields() {
        let page_results = vec![
            json!({"_page": 1, "_error": "boom", "line_items": [{"a": 1}]}),
            json!({"_page": 2, "fields": {"line_items": [{"a": 1}, {"a": 2}]}}),
            json!({"_page": 3, "fields": null}),
            json!({"_page": 4, "line_items": "not a list"}),
        ];
        assert_eq!(build_row_page_map(&page_results), vec![2, 2]);
    }

    #[test]
    fn rows_take_their_page_from_the_row_page_map() {
        let qb = boxes(vec![column_box("qty")]);
        let result = json!({"line_items": [{"qty": 1}, {"qty": 2}]});
        let page_results = vec![
            json!({"_page": 1, "line_items": [{"qty": 1}]}),
            json!({"_page": 2, "line_items": [{"qty": 2}]}),
        ];
        let out = build_field_locations_from_layout(&qb, &pages(), &result, &page_results);
        assert_eq!(out["line_item_0_qty"]["page"], 1);
        assert_eq!(out["line_item_1_qty"]["page"], 2);
    }

    #[test]
    fn po_per_page_returns_one_location_map_per_record() {
        let qb = boxes(vec![
            ("po_number".into(), header_box(0.1, 0.1, 0.2, 0.2, 1)),
            column_box("qty"),
        ]);
        let result = json!([
            {"po_number": "A", "line_items": [{"qty": 1}]},
            {"po_number": "B", "line_items": []},
        ]);
        let out = build_field_locations_from_layout(&qb, &pages(), &result, &[]);
        let list = out.as_array().expect("array");
        assert_eq!(list.len(), 2);
        // Record N is pinned to page N regardless of each box's own page.
        assert_eq!(list[0]["po_number"]["page"], 1);
        assert_eq!(list[0]["line_item_0_qty"]["page"], 1);
        assert_eq!(list[1]["po_number"]["page"], 2);
        assert_eq!(list[1]["po_number"]["matched_text"], "B");
        // Per-record maps never emit the missing-column placeholder.
        assert_eq!(list[1].as_object().expect("object").len(), 1);
    }

    #[test]
    fn non_object_result_is_treated_as_empty() {
        let qb = boxes(vec![("po_number".into(), header_box(0.1, 0.1, 0.2, 0.2, 1))]);
        let out = build_field_locations_from_layout(&qb, &pages(), &json!("nonsense"), &[]);
        // The box still maps; it just has no value to preview.
        assert_eq!(out["po_number"]["matched_text"], "");
    }

    #[test]
    fn denormalize_rounds_half_to_even() {
        // 0.0005 * 1000 = 0.5 → 0 (even); 0.0015 * 1000 = 1.5 → 2 (even).
        let b = denormalize_box(&[0.0005, 0.0015, 0.0025, 0.0035], 1000, 1000);
        assert_eq!(b, json!([0, 2, 2, 4]));
    }
}
