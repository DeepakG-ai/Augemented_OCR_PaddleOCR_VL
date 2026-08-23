//! page_logger.rs ← page_logger.py (append-only page usage + alert logs).
//!
//! Billing/SLA tracking: one human-readable `ts | PREFIX | key=value  …`
//! line per extraction in the file named by `PAGE_USAGE_LOG_PATH`, and
//! limit alerts in `PAGE_ALERTS_LOG_PATH` (both resolved by
//! `augocr_common::config`). Neither writer ever raises — log failures must
//! not crash the pipeline, so errors are downgraded to `tracing::warn!`.
//!
//! Port notes versus Python:
//! * Python kept persistent line-buffered handles behind a threading lock;
//!   here each call opens with `O_APPEND` via tokio (atomic appends), which
//!   preserves the append-only semantics without any shared mutable state.
//! * Records are `serde_json::Map<String, Value>`; workspace-wide
//!   `preserve_order` keeps insertion order so extra keys render exactly as
//!   Python's dict iteration did.

use serde_json::{json, Map, Value};

use augocr_common::config::Config;

/// Keys rendered first (in this order) regardless of record insertion order;
/// everything else follows in insertion order, mirroring `_format_record`.
const ORDERED_KEYS: [&str; 16] = [
    "status",
    "extraction_id",
    "filename",
    "vendor_id",
    "attempt_number",
    "total_pages",
    "billable_pages",
    "digital_pages",
    "scanned_pages",
    "qwen_extracted_pages",
    "qwen_failed_pages",
    "qwen_skipped_pages",
    "field_count",
    "empty_result",
    "duration_ms",
    "errors",
];

/// UTC timestamp in the same shape as Python's
/// `datetime.now(timezone.utc).isoformat()` (`…+00:00`, microsecond precision).
fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

/// Render one value like Python's `_fmt_value`: None → "-", lists as
/// `[a; b]`, objects as `{k: v}`, strings newline-collapsed and trimmed with
/// empty → "-". Bools keep Python's `True`/`False` spelling for log parity.
fn fmt_value(value: &Value) -> String {
    match value {
        Value::Null => "-".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            let text = s.replace('\n', " ").trim().to_string();
            if text.is_empty() {
                "-".to_string()
            } else {
                text
            }
        }
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(fmt_value).collect();
            format!("[{}]", inner.join("; "))
        }
        Value::Object(obj) => {
            let inner: Vec<String> = obj
                .iter()
                .map(|(k, v)| format!("{k}: {}", fmt_value(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// Build one `ts | prefix | k=v` line: canonical keys first, then extras in
/// insertion order. A falsy/absent `ts` becomes the current UTC time.
fn format_record(record: &Map<String, Value>, prefix: &str) -> String {
    let ts = match record.get("ts").and_then(Value::as_str) {
        Some(ts) if !ts.is_empty() => ts.to_string(),
        _ => now_iso(),
    };
    let mut parts: Vec<String> = Vec::new();
    for key in ORDERED_KEYS {
        if let Some(value) = record.get(key) {
            parts.push(format!("{key}={}", fmt_value(value)));
        }
    }
    for (key, value) in record {
        if key == "ts" || ORDERED_KEYS.contains(&key.as_str()) {
            continue;
        }
        parts.push(format!("{key}={}", fmt_value(value)));
    }
    format!("{ts} | {prefix} | {}", parts.join("  "))
}

async fn append_line(path: &std::path::Path, line: &str) -> std::io::Result<()> {
    use tokio::io::AsyncWriteExt;
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            tokio::fs::create_dir_all(parent).await?;
        }
    }
    let mut file = tokio::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .await?;
    file.write_all(line.as_bytes()).await?;
    file.flush().await
}

/// Append one human-readable line to the page usage log. Best-effort; never raises.
pub async fn append_log(record: &mut Map<String, Value>) {
    let ts = now_iso();
    record.entry("ts".to_string()).or_insert_with(|| json!(ts));
    let line = format!("{}\n", format_record(record, "PAGE_USAGE"));
    let cfg = Config::global();
    if let Err(exc) = append_line(&cfg.page_usage_log_path, &line).await {
        tracing::warn!("page_logger: failed to write usage log: {exc}");
    }
}

/// Parameters for [`log_limit_alert`] — mirrors the Python keyword arguments,
/// with the three required values taken by [`LimitAlert::new`] and the rest
/// defaulted (`alert_type`: 'warning' | 'exceeded' | 'small_overage').
pub struct LimitAlert<'a> {
    pub user_id: &'a str,
    pub email: Option<&'a str>,
    pub filename: Option<&'a str>,
    pub total_extracted_pages: i64,
    pub subscription_limit: i64,
    pub alert_type: &'a str,
    pub extraction_id: Option<i64>,
    pub grace_pages_used: i64,
}

impl<'a> LimitAlert<'a> {
    pub fn new(user_id: &'a str, total_extracted_pages: i64, subscription_limit: i64) -> Self {
        Self {
            user_id,
            email: None,
            filename: None,
            total_extracted_pages,
            subscription_limit,
            alert_type: "warning",
            extraction_id: None,
            grace_pages_used: 0,
        }
    }
}

/// Append one line to the alerts log when a user hits or nears their limit.
/// Never raises — billing alerts must not crash the pipeline.
pub async fn log_limit_alert(alert: LimitAlert<'_>) {
    let overage = (alert.total_extracted_pages - alert.subscription_limit).max(0);
    let mut record = Map::new();
    record.insert("ts".to_string(), json!(now_iso()));
    record.insert("alert_type".to_string(), json!(alert.alert_type));
    record.insert("user_id".to_string(), json!(alert.user_id));
    record.insert("email".to_string(), json!(alert.email));
    record.insert("filename".to_string(), json!(alert.filename));
    record.insert(
        "subscription_limit".to_string(),
        json!(alert.subscription_limit),
    );
    record.insert(
        "total_extracted_pages".to_string(),
        json!(alert.total_extracted_pages),
    );
    record.insert("overage".to_string(), json!(overage));
    if alert.grace_pages_used != 0 {
        record.insert(
            "grace_pages_used".to_string(),
            json!(alert.grace_pages_used),
        );
    }
    if let Some(extraction_id) = alert.extraction_id {
        record.insert("extraction_id".to_string(), json!(extraction_id));
    }
    let line = format!("{}\n", format_record(&record, "LIMIT_ALERT"));
    let cfg = Config::global();
    if let Err(exc) = append_line(&cfg.page_alerts_log_path, &line).await {
        tracing::warn!("page_logger: failed to write limit alert: {exc}");
    }
}

/// Classify an error string for SLA reporting: timeout / llm_http_error /
/// invalid_json / extraction_error.
pub fn classify_error_type(error: &str) -> &'static str {
    let e = error.to_lowercase();
    if e.contains("timeout") {
        return "timeout";
    }
    if e.contains("http") || e.contains("connect") || e.contains("network") {
        return "llm_http_error";
    }
    if e.contains("json") || e.contains("decode") || e.contains("parse") {
        return "invalid_json";
    }
    "extraction_error"
}

