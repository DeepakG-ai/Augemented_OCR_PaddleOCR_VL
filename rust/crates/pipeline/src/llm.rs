//! llm.rs ← the transport half of extractor.py (`call_llm`).
//!
//! Posts one page image plus its prompts to llama-server's OpenAI-compatible
//! `/chat/completions` endpoint, strips markdown fences off the reply, and
//! parses it into a page payload — falling back through progressively more
//! forgiving recovery before giving up.
//!
//! ## Cancellation
//!
//! Python raced the HTTP request against an `asyncio.Event` and cancelled the
//! loser. Rust does this natively: [`tokio::select!`] drops the request future
//! the moment the token fires, which closes the connection and lets
//! llama-server abandon the generation — the point of the exercise, since a
//! cancelled page is GPU time nobody will pay for.
//!
//! ## Usage accounting
//!
//! Tokens are billed even when the reply cannot be parsed, because the model
//! consumed them regardless. If the usage insert itself fails, the payload
//! goes to an in-process retry buffer and is flushed on the next successful
//! call — the same recovery Python had, but with the buffer behind a mutex
//! that is never held across an await.

use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use augocr_common::config::Config;
use serde::Serialize;
use serde_json::{json, Map, Value};
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use crate::json_repair::repair_json;

/// Why a page call did not produce a payload.
#[derive(Debug, thiserror::Error)]
pub enum LlmError {
    /// The caller's cancellation token fired while the request was in flight.
    #[error("Extraction cancelled by user")]
    Cancelled,

    /// Transport failure — unreachable server, timeout, connection reset.
    #[error("LLM request failed: {0}")]
    Transport(String),

    /// The server answered with a non-2xx status.
    #[error("LLM returned HTTP {status}: {body}")]
    Status { status: u16, body: String },

    /// The reply was not JSON and could not be repaired into any.
    #[error("LLM returned invalid JSON: {0}")]
    InvalidJson(String),
}

impl LlmError {
    /// True for failures worth retrying the page on.
    pub fn is_retryable(&self) -> bool {
        match self {
            LlmError::Cancelled => false,
            LlmError::Transport(_) => true,
            // 5xx and 429 are transient; a 4xx means the request itself is bad.
            LlmError::Status { status, .. } => *status >= 500 || *status == 429,
            LlmError::InvalidJson(_) => true,
        }
    }
}

/// Identifiers threaded through a job so usage rows can be attributed.
#[derive(Debug, Clone, Default)]
pub struct PipelineContext {
    pub doc_id: Option<String>,
    pub document_id: Option<String>,
    pub extraction_id: Option<i64>,
    pub vendor_id: Option<String>,
    pub request_id: Option<String>,
    pub job_id: Option<String>,
    pub billing_user_id: Option<String>,
    pub api_key_id: Option<i64>,
}

impl PipelineContext {
    /// `context.get("request_id") or context.get("job_id")`.
    fn effective_request_id(&self) -> Option<&str> {
        self.request_id
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(self.job_id.as_deref())
    }

    /// `context.get("doc_id") or context.get("document_id")`.
    fn effective_doc_id(&self) -> Option<&str> {
        self.doc_id
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(self.document_id.as_deref())
    }
}

/// One page's worth of billing data, buffered if the insert fails.
#[derive(Debug, Clone)]
struct UsagePayload {
    doc_id: Option<String>,
    document_id: Option<String>,
    extraction_id: Option<i64>,
    vendor_id: Option<String>,
    page_num: i64,
    total_pages: i64,
    model: String,
    prompt_tokens: i64,
    completion_tokens: i64,
    total_tokens: i64,
    duration_ms: f64,
    llm_url: String,
    request_id: Option<String>,
    billing_user_id: Option<String>,
    api_key_id: Option<i64>,
}

/// Usage rows whose insert failed, retried on the next successful call.
///
/// Bounded so that a database outage during a long batch cannot grow this
/// without limit; the oldest entries are dropped first because the newest
/// are the ones most likely still to matter for the current period.
static FAILED_USAGE: OnceLock<Mutex<Vec<UsagePayload>>> = OnceLock::new();
const FAILED_USAGE_CAPACITY: usize = 1000;

