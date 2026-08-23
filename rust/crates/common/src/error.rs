//! error.rs — unified application error type.
//!
//! Replaces the mix of `HTTPException`, `StorageUnavailableError`,
//! `ObjectNotFoundError` and bare `Exception`s in the Python codebase with one
//! type that converts into an HTTP response exactly once, at the handler
//! boundary. Pipeline workers convert the same variants into job errors.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("{0}")]
    BadRequest(String),

    #[error("Not authenticated")]
    Unauthorized(String),

    #[error("Access denied")]
    Forbidden(String),

    #[error("{0}")]
    NotFound(String),

    #[error("{0}")]
    Conflict(String),

    #[error("payload too large")]
    PayloadTooLarge,

    #[error("rate limit exceeded")]
    TooManyRequests(String),

    #[error("{0}")]
    ServiceUnavailable(String),

    /// Server-side misconfiguration whose message is safe (and meant) to show,
    /// e.g. "SECRET_KEY is not configured on the server".
    #[error("{0}")]
    Misconfigured(String),

    #[error("{0}")]
    Internal(String),

    #[error("database error: {0}")]
    Database(#[from] sqlx::Error),

    #[error("object store unavailable: {0}")]
    StorageUnavailable(String),

    #[error("object not found: {0}")]
    ObjectNotFound(String),

    #[error("object store denied access: {0}")]
    StoragePermission(String),

    /// A structured error body, promoted into the envelope as-is.
    ///
    /// Python raised `HTTPException(status, detail={...})` in a dozen places
    /// and the handler promoted that dict to the top-level `error` object, so
    /// clients branch on fields like `code`, `reason` and `hint`. Anything
    /// missing `code`/`message` gets the same defaults Python applied.
    #[error("{1}")]
    Structured(StatusCode, Box<serde_json::Value>),
}

/// Snake-case error code for an HTTP status (Python's `_HTTP_CODE_NAMES`).
fn code_for_status(status: StatusCode) -> &'static str {
    match status.as_u16() {
        400 => "BAD_REQUEST",
        401 => "UNAUTHORIZED",
        402 => "PAYMENT_REQUIRED",
        403 => "FORBIDDEN",
        404 => "NOT_FOUND",
        409 => "CONFLICT",
        410 => "GONE",
        413 => "FILE_TOO_LARGE",
        422 => "VALIDATION_ERROR",
        429 => "RATE_LIMITED",
        500 => "INTERNAL_ERROR",
        503 => "SERVICE_UNAVAILABLE",
        504 => "TIMEOUT",
        _ => "ERROR",
    }
}

impl AppError {
    pub fn status(&self) -> StatusCode {
        match self {
            AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AppError::Unauthorized(_) => StatusCode::UNAUTHORIZED,
            AppError::Forbidden(_) => StatusCode::FORBIDDEN,
            AppError::NotFound(_) | AppError::ObjectNotFound(_) => StatusCode::NOT_FOUND,
            AppError::Conflict(_) => StatusCode::CONFLICT,
            AppError::PayloadTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::TooManyRequests(_) => StatusCode::TOO_MANY_REQUESTS,
            AppError::ServiceUnavailable(_) => StatusCode::SERVICE_UNAVAILABLE,
            AppError::Misconfigured(_) => StatusCode::INTERNAL_SERVER_ERROR,
            AppError::Internal(_)
            | AppError::Database(_)
            | AppError::StorageUnavailable(_)
            | AppError::StoragePermission(_) => StatusCode::INTERNAL_SERVER_ERROR,
            AppError::Structured(status, _) => *status,
        }
    }

    /// Build a structured error with an explicit code, mirroring
    /// `HTTPException(status, detail={"code": ..., "message": ...})`.
    pub fn structured(status: StatusCode, code: &str, message: impl Into<String>) -> Self {
        AppError::Structured(
            status,
            Box::new(json!({ "code": code, "message": message.into() })),
        )
    }

    /// Build a structured error from an arbitrary detail object.
    pub fn detail_object(status: StatusCode, detail: serde_json::Value) -> Self {
        AppError::Structured(status, Box::new(detail))
    }

    /// Client-safe detail string. Mirrors the Python rule: internals are never
    /// leaked verbatim to clients; known-safe messages are passed through.
    pub fn detail(&self) -> String {
        match self {
            AppError::BadRequest(m)
            | AppError::Unauthorized(m)
            | AppError::Forbidden(m)
            | AppError::NotFound(m)
            | AppError::Conflict(m)
            | AppError::ServiceUnavailable(m)
            | AppError::TooManyRequests(m) => m.clone(),
            AppError::Misconfigured(m) => {
                tracing::error!("misconfiguration: {m}");
                m.clone()
            }
            AppError::PayloadTooLarge => "Uploaded file is too large".to_string(),
            AppError::ObjectNotFound(m) => format!("Object not found: {m}"),
            AppError::Internal(m) => {
                tracing::error!("internal error: {m}");
                "Internal server error".to_string()
            }
            AppError::Database(e) => {
                tracing::error!("database error: {e}");
                "Internal server error".to_string()
            }
            AppError::StorageUnavailable(m) | AppError::StoragePermission(m) => {
                tracing::error!("storage error: {m}");
                "Object store failure".to_string()
            }
            // The structured body is rendered by `error_body`, not here.
            AppError::Structured(status, _) => code_for_status(*status).to_string(),
        }
    }

