//! object_store.rs ← object_store.py.
//!
//! MinIO-backed artifact storage referenced by object key. Uses path-style
//! SigV4-presigned requests (rusty-s3) instead of the Python SDK client;
//! falls back to the local directory store when no endpoint is configured.
//! Error taxonomy mirrors the Python module: Unavailable / NotFound /
//! Permission.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::OnceLock;
use std::time::Duration;

use rusty_s3::{Bucket, Credentials, S3Action, UrlStyle};

use crate::config::Config;
use crate::error::{AppError, AppResult};

/// Presigned-request lifetime for data operations.
const SIGNED_URL_TTL: Duration = Duration::from_secs(300);
/// Presigned-request lifetime for bucket management calls.
const BUCKET_URL_TTL: Duration = Duration::from_secs(60);

struct MinioBackend {
    http: reqwest::Client,
    credentials: Credentials,
    secure: bool,
}

struct LocalBackend {
    root: PathBuf,
}

pub struct ObjectStoreImpl {
    minio: Option<MinioBackend>,
    local: LocalBackend,
    documents_bucket: String,
    artifacts_bucket: String,
}

fn build_bucket(endpoint: &str, secure: bool, name: &str) -> AppResult<Bucket> {
    let scheme = if secure { "https" } else { "http" };
    let url: url::Url = format!("{scheme}://{endpoint}")
        .parse()
        .map_err(|_| AppError::StorageUnavailable(format!("invalid MINIO_ENDPOINT: {endpoint:?}")))?;
    Bucket::new(url, UrlStyle::Path, name.to_string(), "us-east-1".to_string())
        .map_err(|e| AppError::StorageUnavailable(format!("invalid bucket {name:?}: {e}")))
}