fn failed_usage() -> &'static Mutex<Vec<UsagePayload>> {
    FAILED_USAGE.get_or_init(|| Mutex::new(Vec::new()))
}

fn buffer_failed_usage(payload: UsagePayload) {
    // A poisoned mutex means another thread panicked mid-push; usage
    // accounting must not take the pipeline down with it.
    let Ok(mut buf) = failed_usage().lock() else {
        tracing::error!("LLM usage retry buffer is poisoned — dropping payload");
        return;
    };
    if buf.len() >= FAILED_USAGE_CAPACITY {
        buf.remove(0);
        tracing::warn!("LLM usage retry buffer full — dropped the oldest payload");
    }
    buf.push(payload);
}

/// Drain the buffer and retry each row. Anything that fails again goes back.
///
/// The lock is taken to swap the buffer out and released before any await, so
/// no database round trip is ever made while holding it.
async fn flush_failed_usage_buffer(pool: &PgPool) {
    let to_retry: Vec<UsagePayload> = {
        let Ok(mut buf) = failed_usage().lock() else {
            return;
        };
        if buf.is_empty() {
            return;
        }
        std::mem::take(&mut *buf)
    };

    tracing::info!(
        "Attempting to flush {} queued LLM usage payloads...",
        to_retry.len()
    );
    for payload in to_retry {
        if let Err(e) = insert_usage(pool, &payload).await {
            tracing::error!("Failed to flush LLM usage payload from buffer: {e}");
            buffer_failed_usage(payload);
        }
    }
}

async fn insert_usage(pool: &PgPool, p: &UsagePayload) -> augocr_common::error::AppResult<()> {
    augocr_common::db::record_llm_usage(
        pool,
        p.doc_id.as_deref(),
        p.document_id.as_deref(),
        p.extraction_id,
        p.vendor_id.as_deref(),
        Some(p.page_num),
        Some(p.total_pages),
        "extraction",
        &p.model,
        p.prompt_tokens,
        p.completion_tokens,
        p.total_tokens,
        Some(p.duration_ms),
        &p.llm_url,
        p.request_id.as_deref(),
        p.billing_user_id.as_deref(),
        p.api_key_id,
    )
    .await?;
    Ok(())
}

/// Record usage, buffering the payload if the insert fails.
async fn record_usage(pool: Option<&PgPool>, payload: UsagePayload) {
    let Some(pool) = pool else { return };
    match insert_usage(pool, &payload).await {
        Ok(()) => flush_failed_usage_buffer(pool).await,
        Err(e) => {
            tracing::error!(
                "Failed to record LLM usage for extraction page {}, queueing to recovery buffer: {e}",
                payload.page_num
            );
            buffer_failed_usage(payload);
        }
    }
}

// ── Request/response shapes ──────────────────────────────────────────────────

#[derive(Serialize)]
struct ChatRequest<'a> {
    model: &'a str,
    messages: [ChatMessage<'a>; 2],
    temperature: f64,
    top_p: f64,
    presence_penalty: f64,
    max_tokens: i64,
}

#[derive(Serialize)]
#[serde(untagged)]
enum ChatMessage<'a> {
    System {
        role: &'static str,
        content: &'a str,
    },
    User {
        role: &'static str,
        content: [UserPart<'a>; 2],
    },
}

#[derive(Serialize)]
#[serde(untagged)]
enum UserPart<'a> {
    Image {
        #[serde(rename = "type")]
        kind: &'static str,
        image_url: ImageUrl,
    },
    Text {
        #[serde(rename = "type")]
        kind: &'static str,
        text: &'a str,
    },
}

#[derive(Serialize)]
struct ImageUrl {
    /// `data:{mime};base64,{payload}` — built once to avoid a second copy of
    /// the (large) base64 string.
    url: String,
}