    /// The `{ "error": { "code", "message", ... } }` envelope every failure
    /// returns.
    ///
    /// This shape is a client contract, not a convention: `frontend/core.js`
    /// reads `payload.error` in preference to anything else, and
    /// `frontend/review.js` branches on `error.code`, so a bare `detail`
    /// string would silently break the review UI.
    pub fn error_body(&self) -> serde_json::Value {
        let status = self.status();
        let default_code = code_for_status(status);

        let mut error = match self {
            // A structured detail is promoted as-is, with Python's defaults
            // filled in for whatever it omits.
            AppError::Structured(_, detail) => detail
                .as_object()
                .cloned()
                .unwrap_or_else(|| json!({ "message": detail.to_string() }).as_object().cloned().unwrap_or_default()),
            other => {
                let mut map = serde_json::Map::new();
                map.insert("message".into(), json!(other.detail()));
                map
            }
        };
        error
            .entry("code")
            .or_insert_with(|| json!(default_code));
        if !error.contains_key("message") {
            // Python fell back to `hint`, then `reason`, then the code name.
            let fallback = error
                .get("hint")
                .or_else(|| error.get("reason"))
                .map(crate::pyjson::py_str)
                .unwrap_or_else(|| default_code.to_string());
            error.insert("message".into(), json!(fallback));
        }
        json!({ "error": serde_json::Value::Object(error) })
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = self.status();
        let body = self.error_body();
        let mut resp = (status, axum::Json(body)).into_response();
        if status == StatusCode::UNAUTHORIZED {
            if let Ok(v) = axum::http::HeaderValue::from_str("Bearer") {
                resp.headers_mut().insert("WWW-Authenticate", v);
            }
        }
        resp
    }
}

impl From<anyhow::Error> for AppError {
    fn from(e: anyhow::Error) -> Self {
        AppError::Internal(e.to_string())
    }
}

impl From<std::io::Error> for AppError {
    fn from(e: std::io::Error) -> Self {
        AppError::Internal(e.to_string())
    }
}

impl From<serde_json::Error> for AppError {
    fn from(e: serde_json::Error) -> Self {
        AppError::BadRequest(format!("invalid JSON payload: {e}"))
    }
}

pub type AppResult<T> = Result<T, AppError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_shape_matches_the_python_handler() {
        let body = AppError::NotFound("Vendor not found".into()).error_body();
        assert_eq!(body["error"]["code"], "NOT_FOUND");
        assert_eq!(body["error"]["message"], "Vendor not found");
        // The FastAPI default `{"detail": ...}` must not appear: the frontend
        // reads `payload.error` first and branches on `error.code`.
        assert!(body.get("detail").is_none());
    }

    #[test]
    fn every_status_maps_to_its_python_code_name() {
        for (status, code) in [
            (400, "BAD_REQUEST"),
            (401, "UNAUTHORIZED"),
            (403, "FORBIDDEN"),
            (404, "NOT_FOUND"),
            (409, "CONFLICT"),
            (413, "FILE_TOO_LARGE"),
            (429, "RATE_LIMITED"),
            (500, "INTERNAL_ERROR"),
            (503, "SERVICE_UNAVAILABLE"),
        ] {
            let s = StatusCode::from_u16(status).expect("valid status");
            assert_eq!(code_for_status(s), code, "status {status}");
        }
        assert_eq!(code_for_status(StatusCode::from_u16(418).expect("teapot")), "ERROR");
    }

    #[test]
    fn internal_errors_never_leak_their_message() {
        let body = AppError::Internal("connection string is postgres://u:p@h".into()).error_body();
        assert_eq!(body["error"]["message"], "Internal server error");
        assert_eq!(body["error"]["code"], "INTERNAL_ERROR");
    }

    #[test]
    fn structured_detail_is_promoted_verbatim() {
        let err = AppError::detail_object(
            StatusCode::BAD_REQUEST,
            serde_json::json!({
                "reason": "no_template",
                "vendor_id": "acme",
                "hint": "Create a template with at least one field.",
            }),
        );
        let body = err.error_body();
        assert_eq!(body["error"]["reason"], "no_template");
        assert_eq!(body["error"]["vendor_id"], "acme");
        // Code defaults from the status; message falls back to `hint`.
        assert_eq!(body["error"]["code"], "BAD_REQUEST");
        assert_eq!(
            body["error"]["message"],
            "Create a template with at least one field."
        );
    }

    #[test]
    fn structured_detail_keeps_an_explicit_code_and_message() {
        let err = AppError::structured(
            StatusCode::SERVICE_UNAVAILABLE,
            "REVIEW_UNAVAILABLE_OCR_FAILED",
            "OCR failed for this PDF.",
        );
        let body = err.error_body();
        assert_eq!(body["error"]["code"], "REVIEW_UNAVAILABLE_OCR_FAILED");
        assert_eq!(body["error"]["message"], "OCR failed for this PDF.");
        assert_eq!(err.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[test]
    fn message_falls_back_to_reason_then_the_code_name() {
        let err = AppError::detail_object(
            StatusCode::CONFLICT,
            serde_json::json!({"reason": "already_running"}),
        );
        assert_eq!(err.error_body()["error"]["message"], "already_running");

        let bare = AppError::detail_object(StatusCode::GONE, serde_json::json!({"x": 1}));
        assert_eq!(bare.error_body()["error"]["message"], "GONE");
    }
}
