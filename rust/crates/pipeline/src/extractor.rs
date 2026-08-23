//! extractor.rs ← extractor.py (prompts, orchestration, result merging).
//!
//! Two extraction modes:
//!
//! * **Auto Extract** — no configured fields, so the model returns whatever it
//!   finds.
//! * **Extract Fields** — user-defined fields injected into the user message.
//!
//! Every page gets the same prompt. The merger takes the header from page 1
//! and concatenates line items from every page.
//!
//! ## Prompt text is a compatibility surface
//!
//! The literals below are reproduced from the Python f-strings character for
//! character, including the blank lines an empty optional section leaves
//! behind. Prompt text is not cosmetic here: it is what the model was tuned
//! against, and [`compute_prompt_hash`] keys a database-backed prompt cache.
//! Reformatting these strings would silently change extraction behaviour, so
//! they are covered by tests that pin their exact shape.
//!
//! The transport half of the Python module lives in [`crate::llm`].

use std::collections::HashSet;

use augocr_common::config::Config;
use augocr_common::error::AppResult;
use augocr_common::pyjson::{dumps_pretty, dumps_sorted, truthy};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use crate::llm::{header_entries, LlmClient, LlmError, PageRequest, PipelineContext};
use crate::page::RenderedPage;

/// v5.4 = removed vendor_confirmed and restored verified gold correction diffs.
pub const PROMPT_VERSION: &str = "v5.4";

/// Keys the merger treats as metadata rather than extracted header fields.
const META_KEYS: [&str; 7] = [
    "_page",
    "_total_pages",
    "_error",
    "line_items",
    "fields",
    "boxes",
    "invoice",
];

// ── Gold examples ────────────────────────────────────────────────────────────

/// Latest human correction diffs, remapped to prompt-facing key names.
///
/// Only the *shape* of a past correction is shown to the model — the prompt
/// tells it not to copy either value — so this carries the original and
/// corrected pair purely as a signal about where the model went wrong.
pub fn gold_correction_examples(gold_examples: &[Value]) -> Map<String, Value> {
    let mut examples = Map::new();
    for ex in gold_examples {
        let Some(diff) = ex.get("correction_diff").and_then(Value::as_object) else {
            continue;
        };
        for (field_key, correction) in diff {
            let key = field_key.trim();
            let Some(correction) = correction.as_object().filter(|_| !key.is_empty()) else {
                continue;
            };
            examples.insert(
                key.to_string(),
                json!({
                    "original_value": correction.get("original").cloned().unwrap_or(Value::Null),
                    "correct_diff": correction.get("corrected").cloned().unwrap_or(Value::Null),
                }),
            );
        }
    }
    examples
}

// ── System prompt ────────────────────────────────────────────────────────────

/// Inputs to [`build_system_prompt`] and [`compute_prompt_hash`].
#[derive(Debug, Clone, Default)]
pub struct PromptSpec<'a> {
    pub header_fields: &'a [String],
    pub line_item_fields: &'a [String],
    pub instructions: Option<&'a str>,
    pub rules: &'a [String],
    pub format_type: &'a str,
    /// Human-reviewed correction diffs from past extractions.
    pub gold_examples: &'a [Value],
    /// Page 1 only: also ask for label bounding boxes.
    pub include_boxes: bool,
    /// Universal Agent mode: require a per-page tax-invoice flag.
    pub universal_invoice: bool,
}

/// Build the reusable system prompt.
pub fn build_system_prompt(spec: &PromptSpec<'_>) -> String {
    let context_section = match spec.instructions.map(str::trim) {
        Some(text) if !text.is_empty() => {
            format!("\n<document_context>\n{text}\n</document_context>")
        }
        _ => String::new(),
    };

    let rules_section = if spec.rules.is_empty() {
        String::new()
    } else {
        let numbered: Vec<String> = spec
            .rules
            .iter()
            .enumerate()
            .map(|(i, rule)| format!("  {}. {rule}", i + 1))
            .collect();
        format!(
            "\n<extraction_rules>\n{}\n</extraction_rules>",
            numbered.join("\n")
        )
    };

    let correction_examples = gold_correction_examples(spec.gold_examples);
    let gold_section = if correction_examples.is_empty() {
        String::new()
    } else {
        // `ensure_ascii=False` here: this text is read by the model, and
        // escaping vendor names into \uXXXX would only obscure them.
        let examples_json = dumps_pretty(&Value::Object(correction_examples), false);
        format!(
            "\n<correction_examples>\nHuman review has corrected these field values. \
             Do not copy either value. Instead look into the document and return the value \
             which is exactly inside.\n\n{examples_json}\n</correction_examples>"
        )
    };

    let (mut return_keys, bbox_rules) = if spec.include_boxes {
        (
            "Return two top-level keys:\n\
             - `fields`: extracted values\n\
             - `boxes`: bounding box of the LABEL text for each field"
                .to_string(),
            "\n<bbox_rules>\n\
             - For each header field, return the bounding box of the LABEL text \
             (e.g., word \"PO Number:\"), NOT the value next to it.\n\
             - For each line item column, return the bounding box of the COLUMN HEADER text \
             in the table header row.\n\
             - Each box value MUST be a plain JSON array: [x1, y1, x2, y2] — four integers \
             in a 0-1000 normalized grid relative to the full page image. Do NOT nest it in \
             a dict or use any key like \"bbox_2d\".\n\
             - If a label or column header is not visible on this page, set its box to null.\n\
             </bbox_rules>",
        )
    } else {
        (
            "Return one top-level key:\n- `fields`: extracted values".to_string(),
            "",
        )
    };

    let classification_section = if spec.universal_invoice {
        let header_hint = if spec.header_fields.is_empty() {
            "invoice number, invoice date".to_string()
        } else {
            spec.header_fields.join(", ")
        };
        return_keys = format!(
            "Also return a top-level `\"invoice\"` key (string \"True\"/\"False\" — \
             does this page contain a tax invoice?).\n{return_keys}"
        );
        format!(
            "\n<page_classification>\n\
             This PDF may MIX different document types across its pages: tax invoice, \
             purchase order (PO), e-way bill, delivery note, etc.\n\
             FIRST classify the page you are looking at, then extract:\n\
             - Return top-level `\"invoice\": \"True\"` ONLY if this page contains a TAX INVOICE \
             or invoice details (e.g., a \"Tax Invoice\"/\"Invoice\" heading, invoice number and \
             invoice date fields such as: {header_hint}, taxable value, GST/CGST/SGST amounts, \
             grand total).\n\
             - Return top-level `\"invoice\": \"False\"` for every other page type (purchase order, \
             e-way bill, delivery note, cover page, etc.).\n\
             - ALWAYS extract the visible fields regardless of the classification — \
             classification never replaces extraction.\n\
             </page_classification>"
        )
    } else {
        String::new()
    };

    format!(
        "You are a highly accurate document data extraction assistant.\n\
         This request is processed one page at a time.\n\
         \n\
         {return_keys}\n\
         {classification_section}\n\
         {context_section}\n\
         {rules_section}\n\
         {gold_section}\n\
         {bbox_rules}\n\
         <critical>\n\
         Count the number of rows in the line items table FIRST, then extract that exact \
         number of items.\n\
         </critical>\n\
         \n\
         <output_rules>\n\
         - Extract ONLY what is explicitly visible in the document image.\n\
         - Never guess or fabricate data.\n\
         - STRICTLY return ONLY valid JSON. No markdown fences, no explanation, no extra text.\n\
         - Treat each field independently. A missing field gets null; all other visible fields \
         must still be extracted. Do not return all fields as null because one field is absent.\n\
         - Use null for missing fields, never omit them.\n\
         - For line_items, you MUST always return the `fields.line_items` key. Strictly return \
         an array even if only one item visible in document.\n\
         - Return `fields.line_items: []` ONLY if no line-item rows are visible.\n\
         </output_rules>"
    )
}

