//! auth.rs ← auth.py.
//!
//! JWT (HS256) auth + per-user resource ownership checks. Isolation model:
//! ~5 external clients, each owning 1+ vendors; every downstream resource
//! chains back to a vendor, so all checks go through `vendor.user_id`. Admin
//! role bypasses every assertion.
//!
//! `secret_key()` re-reads the environment on each call on purpose, so a
//! rotated secret or test override takes effect without a restart.

use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::PgPool;

use crate::db;
use crate::error::{AppError, AppResult};

pub const TOKEN_TTL_HOURS: i64 = 8;

const BCRYPT_MAX_BYTES: usize = 72;

// -- Secret -----------------------------------------------------------------

/// Read `SECRET_KEY` at call time; 500 when unset (never sign with an empty
/// key). The message is deliberately client-visible.
pub fn secret_key() -> AppResult<String> {
    match std::env::var("SECRET_KEY") {
        Ok(s) if !s.is_empty() => Ok(s),
        _ => Err(AppError::Misconfigured(
            "SECRET_KEY is not configured on the server".into(),
        )),
    }
}

// -- Password ---------------------------------------------------------------

/// bcrypt hashes only the first 72 bytes of input; truncate explicitly so
/// longer passwords can't produce surprising collisions or backend errors.
fn to_bcrypt_bytes(plain: &str) -> Vec<u8> {
    plain.as_bytes()[..plain.len().min(BCRYPT_MAX_BYTES)].to_vec()
}

pub fn hash_password(plain: &str) -> AppResult<String> {
    bcrypt::hash(to_bcrypt_bytes(plain), bcrypt::DEFAULT_COST)
        .map_err(|e| AppError::Internal(format!("bcrypt hash failed: {e}")))
}

pub fn verify_password(plain: &str, hashed: &str) -> bool {
    bcrypt::verify(to_bcrypt_bytes(plain), hashed).unwrap_or(false)
}

// -- Token ------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub role: String,
    pub email: String,
    pub iat: i64,
    pub exp: i64,
}

pub fn create_access_token(user_id: &str, role: &str, email: &str) -> AppResult<String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| AppError::Internal(e.to_string()))?
        .as_secs() as i64;
    let claims = Claims {
        sub: user_id.to_string(),
        role: role.to_string(),
        email: email.to_string(),
        iat: now,
        exp: now + TOKEN_TTL_HOURS * 3600,
    };
    let secret = secret_key()?;
    jsonwebtoken::encode(
        &Header::new(Algorithm::HS256),
        &claims,
        &EncodingKey::from_secret(secret.as_bytes()),
    )
    .map_err(|e| AppError::Internal(format!("jwt encode failed: {e}")))
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter()
        .zip(b.iter())
        .fold(0u8, |acc, (x, y)| acc | (x ^ y))
        == 0
}

/// Reject JWT segments whose text is not canonical base64url (no padding,
/// re-encoding identical) — blocks smuggling tricks before verification.
fn ensure_canonical_b64url_segment(segment: &str) -> Result<(), &'static str> {
    if segment.is_empty() || segment.contains('=') {
        return Err("JWT segment is not canonical base64url");
    }
    let raw = URL_SAFE_NO_PAD
        .decode(segment.as_bytes())
        .map_err(|_| "JWT segment is malformed base64url")?;
    let canonical = URL_SAFE_NO_PAD.encode(&raw);
    if !ct_eq(canonical.as_bytes(), segment.as_bytes()) {
        return Err("JWT segment is not canonical base64url");
    }
    Ok(())
}

fn ensure_canonical_jwt(token: &str) -> Result<(), &'static str> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        return Err("JWT must contain exactly three segments");
    }
    for part in parts {
        ensure_canonical_b64url_segment(part)?;
    }
    Ok(())
}

pub fn decode_token(token: &str) -> AppResult<Claims> {
    let inner = || -> Result<Claims, AppError> {
        ensure_canonical_jwt(token).map_err(|msg| AppError::Unauthorized(msg.to_string()))?;
        let mut validation = Validation::new(Algorithm::HS256);
        validation.validate_aud = false;
        validation.leeway = 0;
        jsonwebtoken::decode::<Claims>(
            token,
            &DecodingKey::from_secret(secret_key()?.as_bytes()),
            &validation,
        )
        .map(|data| data.claims)
        .map_err(|e| AppError::Internal(e.to_string()))
    };
    inner().map_err(|err| {
        tracing::warn!(error = %err, "auth.token_invalid");
        // Sanitized — never leak JWT internals to the client.
        AppError::Unauthorized("Invalid or expired token".into())
    })
}

// -- API keys ---------------------------------------------------------------