/// Everything one page call needs.
pub struct PageRequest<'a> {
    pub image_b64: &'a str,
    pub mime_type: &'a str,
    pub system_prompt: &'a str,
    pub user_message: &'a str,
    pub page_num: i64,
    pub total_pages: i64,
}

/// Client for llama-server's chat-completions endpoint.
#[derive(Clone)]
pub struct LlmClient {
    http: reqwest::Client,
    url: String,
    model: String,
    temperature: f64,
    top_p: f64,
    presence_penalty: f64,
    max_tokens: i64,
}

impl LlmClient {
    pub fn from_config(cfg: &Config) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(cfg.llm_timeout)
                .build()
                .unwrap_or_default(),
            url: cfg.llm_url.clone(),
            model: cfg.llm_model.clone(),
            temperature: cfg.llm_temperature,
            top_p: cfg.llm_top_p,
            presence_penalty: cfg.llm_presence_penalty,
            max_tokens: cfg.llm_max_tokens_fields,
        }
    }

    /// Process-wide client; `reqwest::Client` pools connections internally so
    /// every page reuses the same keep-alive socket to llama-server.
    pub fn global() -> &'static Self {
        static CLIENT: OnceLock<LlmClient> = OnceLock::new();
        CLIENT.get_or_init(|| Self::from_config(Config::global()))
    }

    pub fn url(&self) -> &str {
        &self.url
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    /// POST one page, then parse the reply into a page payload.
    ///
    /// Returns the parsed object on success. A structurally valid reply that
    /// carries no choices comes back as a `_error: "empty_choices"` payload
    /// rather than an `Err`, because that is a page-level outcome the
    /// orchestrator records and moves past.
    pub async fn call_llm(
        &self,
        req: PageRequest<'_>,
        ctx: Option<&PipelineContext>,
        pool: Option<&PgPool>,
        cancel: Option<&CancellationToken>,
    ) -> Result<Value, LlmError> {
        let PageRequest {
            image_b64,
            mime_type,
            system_prompt,
            user_message,
            page_num,
            total_pages,
        } = req;

        let payload = ChatRequest {
            model: &self.model,
            messages: [
                ChatMessage::System {
                    role: "system",
                    content: system_prompt,
                },
                ChatMessage::User {
                    role: "user",
                    content: [
                        UserPart::Image {
                            kind: "image_url",
                            image_url: ImageUrl {
                                url: format!("data:{mime_type};base64,{image_b64}"),
                            },
                        },
                        UserPart::Text {
                            kind: "text",
                            text: user_message,
                        },
                    ],
                },
            ],
            temperature: self.temperature,
            top_p: self.top_p,
            presence_penalty: self.presence_penalty,
            max_tokens: self.max_tokens,
        };

        let started = Instant::now();
        let request = self.http.post(&self.url).json(&payload).send();

        // Dropping `request` on cancellation tears down the connection, which
        // is what actually frees the GPU.
        let response = match cancel {
            Some(token) => tokio::select! {
                biased;
                () = token.cancelled() => return Err(LlmError::Cancelled),
                res = request => res,
            },
            None => request.await,
        }
        .map_err(|e| LlmError::Transport(e.to_string()))?;

        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            let body: String = body.chars().take(200).collect();
            tracing::error!(
                "page {page_num}/{total_pages} call failed | post={} status={} body={body}",
                self.url,
                status.as_u16()
            );
            return Err(LlmError::Status {
                status: status.as_u16(),
                body,
            });
        }

        let resp_json: Value = response
            .json()
            .await
            .map_err(|e| LlmError::Transport(format!("malformed LLM response body: {e}")))?;
        let duration_ms = started.elapsed().as_secs_f64() * 1000.0;

        let usage = resp_json.get("usage").cloned().unwrap_or(Value::Null);
        let prompt_tokens = usage_int(usage.get("prompt_tokens"));
        let completion_tokens = usage_int(usage.get("completion_tokens"));
        let total_tokens = match usage_int(usage.get("total_tokens")) {
            0 => prompt_tokens + completion_tokens,
            n => n,
        };

        tracing::info!(
            "page {page_num}/{total_pages} ok | post={} tok_in={prompt_tokens} \
             tok_out={completion_tokens} ms={duration_ms:.0}",
            self.url
        );

        let usage_payload = || UsagePayload {
            doc_id: ctx.and_then(|c| c.effective_doc_id()).map(str::to_string),
            document_id: ctx.and_then(|c| c.document_id.clone()),
            extraction_id: ctx.and_then(|c| c.extraction_id),
            vendor_id: ctx.and_then(|c| c.vendor_id.clone()),
            page_num,
            total_pages,
            model: self.model.clone(),
            prompt_tokens,
            completion_tokens,
            total_tokens,
            duration_ms,
            llm_url: self.url.clone(),
            request_id: ctx
                .and_then(|c| c.effective_request_id())
                .map(str::to_string),
            billing_user_id: ctx.and_then(|c| c.billing_user_id.clone()),
            api_key_id: ctx.and_then(|c| c.api_key_id),
        };

        // A reply with no usable choice is a page-level error, and Python
        // deliberately did not bill it — there was no completion to bill for.
        let Some(content) = choice_content(&resp_json) else {
            let raw: String = resp_json.to_string().chars().take(200).collect();
            tracing::error!(
                "page {page_num}/{total_pages} malformed response (no choices[0].message) | raw={raw}"
            );
            return Ok(json!({
                "_error": "empty_choices",
                "_page": page_num,
                "_raw": raw,
            }));
        };

        let raw = strip_json_fences(content.trim());

        // Attempt 1: the reply as sent.
        if let Ok(parsed) = serde_json::from_str::<Value>(&raw) {
            record_usage(pool, usage_payload()).await;
            return Ok(coerce_page_json(parsed, page_num, &raw));
        }

        // Attempt 2: quote leading-zero numbers (`0070` → `"0070"`), which
        // are not legal JSON but are extremely common in part and PO numbers.
        let fixed = quote_leading_zero_numbers(&raw);
        if let Ok(parsed) = serde_json::from_str::<Value>(&fixed) {
            tracing::warn!("LLM JSON recovered via leading-zero fix (page {page_num})");
            record_usage(pool, usage_payload()).await;
            return Ok(coerce_page_json(parsed, page_num, &fixed));
        }

        // Attempt 3: structural repair (truncation, trailing commas).
        if let Some(Value::Object(mut repaired)) = repair_json(&raw) {
            tracing::warn!("LLM JSON recovered via json_repair (page {page_num})");
            repaired.insert("_repaired".into(), json!(true));
            record_usage(pool, usage_payload()).await;
            return Ok(strip_newlines(Value::Object(repaired)));
        }

        // Out of options — still bill, the tokens were spent either way.
        record_usage(pool, usage_payload()).await;
        let preview: String = raw.chars().take(200).collect();
        tracing::error!(
            "page {page_num}/{total_pages} JSON parse failed (all fallbacks exhausted) | raw={preview}"
        );
        Err(LlmError::InvalidJson(preview))
    }
}