// ── User message ─────────────────────────────────────────────────────────────

/// Build the per-page user message.
pub fn build_user_message(spec: &PromptSpec<'_>, page_num: i64, total_pages: i64) -> String {
    let invoice_key_section = if spec.universal_invoice {
        "\n- `\"invoice\"`: string \"True\" if this page contains a tax invoice or invoice details\n  \
         (invoice number, invoice date, taxable value, GST amounts), otherwise \"False."
    } else {
        ""
    };

    if spec.header_fields.is_empty() && spec.line_item_fields.is_empty() {
        // ── Auto Extract mode (no bbox support) ──
        let invoice_auto_section = if spec.universal_invoice {
            "\n- Top-level `\"invoice\"`: string \"True\" if this page contains a tax invoice \
             or invoice details\n  (invoice number, invoice date, taxable value, GST amounts), \
             otherwise \"False\".\n"
        } else {
            ""
        };
        return format!(
            "Extract ALL data from this invoice/purchase order document \
             (page {page_num} of {total_pages}).\n\
             \n\
             <critical>\n\
             Count the number of rows in the line items table FIRST, then extract that exact \
             number of items.\n\
             </critical>\n\
             \n\
             Return JSON with:{invoice_auto_section}\n\
             - Header fields: extract all visible header fields (po_number, order_date, vendor, \
             bill_to, ship_to, etc.)\n\
             - Line items: extract all visible line item rows with all their columns\n\
             \n\
             <accuracy>\n\
             - Before extraction: Count total rows in the table visually.\n\
             - After extraction: Verify your line_items array has that many items.\n\
             - Extract ONLY what is explicitly visible in the document image.\n\
             - Never guess or fabricate values.\n\
             </accuracy>\n\
             \n\
             STRICTLY return ONLY valid JSON. No markdown fences, no explanation, no extra text."
        );
    }

    // ── Extract Fields mode ──
    let mut fields_template = Map::new();
    for f in spec.header_fields {
        fields_template.insert(f.clone(), Value::Null);
    }
    if !spec.line_item_fields.is_empty() {
        let mut row = Map::new();
        for col in spec.line_item_fields {
            row.insert(col.clone(), Value::Null);
        }
        fields_template.insert("line_items".into(), json!([Value::Object(row)]));
    }

    let mut full_template = Map::new();
    if spec.universal_invoice {
        // `{"invoice": ..., **full_template}` — the flag leads.
        full_template.insert("invoice".into(), json!("True/False"));
    }
    full_template.insert("fields".into(), Value::Object(fields_template));
    if spec.include_boxes {
        let mut boxes = Map::new();
        for k in spec.header_fields.iter().chain(spec.line_item_fields) {
            boxes.insert(k.clone(), Value::Null);
        }
        full_template.insert("boxes".into(), Value::Object(boxes));
    }

    let header_section = if spec.header_fields.is_empty() {
        String::new()
    } else {
        let list: Vec<String> = spec.header_fields.iter().map(|f| format!("  - {f}")).collect();
        format!("\n<header_fields>\n{}\n</header_fields>", list.join("\n"))
    };
    let line_section = if spec.line_item_fields.is_empty() {
        String::new()
    } else {
        let list: Vec<String> = spec
            .line_item_fields
            .iter()
            .map(|f| format!("  - {f}"))
            .collect();
        format!(
            "\n<line_item_columns>\n{}\n</line_item_columns>",
            list.join("\n")
        )
    };

    // Python used `json.dumps(..., indent=2)`, i.e. ensure_ascii defaulting
    // to True, so a non-ASCII field name appears escaped in the template.
    let template_json = dumps_pretty(&Value::Object(full_template), true);

    format!(
        "Extract the header fields AND all visible line item rows from this purchase order page \
         (page {page_num} of {total_pages}).\n\
         \n\
         If any field is empty or not visible, return null.\n\
         {header_section}\n\
         {line_section}\n\
         \n\
         Return JSON in exactly this shape:\n\
         {template_json}\n\
         \n\
         <rules>\n\
         - Empty or missing cells → null.\n\
         - Extract every visible line item row.{invoice_key_section}\n\
         </rules>\n\
         \n\
         STRICTLY return ONLY valid JSON matching EXACTLY the structure above."
    )
}

// ── Prompt hash & cache ──────────────────────────────────────────────────────

/// Deterministic cache key for a system prompt's inputs.
///
/// The serialization must stay byte-identical to Python's
/// `json.dumps(..., sort_keys=True)`, otherwise every template row already in
/// the database misses its cache and is rebuilt — see
/// [`augocr_common::pyjson::dumps_sorted`].
pub fn compute_prompt_hash(spec: &PromptSpec<'_>) -> String {
    let mut header_fields: Vec<&String> = spec.header_fields.iter().collect();
    header_fields.sort_unstable();
    let mut line_item_fields: Vec<&String> = spec.line_item_fields.iter().collect();
    line_item_fields.sort_unstable();
    let mut rules: Vec<&String> = spec.rules.iter().collect();
    rules.sort_unstable();

    let payload = json!({
        "header_fields": header_fields,
        "line_item_fields": line_item_fields,
        "instructions": spec.instructions.unwrap_or(""),
        "rules": rules,
        "format_type": spec.format_type,
        "prompt_version": PROMPT_VERSION,
        "gold_examples": Value::Object(gold_correction_examples(spec.gold_examples)),
        "universal_invoice": spec.universal_invoice,
    });
    let mut hasher = Sha256::new();
    hasher.update(dumps_sorted(&payload).as_bytes());
    hex::encode(hasher.finalize())
}