/// Generate a new API key. Returns `(raw_key, key_hash, prefix)`; the raw key
/// is shown to the admin ONCE and only the SHA-256 hash is stored.
pub fn generate_api_key() -> (String, String, String) {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    let raw = format!("po_live_{}", URL_SAFE_NO_PAD.encode(bytes));
    let hashed = hex::encode(Sha256::digest(raw.as_bytes()));
    let prefix = format!("{}...", &raw[..raw.len().min(16)]);
    (raw, hashed, prefix)
}

fn fernet_from_secret() -> AppResult<Fernet> {
    let secret = std::env::var("SECRET_KEY").unwrap_or_default();
    if secret.is_empty() {
        return Err(AppError::Internal(
            "SECRET_KEY not configured — cannot encrypt API key".into(),
        ));
    }
    Ok(Fernet::from_secret(&secret))
}

pub fn encrypt_api_key(raw_key: &str) -> AppResult<String> {
    Ok(fernet_from_secret()?.encrypt(raw_key.as_bytes()))
}

pub fn decrypt_api_key(encrypted: &str) -> AppResult<String> {
    fernet_from_secret()?
        .decrypt(encrypted)
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .ok_or_else(|| {
            AppError::Internal("API key decryption failed — server configuration error".into())
        })
}

// -- Fernet-compatible envelope (pure Rust, replaces the openssl-linked crate)
//
// Token layout: base64url(0x80 || ts_be64 || iv_16 || aes-128-cbc(ct) || hmac).
// Key: 32 bytes derived as sha256(SECRET), split signing(16) || encryption(16).
// CBC + PKCS7 are implemented directly on the stable block traits so we don't
// track the fast-moving cipher-crate API surface.

pub(crate) mod fernet {
    use aes::cipher::{Block, BlockCipherDecrypt, BlockCipherEncrypt, KeyInit};
    use base64::Engine as _;
    use hmac::{Hmac, Mac};
    use sha2::{Digest, Sha256};

    use aes::Aes128;

    fn block_from(slice: &[u8]) -> Block<Aes128> {
        let mut block = <Block<Aes128>>::default();
        block.copy_from_slice(slice);
        block
    }

    fn xor_into(dst: &mut Block<Aes128>, src: &Block<Aes128>) {
        for (d, s) in dst.iter_mut().zip(src.iter()) {
            *d ^= *s;
        }
    }

    pub(crate) fn pkcs7_pad(data: &[u8]) -> Vec<u8> {
        // Block size is 16 and never zero, so pad is always in 1..=16.
        let pad = 16 - (data.len() % 16);
        let mut out = Vec::with_capacity(data.len() + pad);
        out.extend_from_slice(data);
        out.resize(data.len() + pad, pad as u8);
        out
    }

    pub(crate) fn pkcs7_unpad(data: &[u8]) -> Option<Vec<u8>> {
        let pad = *data.last()?;
        if pad == 0 || pad > 16 || (pad as usize) > data.len() {
            return None;
        }
        if data[data.len() - pad as usize..].iter().any(|&b| b != pad) {
            return None;
        }
        Some(data[..data.len() - pad as usize].to_vec())
    }

    pub struct Fernet {
        signing: [u8; 16],
        encryption: [u8; 16],
    }

    impl Fernet {
        pub fn from_secret(secret: &str) -> Self {
            let master = Sha256::digest(secret.as_bytes());
            let mut signing = [0u8; 16];
            let mut encryption = [0u8; 16];
            signing.copy_from_slice(&master[..16]);
            encryption.copy_from_slice(&master[16..]);
            Self { signing, encryption }
        }

        fn sign(&self, payload: &[u8]) -> Vec<u8> {
            // HMAC-SHA256 accepts every key length; this cannot fail.
            #[allow(clippy::expect_used)]
            let mut mac = Hmac::<Sha256>::new_from_slice(&self.signing).expect("hmac key");
            mac.update(payload);
            mac.finalize().into_bytes().to_vec()
        }

        pub fn encrypt(&self, data: &[u8]) -> String {
            use rand::RngCore;

            let mut iv = [0u8; 16];
            rand::rngs::OsRng.fill_bytes(&mut iv);
            // Named per the Fernet spec: seconds since epoch, big-endian.
            const TIMESTAMP_BYTES: usize = 8;
            let ts = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0)
                .to_be_bytes();

            let cipher = Aes128::new(&block_from(&self.encryption));
            let plaintext = pkcs7_pad(data);
            let mut ciphertext = vec![0u8; plaintext.len()];
            let mut prev = block_from(&iv);
            for (chunk, out) in plaintext.chunks_exact(16).zip(ciphertext.chunks_exact_mut(16)) {
                let mut block = block_from(chunk);
                xor_into(&mut block, &prev);
                cipher.encrypt_block(&mut block);
                prev.copy_from_slice(&block);
                out.copy_from_slice(&block);
            }

