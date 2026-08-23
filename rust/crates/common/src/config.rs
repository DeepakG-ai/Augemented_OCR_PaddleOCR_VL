//! config.rs ← config.py.
//!
//! Single source of truth for environment-driven configuration. The Python
//! module evaluated constants at import time; here [`Config::from_env`] builds
//! an immutable [`Config`] once at startup and callers hold an `Arc<Config>`
//! (API state / worker context). Validation errors abort startup loudly,
//! exactly like the Python helpers raised `ValueError`.
//!
//! Two deliberate exceptions from Python are kept:
//! * `auth::secret_key()` re-reads `SECRET_KEY` on every call so a rotated
//!   secret or test override takes effect without a restart.
//! * logging setup reads its own env in [`crate::logging`].

use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::Duration;

use url::Url;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

type Result<T> = std::result::Result<T, ConfigError>;

fn env_int(name: &str, default: i64) -> Result<i64> {
    match std::env::var(name) {
        Err(_) => Ok(default),
        Ok(raw) => raw.trim().parse::<i64>().map_err(|_| {
            ConfigError(format!(
                "Environment variable {name:?} must be an integer, got: {raw:?}"
            ))
        }),
    }
}

fn env_int_min(name: &str, default: i64, minimum: i64) -> Result<i64> {
    let value = env_int(name, default)?;
    if value < minimum {
        return Err(ConfigError(format!(
            "Environment variable {name:?} must be >= {minimum}, got: {value}"
        )));
    }
    Ok(value)
}

fn env_int_range(name: &str, default: i64, lo: i64, hi: i64) -> Result<i64> {
    let value = env_int(name, default)?;
    if !(lo..=hi).contains(&value) {
        return Err(ConfigError(format!(
            "Environment variable {name:?} must be between {lo} and {hi}, got: {value}"
        )));
    }
    Ok(value)
}

fn env_f64(name: &str, default: f64) -> Result<f64> {
    match std::env::var(name) {
        Err(_) => Ok(default),
        Ok(raw) => raw.trim().parse::<f64>().map_err(|_| {
            ConfigError(format!(
                "Environment variable {name:?} must be a number, got: {raw:?}"
            ))
        }),
    }
}

fn env_f64_range(name: &str, default: f64, lo: f64, hi: f64) -> Result<f64> {
    let value = env_f64(name, default)?;
    if !(lo..=hi).contains(&value) {
        return Err(ConfigError(format!(
            "Environment variable {name:?} must be between {lo} and {hi}, got: {value}"
        )));
    }
    Ok(value)
}

fn env_bool(name: &str, default: bool) -> Result<bool> {
    match std::env::var(name) {
        Err(_) => Ok(default),
        Ok(raw) => match raw.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => Ok(true),
            "0" | "false" | "no" | "off" => Ok(false),
            other => Err(ConfigError(format!(
                "Environment variable {name:?} must be a boolean, got: {other:?}"
            ))),
        },
    }
}

fn env_string(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn split_csv(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(|o| o.trim().trim_end_matches('/').to_string())
        .filter(|o| !o.is_empty())
        .collect()
}

/// Block SSRF targets: refuse non-http(s) schemes and known metadata endpoints.
fn validate_llm_url(raw: &str) -> Result<String> {
    let parsed = Url::parse(raw)
        .map_err(|_| ConfigError(format!("LLM_URL is not a valid URL: {raw:?}")))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(ConfigError(format!(
            "LLM_URL must use http or https, got: {raw:?}"
        )));
    }
    let host = parsed.host_str().unwrap_or_default();
    if matches!(host, "169.254.169.254" | "metadata.google.internal") {
        return Err(ConfigError(format!(
            "LLM_URL host is a blocked metadata endpoint: {host:?}"
        )));
    }
    if let Ok(addr) = host.parse::<std::net::IpAddr>() {
        let link_local = match addr {
            std::net::IpAddr::V4(v4) => v4.is_link_local(),
            std::net::IpAddr::V6(v6) => v6.is_unicast_link_local(),
        };
        if link_local {
            return Err(ConfigError(format!(
                "LLM_URL must not use a link-local address: {host:?}"
            )));
        }
    }
    Ok(raw.to_string())
}

const DEFAULT_CORS_ALLOW_ORIGINS: &[&str] = &[
    "http://localhost:3000",
    "http://localhost:3002",
    "http://localhost:8000",
    "http://localhost:8055",
    "http://localhost:8057",
    "http://localhost:8058",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3002",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8055",
    "http://127.0.0.1:8057",
    "http://127.0.0.1:8058",
];

#[derive(Debug, Clone)]
pub struct Config {
    // Database
    pub database_url: String,
    pub db_pool_min_api: u32,
    pub db_pool_max_api: u32,
    pub db_pool_min_worker: u32,
    pub db_pool_max_worker: u32,

