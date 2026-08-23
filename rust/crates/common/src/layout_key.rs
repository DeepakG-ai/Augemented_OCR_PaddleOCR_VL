//! layout_key.rs ← layout_key.py.
//!
//! Phase-one spatial memory assumes one stable document format per
//! client/template. Despite the DB column name, the effective grouping key is
//! just `vendor_id + template_id`; field key and page number live in their own
//! columns, so the full reusable-memory identity is
//! `vendor_id + template_id + field_key + page_number`.

/// Compute a deterministic, readable grouping key for spatial memory.
///
/// `page_results` is accepted for backwards-compatible call sites but unused:
/// Qwen anchor boxes can shift slightly between otherwise identical documents.
pub fn compute_layout_key(vendor_id: &str, template_id: Option<i64>) -> String {
    // Mirrors Python `template_id or 'default'`: zero and None both map to the
    // default bucket.
    let template = match template_id {
        Some(id) if id != 0 => id.to_string(),
        _ => "default".to_string(),
    };
    format!("{}:{template}", vendor_id.trim())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_shapes() {
        assert_eq!(compute_layout_key(" v1 ", None), "v1:default");
        assert_eq!(compute_layout_key(" v1 ", Some(0)), "v1:default");
        assert_eq!(compute_layout_key("v1", Some(42)), "v1:42");
    }
}