            let mut payload = Vec::with_capacity(1 + TIMESTAMP_BYTES + 16 + ciphertext.len() + 32);
            payload.push(0x80);
            payload.extend_from_slice(&ts);
            payload.extend_from_slice(&iv);
            payload.extend_from_slice(&ciphertext);

            let mac = self.sign(&payload);
            payload.extend_from_slice(&mac);
            base64::engine::general_purpose::URL_SAFE.encode(payload)
        }

        pub fn decrypt(&self, token: &str) -> Option<Vec<u8>> {
            // Accept padded and unpadded input; Python clients emit padded.
            let trimmed = token.trim_end_matches('=');
            let pad = "=".repeat((4 - trimmed.len() % 4) % 4);
            let raw = base64::engine::general_purpose::URL_SAFE
                .decode(format!("{trimmed}{pad}"))
                .ok()?;
            // version(1) + timestamp(8) + iv(16) + at least one block(16) + hmac(32)
            if raw.len() < 73 || raw.len() % 16 != 9 || raw[0] != 0x80 {
                return None;
            }
            let (payload, mac) = raw.split_at(raw.len() - 32);
            // Constant-time MAC verification.
            let expected = self.sign(payload);
            let diff = expected
                .iter()
                .zip(mac.iter())
                .fold(0u8, |acc, (a, b)| acc | (a ^ b));
            if diff != 0 {
                return None;
            }
            let iv: &[u8] = &payload[9..25];
            let ciphertext = &payload[25..];

            let cipher = Aes128::new(&block_from(&self.encryption));
            let mut out = vec![0u8; ciphertext.len()];
            let mut prev = block_from(iv);
            for (chunk, out_chunk) in ciphertext.chunks_exact(16).zip(out.chunks_exact_mut(16)) {
                let mut block = block_from(chunk);
                cipher.decrypt_block(&mut block);
                xor_into(&mut block, &prev);
                prev.copy_from_slice(chunk);
                out_chunk.copy_from_slice(&block);
            }
            pkcs7_unpad(&out)
        }
    }
}

use fernet::Fernet;

#[cfg(test)]
mod fernet_tests {
    use super::fernet::{pkcs7_pad, pkcs7_unpad, Fernet};

    #[test]
    fn pkcs7_round_trip_and_rejects_garbage() {
        assert_eq!(pkcs7_unpad(&pkcs7_pad(b"")).as_deref(), Some(b"".as_slice()));
        assert_eq!(
            pkcs7_unpad(&pkcs7_pad(b"hello world")).as_deref(),
            Some(b"hello world".as_slice())
        );
        assert_eq!(pkcs7_pad(&[0u8; 16]).len(), 32); // full extra pad block
        assert!(pkcs7_unpad(&[0u8; 16]).is_none()); // all-zero padding invalid
    }

    #[test]
    fn round_trips_and_rejects_tampering() {
        let f = Fernet::from_secret("unit-test-secret");
        let token = f.encrypt(b"po_live_abc123");
        assert_eq!(f.decrypt(&token).as_deref(), Some(b"po_live_abc123".as_slice()));

        let mut chars: Vec<char> = token.chars().collect();
        let mid = chars.len() / 2;
        chars[mid] = if chars[mid] == 'A' { 'B' } else { 'A' };
        let tampered: String = chars.into_iter().collect();
        assert_eq!(f.decrypt(&tampered), None);

        // Cross-secret decryption must fail.
        assert_eq!(Fernet::from_secret("other").decrypt(&token), None);
    }
}

pub fn sha256_hex(input: &str) -> String {
    hex::encode(Sha256::digest(input.as_bytes()))
}

// -- Resolvers --------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize)]
pub struct AuthUser {
    pub id: String,
    pub role: String,
    pub email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auth_method: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_key_id: Option<i64>,
}

impl AuthUser {
    pub fn is_admin(&self) -> bool {
        self.role == "admin"
    }
}

async fn load_active_user(pool: &PgPool, user_id: &str) -> AppResult<AuthUser> {
    let record = db::get_user_by_id(pool, user_id)
        .await?
        .ok_or_else(|| AppError::Unauthorized("User disabled or missing".into()))?;
    if !db::user_is_active(&record) {
        return Err(AppError::Unauthorized("User disabled or missing".into()));
    }
    let role = record
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if role.is_empty() {
        return Err(AppError::Unauthorized("Malformed token".into()));
    }
    Ok(AuthUser {
        id: user_id.to_string(),
        role,
        email: record.get("email").and_then(Value::as_str).map(str::to_string),
        auth_method: None,
        api_key_id: None,
    })
}

