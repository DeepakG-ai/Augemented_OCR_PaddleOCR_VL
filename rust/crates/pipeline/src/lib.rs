//! augocr-pipeline — stage workers (normalize / ocr / llm / postprocess).
//!
//! Port map (backend/*.py → this crate):
//!   worker.py            → worker.rs + runner.rs
//!   processor.py         → processor.rs
//!   ocr_runner.py        → ocr_runner.rs
//!   extractor.py         → extractor.rs (+ llm.rs transport)
//!   vendor_detector.py   → vendor_detector.rs
//!   pdf_extractor.py     → pdf_extractor.rs
//!   geometry.py          → geometry.rs
//!   qwen_layout_apply.py → qwen_layout_apply.rs
//!   spatial_memory.py    → spatial_memory.rs
//!   page_logger.py       → page_logger.rs
//!   syteline_connector.py→ syteline_connector.rs
//!
//! External model capabilities are consumed over HTTP exactly as the Python
//! code talks to llama-server; PaddleOCR and pypdfium2 keep running as their
//! own sidecar processes reached through `OCR_SERVICE_URL` / `PDF_SERVICE_URL`
//! (they have no native Rust engines).

pub mod extractor;
pub mod geometry;
pub mod json_repair;
pub mod llm;
pub mod ocr_runner;
pub mod page;
pub mod page_logger;
pub mod pdf_extractor;
pub mod processor;
pub mod qwen_layout_apply;
pub mod runner;
pub mod spatial_memory;
pub mod syteline_connector;
pub mod vendor_detector;
pub mod worker;

/// Entry point for the `worker` binary: claim jobs from the Postgres queue and
/// drive them through this worker's stage until shutdown.
pub async fn run_worker(stage: &str, worker_name: &str) -> anyhow::Result<()> {
    runner::run(stage, worker_name).await
}