/// Fetch or build the system prompt for a vendor: DB cache, then build.
///
/// Returns `(system_prompt, prompt_hash)`. The hash covers the verified
/// correction diffs too, so a newly reviewed correction invalidates the cached
/// prompt automatically.
pub async fn get_or_build_system_prompt(
    pool: &PgPool,
    vendor_id: &str,
    header_fields: &[String],
    line_item_fields: &[String],
    instructions: Option<&str>,
    rules: &[String],
    format_type: &str,
) -> AppResult<(String, String)> {
    let gold_examples = augocr_common::db::get_gold_examples(pool, vendor_id, None).await?;
    let spec = PromptSpec {
        header_fields,
        line_item_fields,
        instructions,
        rules,
        format_type,
        gold_examples: &gold_examples,
        include_boxes: false,
        universal_invoice: false,
    };
    let prompt_hash = compute_prompt_hash(&spec);
    let hash_prefix: String = prompt_hash.chars().take(12).collect();
    let gold_count = gold_examples.len();

    // 1. Database cache.
    if let Some(tmpl) = augocr_common::db::get_template(pool, vendor_id).await? {
        let cached_hash = tmpl.get("prompt_hash").and_then(Value::as_str);
        let cached_prompt = tmpl.get("system_prompt").and_then(Value::as_str);
        if let (Some(h), Some(p)) = (cached_hash, cached_prompt) {
            if h == prompt_hash && !p.is_empty() {
                tracing::info!(
                    "Prompt cache HIT (DB) vendor={vendor_id} hash={hash_prefix} gold={gold_count}"
                );
                return Ok((p.to_string(), prompt_hash));
            }
        }
    }

    // 2. Build fresh.
    tracing::info!(
        "Prompt cache MISS — building vendor={vendor_id} hash={hash_prefix} gold={gold_count}"
    );
    let system_prompt = build_system_prompt(&spec);
    augocr_common::db::upsert_template(
        pool,
        vendor_id,
        format_type,
        header_fields,
        line_item_fields,
        instructions,
        rules,
        &system_prompt,
        &prompt_hash,
    )
    .await?;

    Ok((system_prompt, prompt_hash))
}

// ── Result merging ───────────────────────────────────────────────────────────

/// Parse the Universal Agent `invoice` flag off a page result.
///
/// The prompt asks for the strings "True"/"False"; booleans and case variants
/// are tolerated. `None` means the model did not answer, which callers treat
/// as fail-open.
pub fn parse_invoice_flag(page_result: &Value) -> Option<bool> {
    match page_result.get("invoice") {
        Some(Value::Bool(b)) => Some(*b),
        Some(Value::String(s)) if !s.trim().is_empty() => Some(s.trim().eq_ignore_ascii_case("true")),
        _ => None,
    }
}

/// Flatten a value into its non-empty text lines.
fn value_lines(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::Null => {}
        Value::Object(obj) => obj.values().for_each(|v| value_lines(v, out)),
        Value::Array(items) => items.iter().for_each(|v| value_lines(v, out)),
        other => {
            let text = augocr_common::pyjson::py_str(other);
            let text = text.trim();
            if text.is_empty() {
                return;
            }
            out.extend(
                text.lines()
                    .map(str::trim)
                    .filter(|l| !l.is_empty())
                    .map(str::to_string),
            );
        }
    }
}

/// Collapse a nested header value into newline-joined, de-duplicated lines.
///
/// Models sometimes return an address as a dict or a list of fragments; the
/// review UI wants one string. Scalars pass through untouched.
fn combine_header_value(value: &Value) -> Value {
    if !value.is_object() && !value.is_array() {
        return value.clone();
    }
    let mut lines = Vec::new();
    value_lines(value, &mut lines);

    let mut deduped: Vec<String> = Vec::with_capacity(lines.len());
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for line in lines {
        // Python's `casefold()`; `to_lowercase` is the closest stable
        // equivalent and agrees for every script this pipeline sees.
        if seen.insert(line.to_lowercase()) {
            deduped.push(line);
        }
    }
    if deduped.is_empty() {
        Value::Null
    } else {
        json!(deduped.join("\n"))
    }
}

/// Flatten nested header values, leaving `line_items` untouched.
pub fn normalize_header_values(result: &Value) -> Value {
    match result {
        Value::Array(records) => Value::Array(records.iter().map(normalize_header_values).collect()),
        Value::Object(obj) => Value::Object(
            obj.iter()
                .map(|(k, v)| {
                    let out = if k == "line_items" {
                        v.clone()
                    } else {
                        combine_header_value(v)
                    };
                    (k.clone(), out)
                })
                .collect(),
        ),
        other => other.clone(),
    }
}

/// Collect line items from every page, tagging each with its source page so
/// the review UI can filter to the current page while showing the merged whole.
fn collect_line_items(valid_pages: &[&Value], v3: bool) -> Vec<Value> {
    let mut all_items = Vec::new();
    for pr in valid_pages {
        let source = if v3 { pr.get("fields") } else { Some(*pr) };
        let Some(Value::Array(items)) = source.map(|f| f.get("line_items").unwrap_or(&Value::Null))
        else {
            continue;
        };
        let page_num = pr.get("_page");
        for item in items {
            match item.as_object() {
                Some(obj) => {
                    let mut copy = obj.clone();
                    if let Some(pn) = page_num.filter(|v| !v.is_null()) {
                        copy.insert("_page".into(), pn.clone());
                    }
                    all_items.push(Value::Object(copy));
                }
                None => all_items.push(item.clone()),
            }
        }
    }
    all_items
}

/// Merge multi-page results: header from page 1, line items from all pages.
///
/// Handles both the v3 shape (`{fields, boxes}`) and the legacy flat shape.
/// Pages carrying `_error` are filtered out; if none survive, the caller gets
/// an `_all_pages_failed` marker rather than a plausible-looking empty result.
pub fn merge_results(
    page_results: &[Value],
    header_fields: &[String],
    _line_item_fields: &[String],
) -> Value {
    let valid_pages: Vec<&Value> = page_results
        .iter()
        .filter(|pr| pr.get("_error").is_none())
        .collect();

    let Some(first_page) = valid_pages.first() else {
        let errors: Vec<Value> = page_results
            .iter()
            .map(|pr| pr.get("_error").cloned().unwrap_or(json!("unknown")))
            .collect();
        return json!({"_all_pages_failed": true, "errors": errors});
    };

    let v3 = first_page
        .get("fields")
        .is_some_and(|f| f.is_object());
    let empty = json!({});
    let first_fields = if v3 {
        first_page.get("fields").unwrap_or(&empty)
    } else {
        first_page
    };

    let mut merged = Map::new();
    if header_fields.is_empty() {
        // No configured template: take every non-metadata key from page 1.
        if let Some(obj) = first_fields.as_object() {
            for (key, val) in obj {
                let skip = if v3 {
                    key == "line_items"
                } else {
                    META_KEYS.contains(&key.as_str())
                };
                if !skip {
                    merged.insert(key.clone(), val.clone());
                }
            }
        }
    } else {
        for f in header_fields {
            merged.insert(
                f.clone(),
                first_fields.get(f).cloned().unwrap_or(Value::Null),
            );
        }
    }

    merged.insert(
        "line_items".into(),
        Value::Array(collect_line_items(&valid_pages, v3)),
    );
    normalize_header_values(&Value::Object(merged))
}

