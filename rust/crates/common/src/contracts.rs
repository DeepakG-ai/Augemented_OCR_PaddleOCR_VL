//! contracts.rs ← contracts.py.
//!
//! Normalized document contracts for downstream integrations. The Python
//! version worked on arbitrary dicts; here everything is [`serde_json::Value`]
//! so both shapes (`dict` single-document and `list` po-per-page) flow through
//! unchanged.

use serde_json::{json, Map, Value};

use crate::pyjson::truthy;

fn as_object(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

fn header_fields(result: &Value) -> Value {
    let mut out = as_object(result);
    out.remove("line_items");
    Value::Object(out)
}

/// `result.get("line_items") or []` — a truthiness chain, so `null`, `[]` and
/// `""` all collapse to the empty list while any other value passes through
/// untouched.
fn line_items_or_empty(result: &Value) -> Value {
    match result.get("line_items") {
        Some(v) if truthy(v) => v.clone(),
        _ => json!([]),
    }
}

fn document_payload(result: &Value, document_index: usize) -> Value {
    json!({
        "document_index": document_index,
        "header": header_fields(result),
        "line_items": line_items_or_empty(result),
    })
}

fn multi_document_export_rows(documents: &[Value]) -> Vec<Value> {
    // One row per line item, plus a blank-marker row for documents that have
    // none. The capacity is a lower bound, not an exact count.
    let mut rows = Vec::with_capacity(documents.len());
    for document in documents {
        let doc_obj = as_object(document);
        let mut base_row = Map::new();
        base_row.insert(
            "document_index".into(),
            doc_obj.get("document_index").cloned().unwrap_or(Value::Null),
        );

        if let Some(header) = doc_obj.get("header").and_then(Value::as_object) {
            for (k, v) in header {
                base_row.insert(k.clone(), v.clone());
            }
        }

        // Python iterates whatever `line_items` holds. Only a JSON array is a
        // meaningful row sequence; any other truthy scalar becomes a single
        // opaque row instead of Python's character-wise iteration.
        let line_items = doc_obj.get("line_items").unwrap_or(&Value::Null);
        let items: &[Value] = match line_items {
            Value::Array(a) => a.as_slice(),
            v if truthy(v) => std::slice::from_ref(v),
            _ => &[],
        };

        let Some(last) = items.len().checked_sub(1) else {
            base_row.insert("line_items".into(), json!(""));
            rows.push(Value::Object(base_row));
            continue;
        };

        for (idx, item) in items.iter().enumerate() {
            // The final row can consume `base_row`; earlier ones must clone.
            let mut row = if idx == last {
                std::mem::take(&mut base_row)
            } else {
                base_row.clone()
            };
            match item.as_object() {
                Some(obj) => row.extend(obj.iter().map(|(k, v)| (k.clone(), v.clone()))),
                None => {
                    row.insert("line_items".into(), item.clone());
                }
            }
            rows.push(Value::Object(row));
        }
    }
    rows
}

/// Build the `purchase_order.v1` contract from an extraction row.
///
/// `extraction` is the DB extraction record (id + result/corrected_result and
/// friends) as a JSON value.
pub fn build_purchase_order_contract(extraction: &Value) -> Value {
    let ext = as_object(extraction);
    let corrected = ext.get("corrected_result").filter(|v| truthy(v));
    // Python: `corrected_result or result or {}` — a truthiness chain, so an
    // empty dict/list correction falls through to the machine result.
    let effective = corrected
        .or_else(|| ext.get("result").filter(|v| truthy(v)))
        .cloned()
        .unwrap_or(Value::Null);
    let canonical_source = if corrected.is_some() { "human" } else { "machine" };
    let review_meta = as_object(ext.get("correction_meta").unwrap_or(&Value::Null));

    let documents: Vec<Value>;
    let header: Value;
    let line_items: Value;

    match effective {
        Value::Array(list) => {
            documents = list
                .iter()
                .enumerate()
                .map(|(idx, r)| document_payload(r, idx + 1))
                .collect();
            header = json!({ "document_count": documents.len() });
            line_items = Value::Array(multi_document_export_rows(&documents));
        }
        _ => {
            // `isinstance(effective, dict)` — anything else becomes `{}`.
            let primary = if effective.is_object() { effective } else { json!({}) };
            documents = vec![document_payload(&primary, 1)];
            header = header_fields(&primary);
            line_items = line_items_or_empty(&primary);
        }
    }

    json!({
        "contract_version": "purchase_order.v1",
        "document_type": "purchase_order",
        "extraction_id": ext.get("id").cloned().unwrap_or(Value::Null),
        "vendor_id": ext.get("vendor_id").cloned().unwrap_or(Value::Null),
        "vendor_name": ext.get("vendor_name").cloned().unwrap_or(Value::Null),
        "filename": ext.get("filename").cloned().unwrap_or(Value::Null),
        "source": {
            "format_type": ext.get("format_type").cloned().unwrap_or(Value::Null),
            "total_pages": ext.get("total_pages").cloned().unwrap_or(Value::Null),
            "status": ext.get("status").cloned().unwrap_or(Value::Null),
        },
        "document_count": documents.len(),
        "documents": documents,
        "header": header,
        "line_items": line_items,
        "review": {
            "canonical_source": canonical_source,
            "fields_changed": review_meta.get("fields_changed").cloned().unwrap_or(json!([])),
            "reviewed_at": review_meta.get("corrected_at").cloned().unwrap_or(Value::Null),
            "reason_code": review_meta.get("reason_code").cloned().unwrap_or(Value::Null),
        },
    })
}

/// Return `result` with a leading `"vendor"` key set to the detected vendor
/// name. Non-mutating; no-op when `vendor_name` is empty/absent or when
/// `"vendor"` is already present. Arrays get per-document injection
/// (po_per_page); anything else is returned unchanged.
pub fn attach_vendor(result: &Value, vendor_name: Option<&str>) -> Value {
    let Some(name) = vendor_name.filter(|n| !n.is_empty()) else {
        return result.clone();
    };
    match result {
        Value::Object(obj) => {
            if obj.contains_key("vendor") {
                return result.clone();
            }
            // `{"vendor": name, **result}` — vendor first. serde_json is built
            // with `preserve_order`, so insertion order is the wire order.
            let mut out = Map::with_capacity(obj.len() + 1);
            out.insert("vendor".into(), json!(name));
            out.extend(obj.iter().map(|(k, v)| (k.clone(), v.clone())));
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|d| match d {
                    v @ Value::Object(_) => attach_vendor(v, Some(name)),
                    other => other.clone(),
                })
                .collect(),
        ),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contract_single_document() {
        let extraction = json!({
            "id": 7, "vendor_id": "acme", "vendor_name": "Acme", "filename": "a.pdf",
            "format_type": "single_po_multipage", "total_pages": 2, "status": "completed",
            "result": {"po_number": "P1", "line_items": [{"item": "x"}]},
            "correction_meta": null
        });
        let c = build_purchase_order_contract(&extraction);
        assert_eq!(c["contract_version"], "purchase_order.v1");
        assert_eq!(c["document_count"], 1);
        assert_eq!(c["header"]["po_number"], "P1");
        assert!(c["header"].get("line_items").is_none());
        assert_eq!(c["review"]["canonical_source"], "machine");
        assert_eq!(c["documents"][0]["document_index"], 1);
        assert_eq!(c["line_items"][0]["item"], "x");
    }

    #[test]
    fn contract_corrected_result_wins_and_multi_doc_rows() {
        let extraction = json!({
            "id": 1,
            "corrected_result": [
                {"po_number": "A", "line_items": []},
                {"po_number": "B", "line_items": [{"qty": 2}]}
            ],
            "correction_meta": {
                "fields_changed": ["po_number"], "corrected_at": "t", "reason_code": "rc"
            }
        });
        let c = build_purchase_order_contract(&extraction);
        assert_eq!(c["document_count"], 2);
        assert_eq!(c["header"]["document_count"], 2);
        // Empty line items produce one blank-marker row; populated ones expand.
        let rows = c["line_items"].as_array().expect("rows");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["line_items"], "");
        assert_eq!(rows[0]["po_number"], "A");
        assert_eq!(rows[1]["qty"], 2);
        assert_eq!(rows[1]["document_index"], 2);
        assert_eq!(c["review"]["canonical_source"], "human");
        assert_eq!(c["review"]["fields_changed"][0], "po_number");
    }

    #[test]
    fn empty_corrected_result_is_not_a_human_correction() {
        // Regression: Python's `or` treats `{}` as falsy, so an empty
        // correction must fall through to `result` and stay "machine".
        let extraction = json!({
            "id": 3,
            "corrected_result": {},
            "result": {"po_number": "FROM_MACHINE"},
        });
        let c = build_purchase_order_contract(&extraction);
        assert_eq!(c["review"]["canonical_source"], "machine");
        assert_eq!(c["header"]["po_number"], "FROM_MACHINE");
    }

    #[test]
    fn missing_result_yields_empty_header_and_rows() {
        let c = build_purchase_order_contract(&json!({"id": 4}));
        assert_eq!(c["document_count"], 1);
        assert_eq!(c["header"], json!({}));
        assert_eq!(c["line_items"], json!([]));
        assert_eq!(c["extraction_id"], 4);
        assert_eq!(c["vendor_id"], Value::Null);
    }

    #[test]
    fn scalar_line_item_rows_keep_the_value() {
        let extraction = json!({
            "id": 5,
            "result": [{"po_number": "A", "line_items": ["loose", {"qty": 1}]}],
        });
        let c = build_purchase_order_contract(&extraction);
        let rows = c["line_items"].as_array().expect("rows");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["line_items"], "loose");
        assert_eq!(rows[1]["qty"], 1);
    }

    #[test]
    fn attach_vendor_leading_key_noop_cases() {
        let doc = json!({"po_number": "X"});
        let with_vendor = attach_vendor(&doc, Some("ACME"));
        let obj = with_vendor.as_object().expect("object");
        assert_eq!(obj.keys().next().map(String::as_str), Some("vendor"));

        let already = json!({"vendor": "OLD", "po_number": "X"});
        assert_eq!(attach_vendor(&already, Some("NEW")), already);
        assert_eq!(attach_vendor(&doc, None), doc);
        assert_eq!(attach_vendor(&doc, Some("")), doc);

        let list = json!([{"a": 1}, "scalar"]);
        let out = attach_vendor(&list, Some("V"));
        assert_eq!(out[0]["vendor"], "V");
        assert_eq!(out[1], json!("scalar"));
    }
}
