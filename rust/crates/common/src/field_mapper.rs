//! field_mapper.rs ← field_mapper.py.
//!
//! ERP field mapping: renames a vendor's extracted (Qwen) field names to the
//! program's fixed canonical "custom" fields before the JSON is sent to a
//! client system. The merge logic (single_po_multipage / po_per_page) runs
//! upstream — this module only renames keys.

use serde_json::{json, Map, Value};

/// Fixed canonical target fields the program ("custom" system) expects.
pub const HEADER_TARGETS: &[&str] = &[
    "vendor_name",
    "vendor_address",
    "invoice_number",
    "invoice_date",
    "po_number",
    "invoice_total",
    "invoice_subtotal",
    "tax_amount",
    "freight_amount",
    "terms",
];

pub const LINE_TARGETS: &[&str] = &[
    "item",
    "line_description",
    "quantity_ordered",
    "quantity_received",
    "unit_price",
    "line_total",
    "uom",
];

fn nulls_for(targets: &[&str]) -> Map<String, Value> {
    targets
        .iter()
        .map(|t| ((*t).to_string(), Value::Null))
        .collect()
}

/// Map a single PO document to the canonical shape.
///
/// Output always carries every target key; unmapped targets are null. Source
/// fields with no mapping are dropped.
fn map_document(doc: &Value, header_map: &Value, line_map: &Value, header_targets: &[&str], line_targets: &[&str]) -> Value {
    let mut out = nulls_for(header_targets);
    let doc_obj = doc.as_object().cloned().unwrap_or_default();

    if let Some(hm) = header_map.as_object() {
        for (source_field, target_field) in hm {
            if let Some(target) = target_field.as_str() {
                if out.contains_key(target) {
                    out.insert(
                        target.to_string(),
                        doc_obj.get(source_field).cloned().unwrap_or(Value::Null),
                    );
                }
            }
        }
    }

    let mut items: Vec<Value> = Vec::new();
    let raw_items = doc_obj
        .get("line_items")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    for raw_item in raw_items {
        let Some(raw_obj) = raw_item.as_object() else {
            continue;
        };
        let mut mapped_item = nulls_for(line_targets);
        if let Some(lm) = line_map.as_object() {
            for (source_field, target_field) in lm {
                if let Some(target) = target_field.as_str() {
                    if mapped_item.contains_key(target) {
                        mapped_item.insert(
                            target.to_string(),
                            raw_obj.get(source_field).cloned().unwrap_or(Value::Null),
                        );
                    }
                }
            }
        }
        items.push(Value::Object(mapped_item));
    }
    out.insert("line_items".into(), Value::Array(items));
    Value::Object(out)
}

/// Apply a stored field mapping to a merged extraction result.
///
/// Handles both result shapes:
/// * object → single_po_multipage / single_page (returns an object)
/// * array  → po_per_page (returns an array of objects)
///
/// `schema` may carry `header_fields` / `line_fields`; otherwise the canonical
/// [`HEADER_TARGETS`] / [`LINE_TARGETS`] are used.
pub fn apply_mapping(result: &Value, mapping: &Value, schema: Option<&Value>) -> Value {
    let empty = Map::new();
    let mapping_obj = mapping.as_object().unwrap_or(&empty);
    let schema_obj = schema.and_then(Value::as_object);

    // Mirrors Python `(schema or {}).get('header_fields') or HEADER_TARGETS`:
    // an empty list is falsy and falls back to the canonical targets.
    let header_targets: Vec<&str> = schema_obj
        .and_then(|s| s.get("header_fields"))
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_else(|| HEADER_TARGETS.to_vec());
    let line_targets: Vec<&str> = schema_obj
        .and_then(|s| s.get("line_fields"))
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_else(|| LINE_TARGETS.to_vec());

    let header_map = mapping_obj.get("header_map").cloned().unwrap_or(json!({}));
    let line_map = mapping_obj.get("line_map").cloned().unwrap_or(json!({}));

    match result {
        Value::Array(docs) => Value::Array(
            docs.iter()
                .map(|d| map_document(d, &header_map, &line_map, &header_targets, &line_targets))
                .collect(),
        ),
        r @ Value::Object(_) => map_document(r, &header_map, &line_map, &header_targets, &line_targets),
        other => other.clone(),
    }
}

/// Detect field renames by position.
///
/// Same-length lists: any slot whose name changed is a rename. A length change
/// is an add/remove — positions can no longer be aligned safely, so no pairs.
pub fn detect_renames(old_fields: Option<&[String]>, new_fields: Option<&[String]>) -> Vec<(String, String)> {
    let old = old_fields.unwrap_or_default();
    let new = new_fields.unwrap_or_default();
    if old.len() != new.len() {
        return Vec::new();
    }
    old.iter()
        .zip(new.iter())
        .filter(|(o, n)| o != n)
        .map(|(o, n)| (o.clone(), n.clone()))
        .collect()
}

/// Return a copy of `field_map` with renamed source keys carried forward.
pub fn apply_renames(field_map: &Map<String, Value>, renames: &[(String, String)]) -> Map<String, Value> {
    let mut updated = field_map.clone();
    for (old_name, new_name) in renames {
        if let Some(v) = updated.remove(old_name) {
            updated.insert(new_name.clone(), v);
        }
    }
    updated
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::from_value;

    #[test]
    fn maps_dict_and_list_shapes() {
        let result = from_value(json!({
            "supplier": "ACME", "po_num": "P-9", "junk": true,
            "line_items": [
                {"qty": "2", "desc": "bolt", "extra": 1},
                "not-a-dict-is-skipped"
            ]
        }))
        .unwrap();
        let mapping = from_value(json!({
            "header_map": {"supplier": "vendor_name", "po_num": "po_number", "missing_target": "nope"},
            "line_map": {"qty": "quantity_ordered", "desc": "line_description"}
        }))
        .unwrap();

        let mapped = apply_mapping(&result, &mapping, None);
        assert_eq!(mapped["vendor_name"], "ACME");
        assert_eq!(mapped["po_number"], "P-9");
        assert_eq!(mapped["invoice_number"], Value::Null);
        assert_eq!(mapped["junk"], Value::Null);
        let items = mapped["line_items"].as_array().unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["quantity_ordered"], "2");
        assert_eq!(items[0]["line_description"], "bolt");
        assert_eq!(items[0]["uom"], Value::Null);

        let mapped_list = apply_mapping(&json!([result]), &mapping, None);
        assert_eq!(mapped_list.as_array().unwrap()[0]["po_number"], "P-9");

        // Non-dict/list results pass through untouched.
        assert_eq!(apply_mapping(&json!("x"), &mapping, None), json!("x"));
    }

    #[test]
    fn renames_by_position_only() {
        let old = vec!["a".into(), "b".into()];
        let new = vec!["a".into(), "c".into()];
        assert_eq!(
            detect_renames(Some(&old), Some(&new)),
            vec![("b".to_string(), "c".to_string())]
        );
        let longer = vec!["a".into(), "c".into(), "d".into()];
        assert!(detect_renames(Some(&old), Some(&longer)).is_empty());

        let mut fm = Map::new();
        fm.insert("b".to_string(), json!(1));
        let renamed = apply_renames(&fm, &[("b".to_string(), "c".to_string())]);
        assert!(renamed.contains_key("c"));
        assert!(!renamed.contains_key("b"));
    }
}
