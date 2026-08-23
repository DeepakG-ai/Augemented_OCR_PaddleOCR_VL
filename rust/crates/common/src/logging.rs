//! logging.rs ← logging_config.py.
//!
//! Minimal tracing bootstrap now; the full port (per-extraction log files,
//! security-only filter, CloudWatch handler, stage spans / event helpers)
//! lands with the logging conversion. Binaries call [`init`] once at startup.

use crate::config::Config;

/// Install a global tracing subscriber honouring `LOG_LEVEL`
/// (and `RUST_LOG` when explicitly set).
pub fn init() {
    let cfg = Config::global();
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(&cfg.log_level));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(true)
        .init();
}
