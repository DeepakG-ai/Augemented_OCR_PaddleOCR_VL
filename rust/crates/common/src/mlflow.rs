//! mlflow.rs ← mlflow_tracing.py.
//!
//! MLflow REST tracing client with circuit breaker and no-op fallbacks.
//! Placeholder now — the conversion implements `setup`, the `span` helper,
//! tag plumbing and `clean_chat_messages` against the MLflow HTTP API
//! (no native client dependency).

use crate::config::Config;

pub fn enabled() -> bool {
    Config::global().mlflow_enabled
}
