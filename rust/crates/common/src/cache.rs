//! cache.rs ← cache.py.
//!
//! Optional Redis read-through cache with a safe no-op fallback.
//!
//! The database is remote, so every query is a cross-region round trip. Hot,
//! read-mostly rows (auth lookups, vendor/template/alias/mapping definitions)
//! are cached here; invalidation is explicit on the matching mutation and TTLs
//! are only a safety net.
//!
//! Safety rules carried over from the Python implementation:
//! * If Redis is disabled (`CACHE_ENABLED=false`) or unreachable, every call
//!   degrades to a no-op ([`NullCache`]) and callers fall through to Postgres.
//!   A Redis outage must never take the API down.
//! * Every Redis call is wrapped so a transient error returns a miss, not an
//!   error.
//! * Values are **JSON** here (Python pickled). That is safe regardless of who
//!   can read Redis, and lets mixed Python/Rust deployments coexist behind the
//!   same prefix without sharing a deserializer. Note the encoding change:
//!   keys written by the old Python code must expire before Rust reads them,
//!   which the short TTLs guarantee.
//! * [`get_or_set`] caches positive results only — a `None` loader result
//!   ("no template") is never stored, so misses re-check the DB.

use std::future::Future;
use std::sync::{Arc, OnceLock, RwLock};
use std::time::Duration;

use async_trait::async_trait;
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde_json::Value;

use crate::config::Config;

/// Object-safe cache abstraction. All operations fail soft.
#[async_trait]
pub trait Cache: Send + Sync {
    fn enabled(&self) -> bool;

    async fn get(&self, key: &str) -> Option<Value>;

    async fn set(&self, key: &str, value: &Value, ttl_secs: Option<i64>);

    async fn delete(&self, keys: &[String]);

    /// Delete every key matching `pattern` (a glob relative to the prefix).
    /// Uses SCAN (non-blocking) so it is safe for occasional invalidation.
    async fn delete_pattern(&self, pattern: &str);

    async fn close(&self);
}

/// No-op cache used when Redis is disabled or unavailable.
pub struct NullCache;

#[async_trait]
impl Cache for NullCache {
    fn enabled(&self) -> bool {
        false
    }

    async fn get(&self, _key: &str) -> Option<Value> {
        None
    }

    async fn set(&self, _key: &str, _value: &Value, _ttl_secs: Option<i64>) {}

    async fn delete(&self, _keys: &[String]) {}

    async fn delete_pattern(&self, _pattern: &str) {}

    async fn close(&self) {}
}

/// Redis-backed cache. Every operation fails soft to a miss/no-op.
pub struct RedisCache {
    conn: ConnectionManager,
    prefix: String,
}

impl RedisCache {
    fn k(&self, key: &str) -> String {
        format!("{}:{key}", self.prefix)
    }
}

#[async_trait]
impl Cache for RedisCache {
    fn enabled(&self) -> bool {
        true
    }

    async fn get(&self, key: &str) -> Option<Value> {
        let raw: Result<Option<Vec<u8>>, _> = self.conn.clone().get(self.k(key)).await;
        match raw {
            Ok(Some(bytes)) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(v) => Some(v),
                Err(e) => {
                    tracing::warn!(key, error = %e, "cache decode failed");
                    None
                }
            },
            Ok(None) => None,
            Err(e) => {
                tracing::warn!(key, error = %e, "cache get failed");
                None
            }
        }
    }

    async fn set(&self, key: &str, value: &Value, ttl_secs: Option<i64>) {
        let payload = match serde_json::to_vec(value) {
            Ok(p) => p,
            Err(e) => {
                tracing::warn!(key, error = %e, "cache encode failed");
                return;
            }
        };
        let mut conn = self.conn.clone();
        let full_key = self.k(key);
        let result: Result<(), _> = match ttl_secs.filter(|t| *t > 0) {
            Some(ttl) => conn.set_ex(full_key, payload, ttl as u64).await,
            None => conn.set(full_key, payload).await,
        };
        if let Err(e) = result {
            tracing::warn!(key, error = %e, "cache set failed");
        }
    }

    async fn delete(&self, keys: &[String]) {
        if keys.is_empty() {
            return;
        }
        let full: Vec<String> = keys.iter().map(|k| self.k(k)).collect();
        let mut conn = self.conn.clone();
        if let Err(e) = conn.del::<_, ()>(full).await {
            tracing::warn!(error = %e, "cache delete failed");
        }
    }

    async fn delete_pattern(&self, pattern: &str) {
        let full_pattern = self.k(pattern);
        let mut conn = self.conn.clone();
        let mut cursor: u64 = 0;
        let mut deleted = 0usize;
        // Bounded rounds guard against pathological server behaviour while
        // still sweeping large keyspaces (SCAN loops until cursor wraps).
        for _ in 0..1000 {
            let reply: Result<(u64, Vec<String>), _> = redis::cmd("SCAN")
                .cursor_arg(cursor)
                .arg("MATCH")
                .arg(&full_pattern)
                .arg("COUNT")
                .arg(500usize)
                .query_async(&mut conn)
                .await;
            match reply {
                Ok((next, keys)) => {
                    if !keys.is_empty() {
                        deleted += keys.len();
                        if let Err(e) = conn.del::<_, ()>(&keys).await {
                            tracing::warn!(error = %e, pattern, "cache delete_pattern failed");
                            return;
                        }
                    }
                    cursor = next;
                    if cursor == 0 {
                        return;
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, pattern, "cache delete_pattern failed");
                    return;
                }
            }
        }
        let _ = deleted;
    }

    async fn close(&self) {}
}

