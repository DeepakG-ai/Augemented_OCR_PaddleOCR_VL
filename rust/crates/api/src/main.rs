//! augocr-api — axum HTTP server (port of backend/main.py).
//!
//! The API conversion replaces this stub with the real router split into
//! route modules per resource (auth, vendors, templates, extractions,
//! jobs, admin) plus static frontend serving.

use augocr_common::config::Config;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    augocr_common::logging::init();
    let _cfg = Config::global();
    let app = axum::Router::new()
        .route("/api/health", axum::routing::get(health));
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!("augocr-api listening on :8000");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn health() -> &'static str {
    let _ = Config::global();
    "ok"
}