    // Cache (Redis, optional)
    pub redis_url: String,
    pub cache_enabled: bool,
    pub cache_prefix: String,
    pub cache_ttl_auth: i64,
    pub cache_ttl_vendor: i64,
    pub cache_ttl_template: i64,
    pub cache_ttl_mapping: i64,
    pub cache_ttl_alias: i64,
    pub cache_ttl_dashboard: i64,
    pub cache_ttl_apikey_touch: i64,

    // LLM (Qwen3-VL via llama-server)
    pub llm_url: String,
    pub llm_model: String,
    pub llm_temperature: f64,
    pub llm_top_p: f64,
    pub llm_presence_penalty: f64,
    pub llm_max_tokens_fields: i64,
    pub llm_timeout: Duration,
    pub llm_page_batch_size: usize,

    // Worker
    pub worker_poll_seconds: f64,

    // HTTP / API
    pub rate_limit_per_minute: u32,
    pub max_upload_mb: i64,
    pub max_upload_bytes: usize,
    pub max_document_pages: usize,
    pub cors_allow_origins: Vec<String>,
    pub frame_ancestors: Vec<String>,

    // PDF / image processing
    pub max_long_side_px: u32,
    pub max_pixels: u32,
    pub jpeg_quality: u8,
    pub dpi_floor: u32,
    pub dpi_default: u32,
    pub pdf_workers: usize,
    pub ocr_workers: usize,
    pub ocr_device: String,

    /// Base URL of the OCR sidecar service (PaddleOCR has no native Rust
    /// engine; it keeps running as its own process and we speak HTTP to it).
    pub ocr_service_url: String,

    /// Base URL of the PDF sidecar service (pypdfium2-equivalent capabilities:
    /// digital-text/word extraction and page rasterization).
    pub pdf_service_url: String,

    // Object store (MinIO + local fallback)
    pub minio_endpoint: String,
    pub minio_access_key: String,
    pub minio_secret_key: String,
    pub minio_secure: bool,
    pub minio_region: String,
    pub minio_documents_bucket: String,
    pub minio_artifacts_bucket: String,
    pub local_object_store_dir: PathBuf,

    // MLflow observability
    pub mlflow_enabled: bool,
    pub mlflow_tracking_uri: String,

    // Logging
    pub log_level: String,
    pub pipeline_log_dir: PathBuf,
    pub page_usage_log_path: PathBuf,
    pub page_alerts_log_path: PathBuf,

    // Subscription / page limits
    pub default_subscription_limit: i64,
    pub subscription_warning_threshold: f64,

    // Debug toggles
    pub debug_dump_bbox: bool,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let _ = dotenvy::dotenv();

        let llm_timeout_secs = env_f64_range("LLM_TIMEOUT", 300.0, 1.0, 3600.0)?;
        let max_upload_mb = env_int_min("MAX_UPLOAD_MB", 50, 1)?;
        let jpeg_quality = env_int_range("JPEG_QUALITY", 92, 1, 100)? as u8;

        let project_root = std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."));
        let page_usage_default = project_root.join("logs/page_usage/log.txt");
        let page_alerts_default = project_root.join("logs/page_usage/alerts.log");