// ── Response shaping ─────────────────────────────────────────────────────────

/// `int(value or 0)`, tolerating strings and nulls.
fn usage_int(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
        Some(Value::Bool(true)) => 1,
        _ => 0,
    }
}

/// `choices[0].message.content`, or `None` when the shape is unusable.
fn choice_content(resp: &Value) -> Option<&str> {
    let first = resp.get("choices")?.as_array()?.first()?;
    // Python required the `message` key to exist but tolerated a missing
    // `content`, which then defaulted to "".
    let message = first.as_object()?.get("message")?;
    Some(message.get("content").and_then(Value::as_str).unwrap_or(""))
}

/// Remove ```json fences the model wraps its answer in.
fn strip_json_fences(raw: &str) -> String {
    let mut out = raw;
    if let Some(rest) = out.strip_prefix("```json") {
        out = rest;
    } else if let Some(rest) = out.strip_prefix("```") {
        out = rest;
    }
    out = out.trim_start_matches(['\r', '\n']);
    if let Some(rest) = out.trim_end().strip_suffix("```") {
        out = rest;
    }
    out.trim().to_string()
}

/// Quote numbers with a leading zero so they survive JSON parsing.
///
/// Mirrors the Python regex `([\[:,]\s*)(-?0[0-9]+)(\s*[\]},])`. Written as a
/// scanner rather than a regex so it can skip string contents — the regex
/// could corrupt a value like `"code: 007"`, this cannot.
fn quote_leading_zero_numbers(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = String::with_capacity(raw.len() + 16);
    let mut in_string = false;
    let mut escaped = false;
    let mut i = 0;

    while i < bytes.len() {
        let ch = bytes[i];
        if in_string {
            out.push(ch as char);
            if escaped {
                escaped = false;
            } else if ch == b'\\' {
                escaped = true;
            } else if ch == b'"' {
                in_string = false;
            }
            i += 1;
            continue;
        }
        if ch == b'"' {
            in_string = true;
            out.push('"');
            i += 1;
            continue;
        }

        // A number can only start right after `[`, `:` or `,` (plus space).
        if matches!(ch, b'[' | b':' | b',') {
            out.push(ch as char);
            let mut j = i + 1;
            while j < bytes.len() && (bytes[j] == b' ' || bytes[j] == b'\t') {
                out.push(bytes[j] as char);
                j += 1;
            }
            if let Some((token, after)) = leading_zero_token(bytes, j) {
                // Only rewrite when a delimiter really follows, so a partial
                // token at end-of-input is left alone.
                let mut k = after;
                while k < bytes.len() && (bytes[k] == b' ' || bytes[k] == b'\t') {
                    k += 1;
                }
                if k < bytes.len() && matches!(bytes[k], b']' | b'}' | b',') {
                    out.push('"');
                    out.push_str(token);
                    out.push('"');
                    i = after;
                    continue;
                }
            }
            i = j;
            continue;
        }

        out.push(ch as char);
        i += 1;
    }
    out
}