/// Strict Bearer-token authentication.
pub async fn resolve_bearer_user(pool: &PgPool, bearer_token: &str) -> AppResult<AuthUser> {
    let claims = decode_token(bearer_token)?;
    load_active_user(pool, &claims.sub).await
}

/// Dual auth: `X-API-Key` header OR JWT Bearer token, strictly from headers.
///
/// API keys resolve to the owning user so downstream vendor-isolation logic
/// works unchanged. `last_used_at` updates fire-and-forget.
pub async fn resolve_user_or_api_key(
    pool: &PgPool,
    bearer_token: Option<&str>,
    x_api_key: Option<&str>,
) -> AppResult<AuthUser> {
    if let Some(api_key) = x_api_key.filter(|k| !k.is_empty()) {
        let hashed = sha256_hex(api_key);
        let key_row = db::verify_api_key_hash(pool, &hashed)
            .await?
            .filter(|row| row.get("is_active").and_then(Value::as_bool).unwrap_or(false))
            .ok_or_else(|| AppError::Unauthorized("Invalid or inactive API key".into()))?;

        let user_id = key_row
            .get("user_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let mut user = load_active_user(pool, &user_id).await.map_err(|_| {
            AppError::Unauthorized("API key owner disabled".into())
        })?;

        let pool_for_touch = pool.clone();
        let hash_for_touch = hashed.clone();
        tokio::spawn(async move {
            if let Err(e) = db::touch_api_key(&pool_for_touch, &hash_for_touch).await {
                tracing::debug!(error = %e, "api-key touch failed");
            }
        });

        user.auth_method = Some("api_key");
        user.api_key_id = key_row.get("id").and_then(Value::as_i64);
        return Ok(user);
    }

    let token = bearer_token.ok_or_else(|| AppError::Unauthorized("Not authenticated".into()))?;
    let claims = decode_token(token)?;
    let mut user = load_active_user(pool, &claims.sub).await?;
    user.auth_method = Some("jwt");
    Ok(user)
}

pub fn ensure_admin(user: &AuthUser) -> AppResult<()> {
    if user.is_admin() {
        Ok(())
    } else {
        Err(AppError::Forbidden("Admin only".into()))
    }
}

// -- Ownership assertions ---------------------------------------------------

pub async fn assert_vendor_access(pool: &PgPool, vendor_id: &str, user: &AuthUser) -> AppResult<()> {
    if user.is_admin() {
        return Ok(());
    }
    let owner = db::get_vendor_owner(pool, vendor_id).await?;
    match owner {
        // Either vendor doesn't exist, or it's an unowned legacy row.
        None => Err(AppError::NotFound("Vendor not found".into())),
        Some(owner) if owner == user.id => Ok(()),
        Some(_) => Err(AppError::Forbidden("Access denied".into())),
    }
}

pub async fn assert_extraction_access(
    pool: &PgPool,
    extraction_id: i64,
    user: &AuthUser,
) -> AppResult<()> {
    if user.is_admin() {
        return Ok(());
    }
    let ext = db::get_extraction(pool, extraction_id)
        .await?
        .ok_or_else(|| AppError::NotFound("Extraction not found".into()))?;

    // Prefer the billing_user_id recorded in document metadata.
    let billing_user_id = match ext.get("document_id").and_then(Value::as_i64) {
        Some(doc_id) => db::get_document(pool, doc_id)
            .await?
            .and_then(|doc| doc.get("metadata").cloned())
            .filter(|m| !m.is_null())
            .and_then(|m| m.get("billing_user_id").and_then(Value::as_str).map(str::to_string)),
        None => None,
    };

    match billing_user_id {
        Some(id) if id != user.id => Err(AppError::Forbidden("Access denied".into())),
        Some(_) => Ok(()),
        None => {
            let vendor_id = ext
                .get("vendor_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            assert_vendor_access(pool, vendor_id, user).await
        }
    }
}

pub async fn assert_job_access(pool: &PgPool, job_id: i64, user: &AuthUser) -> AppResult<()> {
    if user.is_admin() {
        return Ok(());
    }
    let job = db::get_job(pool, job_id)
        .await?
        .ok_or_else(|| AppError::NotFound("Job not found".into()))?;
    let ext_id = job
        .get("extraction_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| AppError::Forbidden("Access denied".into()))?;
    assert_extraction_access(pool, ext_id, user).await
}

pub async fn assert_alias_access(pool: &PgPool, alias_id: i64, user: &AuthUser) -> AppResult<()> {
    if user.is_admin() {
        return Ok(());
    }
    let vendor_id = db::get_alias_vendor_id(pool, alias_id)
        .await?
        .ok_or_else(|| AppError::NotFound("Alias not found".into()))?;
    assert_vendor_access(pool, &vendor_id, user).await
}
