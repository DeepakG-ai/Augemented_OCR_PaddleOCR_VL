//! json_repair.rs — salvage almost-valid JSON from a language model.
//!
//! Python used the `json-repair` package as the last fallback in
//! `extractor.call_llm`. There is no drop-in Rust equivalent, so this module
//! covers the failure modes that actually occur with llama-server output,
//! in the order they occur:
//!
//! 1. **Truncation** — the model hits `max_tokens` mid-object. This is by far
//!    the most common failure and produces an unterminated string and a stack
//!    of unclosed `{`/`[`.
//! 2. **Trailing commas** — `{"a": 1,}` / `[1, 2,]`.
//! 3. **A dangling key** — `{"a": 1, "b":` with nothing after the colon.
//!
//! It deliberately does *not* try to fix single-quoted strings, unquoted keys,
//! or embedded commentary. Those indicate the model ignored the JSON
//! instruction outright, and silently reinterpreting that text risks inventing
//! field values — which for an extraction pipeline is worse than failing.
//!
//! The strategy: scan once, remembering every byte offset where a complete
//! value ended, then walk those offsets newest-first, closing the open
//! containers at each and trying a real parse. The first candidate that parses
//! wins, so the result is always genuine `serde_json` output — never a
//! hand-rolled approximation of one.

use serde_json::Value;

/// How many truncation points to try before giving up. Each attempt is a full
/// parse, so this bounds the worst case on a large malformed payload; the
/// answer is virtually always found in the first one or two.
const MAX_ATTEMPTS: usize = 64;

/// A point where a complete JSON value had just ended, with the containers
/// still open at that moment.
struct Boundary {
    /// Byte offset just past the completed value.
    end: usize,
    /// Closing characters needed, outermost last.
    open: Vec<char>,
}

/// Parse `input`, repairing truncation and trailing commas if needed.
///
/// Recovery is deliberately *maximal*, matching the Python `json-repair`
/// package it replaces: a partially written trailing object is kept with the
/// fields it did manage to emit rather than discarded. Callers mark such
/// results with `_repaired` so a human reviewer knows to check them.
///
/// Returns `None` when the text cannot be salvaged into valid JSON.
pub fn repair_json(input: &str) -> Option<Value> {
    // Always try the input as-is first: a well-formed payload must never be
    // touched by the repair heuristics.
    if let Ok(value) = serde_json::from_str::<Value>(input) {
        return Some(value);
    }

    // Trailing commas can sit anywhere in the document, not just at the cut
    // point, so they are removed up front rather than by truncation.
    let cleaned = strip_trailing_commas(input);
    if let Ok(value) = serde_json::from_str::<Value>(&cleaned) {
        return Some(value);
    }

    let boundaries = scan_boundaries(&cleaned);
    for boundary in boundaries.iter().rev().take(MAX_ATTEMPTS) {
        let mut candidate = String::with_capacity(boundary.end + boundary.open.len());
        candidate.push_str(&cleaned[..boundary.end]);
        // Innermost container closes first.
        for ch in boundary.open.iter().rev() {
            candidate.push(match ch {
                '{' => '}',
                _ => ']',
            });
        }
        if let Ok(value) = serde_json::from_str::<Value>(&candidate) {
            return Some(value);
        }
    }
    None
}

/// Remove commas that are immediately followed by `}` or `]`, ignoring any
/// that appear inside string literals.
fn strip_trailing_commas(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut in_string = false;
    let mut escaped = false;
    // Offset in `out` of a comma awaiting a verdict, plus the whitespace that
    // followed it — both are dropped if a closing bracket comes next.
    let mut pending_comma: Option<usize> = None;

    for ch in input.chars() {
        if in_string {
            out.push(ch);
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            continue;
        }

        match ch {
            ',' => {
                pending_comma = Some(out.len());
                out.push(ch);
            }
            '}' | ']' => {
                if let Some(at) = pending_comma.take() {
                    out.truncate(at);
                }
                out.push(ch);
            }
            c if c.is_whitespace() => out.push(c),
            c => {
                // Real content after the comma, so the comma was legitimate.
                pending_comma = None;
                if c == '"' {
                    in_string = true;
                }
                out.push(c);
            }
        }
    }
    out
}

