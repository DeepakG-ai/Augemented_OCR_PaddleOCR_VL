//! db — Postgres access layer (port of backend/db.py).
//!
//! Conventions for every function added here:
//! * Reads wrap Python's exact column list with [`util::row_query`] and return
//!   `serde_json::Value` rows; writes use plain parameterised SQL.
//! * Hot read-mostly rows go through [`cached_read`] with the same cache keys
//!   and TTLs as Python; invalidation is explicit on the matching mutation.
//! * All functions take `&PgPool` (asyncpg's `pool.acquire()` has no analogue
//!   — sqlx checks connections out per-query automatically).

pub mod billing;
pub mod catalog;
pub mod docs;
pub mod jobs;
pub mod schema;
mod util;
pub mod spatial;
pub mod users;
pub mod vendors;

pub use billing::*;
pub use catalog::*;
pub use docs::*;
pub use jobs::*;
pub use schema::*;
pub use spatial::*;
pub use users::*;
pub use vendors::*;

use std::future::Future;
use std::time::Duration;

use serde_json::Value;
use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;

use crate::cache::get_cache;
use crate::config::Config;
use crate::error::AppResult;

/// Create a connection pool. The API uses `cfg.db_pool_*_api`; each pipeline
/// worker passes the smaller worker sizing so many workers never exhaust
/// Postgres `max_connections`.
#[allow(dead_code)] // used by binaries once conversions land
pub async fn create_pool(cfg: &Config, min_size: u32, max_size: u32) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .min_connections(min_size)
        .max_connections(max_size)
        .acquire_timeout(Duration::from_secs(30))
        .connect(&cfg.database_url)
        .await
}

/// Read-through cache helper: positive-only, loader errors propagate.
///
/// Rust ownership replaces Python's deepcopy: cache hits hand out an owned
/// `Value`, so callers can never corrupt the stored entry.
pub(crate) async fn cached_read<F, Fut>(key: &str, ttl_secs: i64, loader: F) -> AppResult<Option<Value>>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = AppResult<Option<Value>>>,
{
    let cache = get_cache();
    if let Some(hit) = cache.get(key).await {
        return Ok(Some(hit));
    }
    let value = loader().await?;
    if let Some(ref v) = value {
        cache.set(key, v, Some(ttl_secs)).await;
    }
    Ok(value)
}

#[allow(dead_code)] // consumed by the remaining db conversions
fn cfg() -> &'static Config {
    Config::global()
}

// -- Cache invalidation (explicit, on the matching mutation) ----------------

pub async fn invalidate_user_cache(user_id: Option<&str>) {
    let Some(uid) = util::uuid_or_none(user_id) else {
        return;
    };
    get_cache().delete(&[format!("user:{uid}")]).await;
}

/// Key cache is keyed by hash; mutations carry only the key id, so clear all
/// cached keys (api-key mutations are rare and the set is tiny).
pub async fn invalidate_api_key_cache() {
    get_cache().delete_pattern("apikey:*").await;
}

pub async fn invalidate_vendor_cache(vendor_id: Option<&str>) {
    let cache = get_cache();
    if let Some(id) = vendor_id.filter(|v| !v.is_empty()) {
        cache
            .delete(&[
                format!("vendor:{id}"),
                format!("template:{id}"),
                format!("mapping:{id}"),
                format!("aliases:list:{id}"),
            ])
            .await;
    }
    cache.delete_pattern("vendors:list:*").await;
    cache.delete_pattern("templates:list:*").await;
    cache.delete_pattern("aliases:detect:*").await;
}

pub async fn invalidate_template_cache(vendor_id: &str) {
    let cache = get_cache();
    cache.delete(&[format!("template:{vendor_id}")]).await;
    cache.delete_pattern("templates:list:*").await;
}

pub async fn invalidate_mapping_cache(vendor_id: &str) {
    get_cache().delete(&[format!("mapping:{vendor_id}")]).await;
}

/// "aliases:list:all" is the unfiltered variant — cleared too in case callers
/// ever list all aliases.
pub async fn invalidate_alias_cache(vendor_id: &str) {
    let cache = get_cache();
    cache
        .delete(&[format!("aliases:list:{vendor_id}"), "aliases:list:all".into()])
        .await;
    cache.delete_pattern("aliases:detect:*").await;
}
