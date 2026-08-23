//! augocr-common — shared foundation for the Augmented OCR Rust port.
//!
//! Mirrors `backend/*.py` one-to-one where practical:
//! `config` ← config.py, `error` ← HTTPException semantics, `contracts` ←
//! contracts.py, `cache` ← cache.py, `auth` ← auth.py, `models` ← models.py,
//! `object_store` ← object_store.py, `db` ← db.py.

pub mod auth;
pub mod cache;
pub mod config;
pub mod contracts;
pub mod db;
pub mod error;
pub mod field_mapper;
pub mod layout_key;
pub mod logging;
pub mod mlflow;
pub mod models;
pub mod object_store;
pub mod pyjson;