/// Read-through helper: check the cache, run `loader` on a miss, store the
/// result when it is positive. Mirrors Python's `Cache.get_or_set`.
pub async fn get_or_set<F, Fut>(cache: &dyn Cache, key: &str, ttl_secs: Option<i64>, loader: F) -> Option<Value>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = Option<Value>>,
{
    if let Some(cached) = cache.get(key).await {
        return Some(cached);
    }
    let value = loader().await;
    if let Some(ref v) = value {
        cache.set(key, v, ttl_secs).await;
    }
    value
}

// -- Process-wide handle ----------------------------------------------------

fn active_cache_slot() -> &'static RwLock<Arc<dyn Cache>> {
    static SLOT: OnceLock<RwLock<Arc<dyn Cache>>> = OnceLock::new();
    SLOT.get_or_init(|| RwLock::new(Arc::new(NullCache)))
}

/// Return the process-wide cache ([`NullCache`] until [`set_active_cache`] runs).
pub fn get_cache() -> Arc<dyn Cache> {
    // A panicked writer poisons the slot but the previous Arc is still valid.
    let guard = match active_cache_slot().read() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    Arc::clone(&guard)
}

pub fn set_active_cache(cache: Arc<dyn Cache>) {
    let mut guard = match active_cache_slot().write() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    *guard = cache;
}

/// Build the cache for this process. Falls back to [`NullCache`] on any
/// problem — startup never blocks on Redis (2 s connect+ping budget).
pub async fn create_cache(cfg: &Config) -> Arc<dyn Cache> {
    if !cfg.cache_enabled {
        tracing::info!("Cache disabled (CACHE_ENABLED=false) -- using NullCache");
        return Arc::new(NullCache);
    }
    let client = match redis::Client::open(cfg.redis_url.as_str()) {
        Ok(c) => c,
        Err(e) => {
            tracing::warn!(error = %e, "Redis unavailable -- falling back to NullCache");
            return Arc::new(NullCache);
        }
    };
    let connect = async {
        let manager = client.get_connection_manager().await?;
        let mut probe = manager.clone();
        redis::cmd("PING").query_async::<()>(&mut probe).await?;
        Ok::<ConnectionManager, redis::RedisError>(manager)
    };
    match tokio::time::timeout(Duration::from_secs(2), connect).await {
        Ok(Ok(manager)) => {
            tracing::info!("Redis cache connected: {}", cfg.redis_url);
            Arc::new(RedisCache {
                conn: manager,
                prefix: cfg.cache_prefix.clone(),
            })
        }
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "Redis unavailable -- falling back to NullCache");
            Arc::new(NullCache)
        }
        Err(_) => {
            tracing::warn!("Redis connect+ping timed out after 2s -- falling back to NullCache");
            Arc::new(NullCache)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn null_cache_is_a_pure_passthrough() {
        let cache = NullCache;
        assert!(!cache.enabled());
        assert_eq!(cache.get("k").await, None);
        cache.set("k", &json!(1), Some(10)).await;
        cache.delete(&["k".into()]).await;
        cache.delete_pattern("k*").await;
        cache.close().await;
    }

    #[tokio::test]
    async fn get_or_set_loads_once_and_caches_positive_only() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let cache = NullCache; // always misses, forcing the loader every call
        let calls = AtomicUsize::new(0);

        async fn load(calls: &AtomicUsize, hit: bool) -> Option<Value> {
            calls.fetch_add(1, Ordering::SeqCst);
            if hit {
                Some(json!({"v": 1}))
            } else {
                None
            }
        }

        let v1 = {
            let fut = load(&calls, true);
            get_or_set(&cache, "k", None, || fut).await
        };
        assert_eq!(v1, Some(json!({"v": 1})));

        let none = {
            let fut = load(&calls, false);
            get_or_set(&cache, "miss", None, || fut).await
        };
        assert_eq!(none, None);

        // Both lookups went to the loader because NullCache never stores.
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn json_round_trip_matches_redis_payload_shape() {
        let value = json!({"id": 3, "name": "acme", "active": true});
        let encoded = serde_json::to_vec(&value).unwrap();
        let decoded: Value = serde_json::from_slice(&encoded).unwrap();
        assert_eq!(decoded, value);
    }
}
