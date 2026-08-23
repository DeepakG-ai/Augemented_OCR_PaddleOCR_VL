//! pyjson.rs — Python value semantics for [`serde_json::Value`].
//!
//! The Python backend leans on truthiness (`x or default`, `if meta:`) in
//! places where the value is arbitrary JSON. Rust's `Option` only models
//! `null`, so ports that use `is_null()` silently disagree with Python for
//! `0`, `""`, `[]` and `{}`. Everything that needs Python's rule uses the
//! helpers here so the semantics live in exactly one place.

use serde_json::Value;

/// Python truthiness for a JSON value.
///
/// Falsy: `null`, `false`, `0` / `0.0`, `""`, `[]`, `{}`. Everything else is
/// truthy — matching `bool(x)` on the equivalent Python object.
#[inline]
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        // A number that cannot be read as f64 is an arbitrary-precision
        // literal, which is never zero in practice — treat it as truthy.
        Value::Number(n) => n.as_f64().is_none_or(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python truthiness for an optional value; a missing key is falsy, exactly
/// like `dict.get(k)` returning `None`.
#[inline]
pub fn truthy_opt(v: Option<&Value>) -> bool {
    v.is_some_and(truthy)
}

/// `value or fallback` over borrowed JSON: the first truthy operand.
#[inline]
pub fn or<'a>(value: Option<&'a Value>, fallback: &'a Value) -> &'a Value {
    match value {
        Some(v) if truthy(v) => v,
        _ => fallback,
    }
}

/// Python `str(value)`.
///
/// Used where a JSON value is rendered into human-facing text (a UI hint, a
/// log line), so the output has to look the way the Python code's did:
/// `True`/`False`/`None` rather than `true`/`false`/`null`, bare strings
/// rather than quoted ones, and single-quoted members inside containers.
pub fn py_str(value: &Value) -> String {
    match value {
        // `str("abc")` is `abc`, not `'abc'` — quoting only applies to
        // members nested inside a container, which go through `py_repr`.
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python `repr(value)` — as [`py_str`], but strings keep their quotes.
pub fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        // serde_json renders floats with a trailing `.0` just as Python does.
        Value::Number(n) => n.to_string(),
        Value::String(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(obj) => {
            let inner: Vec<String> = obj
                .iter()
                .map(|(k, v)| format!("'{k}': {}", py_repr(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// `json.dumps(value, sort_keys=True)` — byte-for-byte.
///
/// Used where the serialized text is itself meaningful: the extractor hashes
/// this to key the system-prompt cache, so any deviation would invalidate
/// every template row already stored by the Python implementation and force a
/// needless prompt rebuild for every vendor.
///
/// Three things differ from `serde_json::to_string`, and all three matter:
/// object keys are sorted, the separators are `", "` and `": "` rather than
/// `","` and `":"`, and `ensure_ascii=True` escapes every non-ASCII character
/// as `\uXXXX`.
pub fn dumps_sorted(value: &Value) -> String {
    let mut out = String::new();
    write_sorted(value, &mut out);
    out
}

fn write_sorted(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_py_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_sorted(item, out);
            }
            out.push(']');
        }
        Value::Object(obj) => {
            // `sort_keys=True` sorts by Unicode code point, which is what
            // Rust's `str` ordering already does.
            let mut keys: Vec<&String> = obj.keys().collect();
            keys.sort_unstable();
            out.push('{');
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py_string(key, out);
                out.push_str(": ");
                if let Some(v) = obj.get(key) {
                    write_sorted(v, out);
                }
            }
            out.push('}');
        }
    }
}

/// `json.dumps(value, indent=2)` — key order preserved, not sorted.
///
/// `ensure_ascii` mirrors the Python argument of the same name: `true`
/// escapes non-ASCII as `\uXXXX`, `false` emits it literally. Both forms
/// appear in the extractor's prompt building, and the text goes to the model
/// verbatim, so the distinction is preserved rather than normalised away.
pub fn dumps_pretty(value: &Value, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write_pretty(value, 0, ensure_ascii, &mut out);
    out
}

fn write_pretty(value: &Value, depth: usize, ensure_ascii: bool, out: &mut String) {
    const INDENT: &str = "  ";
    match value {
        Value::Array(items) if !items.is_empty() => {
            out.push_str("[\n");
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                for _ in 0..=depth {
                    out.push_str(INDENT);
                }
                write_pretty(item, depth + 1, ensure_ascii, out);
            }
            out.push('\n');
            for _ in 0..depth {
                out.push_str(INDENT);
            }
            out.push(']');
        }
        Value::Object(obj) if !obj.is_empty() => {
            out.push_str("{\n");
            for (i, (key, val)) in obj.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                for _ in 0..=depth {
                    out.push_str(INDENT);
                }
                write_string(key, ensure_ascii, out);
                out.push_str(": ");
                write_pretty(val, depth + 1, ensure_ascii, out);
            }
            out.push('\n');
            for _ in 0..depth {
                out.push_str(INDENT);
            }
            out.push('}');
        }
        // Empty containers stay on one line, as CPython writes them.
        Value::Array(_) => out.push_str("[]"),
        Value::Object(_) => out.push_str("{}"),
        Value::String(s) => write_string(s, ensure_ascii, out),
        other => write_sorted(other, out),
    }
}

/// A JSON string literal as `json.dumps` writes it with `ensure_ascii=True`.
fn write_py_string(s: &str, out: &mut String) {
    write_string(s, true, out)
}

fn write_string(s: &str, ensure_ascii: bool, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if c.is_ascii() || !ensure_ascii => out.push(c),
            // Under ensure_ascii, non-ASCII is escaped; anything outside the
            // BMP becomes a surrogate pair, exactly as CPython emits it.
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// Python's `s[:n]` on a `str`, which slices by character, not by byte.
///
/// Slicing a multi-byte string by byte offset would panic in Rust and corrupt
/// the text in any language, so every truncation of user/model text goes
/// through this.
pub fn truncate_chars(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_python_bool() {
        for falsy in [
            json!(null),
            json!(false),
            json!(0),
            json!(0.0),
            json!(""),
            json!([]),
            json!({}),
        ] {
            assert!(!truthy(&falsy), "expected falsy: {falsy}");
        }
        for t in [
            json!(true),
            json!(1),
            json!(-0.5),
            json!("0"),
            json!([null]),
            json!({"a": null}),
        ] {
            assert!(truthy(&t), "expected truthy: {t}");
        }
    }

    #[test]
    fn py_str_matches_python_rendering() {
        assert_eq!(py_str(&json!("abc")), "abc");
        assert_eq!(py_str(&json!(42)), "42");
        assert_eq!(py_str(&json!(42.5)), "42.5");
        assert_eq!(py_str(&json!(true)), "True");
        assert_eq!(py_str(&json!(false)), "False");
        assert_eq!(py_str(&json!(null)), "None");
        // Nested strings are quoted, matching `str([...])` in Python.
        assert_eq!(py_str(&json!(["a", 1, null])), "['a', 1, None]");
        assert_eq!(py_str(&json!({"k": "v"})), "{'k': 'v'}");
        assert_eq!(py_repr(&json!("abc")), "'abc'");
        assert_eq!(py_repr(&json!("it's")), "'it\\'s'");
    }

    #[test]
    fn dumps_sorted_matches_python_json_dumps() {
        // Keys sorted, `", "` and `": "` separators — not serde's compact form.
        assert_eq!(
            dumps_sorted(&json!({"b": 1, "a": 2})),
            r#"{"a": 2, "b": 1}"#
        );
        assert_eq!(dumps_sorted(&json!([1, 2, 3])), "[1, 2, 3]");
        assert_eq!(dumps_sorted(&json!({})), "{}");
        assert_eq!(dumps_sorted(&json!([])), "[]");
        assert_eq!(
            dumps_sorted(&json!({"t": true, "f": false, "n": null})),
            r#"{"f": false, "n": null, "t": true}"#
        );
        // Nested objects are sorted at every level.
        assert_eq!(
            dumps_sorted(&json!({"z": {"y": 1, "x": 2}})),
            r#"{"z": {"x": 2, "y": 1}}"#
        );
    }

    #[test]
    fn dumps_sorted_escapes_like_ensure_ascii() {
        // Non-ASCII is escaped, so the output is always pure ASCII.
        assert_eq!(dumps_sorted(&json!("café")), r#""caf\u00e9""#);
        assert_eq!(dumps_sorted(&json!("a\nb\tc")), r#""a\nb\tc""#);
        assert_eq!(dumps_sorted(&json!("quote\"back\\slash")), r#""quote\"back\\slash""#);
        assert_eq!(dumps_sorted(&json!("\x01")), r#""\u0001""#);
        // Astral-plane characters become a surrogate pair, as CPython emits.
        assert_eq!(dumps_sorted(&json!("😀")), r#""\ud83d\ude00""#);
        assert!(dumps_sorted(&json!("日本")).is_ascii());
    }

    #[test]
    fn truncate_slices_by_character_not_byte() {
        assert_eq!(truncate_chars("abcdef", 3), "abc");
        assert_eq!(truncate_chars("abc", 10), "abc");
        // A byte-based slice at 3 would split this 3-byte character.
        assert_eq!(truncate_chars("日本語テスト", 3), "日本語");
        assert_eq!(truncate_chars("", 5), "");
    }

    #[test]
    fn optional_and_or_helpers() {
        assert!(!truthy_opt(None));
        assert!(!truthy_opt(Some(&json!([]))));
        assert!(truthy_opt(Some(&json!([1]))));

        let fallback = json!({"d": 1});
        assert_eq!(or(None, &fallback), &fallback);
        assert_eq!(or(Some(&json!({})), &fallback), &fallback);
        let live = json!({"a": 1});
        assert_eq!(or(Some(&live), &fallback), &live);
    }
}