impl ObjectStoreImpl {
    pub fn from_config(cfg: &Config) -> Self {
        let minio = (!cfg.minio_endpoint.trim().is_empty()).then(|| MinioBackend {
            // rustls builds without host configuration in practice; fall back
            // to the default client rather than failing startup if not.
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(60))
                .build()
                .unwrap_or_default(),
            credentials: Credentials::new(
                cfg.minio_access_key.as_str(),
                cfg.minio_secret_key.as_str(),
            ),
            secure: cfg.minio_secure,
        });
        Self {
            minio,
            local: LocalBackend {
                root: cfg.local_object_store_dir.clone(),
            },
            documents_bucket: cfg.minio_documents_bucket.clone(),
            artifacts_bucket: cfg.minio_artifacts_bucket.clone(),
        }
    }

    pub fn documents_bucket(&self) -> &str {
        &self.documents_bucket
    }

    pub fn artifacts_bucket(&self) -> &str {
        &self.artifacts_bucket
    }

    pub async fn ensure_buckets(&self) -> AppResult<()> {
        let Some(minio) = self.minio.as_ref() else {
            for bucket in [&self.documents_bucket, &self.artifacts_bucket] {
                tokio::fs::create_dir_all(self.local.root.join(bucket))
                    .await
                    .map_err(|e| {
                        AppError::StorageUnavailable(format!(
                            "local object store init failed for {bucket:?}: {e}"
                        ))
                    })?;
            }
            return Ok(());
        };

        for name in [&self.documents_bucket, &self.artifacts_bucket] {
            let bucket =
                build_bucket(&Config::global().minio_endpoint, minio.secure, name)?;
            let head = bucket.head_bucket(Some(&minio.credentials));
            let url = head.sign(BUCKET_URL_TTL);
            match minio.http.head(url).send().await {
                Ok(resp) if resp.status().is_success() => continue,
                Ok(resp) if resp.status().as_u16() == 404 => {}
                Ok(resp) if resp.status().as_u16() == 401 || resp.status().as_u16() == 403 => {
                    return Err(AppError::StoragePermission(format!(
                        "object store denied access to bucket {name:?}: HTTP {}",
                        resp.status()
                    )));
                }
                Ok(resp) => {
                    return Err(AppError::StorageUnavailable(format!(
                        "object store unreachable while checking bucket {name:?}: HTTP {}",
                        resp.status()
                    )));
                }
                Err(e) => {
                    return Err(AppError::StorageUnavailable(format!(
                        "object store unreachable while checking bucket {name:?}: {e}"
                    )));
                }
            }

            let create = bucket.create_bucket(&minio.credentials);
            let url = create.sign(BUCKET_URL_TTL);
            let resp = minio
                .http
                .put(url)
                .send()
                .await
                .map_err(|e| {
                    AppError::StorageUnavailable(format!(
                        "object store unreachable while creating bucket {name:?}: {e}"
                    ))
                })?;
            let status = resp.status();
            // 409 = already exists (raced with another worker) — fine.
            if !(status.is_success() || status.as_u16() == 409) {
                return Err(AppError::StorageUnavailable(format!(
                    "creating bucket {name:?} failed: HTTP {status}"
                )));
            }
        }
        Ok(())
    }

    fn validate_key(object_key: &str) -> AppResult<()> {
        if object_key.is_empty() {
            return Err(AppError::BadRequest("object_key cannot be empty".into()));
        }
        if object_key.contains("..") || object_key.starts_with('/') {
            return Err(AppError::BadRequest(format!(
                "Invalid object_key: path traversal detected ({object_key:?})"
            )));
        }
        if object_key.contains('\\') {
            return Err(AppError::BadRequest(
                "Invalid object_key: backslashes not allowed".into(),
            ));
        }
        Ok(())
    }

    pub async fn put_bytes(
        &self,
        bucket: &str,
        object_key: &str,
        data: Vec<u8>,
        content_type: &str,
    ) -> AppResult<()> {
        Self::validate_key(object_key)?;
        let Some(minio) = self.minio.as_ref() else {
            return self.local_put(bucket, object_key, data).await;
        };
        let bkt = build_bucket(&Config::global().minio_endpoint, minio.secure, bucket)?;
        let action = bkt.put_object(Some(&minio.credentials), object_key);
        let url = action.sign(SIGNED_URL_TTL);
        let resp = minio
            .http
            .put(url)
            .header("content-type", content_type)
            .body(data)
            .send()
            .await;
        match resp {
            Ok(r) if r.status().is_success() => Ok(()),
            Ok(r) if r.status().as_u16() == 401 || r.status().as_u16() == 403 => Err(
                AppError::StoragePermission(format!("denied write: key={object_key:?} bucket={bucket:?}")),
            ),
            Ok(r) => Err(AppError::StorageUnavailable(format!(
                "write failed: key={object_key:?} bucket={bucket:?}: HTTP {}",
                r.status()
            ))),
            Err(e) => Err(AppError::StorageUnavailable(format!(
                "write failed: key={object_key:?} bucket={bucket:?}: {e}"
            ))),
        }
    }

    pub async fn get_bytes(&self, bucket: &str, object_key: &str) -> AppResult<Vec<u8>> {
        Self::validate_key(object_key)?;
        let Some(minio) = self.minio.as_ref() else {
            return self.local_get(bucket, object_key).await;
        };
        let bkt = build_bucket(&Config::global().minio_endpoint, minio.secure, bucket)?;
        let action = bkt.get_object(Some(&minio.credentials), object_key);
        let url = action.sign(SIGNED_URL_TTL);
        match minio.http.get(url).send().await {
            Ok(r) if r.status().is_success() => r.bytes().await.map(|b| b.to_vec()).map_err(|e| {
                AppError::StorageUnavailable(format!(
                    "read body failed: key={object_key:?} bucket={bucket:?}: {e}"
                ))
            }),
            Ok(r) if r.status().as_u16() == 404 => {
                Err(AppError::ObjectNotFound(format!(
                    "key={object_key:?} bucket={bucket:?}"
                )))
            }
            Ok(r) if r.status().as_u16() == 401 || r.status().as_u16() == 403 => Err(
                AppError::StoragePermission(format!("denied read: key={object_key:?} bucket={bucket:?}")),
            ),
            Ok(r) => Err(AppError::StorageUnavailable(format!(
                "read failed: key={object_key:?} bucket={bucket:?}: HTTP {}",
                r.status()
            ))),
            Err(e) => Err(AppError::StorageUnavailable(format!(
                "read failed: key={object_key:?} bucket={bucket:?}: {e}"
            ))),
        }
    }

    /// Delete an object if present; missing objects are ignored.
    pub async fn delete_object(&self, bucket: &str, object_key: &str) -> AppResult<()> {
        Self::validate_key(object_key)?;
        let Some(minio) = self.minio.as_ref() else {
            return self.local_delete(bucket, object_key).await;
        };
        let bkt = build_bucket(&Config::global().minio_endpoint, minio.secure, bucket)?;
        let action = bkt.delete_object(Some(&minio.credentials), object_key);
        let url = action.sign(SIGNED_URL_TTL);
        match minio.http.delete(url).send().await {
            Ok(_) => Ok(()),
            Err(e) => Err(AppError::StorageUnavailable(format!(
                "delete failed: key={object_key:?} bucket={bucket:?}: {e}"
            ))),
        }
    }

    // -- Local fallback -----------------------------------------------------

    fn resolve_local(&self, bucket: &str, object_key: &str) -> AppResult<PathBuf> {
        let target = self.local.root.join(bucket).join(object_key);
        let target = target
            .canonicalize()
            .unwrap_or(target);
        let root = self
            .local
            .root
            .canonicalize()
            .unwrap_or_else(|_| self.local.root.clone());
        if !target.starts_with(&root) {
            return Err(AppError::BadRequest(
                "object_key attempts to escape storage directory".into(),
            ));
        }
        Ok(target)
    }

    async fn local_put(&self, bucket: &str, object_key: &str, data: Vec<u8>) -> AppResult<()> {
        let target = self.resolve_local(bucket, object_key)?;
        if let Some(parent) = target.parent() {
            tokio::fs::create_dir_all(parent).await.map_err(|e| {
                AppError::Internal(format!("mkdir failed for {parent:?}: {e}"))
            })?;
        }
        tokio::fs::write(&target, data).await.map_err(|e| {
            AppError::Internal(format!(
                "failed writing key={object_key:?} to {}: {e}",
                target.display()
            ))
        })
    }

    async fn local_get(&self, bucket: &str, object_key: &str) -> AppResult<Vec<u8>> {
        let target = self.resolve_local(bucket, object_key)?;
        match tokio::fs::read(&target).await {
            Ok(bytes) => Ok(bytes),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(AppError::ObjectNotFound(
                format!("key={object_key:?} bucket={bucket:?} path={}", target.display()),
            )),
            Err(e) => Err(AppError::Internal(format!(
                "failed reading key={object_key:?}: {e}"
            ))),
        }
    }

    async fn local_delete(&self, bucket: &str, object_key: &str) -> AppResult<()> {
        let target = self.resolve_local(bucket, object_key)?;
        if target.exists() {
            tokio::fs::remove_file(&target)
                .await
                .map_err(|e| AppError::Internal(format!("failed deleting {object_key:?}: {e}")))?;
        }
        Ok(())
    }
}

// -- Process-wide handle ----------------------------------------------------

static STORE: OnceLock<Arc<ObjectStoreImpl>> = OnceLock::new();
static BUCKETS_ENSURED: AtomicBool = AtomicBool::new(false);

/// Process-wide store; buckets are ensured exactly once (first call), matching
/// Python's cached `get_store()`. Concurrent callers during warm-up may race
/// harmlessly — both operations are idempotent.
pub async fn get_store() -> AppResult<Arc<ObjectStoreImpl>> {
    let store = STORE.get_or_init(|| {
        Arc::new(ObjectStoreImpl::from_config(Config::global()))
    });
    if !BUCKETS_ENSURED.load(std::sync::atomic::Ordering::Acquire) {
        store.ensure_buckets().await?;
        BUCKETS_ENSURED.store(true, std::sync::atomic::Ordering::Release);
    }
    Ok(Arc::clone(store))
}