/// The page payload with metadata keys removed — Python's
/// `{k: v for k, v in pr.items() if not k.startswith("_")}`.
fn strip_meta(page_result: &Value, drop_invoice: bool) -> Value {
    let Some(obj) = page_result.as_object() else {
        return json!({});
    };
    Value::Object(
        obj.iter()
            .filter(|(k, _)| !k.starts_with('_') && (!drop_invoice || k.as_str() != "invoice"))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
    )
}

/// `pr["fields"]` when present, else the page stripped of metadata.
fn page_payload(page_result: &Value, drop_invoice: bool) -> Value {
    match page_result.get("fields") {
        Some(f) if !f.is_null() => f.clone(),
        _ => strip_meta(page_result, drop_invoice),
    }
}

/// Assemble the final document result from per-page results.
///
/// Split out of `extract_document` so the format branching is testable
/// without a language model, a database, or a network.
pub fn build_final_result(
    page_results: &mut [Value],
    header_fields: &[String],
    line_item_fields: &[String],
    format_type: &str,
    universal_agent: bool,
) -> (Option<Value>, Vec<i64>) {
    let mut dropped_pages = Vec::new();

    let final_value = if universal_agent {
        // Keep only pages classified as tax invoices; the survivors form ONE
        // logical document regardless of format_type.
        for pr in page_results.iter_mut() {
            let Some(obj) = pr.as_object_mut() else { continue };
            if obj.contains_key("_error") {
                obj.insert("_invoice".into(), Value::Null);
                continue;
            }
            // Fail-open: a missing classification keeps the page, so a prompt
            // regression can never silently discard real invoice data.
            let flag = parse_invoice_flag(pr).unwrap_or(true);
            if let Some(obj) = pr.as_object_mut() {
                obj.insert("_invoice".into(), json!(flag));
            }
        }
        let invoice_pages: Vec<Value> = page_results
            .iter()
            .filter(|pr| pr.get("_invoice") == Some(&json!(true)))
            .cloned()
            .collect();
        dropped_pages = page_results
            .iter()
            .filter(|pr| pr.get("_invoice") == Some(&json!(false)))
            .filter_map(|pr| pr.get("_page").and_then(Value::as_i64))
            .collect();
        tracing::info!(
            "Universal agent: {} invoice page(s) kept, dropped non-invoice page(s): {}",
            invoice_pages.len(),
            if dropped_pages.is_empty() {
                "none".to_string()
            } else {
                format!("{dropped_pages:?}")
            }
        );
        if invoice_pages.is_empty() {
            return (None, dropped_pages);
        }
        Some(merge_results(&invoice_pages, header_fields, line_item_fields))
    } else if format_type == "po_per_page" {
        Some(Value::Array(
            page_results
                .iter()
                .filter(|pr| pr.get("_error").is_none())
                .map(|pr| page_payload(pr, true))
                .collect(),
        ))
    } else if format_type == "single_page" && page_results.len() == 1 {
        match page_results.first() {
            Some(pr) if pr.get("_error").is_none() => Some(page_payload(pr, false)),
            _ => None,
        }
    } else {
        // single_po_multipage — merge_results filters `_error` pages itself.
        Some(merge_results(page_results, header_fields, line_item_fields))
    };

    // `_all_pages_failed` is a marker, not a result; normalising it would
    // flatten the error list into a string.
    let final_value = final_value.map(|v| {
        if truthy(v.get("_all_pages_failed").unwrap_or(&Value::Null)) {
            v
        } else {
            normalize_header_values(&v)
        }
    });
    (final_value, dropped_pages)
}

// ── Orchestration ────────────────────────────────────────────────────────────

/// Notified as each page finishes, so callers can stream progress.
///
/// Python passed an `on_page_done` coroutine; a trait keeps the callback
/// object-safe and lets the observer hold its own state (an SSE channel, a
/// database handle) without the orchestrator knowing about it.
#[async_trait::async_trait]
pub trait PageObserver: Send + Sync {
    async fn on_page_done(&self, page_num: i64, total_pages: i64, result: &Value);
}

/// Everything [`extract_document`] needs.
pub struct ExtractRequest<'a> {
    pub pages: &'a [RenderedPage],
    /// Field configuration and the Universal Agent flag. `include_boxes` is
    /// ignored here — page 1 decides it via `include_page1_boxes`.
    pub spec: PromptSpec<'a>,
    pub system_prompt: &'a str,
    /// Page 1's prompt when it differs (it also requests bounding boxes).
    pub system_prompt_page1: Option<&'a str>,
    pub format_type: &'a str,
    pub include_page1_boxes: bool,
    /// Resume point for a retry; pages below this are not re-sent.
    pub start_from_page: i64,
    /// Page results from a previous attempt. Successful ones are reused;
    /// failed ones are dropped so they get retried.
    pub existing_page_results: Vec<Value>,
    pub pipeline_context: Option<&'a PipelineContext>,
    pub pool: Option<&'a PgPool>,
    pub cancel: Option<&'a CancellationToken>,
    pub observer: Option<&'a dyn PageObserver>,
}

/// The outcome of extracting one document.
#[derive(Debug, Default)]
pub struct ExtractOutcome {
    pub result: Option<Value>,
    pub page_results: Vec<Value>,
    /// Set when the user cancelled *or* a page failed — either way the
    /// document is incomplete and resumable.
    pub cancelled: bool,
    pub last_completed_page: i64,
    pub dropped_pages: Vec<i64>,
    /// Universal Agent found no tax-invoice page in the document.
    pub no_invoice_pages: bool,
}