/// Match `-?0[0-9]+` at `start`, returning the token and the offset past it.
fn leading_zero_token(bytes: &[u8], start: usize) -> Option<(&str, usize)> {
    let mut i = start;
    if i < bytes.len() && bytes[i] == b'-' {
        i += 1;
    }
    if i >= bytes.len() || bytes[i] != b'0' {
        return None;
    }
    i += 1;
    let digits_start = i;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return None; // plain `0`, which is valid JSON already
    }
    std::str::from_utf8(&bytes[start..i]).ok().map(|s| (s, i))
}

/// Recursively replace embedded newlines in string values with a space.
pub fn strip_newlines(value: Value) -> Value {
    match value {
        Value::String(s) => Value::String(s.replace(['\n', '\r'], " ").trim().to_string()),
        Value::Array(items) => Value::Array(items.into_iter().map(strip_newlines).collect()),
        Value::Object(obj) => Value::Object(
            obj.into_iter()
                .map(|(k, v)| (k, strip_newlines(v)))
                .collect(),
        ),
        other => other,
    }
}

/// A page payload, or a page-level error when the top-level JSON is not an
/// object (a bare list or string means the model ignored the shape entirely).
fn coerce_page_json(parsed: Value, page_num: i64, raw: &str) -> Value {
    if parsed.is_object() {
        return strip_newlines(parsed);
    }
    let json_type = match &parsed {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_i64() || n.is_u64() => "int",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    };
    let preview: String = raw.chars().take(200).collect();
    tracing::error!(
        "page {page_num} malformed response (top-level JSON is not an object) | \
         json_type={json_type} raw={preview}"
    );
    json!({
        "_error": "invalid_json_shape",
        "_page": page_num,
        "_raw": preview,
    })
}

