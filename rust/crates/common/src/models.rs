//! models.rs ← models.py.
//!
//! Serde DTOs for API request/response bodies plus the shared field-list
//! validation. The API conversion fills in the typed structs; validation
//! helpers below are final and shared.

use std::collections::HashSet;

pub const MAX_FIELD_COUNT: usize = 200;
pub const MAX_FIELD_NAME_LEN: usize = 128;
pub const MAX_RULE_LEN: usize = 512;

pub const RESERVED_FIELD_NAMES: &[&str] = &[
    "line_items",
    "fields",
    "boxes",
    "_page",
    "_error",
    "_raw",
];

/// Validate a list of user-supplied field names / rules.
///
/// Rejects non-lists, oversized lists, non-string items, blank or overly long
/// items, reserved names (and anything starting with `_`), duplicates
/// (case-insensitive). Returns each item stripped.
pub fn validate_field_name_list(
    values: &serde_json::Value,
    max_len: usize,
    check_reserved: bool,
    check_duplicates: bool,
) -> Result<Vec<String>, String> {
    let items = values
        .as_array()
        .ok_or_else(|| "must be a list".to_string())?;
    if items.len() > MAX_FIELD_COUNT {
        return Err(format!("too many entries (max {MAX_FIELD_COUNT})"));
    }
    let mut cleaned: Vec<String> = Vec::with_capacity(items.len());
    let mut seen_lower: HashSet<String> = HashSet::new();
    for item in items {
        let Some(s) = item.as_str() else {
            return Err("each entry must be a string".into());
        };
        let trimmed = s.trim();
        if trimmed.is_empty() {
            return Err("entries must not be blank".into());
        }
        if trimmed.chars().count() > max_len {
            return Err(format!("entry too long (max {max_len} chars)"));
        }
        let lowered = trimmed.to_lowercase();
        if check_reserved
            && (RESERVED_FIELD_NAMES.contains(&lowered.as_str()) || lowered.starts_with('_'))
        {
            return Err(format!("{trimmed:?} is a reserved field name"));
        }
        if check_duplicates && !seen_lower.insert(lowered) {
            return Err(format!("duplicate field name: {trimmed:?}"));
        }
        cleaned.push(trimmed.to_string());
    }
    Ok(cleaned)
}

/// Validate free-text extraction rules: drop blanks, cap length.
pub fn validate_rule_list(values: &serde_json::Value) -> Result<Vec<String>, String> {
    let items = values
        .as_array()
        .ok_or_else(|| "must be a list".to_string())?;
    let mut cleaned = Vec::with_capacity(items.len());
    for item in items {
        let Some(s) = item.as_str() else {
            return Err("each rule must be a string".into());
        };
        let trimmed = s.trim();
        if trimmed.is_empty() {
            continue;
        }
        if trimmed.len() > MAX_RULE_LEN {
            let preview: String = trimmed.chars().take(40).collect();
            return Err(format!("rule too long (max {MAX_RULE_LEN} chars): {preview:?}..."));
        }
        cleaned.push(trimmed.to_string());
    }
    Ok(cleaned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rejects_bad_field_lists() {
        assert!(validate_field_name_list(&json!("nope"), 128, true, true).is_err());
        assert!(validate_field_name_list(&json!([1]), 128, true, true).is_err());
        assert!(validate_field_name_list(&json!([""]), 128, true, true).is_err());
        assert!(validate_field_name_list(&json!(["line_items"]), 128, true, true).is_err());
        assert!(validate_field_name_list(&json!(["_hidden"]), 128, true, true).is_err());
        assert!(
            validate_field_name_list(&json!(["A", "a"]), 128, true, true)
                .unwrap_err()
                .contains("duplicate")
        );
        let long = "x".repeat(200);
        assert!(validate_field_name_list(&json!([long]), 128, true, true).is_err());
    }

    #[test]
    fn accepts_and_strips_good_lists() {
        let out =
            validate_field_name_list(&json!(["  po_number ", "DATE"]), 128, true, true).unwrap();
        assert_eq!(out, vec!["po_number".to_string(), "DATE".to_string()]);
        // Reserved check can be disabled for schema-driven callers.
        assert!(validate_field_name_list(&json!(["_page"]), 128, false, true).is_ok());
    }

    #[test]
    fn rules_drop_blanks_and_cap_length() {
        let out = validate_rule_list(&json!(["  ", "total > 0"])).unwrap();
        assert_eq!(out, vec!["total > 0".to_string()]);
        assert!(validate_rule_list(&json!(["x".repeat(600)])).is_err());
    }
}