/// Record every offset at which a complete value ended, along with the
/// containers open at that point.
///
/// A "complete value" is a closed string, a finished number or literal, or a
/// closed object/array. Recording the open-container stack at each point is
/// what lets the repair close them correctly without re-scanning.
fn scan_boundaries(input: &str) -> Vec<Boundary> {
    let mut boundaries = Vec::new();
    let mut open: Vec<char> = Vec::new();
    let mut in_string = false;
    let mut escaped = false;
    // Set while scanning a bare token (number, true, false, null), which ends
    // at the next delimiter rather than at a character of its own.
    let mut in_bare = false;

    let mut chars = input.char_indices().peekable();
    while let Some((idx, ch)) = chars.next() {
        let next_idx = chars.peek().map_or(input.len(), |(i, _)| *i);

        if in_string {
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
                // A closed string is a complete value — but only when it is a
                // value, not an object key. A key is followed by `:`, which
                // would fail to parse, and that candidate is simply discarded.
                if !open.is_empty() {
                    boundaries.push(Boundary {
                        end: next_idx,
                        open: open.clone(),
                    });
                }
            }
            continue;
        }

        if in_bare && (ch.is_whitespace() || ch == ',' || ch == '}' || ch == ']') {
            in_bare = false;
            if !open.is_empty() {
                boundaries.push(Boundary {
                    end: idx,
                    open: open.clone(),
                });
            }
            // Fall through: this character still needs its own handling.
        }

        match ch {
            '"' => in_string = true,
            '{' | '[' => open.push(ch),
            '}' | ']' => {
                open.pop();
                if !open.is_empty() {
                    boundaries.push(Boundary {
                        end: next_idx,
                        open: open.clone(),
                    });
                }
            }
            c if !c.is_whitespace() && c != ',' && c != ':' => in_bare = true,
            _ => {}
        }
    }

    // A bare token running to end-of-input is complete only if the document
    // ended cleanly; when truncated it may be a half-written number, so the
    // preceding boundary is the safe one. Offering both costs one parse.
    if in_bare && !open.is_empty() {
        boundaries.push(Boundary {
            end: input.len(),
            open: open.clone(),
        });
    }

    boundaries
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn valid_json_passes_through_untouched() {
        let input = r#"{"a": 1, "b": [1, 2], "c": {"d": null}}"#;
        assert_eq!(repair_json(input), Some(json!({
            "a": 1, "b": [1, 2], "c": {"d": null}
        })));
    }

    #[test]
    fn closes_an_object_truncated_after_a_value() {
        let input = r#"{"po_number": "P-123", "total": 42"#;
        assert_eq!(
            repair_json(input),
            Some(json!({"po_number": "P-123", "total": 42}))
        );
    }

    #[test]
    fn drops_a_dangling_key_with_no_value() {
        let input = r#"{"po_number": "P-123", "vendor":"#;
        assert_eq!(repair_json(input), Some(json!({"po_number": "P-123"})));
    }

    #[test]
    fn closes_a_truncated_string_by_discarding_it() {
        // The half-written value cannot be trusted, so the last complete
        // field is what survives.
        let input = r#"{"a": "done", "b": "half writ"#;
        assert_eq!(repair_json(input), Some(json!({"a": "done"})));
    }

    #[test]
    fn closes_nested_containers_in_the_right_order() {
        let input = r#"{"fields": {"line_items": [{"qty": 1}, {"qty": 2}"#;
        assert_eq!(
            repair_json(input),
            Some(json!({"fields": {"line_items": [{"qty": 1}, {"qty": 2}]}}))
        );
    }

    #[test]
    fn recovers_the_realistic_max_tokens_truncation() {
        // What llama-server actually emits when it runs out of budget
        // mid-table: several good rows then a partial one.
        let input = r#"{"fields": {"po_number": "PO-9", "line_items": [
            {"sku": "A-1", "qty": 2, "price": "10.00"},
            {"sku": "A-2", "qty": 5, "price": "25.50"},
            {"sku": "A-3", "qty"#;
        let repaired = repair_json(input).expect("repairable");
        assert_eq!(repaired["fields"]["po_number"], "PO-9");
        let items = repaired["fields"]["line_items"].as_array().expect("items");
        // Recovery is maximal: the two complete rows survive intact and the
        // partial third keeps the one field it managed to emit. The dangling
        // `"qty"` key, which has no value, is dropped.
        assert_eq!(items.len(), 3);
        assert_eq!(items[1]["sku"], "A-2");
        assert_eq!(items[2], json!({"sku": "A-3"}));
    }

    #[test]
    fn strips_trailing_commas() {
        assert_eq!(repair_json(r#"{"a": 1,}"#), Some(json!({"a": 1})));
        assert_eq!(repair_json(r#"[1, 2,]"#), Some(json!([1, 2])));
        assert_eq!(
            repair_json(r#"{"a": [1,], "b": 2,}"#),
            Some(json!({"a": [1], "b": 2}))
        );
    }

    #[test]
    fn handles_escapes_and_braces_inside_strings() {
        let input = r#"{"note": "he said \"{[\" and left", "n": 1"#;
        assert_eq!(
            repair_json(input),
            Some(json!({"note": "he said \"{[\" and left", "n": 1}))
        );
    }

    #[test]
    fn preserves_unicode_without_splitting_characters() {
        // Byte-offset slicing must land on character boundaries.
        let input = r#"{"vendor": "日本語テスト", "x": 1"#;
        assert_eq!(
            repair_json(input),
            Some(json!({"vendor": "日本語テスト", "x": 1}))
        );
    }

    #[test]
    fn truncated_number_falls_back_to_the_previous_field() {
        // A trailing "1" could be the start of "1234"; both candidates are
        // tried and one of them parses.
        let repaired = repair_json(r#"{"a": 1, "b": 1"#).expect("repairable");
        assert_eq!(repaired["a"], 1);
    }

    #[test]
    fn refuses_text_that_is_not_json_at_all() {
        assert_eq!(repair_json("I could not read the document."), None);
        assert_eq!(repair_json(""), None);
        // Single quotes signal the model ignored the JSON instruction; we do
        // not guess at what it meant.
        assert_eq!(repair_json("{'a': 1}"), None);
    }

    #[test]
    fn top_level_scalars_are_left_to_the_caller() {
        // Valid, so it round-trips; the extractor rejects non-object shapes.
        assert_eq!(repair_json("42"), Some(json!(42)));
        assert_eq!(repair_json(r#""text""#), Some(json!("text")));
    }
}