        Ok(Self {
            database_url: env_string(
                "DATABASE_URL",
                "postgresql://augocr:augocr@localhost:5432/augocr",
            ),

            db_pool_min_api: env_int_min("DB_POOL_MIN_API", 2, 0)?.max(0) as u32,
            db_pool_max_api: env_int_min("DB_POOL_MAX_API", 15, 1)? as u32,
            db_pool_min_worker: env_int_min("DB_POOL_MIN_WORKER", 1, 0)?.max(0) as u32,
            db_pool_max_worker: env_int_min("DB_POOL_MAX_WORKER", 3, 1)? as u32,

            redis_url: env_string("REDIS_URL", "redis://localhost:6379/0"),
            cache_enabled: env_bool("CACHE_ENABLED", true)?,
            cache_prefix: env_string("CACHE_PREFIX", "augocr"),
            cache_ttl_auth: env_int_min("CACHE_TTL_AUTH", 60, 1)?,
            cache_ttl_vendor: env_int_min("CACHE_TTL_VENDOR", 600, 1)?,
            cache_ttl_template: env_int_min("CACHE_TTL_TEMPLATE", 600, 1)?,
            cache_ttl_mapping: env_int_min("CACHE_TTL_MAPPING", 600, 1)?,
            cache_ttl_alias: env_int_min("CACHE_TTL_ALIAS", 300, 1)?,
            cache_ttl_dashboard: env_int_min("CACHE_TTL_DASHBOARD", 30, 1)?,
            cache_ttl_apikey_touch: env_int_min("CACHE_TTL_APIKEY_TOUCH", 60, 1)?,

            llm_url: validate_llm_url(&env_string(
                "LLM_URL",
                "http://localhost:8056/v1/chat/completions",
            ))?,
            llm_model: env_string("LLM_MODEL", "qwen3vl"),
            llm_temperature: env_f64_range("LLM_TEMPERATURE", 0.7, 0.0, 2.0)?,
            llm_top_p: env_f64_range("LLM_TOP_P", 0.8, 0.0, 1.0)?,
            llm_presence_penalty: env_f64_range("LLM_PRESENCE_PENALTY", 1.5, -2.0, 2.0)?,
            llm_max_tokens_fields: env_int_min("LLM_MAX_TOKENS_FIELDS", 8192, 1)?,
            llm_timeout: Duration::from_secs_f64(llm_timeout_secs),
            llm_page_batch_size: env_int_min("LLM_PAGE_BATCH_SIZE", 1, 1)? as usize,

            worker_poll_seconds: env_f64_range("WORKER_POLL_SECONDS", 1.0, 0.1, 3600.0)?,

            rate_limit_per_minute: env_int_min("RATE_LIMIT_PER_MINUTE", 30, 1)? as u32,
            max_upload_mb,
            max_upload_bytes: (max_upload_mb.max(0) as usize) * 1024 * 1024,
            max_document_pages: env_int_min("MAX_DOCUMENT_PAGES", 100, 1)? as usize,
            cors_allow_origins: split_csv(&env_string(
                "CORS_ALLOW_ORIGINS",
                &DEFAULT_CORS_ALLOW_ORIGINS.join(","),
            )),
            frame_ancestors: split_csv(&env_string("FRAME_ANCESTORS", "")),

            max_long_side_px: env_int_min("MAX_LONG_SIDE_PX", 1536, 1)? as u32,
            max_pixels: env_int_min("MAX_PIXELS", 1536 * 1120, 1)? as u32,
            jpeg_quality,
            dpi_floor: env_int_min("DPI_FLOOR", 96, 1)? as u32,
            dpi_default: env_int_min("DPI_DEFAULT", 128, 1)? as u32,
            pdf_workers: env_int_min("PDF_WORKERS", 2, 1)? as usize,
            ocr_workers: env_int_min("OCR_WORKERS", 3, 1)? as usize,
            ocr_device: env_string("OCR_DEVICE", "cpu"),
            ocr_service_url: env_string("OCR_SERVICE_URL", "http://localhost:8057"),
            pdf_service_url: env_string("PDF_SERVICE_URL", "http://localhost:8058"),

            minio_endpoint: env_string("MINIO_ENDPOINT", "localhost:9000"),
            minio_access_key: env_string("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key: env_string("MINIO_SECRET_KEY", "minioadmin"),
            minio_secure: env_bool("MINIO_SECURE", false)?,
            minio_region: env_string("MINIO_REGION", "us-east-1"),
            minio_documents_bucket: env_string("MINIO_DOCUMENTS_BUCKET", "augocr-documents"),
            minio_artifacts_bucket: env_string("MINIO_ARTIFACTS_BUCKET", "augocr-artifacts"),
            local_object_store_dir: PathBuf::from(env_string(
                "LOCAL_OBJECT_STORE_DIR",
                ".local_object_store",
            )),

            mlflow_enabled: env_bool("MLFLOW_ENABLED", true)?,
            mlflow_tracking_uri: env_string("MLFLOW_TRACKING_URI", "http://localhost:5000"),

            log_level: env_string("LOG_LEVEL", "INFO").to_uppercase(),
            pipeline_log_dir: PathBuf::from(env_string("PIPELINE_LOG_DIR", "logs/pipeline")),
            page_usage_log_path: PathBuf::from(env_string(
                "PAGE_USAGE_LOG_PATH",
                &page_usage_default.display().to_string(),
            )),
            page_alerts_log_path: PathBuf::from(env_string(
                "PAGE_ALERTS_LOG_PATH",
                &page_alerts_default.display().to_string(),
            )),

            default_subscription_limit: env_int_min("DEFAULT_SUBSCRIPTION_LIMIT", 0, 0)?,
            subscription_warning_threshold: env_f64_range(
                "SUBSCRIPTION_WARNING_THRESHOLD",
                0.9,
                0.0,
                1.0,
            )?,

            debug_dump_bbox: env_bool("DEBUG_DUMP_BBOX", false)?,
        })
    }

    /// Process-wide configuration, loaded on first access. Panics with the
    /// precise validation error when the environment is misconfigured — this
    /// mirrors the Python import-time failure mode.
    pub fn global() -> &'static Config {
        static CONFIG: OnceLock<Config> = OnceLock::new();
        CONFIG.get_or_init(|| match Self::from_env() {
            Ok(cfg) => cfg,
            Err(e) => panic!("configuration error: {e}"),
        })
    }
}