/// Extract every page of a document and merge the results.
///
/// Ordering guarantees, all of which the review UI depends on:
/// * page 1 runs alone, so bbox/layout extraction stays deterministic;
/// * within a batch, results are collected in input order;
/// * pages already successful in `existing_page_results` are skipped;
/// * if any page in a batch fails, no further batch starts;
/// * `merge_results` always sees pages in page-number order.
pub async fn extract_document(req: ExtractRequest<'_>) -> ExtractOutcome {
    let total = req.pages.len() as i64;
    if total == 0 {
        return ExtractOutcome::default();
    }
    let batch_size = Config::global().llm_page_batch_size.max(1);

    // Successful pages from a previous attempt are kept; failed ones are
    // dropped here so the retry re-sends them.
    let already_done: HashSet<i64> = req
        .existing_page_results
        .iter()
        .filter(|pr| pr.get("_error").is_none())
        .filter_map(|pr| pr.get("_page").and_then(Value::as_i64))
        .collect();
    let mut page_results: Vec<Value> = req
        .existing_page_results
        .iter()
        .filter(|pr| pr.get("_error").is_none())
        .cloned()
        .collect();

    let pending: Vec<&RenderedPage> = req
        .pages
        .iter()
        .filter(|p| p.page_number >= req.start_from_page && !already_done.contains(&p.page_number))
        .collect();

    tracing::info!(
        "Parallel extraction: {} pages pending, {} already done, batch_size={batch_size}",
        pending.len(),
        already_done.len()
    );

    let mut cancelled = false;
    let mut batch_had_failure = false;

    // Phase A — page 1 alone.
    let (page_one, later): (Vec<&RenderedPage>, Vec<&RenderedPage>) =
        pending.into_iter().partition(|p| p.page_number == 1);

    if let Some(p1) = page_one.first() {
        if req.cancel.is_some_and(CancellationToken::is_cancelled) {
            cancelled = true;
            tracing::info!("Extraction cancelled before starting page 1");
        } else {
            tracing::info!("Phase A: Processing Page 1 sequentially first");
            let result = process_page(&req, p1, total).await;
            if result.get("_error").is_some() {
                batch_had_failure = true;
            }
            if let Some(obs) = req.observer {
                obs.on_page_done(1, total, &result).await;
            }
            if batch_had_failure {
                tracing::warn!("Page 1 failed — stopping extraction.");
            }
            page_results.push(result);
        }
    }

    // Phase B — pages 2..N, `batch_size` at a time.
    if !cancelled && !batch_had_failure && !later.is_empty() {
        tracing::info!(
            "Phase B: Processing remaining {} page(s) in parallel batches of {batch_size}",
            later.len()
        );
        for batch in later.chunks(batch_size) {
            if req.cancel.is_some_and(CancellationToken::is_cancelled) {
                cancelled = true;
                let first = batch.first().map_or(0, |p| p.page_number);
                tracing::info!("Extraction cancelled before batch starting page {first}");
                break;
            }

            // `join_all` fires the whole batch at once and yields results in
            // input order, so page numbering stays stable.
            let batch_results =
                futures::future::join_all(batch.iter().map(|p| process_page(&req, p, total))).await;

            let failed: Vec<i64> = batch_results
                .iter()
                .filter(|r| r.get("_error").is_some())
                .filter_map(|r| r.get("_page").and_then(Value::as_i64))
                .collect();

            for result in &batch_results {
                if let Some(obs) = req.observer {
                    let page_num = result.get("_page").and_then(Value::as_i64).unwrap_or(0);
                    obs.on_page_done(page_num, total, result).await;
                }
            }
            page_results.extend(batch_results);

            if !failed.is_empty() {
                batch_had_failure = true;
                tracing::warn!(
                    "Batch had failures — stopping extraction. Failed pages: {failed:?}"
                );
                break;
            }
        }
    }

    // Merge order must be page order regardless of completion order.
    page_results.sort_by_key(|pr| pr.get("_page").and_then(Value::as_i64).unwrap_or(0));
    let last_completed = last_completed_page(&page_results, total);

    let (result, dropped_pages) = build_final_result(
        &mut page_results,
        req.spec.header_fields,
        req.spec.line_item_fields,
        req.format_type,
        req.spec.universal_invoice,
    );
    let no_invoice_pages = req.spec.universal_invoice && result.is_none();

    ExtractOutcome {
        result,
        page_results,
        cancelled: cancelled || batch_had_failure,
        last_completed_page: last_completed,
        dropped_pages,
        no_invoice_pages,
    }
}

/// Run one page through the model. Never returns `Err`: a failure becomes a
/// `_error` page result so sibling pages and the merge still proceed.
async fn process_page(req: &ExtractRequest<'_>, page: &RenderedPage, total: i64) -> Value {
    let page_num = page.page_number;
    let is_page1 = page_num == 1;
    let effective_prompt = match req.system_prompt_page1 {
        Some(p) if is_page1 => p,
        _ => req.system_prompt,
    };

    let mut page_spec = req.spec.clone();
    page_spec.include_boxes = is_page1 && req.include_page1_boxes;
    let user_message = build_user_message(&page_spec, page_num, total);

    let outcome = LlmClient::global()
        .call_llm(
            PageRequest {
                image_b64: &page.image_b64,
                mime_type: &page.mime_type,
                system_prompt: effective_prompt,
                user_message: &user_message,
                page_num,
                total_pages: total,
            },
            req.pipeline_context,
            req.pool,
            req.cancel,
        )
        .await;

    match outcome {
        Ok(mut result) => {
            if let Some(obj) = result.as_object_mut() {
                obj.insert("_page".into(), json!(page_num));
                obj.insert("_total_pages".into(), json!(total));
            }
            log_page_summary(&result, page_num, total);
            result
        }
        Err(LlmError::Cancelled) => {
            tracing::info!("Page {page_num} LLM call cancelled by user");
            json!({"_page": page_num, "_total_pages": total, "_error": "cancelled"})
        }
        Err(e) => {
            tracing::error!("page {page_num}/{total} extraction failed | exc={e}");
            json!({"_page": page_num, "_total_pages": total, "_error": e.to_string()})
        }
    }
}

/// Log what a page yielded — line-item count and the header values.
fn log_page_summary(result: &Value, page_num: i64, total: i64) {
    let empty = json!({});
    let fields = match result.get("fields") {
        Some(f) if f.is_object() => f,
        _ => result,
    };
    let Some(fields) = fields.as_object().or_else(|| empty.as_object()) else {
        return;
    };
    let line_items = fields
        .get("line_items")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    tracing::info!("Page {page_num}/{total} done — {line_items} line item(s)");

    let header: Vec<String> = header_entries(fields)
        .into_iter()
        .map(|(k, v)| format!("{k}={}", augocr_common::pyjson::py_str(v)))
        .collect();
    tracing::info!(
        "Page {page_num}/{total} extracted: {line_items} line items | {}",
        if header.is_empty() {
            "(no fields)".to_string()
        } else {
            header.join(", ")
        }
    );
}

