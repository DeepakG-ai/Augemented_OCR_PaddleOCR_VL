//! Shared row-shaping utilities for the db module.
//!
//! Every read wraps Python's verbatim column list as
//! `SELECT to_jsonb(r.*) AS row FROM (<inner select>) r`, so each row arrives
//! as one `serde_json::Value` — JSONB columns decode natively, UUIDs arrive
//! pre-stringified, timestamps as ISO strings — exactly matching the dicts
//! asyncpg produced upstream.

use serde_json::Value;
use sqlx::Row;
use uuid::Uuid;

use crate::error::{AppError, AppResult};

/// Wrap an inner SELECT so each row comes back as a single jsonb column.
pub(crate) fn row_query(inner_select: &str) -> String {
    format!("SELECT to_jsonb(r.*) AS row FROM ({inner_select}) r")
}

/// Extract the wrapped row payload from a fetched record.
pub(crate) fn row_value(rec: sqlx::postgres::PgRow) -> Value {
    rec.get("row")
}

/// Parse a UUID-ish input; `None` when malformed (mirrors `_uuid_or_none`).
pub(crate) fn uuid_or_none(value: Option<&str>) -> Option<Uuid> {
    value.and_then(|v| Uuid::parse_str(v.trim()).ok())
}

/// Parse a required UUID or fail the request cleanly.
#[allow(dead_code)] // consumed by the remaining db conversions
pub(crate) fn uuid_or_bad(value: &str) -> AppResult<Uuid> {
    Uuid::parse_str(value.trim())
        .map_err(|_| AppError::BadRequest(format!("invalid UUID: {value:?}")))
}

#[allow(dead_code)] // consumed by the remaining db conversions
pub(crate) fn int_or_zero(value: &Value) -> i64 {
    match value {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(0),
        Value::String(s) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

#[allow(dead_code)] // consumed by the remaining db conversions
pub(crate) fn int_or_none(value: &Value) -> Option<i64> {
    match value {
        Value::Null => None,
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}