/// Header fields of a page payload, excluding metadata and containers.
pub(crate) fn header_entries(fields: &Map<String, Value>) -> Vec<(&String, &Value)> {
    fields
        .iter()
        .filter(|(k, _)| {
            k.as_str() != "line_items" && k.as_str() != "boxes" && !k.starts_with('_')
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fences_are_stripped_in_both_forms() {
        assert_eq!(strip_json_fences("```json\n{\"a\": 1}\n```"), "{\"a\": 1}");
        assert_eq!(strip_json_fences("```\n{\"a\": 1}```"), "{\"a\": 1}");
        assert_eq!(strip_json_fences("{\"a\": 1}"), "{\"a\": 1}");
        assert_eq!(strip_json_fences("  {\"a\": 1}  "), "{\"a\": 1}");
    }

    #[test]
    fn usage_int_tolerates_the_shapes_llama_server_sends() {
        assert_eq!(usage_int(Some(&json!(42))), 42);
        assert_eq!(usage_int(Some(&json!("42"))), 42);
        assert_eq!(usage_int(Some(&json!(42.9))), 42);
        assert_eq!(usage_int(Some(&json!(null))), 0);
        assert_eq!(usage_int(None), 0);
        assert_eq!(usage_int(Some(&json!("junk"))), 0);
    }

    #[test]
    fn choice_content_reads_the_openai_shape() {
        let resp = json!({"choices": [{"message": {"content": "hi"}}]});
        assert_eq!(choice_content(&resp), Some("hi"));
        // Present message, absent content → empty string, not a failure.
        let resp = json!({"choices": [{"message": {}}]});
        assert_eq!(choice_content(&resp), Some(""));
        // No choices, or a choice without a message → unusable.
        assert_eq!(choice_content(&json!({"choices": []})), None);
        assert_eq!(choice_content(&json!({"choices": [{}]})), None);
        assert_eq!(choice_content(&json!({})), None);
    }

    #[test]
    fn leading_zero_numbers_are_quoted() {
        assert_eq!(
            quote_leading_zero_numbers(r#"{"po": 0070, "n": 5}"#),
            r#"{"po": "0070", "n": 5}"#
        );
        assert_eq!(
            quote_leading_zero_numbers(r#"{"a": [0070, 0081]}"#),
            r#"{"a": ["0070", "0081"]}"#
        );
        assert_eq!(
            quote_leading_zero_numbers(r#"{"a": -0070}"#),
            r#"{"a": "-0070"}"#
        );
    }

    #[test]
    fn leading_zero_fix_leaves_valid_numbers_and_strings_alone() {
        // A plain zero and ordinary numbers are already valid.
        let untouched = r#"{"a": 0, "b": 10, "c": 0.5}"#;
        assert_eq!(quote_leading_zero_numbers(untouched), untouched);
        // Digits inside a string must never be rewritten.
        let in_string = r#"{"note": "code: 0070, ok"}"#;
        assert_eq!(quote_leading_zero_numbers(in_string), in_string);
    }

    #[test]
    fn leading_zero_fix_makes_the_payload_parseable() {
        let raw = r#"{"fields": {"po_number": 0070, "line_items": [{"sku": 0081}]}}"#;
        let fixed = quote_leading_zero_numbers(raw);
        let parsed: Value = serde_json::from_str(&fixed).expect("parses after fix");
        assert_eq!(parsed["fields"]["po_number"], "0070");
        assert_eq!(parsed["fields"]["line_items"][0]["sku"], "0081");
    }

    #[test]
    fn strip_newlines_flattens_nested_strings() {
        let v = json!({
            "a": "line1\nline2",
            "b": ["x\r\ny", 5],
            "c": {"d": "  padded  "}
        });
        assert_eq!(
            strip_newlines(v),
            json!({"a": "line1 line2", "b": ["x  y", 5], "c": {"d": "padded"}})
        );
    }

    #[test]
    fn coerce_rejects_non_object_top_level() {
        let out = coerce_page_json(json!([1, 2]), 3, "[1, 2]");
        assert_eq!(out["_error"], "invalid_json_shape");
        assert_eq!(out["_page"], 3);
        assert_eq!(out["_raw"], "[1, 2]");

        let ok = coerce_page_json(json!({"a": "x\ny"}), 1, "");
        assert_eq!(ok["a"], "x y");
    }

    #[test]
    fn retryable_classification() {
        assert!(!LlmError::Cancelled.is_retryable());
        assert!(LlmError::Transport("timeout".into()).is_retryable());
        assert!(LlmError::InvalidJson("junk".into()).is_retryable());
        assert!(LlmError::Status {
            status: 503,
            body: String::new()
        }
        .is_retryable());
        assert!(LlmError::Status {
            status: 429,
            body: String::new()
        }
        .is_retryable());
        assert!(!LlmError::Status {
            status: 400,
            body: String::new()
        }
        .is_retryable());
    }

    #[test]
    fn pipeline_context_falls_back_across_id_aliases() {
        let ctx = PipelineContext {
            job_id: Some("job-1".into()),
            document_id: Some("doc-1".into()),
            ..Default::default()
        };
        assert_eq!(ctx.effective_request_id(), Some("job-1"));
        assert_eq!(ctx.effective_doc_id(), Some("doc-1"));

        let ctx = PipelineContext {
            request_id: Some("req-1".into()),
            job_id: Some("job-1".into()),
            doc_id: Some("d".into()),
            document_id: Some("doc-1".into()),
            ..Default::default()
        };
        assert_eq!(ctx.effective_request_id(), Some("req-1"));
        assert_eq!(ctx.effective_doc_id(), Some("d"));

        // An empty string is falsy in Python's `or` chain.
        let ctx = PipelineContext {
            request_id: Some(String::new()),
            job_id: Some("job-1".into()),
            ..Default::default()
        };
        assert_eq!(ctx.effective_request_id(), Some("job-1"));
    }

    #[test]
    fn failed_usage_buffer_is_bounded() {
        let make = |page_num: i64| UsagePayload {
            doc_id: None,
            document_id: None,
            extraction_id: None,
            vendor_id: None,
            page_num,
            total_pages: 1,
            model: "m".into(),
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0,
            duration_ms: 0.0,
            llm_url: "u".into(),
            request_id: None,
            billing_user_id: None,
            api_key_id: None,
        };
        // Start from a known state; other tests do not touch the buffer.
        if let Ok(mut buf) = failed_usage().lock() {
            buf.clear();
        }
        for i in 0..(FAILED_USAGE_CAPACITY as i64 + 10) {
            buffer_failed_usage(make(i));
        }
        let buf = failed_usage().lock().expect("lock");
        assert_eq!(buf.len(), FAILED_USAGE_CAPACITY);
        // The oldest were dropped, so the newest page survived.
        assert_eq!(buf[buf.len() - 1].page_num, FAILED_USAGE_CAPACITY as i64 + 9);
    }

    #[test]
    fn request_payload_matches_the_openai_chat_shape() {
        let payload = ChatRequest {
            model: "qwen3.5",
            messages: [
                ChatMessage::System {
                    role: "system",
                    content: "sys",
                },
                ChatMessage::User {
                    role: "user",
                    content: [
                        UserPart::Image {
                            kind: "image_url",
                            image_url: ImageUrl {
                                url: "data:image/jpeg;base64,AAA".into(),
                            },
                        },
                        UserPart::Text {
                            kind: "text",
                            text: "usr",
                        },
                    ],
                },
            ],
            temperature: 0.1,
            top_p: 0.9,
            presence_penalty: 0.0,
            max_tokens: 4096,
        };
        let v = serde_json::to_value(&payload).expect("serialize");
        assert_eq!(v["model"], "qwen3.5");
        assert_eq!(v["messages"][0]["role"], "system");
        assert_eq!(v["messages"][0]["content"], "sys");
        assert_eq!(v["messages"][1]["role"], "user");
        assert_eq!(v["messages"][1]["content"][0]["type"], "image_url");
        assert_eq!(
            v["messages"][1]["content"][0]["image_url"]["url"],
            "data:image/jpeg;base64,AAA"
        );
        assert_eq!(v["messages"][1]["content"][1]["type"], "text");
        assert_eq!(v["messages"][1]["content"][1]["text"], "usr");
        assert_eq!(v["max_tokens"], 4096);
    }
}