/// Highest page number for which that page and every page before it succeeded.
///
/// Pages `[1 ok, 2 failed, 3 ok]` yield 1, not 3 — a retry has to resume from
/// the first gap or it would leave page 2 permanently missing.
pub fn last_completed_page(page_results: &[Value], total: i64) -> i64 {
    let mut last = 0;
    for pn in 1..=total {
        let ok = page_results
            .iter()
            .find(|r| r.get("_page").and_then(Value::as_i64) == Some(pn))
            .is_some_and(|r| r.get("_error").is_none());
        if ok {
            last = pn;
        } else {
            break;
        }
    }
    last
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| (*s).to_string()).collect()
    }

    fn spec<'a>(header: &'a [String], line: &'a [String]) -> PromptSpec<'a> {
        PromptSpec {
            header_fields: header,
            line_item_fields: line,
            instructions: None,
            rules: &[],
            format_type: "single_po_multipage",
            gold_examples: &[],
            include_boxes: false,
            universal_invoice: false,
        }
    }

    #[test]
    fn gold_examples_remap_correction_diffs() {
        let gold = vec![json!({
            "correction_diff": {
                "po_number": {"original": "P1", "corrected": "P2"},
                "  spaced  ": {"original": 1, "corrected": 2},
                "bad": "not a dict",
                "": {"original": "x", "corrected": "y"},
            }
        })];
        let out = gold_correction_examples(&gold);
        assert_eq!(out["po_number"]["original_value"], "P1");
        assert_eq!(out["po_number"]["correct_diff"], "P2");
        assert!(out.contains_key("spaced"), "keys are trimmed");
        assert!(!out.contains_key("bad"), "non-dict corrections are skipped");
        assert!(!out.contains_key(""), "blank keys are skipped");
        assert_eq!(out.len(), 2);
    }

    #[test]
    fn gold_examples_ignore_rows_without_a_diff() {
        let gold = vec![json!({"id": 1}), json!({"correction_diff": null})];
        assert!(gold_correction_examples(&gold).is_empty());
    }

    #[test]
    fn system_prompt_default_shape() {
        let p = build_system_prompt(&spec(&[], &[]));
        assert!(p.starts_with("You are a highly accurate document data extraction assistant.\n"));
        assert!(p.contains("Return one top-level key:\n- `fields`: extracted values"));
        assert!(p.contains("<critical>"));
        assert!(p.contains("<output_rules>"));
        // Absent optional sections must not leak their tags.
        assert!(!p.contains("<document_context>"));
        assert!(!p.contains("<extraction_rules>"));
        assert!(!p.contains("<correction_examples>"));
        assert!(!p.contains("<bbox_rules>"));
        assert!(!p.contains("<page_classification>"));
    }

    #[test]
    fn system_prompt_with_boxes_asks_for_label_boxes() {
        let mut s = spec(&[], &[]);
        s.include_boxes = true;
        let p = build_system_prompt(&s);
        assert!(p.contains("Return two top-level keys:"));
        assert!(p.contains("- `boxes`: bounding box of the LABEL text for each field"));
        assert!(p.contains("<bbox_rules>"));
        assert!(p.contains("0-1000 normalized grid"));
    }

    #[test]
    fn system_prompt_universal_agent_adds_classification() {
        let header = strings(&["invoice_no", "invoice_date"]);
        let mut s = spec(&header, &[]);
        s.universal_invoice = true;
        let p = build_system_prompt(&s);
        assert!(p.contains("<page_classification>"));
        assert!(p.contains("invoice_no, invoice_date"), "header hint is inlined");
        assert!(p.contains("Also return a top-level `\"invoice\"` key"));

        // With no configured headers the hint falls back to a default.
        let mut s = spec(&[], &[]);
        s.universal_invoice = true;
        assert!(build_system_prompt(&s).contains("invoice number, invoice date"));
    }

    #[test]
    fn system_prompt_includes_context_rules_and_gold_sections() {
        let rules = strings(&["Always read totals from the last page", "Ignore watermarks"]);
        let gold = vec![json!({
            "correction_diff": {"po_number": {"original": "P1", "corrected": "P2"}}
        })];
        let header = strings(&["po_number"]);
        let s = PromptSpec {
            header_fields: &header,
            line_item_fields: &[],
            instructions: Some("  This vendor uses two-column layouts.  "),
            rules: &rules,
            format_type: "single_po_multipage",
            gold_examples: &gold,
            include_boxes: false,
            universal_invoice: false,
        };
        let p = build_system_prompt(&s);
        assert!(p.contains("<document_context>\nThis vendor uses two-column layouts.\n</document_context>"));
        assert!(p.contains("  1. Always read totals from the last page"));
        assert!(p.contains("  2. Ignore watermarks"));
        assert!(p.contains("<correction_examples>"));
        assert!(p.contains("\"original_value\": \"P1\""));
        assert!(p.contains("\"correct_diff\": \"P2\""));
    }

    #[test]
    fn blank_instructions_do_not_open_a_context_section() {
        let mut s = spec(&[], &[]);
        s.instructions = Some("   ");
        assert!(!build_system_prompt(&s).contains("<document_context>"));
    }

    #[test]
    fn user_message_auto_extract_mode() {
        let m = build_user_message(&spec(&[], &[]), 2, 5);
        assert!(m.starts_with("Extract ALL data from this invoice/purchase order document (page 2 of 5)."));
        assert!(m.contains("<accuracy>"));
        assert!(!m.contains("<header_fields>"));
        assert!(!m.contains("\"invoice\""));
    }

    #[test]
    fn user_message_fields_mode_embeds_the_json_template() {
        let header = strings(&["po_number", "order_date"]);
        let line = strings(&["sku", "qty"]);
        let m = build_user_message(&spec(&header, &line), 1, 3);
        assert!(m.contains("(page 1 of 3)"));
        assert!(m.contains("<header_fields>\n  - po_number\n  - order_date\n</header_fields>"));
        assert!(m.contains("<line_item_columns>\n  - sku\n  - qty\n</line_item_columns>"));
        // The embedded template must be real, parseable JSON in field order.
        let start = m.find("{").expect("template start");
        let end = m.rfind("}").expect("template end");
        let template: Value = serde_json::from_str(&m[start..=end]).expect("valid template");
        assert_eq!(template["fields"]["po_number"], Value::Null);
        assert_eq!(template["fields"]["line_items"][0]["sku"], Value::Null);
        assert!(template.get("boxes").is_none());
    }

    #[test]
    fn user_message_page_one_template_carries_boxes_for_every_field() {
        let header = strings(&["po_number"]);
        let line = strings(&["sku"]);
        let mut s = spec(&header, &line);
        s.include_boxes = true;
        let m = build_user_message(&s, 1, 1);
        let start = m.find("{").expect("start");
        let end = m.rfind("}").expect("end");
        let template: Value = serde_json::from_str(&m[start..=end]).expect("valid template");
        let boxes = template["boxes"].as_object().expect("boxes");
        assert!(boxes.contains_key("po_number"));
        assert!(boxes.contains_key("sku"), "line item columns get boxes too");
    }

    #[test]
    fn user_message_universal_agent_puts_invoice_first() {
        let header = strings(&["po_number"]);
        let mut s = spec(&header, &[]);
        s.universal_invoice = true;
        let m = build_user_message(&s, 1, 1);
        let start = m.find("{").expect("start");
        let end = m.rfind("}").expect("end");
        let raw = &m[start..=end];
        let template: Value = serde_json::from_str(raw).expect("valid template");
        assert_eq!(template["invoice"], "True/False");
        // Key order is part of the instruction to the model.
        assert!(raw.find("\"invoice\"") < raw.find("\"fields\""));
    }

    #[test]
    fn prompt_hash_is_stable_and_order_independent() {
        let a = strings(&["b", "a"]);
        let b = strings(&["a", "b"]);
        let h1 = compute_prompt_hash(&spec(&a, &[]));
        let h2 = compute_prompt_hash(&spec(&b, &[]));
        assert_eq!(h1, h2, "field order must not change the hash");
        assert_eq!(h1.len(), 64, "sha256 hex");
        assert_eq!(h1, compute_prompt_hash(&spec(&a, &[])), "deterministic");
    }

    #[test]
    fn prompt_hash_changes_with_every_meaningful_input() {
        let header = strings(&["a"]);
        let base = compute_prompt_hash(&spec(&header, &[]));

        let mut s = spec(&header, &[]);
        s.instructions = Some("new");
        assert_ne!(base, compute_prompt_hash(&s));

        let mut s = spec(&header, &[]);
        s.format_type = "po_per_page";
        assert_ne!(base, compute_prompt_hash(&s));

        let mut s = spec(&header, &[]);
        s.universal_invoice = true;
        assert_ne!(base, compute_prompt_hash(&s));

        let rules = strings(&["r"]);
        let mut s = spec(&header, &[]);
        s.rules = &rules;
        assert_ne!(base, compute_prompt_hash(&s));

        let gold = vec![json!({"correction_diff": {"a": {"original": 1, "corrected": 2}}})];
        let mut s = spec(&header, &[]);
        s.gold_examples = &gold;
        assert_ne!(base, compute_prompt_hash(&s), "a new correction rebuilds the prompt");
    }

    #[test]
    fn prompt_hash_ignores_include_boxes() {
        // Page 1 and page 2+ share one cached template row, so the boxes flag
        // must not fork the cache key.
        let header = strings(&["a"]);
        let mut with_boxes = spec(&header, &[]);
        with_boxes.include_boxes = true;
        assert_eq!(
            compute_prompt_hash(&spec(&header, &[])),
            compute_prompt_hash(&with_boxes)
        );
    }

    #[test]
    fn invoice_flag_parsing_tolerates_shapes() {
        assert_eq!(parse_invoice_flag(&json!({"invoice": true})), Some(true));
        assert_eq!(parse_invoice_flag(&json!({"invoice": "True"})), Some(true));
        assert_eq!(parse_invoice_flag(&json!({"invoice": " true "})), Some(true));
        assert_eq!(parse_invoice_flag(&json!({"invoice": "False"})), Some(false));
        assert_eq!(parse_invoice_flag(&json!({"invoice": "anything"})), Some(false));
        assert_eq!(parse_invoice_flag(&json!({"invoice": "  "})), None);
        assert_eq!(parse_invoice_flag(&json!({"invoice": null})), None);
        assert_eq!(parse_invoice_flag(&json!({})), None);
    }

    #[test]
    fn combine_header_value_flattens_and_dedupes() {
        assert_eq!(combine_header_value(&json!("plain")), json!("plain"));
        assert_eq!(combine_header_value(&json!(42)), json!(42));
        assert_eq!(combine_header_value(&json!(null)), json!(null));

        let nested = json!({"line1": "ACME Corp", "line2": "12 Road", "dup": "acme corp"});
        // Case-insensitive dedupe keeps the first spelling seen.
        assert_eq!(combine_header_value(&nested), json!("ACME Corp\n12 Road"));

        let list = json!(["a", ["b", {"c": "c"}], null, "  "]);
        assert_eq!(combine_header_value(&list), json!("a\nb\nc"));

        // Nothing usable inside → null, not an empty string.
        assert_eq!(combine_header_value(&json!({"a": null, "b": "  "})), json!(null));
        assert_eq!(combine_header_value(&json!([])), json!(null));
    }

    #[test]
    fn combine_header_value_splits_embedded_newlines() {
        let v = json!({"addr": "12 Road\n\nSuite 4\n12 Road"});
        assert_eq!(combine_header_value(&v), json!("12 Road\nSuite 4"));
    }

    #[test]
    fn normalize_header_values_leaves_line_items_alone() {
        let result = json!({
            "vendor": {"a": "ACME", "b": "Ltd"},
            "line_items": [{"sku": {"nested": "kept"}}]
        });
        let out = normalize_header_values(&result);
        assert_eq!(out["vendor"], "ACME\nLtd");
        assert_eq!(out["line_items"][0]["sku"], json!({"nested": "kept"}));
    }

    #[test]
    fn normalize_header_values_maps_over_po_per_page_lists() {
        let result = json!([{"vendor": ["A", "B"]}, {"vendor": "C"}]);
        let out = normalize_header_values(&result);
        assert_eq!(out[0]["vendor"], "A\nB");
        assert_eq!(out[1]["vendor"], "C");
    }

    #[test]
    fn merge_v3_takes_header_from_page_one_and_items_from_all() {
        let pages = vec![
            json!({"_page": 1, "fields": {"po_number": "P1", "line_items": [{"sku": "a"}]}}),
            json!({"_page": 2, "fields": {"po_number": "IGNORED", "line_items": [{"sku": "b"}]}}),
        ];
        let header = strings(&["po_number"]);
        let merged = merge_results(&pages, &header, &[]);
        assert_eq!(merged["po_number"], "P1", "header comes from page 1 only");
        let items = merged["line_items"].as_array().expect("items");
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["_page"], 1, "rows are tagged with their page");
        assert_eq!(items[1]["_page"], 2);
        assert_eq!(items[1]["sku"], "b");
    }

    #[test]
    fn merge_without_configured_fields_takes_every_key_from_page_one() {
        let pages = vec![json!({
            "_page": 1,
            "fields": {"po_number": "P1", "vendor": "ACME", "line_items": []}
        })];
        let merged = merge_results(&pages, &[], &[]);
        assert_eq!(merged["po_number"], "P1");
        assert_eq!(merged["vendor"], "ACME");
        assert_eq!(merged["line_items"], json!([]));
    }

    #[test]
    fn merge_configured_field_missing_on_page_one_becomes_null() {
        let pages = vec![json!({"_page": 1, "fields": {"po_number": "P1", "line_items": []}})];
        let header = strings(&["po_number", "order_date"]);
        let merged = merge_results(&pages, &header, &[]);
        assert_eq!(merged["order_date"], Value::Null);
    }

    #[test]
    fn merge_legacy_flat_shape_skips_metadata_keys() {
        let pages = vec![json!({
            "_page": 1, "_total_pages": 1,
            "po_number": "P1", "boxes": {"x": 1}, "invoice": "True",
            "line_items": [{"sku": "a"}]
        })];
        let merged = merge_results(&pages, &[], &[]);
        assert_eq!(merged["po_number"], "P1");
        assert!(merged.get("_page").is_none());
        assert!(merged.get("boxes").is_none());
        assert!(merged.get("invoice").is_none());
        assert_eq!(merged["line_items"][0]["sku"], "a");
    }

    #[test]
    fn merge_skips_failed_pages_and_flags_total_failure() {
        let pages = vec![
            json!({"_page": 1, "_error": "boom"}),
            json!({"_page": 2, "fields": {"po_number": "P2", "line_items": [{"sku": "b"}]}}),
        ];
        let header = strings(&["po_number"]);
        let merged = merge_results(&pages, &header, &[]);
        assert_eq!(merged["po_number"], "P2", "page 2 becomes the header source");

        let all_failed = vec![
            json!({"_page": 1, "_error": "boom"}),
            json!({"_page": 2, "_error": "bang"}),
        ];
        let merged = merge_results(&all_failed, &header, &[]);
        assert_eq!(merged["_all_pages_failed"], true);
        assert_eq!(merged["errors"], json!(["boom", "bang"]));
    }

    #[test]
    fn merge_keeps_non_object_line_items_untagged() {
        let pages = vec![json!({
            "_page": 1, "fields": {"line_items": ["loose", {"sku": "a"}]}
        })];
        let merged = merge_results(&pages, &[], &[]);
        assert_eq!(merged["line_items"][0], "loose");
        assert_eq!(merged["line_items"][1]["_page"], 1);
    }

    #[test]
    fn final_result_po_per_page_returns_one_record_per_page() {
        let mut pages = vec![
            json!({"_page": 1, "fields": {"po_number": "A"}}),
            json!({"_page": 2, "_error": "boom"}),
            json!({"_page": 3, "fields": {"po_number": "C"}}),
        ];
        let (result, dropped) = build_final_result(&mut pages, &[], &[], "po_per_page", false);
        let list = result.expect("result").as_array().expect("array").clone();
        assert_eq!(list.len(), 2, "failed pages are excluded");
        assert_eq!(list[0]["po_number"], "A");
        assert_eq!(list[1]["po_number"], "C");
        assert!(dropped.is_empty());
    }

    #[test]
    fn final_result_single_page_uses_the_only_page() {
        let mut pages = vec![json!({"_page": 1, "fields": {"po_number": "A"}})];
        let (result, _) = build_final_result(&mut pages, &[], &[], "single_page", false);
        assert_eq!(result.expect("result")["po_number"], "A");

        let mut failed = vec![json!({"_page": 1, "_error": "boom"})];
        let (result, _) = build_final_result(&mut failed, &[], &[], "single_page", false);
        assert!(result.is_none(), "a failed single page has no result");
    }

    #[test]
    fn final_result_legacy_page_without_fields_key_is_stripped_of_metadata() {
        let mut pages = vec![json!({"_page": 1, "_total_pages": 1, "po_number": "A"})];
        let (result, _) = build_final_result(&mut pages, &[], &[], "single_page", false);
        let r = result.expect("result");
        assert_eq!(r["po_number"], "A");
        assert!(r.get("_page").is_none());
    }

    #[test]
    fn universal_agent_drops_non_invoice_pages() {
        let mut pages = vec![
            json!({"_page": 1, "invoice": "True", "fields": {"po_number": "A", "line_items": [{"s": 1}]}}),
            json!({"_page": 2, "invoice": "False", "fields": {"po_number": "PO", "line_items": [{"s": 2}]}}),
            json!({"_page": 3, "invoice": "True", "fields": {"po_number": "IGN", "line_items": [{"s": 3}]}}),
        ];
        let header = strings(&["po_number"]);
        let (result, dropped) = build_final_result(&mut pages, &header, &[], "po_per_page", true);
        let r = result.expect("result");
        assert_eq!(dropped, vec![2]);
        // Invoice pages merge into ONE document regardless of format_type.
        assert_eq!(r["po_number"], "A");
        let items = r["line_items"].as_array().expect("items");
        assert_eq!(items.len(), 2, "only invoice pages contribute rows");
        assert_eq!(items[0]["s"], 1);
        assert_eq!(items[1]["s"], 3);
    }

    #[test]
    fn universal_agent_fails_open_on_a_missing_flag() {
        let mut pages = vec![
            json!({"_page": 1, "fields": {"po_number": "A", "line_items": []}}),
            json!({"_page": 2, "invoice": "False", "fields": {"line_items": []}}),
        ];
        let header = strings(&["po_number"]);
        let (result, dropped) = build_final_result(&mut pages, &header, &[], "single_po_multipage", true);
        assert_eq!(result.expect("result")["po_number"], "A");
        assert_eq!(dropped, vec![2]);
        assert_eq!(pages[0]["_invoice"], true, "unclassified pages are kept");
    }

    #[test]
    fn universal_agent_with_no_invoice_pages_yields_no_result() {
        let mut pages = vec![json!({"_page": 1, "invoice": "False", "fields": {}})];
        let (result, dropped) = build_final_result(&mut pages, &[], &[], "single_page", true);
        assert!(result.is_none());
        assert_eq!(dropped, vec![1]);
    }

    #[test]
    fn universal_agent_marks_failed_pages_unclassified() {
        let mut pages = vec![
            json!({"_page": 1, "_error": "boom"}),
            json!({"_page": 2, "invoice": "True", "fields": {"line_items": []}}),
        ];
        let (_, dropped) = build_final_result(&mut pages, &[], &[], "single_page", true);
        assert_eq!(pages[0]["_invoice"], Value::Null);
        assert!(dropped.is_empty(), "a failed page is not a dropped page");
    }

    #[test]
    fn all_pages_failed_marker_survives_normalisation() {
        let mut pages = vec![json!({"_page": 1, "_error": "boom"})];
        let (result, _) = build_final_result(&mut pages, &[], &[], "single_po_multipage", false);
        let r = result.expect("result");
        assert_eq!(r["_all_pages_failed"], true);
        // The error list must stay a list, not be flattened into a string.
        assert_eq!(r["errors"], json!(["boom"]));
    }

    #[test]
    fn last_completed_page_stops_at_the_first_gap() {
        let pages = vec![
            json!({"_page": 1}),
            json!({"_page": 2, "_error": "boom"}),
            json!({"_page": 3}),
        ];
        assert_eq!(last_completed_page(&pages, 3), 1);

        let all_ok = vec![json!({"_page": 1}), json!({"_page": 2})];
        assert_eq!(last_completed_page(&all_ok, 2), 2);

        assert_eq!(last_completed_page(&[], 3), 0);
        // A missing page is a gap even without an explicit error.
        let gap = vec![json!({"_page": 2})];
        assert_eq!(last_completed_page(&gap, 2), 0);
    }
}