/// Count non-null, non-empty header fields in the extraction result.
/// `line_items` is excluded; a list result sums over its dict rows.
pub fn count_result_fields(result: &Value) -> usize {
    fn countable(v: &Value) -> bool {
        !v.is_null() && v.as_str() != Some("")
    }
    match result {
        Value::Object(obj) => obj
            .iter()
                    .filter(|(k, v)| k.as_str() != "line_items" && countable(v))
            .count(),
        Value::Array(items) => items
            .iter()
            .filter_map(Value::as_object)
            .map(|obj| {
                obj.iter()
            .filter(|(k, v)| k.as_str() != "line_items" && countable(v))
                    .count()
            })
            .sum(),
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(pairs: &[(&str, Value)]) -> Map<String, Value> {
        pairs.iter().fold(Map::new(), |mut m, (k, v)| {
            m.insert((*k).to_string(), v.clone());
            m
        })
    }

    #[test]
    fn classify_matches_python_keyword_rules_in_order() {
        assert_eq!(classify_error_type("Request timeout after 30s"), "timeout");
        // "timeout" wins even when other markers are present.
        assert_eq!(classify_error_type("HTTP request timeout"), "timeout");
        assert_eq!(classify_error_type("HTTP 502 Bad Gateway"), "llm_http_error");
        assert_eq!(classify_error_type("connection refused"), "llm_http_error");
        assert_eq!(classify_error_type("network unreachable"), "llm_http_error");
        assert_eq!(classify_error_type("failed to decode JSON"), "invalid_json");
        assert_eq!(
            classify_error_type("model returned garbage"),
            "extraction_error"
        );
    }

    #[test]
    fn count_fields_skips_line_items_null_and_empty_strings() {
        let obj = rec(&[
            ("vendor", json!("acme")),
            ("po_number", json!("")),
            ("order_date", json!(null)),
            ("qty", json!(0)),
            ("line_items", json!([{"item": "bolt"}])),
        ]);
        assert_eq!(count_result_fields(&Value::Object(obj)), 2);
        // Zero still counts (Python: 0 != "" is True).
        assert_eq!(count_result_fields(&json!({"qty": 0})), 1);
    }

    #[test]
    fn count_fields_sums_list_rows_and_handles_scalars() {
        let rows = json!([
            {"item": "bolt", "qty": 5},
            {"item": "", "qty": null, "price": 1.5}
        ]);
        assert_eq!(count_result_fields(&rows), 3);
        assert_eq!(count_result_fields(&json!("scalar")), 0);
        assert_eq!(count_result_fields(&json!(42)), 0);
        assert_eq!(count_result_fields(&json!(null)), 0);
    }

    #[test]
    fn fmt_value_renders_python_style() {
        assert_eq!(fmt_value(&json!(null)), "-");
        assert_eq!(fmt_value(&json!(true)), "True");
        assert_eq!(fmt_value(&json!(false)), "False");
        assert_eq!(fmt_value(&json!(3)), "3");
        assert_eq!(fmt_value(&json!("\n spaced \n")), "spaced");
        assert_eq!(fmt_value(&json!("   ")), "-");
        assert_eq!(fmt_value(&json!(["a", null])), "[a; -]");
        assert_eq!(fmt_value(&json!({"k": 1})), "{k: 1}");
    }

    #[test]
    fn format_record_orders_canonical_keys_then_extras_and_keeps_given_ts() {
        let record = rec(&[
            ("custom", json!("x")),
            ("filename", json!("inv.pdf")),
            ("status", json!("ok")),
            ("vendor_id", json!(null)),
            ("errors", json!([{"page": 2, "error": "bad"}])),
            ("ts", json!("2026-08-23T00:00:00.000000+00:00")),
        ]);
        let ts = record.get("ts").and_then(Value::as_str).unwrap_or_default();
        let line = format_record(&record, "PAGE_USAGE");
        assert_eq!(
            line,
            format!(
                "{ts} | PAGE_USAGE | status=ok  filename=inv.pdf  vendor_id=-  \
                 errors=[{{page: 2, error: bad}}]  custom=x"
            )
        );
    }

    #[test]
    fn format_record_without_ts_still_produces_timestamped_line() {
        let record = rec(&[("status", json!("ok"))]);
        let line = format_record(&record, "LIMIT_ALERT");
        assert!(line.starts_with("20"));
        assert!(line.contains(" | LIMIT_ALERT | status=ok"));
    }
}
