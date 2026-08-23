//! runner.rs ← `worker.run_worker` (the claim/execute/complete loop).
//!
//! One process per stage, each polling the Postgres job queue for work of its
//! own type. The loop is deliberately boring; the interesting parts are the
//! three failure modes it has to survive:
//!
//! * **A job fails** — the extraction is marked failed, the page reservation is
//!   released, and a billing log line is written so the failure is still
//!   accounted for.
//! * **A worker dies mid-job** — its jobs stay `running` forever, so every
//!   worker sweeps for stale jobs at boot and once a minute thereafter.
//! * **The database goes away** — the pool is rebuilt behind exponential
//!   backoff rather than the process exiting, because supervisord restarting a
//!   worker costs another OCR engine build.
//!
//! Shutdown is graceful: SIGINT/SIGTERM stops the loop between jobs rather
//! than abandoning one mid-flight.

use std::time::{Duration, Instant};

use augocr_common::config::Config;
use serde_json::{json, Map, Value};
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use crate::page_logger;
use crate::worker::{self, JobError};

/// Jobs left `running` by a crashed worker are requeued after this long.
const STARTUP_STALE_MINUTES: i64 = 5;
const PERIODIC_STALE_MINUTES: i64 = 10;
/// How often to sweep for stale jobs once running.
const RECOVERY_INTERVAL: Duration = Duration::from_secs(60);
/// Database reconnect backoff bounds.
const BACKOFF_START: Duration = Duration::from_secs(2);
const BACKOFF_MAX: Duration = Duration::from_secs(60);

/// Run one stage worker until shutdown.
pub async fn run(stage: &str, worker_name: &str) -> anyhow::Result<()> {
    let cfg = Config::global();
    validate_stage(stage)?;

    // The PaddleOCR engine build costs 20–46s. Both stages that OCR pay it at
    // boot — `ocr` obviously, and `normalize` because it reads page 1 for
    // vendor detection — so no user request ever hits a cold start.
    if matches!(stage, "ocr" | "normalize") {
        crate::ocr_runner::OcrClient::global().warmup(None).await;
    }

    let mut pool =
        augocr_common::db::create_pool(cfg, cfg.db_pool_min_worker, cfg.db_pool_max_worker).await?;
    augocr_common::db::init_db(&pool).await?;
    tracing::info!("Worker started stage={stage} name={worker_name}");

    let shutdown = CancellationToken::new();
    spawn_signal_handler(shutdown.clone());

    match db_recover_stale(&pool, stage, STARTUP_STALE_MINUTES).await {
        Ok(n) if n > 0 => {
            tracing::info!("Startup recovery: reset {n} stale '{stage}' job(s) to queued")
        }
        Ok(_) => {}
        Err(e) => tracing::warn!("Startup recovery failed: {e}"),
    }

    let poll_interval = Duration::from_secs_f64(cfg.worker_poll_seconds.max(0.05));
    let mut last_recovery = Instant::now();
    let mut backoff = BACKOFF_START;

    while !shutdown.is_cancelled() {
        // One database error should not spin the loop; `tick` returns Err only
        // for pool-level failures, which trigger a reconnect.
        match tick(&pool, stage, worker_name, &mut last_recovery, &shutdown, poll_interval).await {
            Ok(()) => backoff = BACKOFF_START,
            Err(e) => {
                tracing::error!(
                    "Worker DB error stage={stage}: {e}. Reconnecting pool in {:.1}s...",
                    backoff.as_secs_f64()
                );
                pool.close().await;
                if sleep_or_shutdown(backoff, &shutdown).await {
                    break;
                }
                backoff = (backoff * 2).min(BACKOFF_MAX);
                match augocr_common::db::create_pool(
                    cfg,
                    cfg.db_pool_min_worker,
                    cfg.db_pool_max_worker,
                )
                .await
                {
                    Ok(fresh) => {
                        if let Err(e) = augocr_common::db::init_db(&fresh).await {
                            tracing::error!("Worker DB reconnect failed: {e}");
                        }
                        pool = fresh;
                    }
                    Err(e) => tracing::error!("Worker DB reconnect failed: {e}"),
                }
            }
        }
    }

    tracing::info!("Worker stopping stage={stage} name={worker_name}");
    pool.close().await;
    Ok(())
}

/// One iteration: sweep, claim, run.
async fn tick(
    pool: &PgPool,
    stage: &str,
    worker_name: &str,
    last_recovery: &mut Instant,
    shutdown: &CancellationToken,
    poll_interval: Duration,
) -> augocr_common::error::AppResult<()> {
    if last_recovery.elapsed() >= RECOVERY_INTERVAL {
        let recovered = db_recover_stale(pool, stage, PERIODIC_STALE_MINUTES).await?;
        if recovered > 0 {
            tracing::info!("Periodic recovery: reset {recovered} stale '{stage}' job(s) to queued");
        }
        *last_recovery = Instant::now();
    }

    let Some(job) = augocr_common::db::claim_job(pool, stage, worker_name).await? else {
        sleep_or_shutdown(poll_interval, shutdown).await;
        return Ok(());
    };

    let Some(job_id) = job.get("id").and_then(Value::as_i64) else {
        tracing::error!("Claimed a job with no id — skipping: {job}");
        return Ok(());
    };
    let extraction_id = job.get("extraction_id").and_then(Value::as_i64);
    tracing::info!("Claimed job id={job_id} stage={stage} extraction={extraction_id:?}");

    let started = Instant::now();
    let outcome = worker::process_job(pool, stage, &job).await;
    let elapsed_ms = started.elapsed().as_millis();

    match outcome {
        Ok(()) => {
            augocr_common::db::complete_job(
                pool,
                job_id,
                Some(&json!({"stage": stage, "message": "done"})),
            )
            .await?;
            tracing::info!(
                "Job completed: id={job_id} stage={stage} ext={extraction_id:?} ({elapsed_ms}ms)"
            );
        }
        Err(JobError::Cancelled(message)) => {
            tracing::info!(
                "Job cancelled id={job_id} stage={stage} ext={extraction_id:?}: {message}"
            );
            augocr_common::db::cancel_job(
                pool,
                job_id,
                Some(&json!({"stage": stage, "message": message})),
                Some(&message),
            )
            .await?;
        }
        Err(JobError::Failed(message)) => {
            handle_failure(pool, stage, &job, job_id, &message, elapsed_ms as i64).await?;
        }
    }
    Ok(())
}

/// Record a job failure everywhere it needs to be recorded.
async fn handle_failure(
    pool: &PgPool,
    stage: &str,
    job: &Value,
    job_id: i64,
    message: &str,
    elapsed_ms: i64,
) -> augocr_common::error::AppResult<()> {
    tracing::error!("Job failed id={job_id} stage={stage}: {message}");
    let extraction_id = job.get("extraction_id").and_then(Value::as_i64);

    if let Some(ext_id) = extraction_id {
        let row = augocr_common::db::get_extraction(pool, ext_id)
            .await
            .ok()
            .flatten();
        write_failure_log(job, stage, ext_id, row.as_ref(), message, elapsed_ms).await;

        // OCR is special: its failure does not doom the document. The JSON may
        // already be extracted, so the pipeline continues to postprocess,
        // which finalises a JSON-only success with review disabled.
        if stage == "ocr" {
            let status = row
                .as_ref()
                .and_then(|r| r.get("status"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if !worker::TERMINAL_EXTRACTION_STATUSES.contains(&status) {
                augocr_common::db::update_extraction_progress(
                    pool,
                    ext_id,
                    &json!({
                        "stage": "ocr",
                        "message": worker::OCR_REVIEW_UNAVAILABLE_MESSAGE,
                        "review_available": false,
                        "ocr_error": message,
                    }),
                    Some("processing"),
                )
                .await?;
            }
            augocr_common::db::fail_job(pool, job_id, message, false).await?;
            if let Err(e) = worker::maybe_enqueue_postprocess(
                pool,
                ext_id,
                job.get("document_id").and_then(Value::as_i64),
                worker::job_trace_context(job),
            )
            .await
            {
                tracing::warn!("could not enqueue postprocess after OCR failure: {e}");
            }
            return Ok(());
        }

        augocr_common::db::set_extraction_status(
            pool,
            ext_id,
            "failed",
            Some(&json!({"stage": stage, "message": message})),
            Some(message),
            None,
            false,
        )
        .await?;

        // The reservation must come back or the user's next upload is blocked
        // by pages that will never be billed.
        let document_id = job
            .get("document_id")
            .and_then(Value::as_i64)
            .or_else(|| {
                row.as_ref()
                    .and_then(|r| r.get("document_id"))
                    .and_then(Value::as_i64)
            });
        worker::release_quota_quietly(pool, document_id, None, "job failure", ext_id).await;
    }

    augocr_common::db::fail_job(pool, job_id, message, false).await
}

/// Write the per-stage billing log line for a failed job.
///
/// A failed document still consumed pages and money, so it must appear in the
/// page log; the counts differ per stage because each stage knows a different
/// amount about the document.
async fn write_failure_log(
    job: &Value,
    stage: &str,
    extraction_id: i64,
    row: Option<&Value>,
    message: &str,
    elapsed_ms: i64,
) {
    let empty = json!({});
    let row = row.unwrap_or(&empty);
    let total_pages = row.get("total_pages").and_then(Value::as_i64).unwrap_or(0);
    let page_results = row.get("page_results").and_then(Value::as_array);
    let extracted = page_results
        .map(|prs| prs.iter().filter(|pr| pr.get("_error").is_none()).count() as i64)
        .unwrap_or(0);
    let failed = page_results
        .map(|prs| prs.iter().filter(|pr| pr.get("_error").is_some()).count() as i64)
        .unwrap_or(0);

    let mut record: Map<String, Value> = json!({
        "extraction_id": extraction_id,
        "filename": row.get("filename").cloned().unwrap_or(Value::Null),
        "vendor_id": row.get("vendor_id").cloned().unwrap_or(Value::Null),
        "attempt_number": job.get("attempts").and_then(Value::as_i64).unwrap_or(1),
        "duration_ms": elapsed_ms,
        "errors": [{
            "page": Value::Null,
            "error": message,
            "error_type": page_logger::classify_error_type(message),
        }],
        "digital_pages": Value::Null,
        "scanned_pages": Value::Null,
        "empty_result": true,
    })
    .as_object()
    .cloned()
    .unwrap_or_default();

    // Each stage knows a different amount about the document by the time it
    // fails, so the counts it can honestly report differ.
    let (total, billable, extracted, failed_count, skipped, field_count, status) = match stage {
        // Failed before the page count was even known.
        "normalize" => (Value::Null, 0, 0, 0, 0, 0, "failed_before_page_count"),
        // Pages are known, but none were read.
        "ocr" => (
            null_if_zero(total_pages),
            0,
            0,
            0,
            total_pages,
            0,
            "failed_ocr_stage",
        ),
        "llm" => (
            null_if_zero(total_pages),
            total_pages,
            extracted,
            failed,
            (total_pages - (extracted + failed)).max(0),
            0,
            "error",
        ),
        _ => (
            null_if_zero(total_pages),
            total_pages,
            extracted,
            failed,
            (total_pages - (extracted + failed)).max(0),
            page_logger::count_result_fields(row.get("result").unwrap_or(&Value::Null)) as i64,
            "postprocess_failed",
        ),
    };

    record.insert("total_pages".into(), total);
    record.insert("billable_pages".into(), json!(billable));
    record.insert("qwen_extracted_pages".into(), json!(extracted));
    record.insert("qwen_failed_pages".into(), json!(failed_count));
    record.insert("qwen_skipped_pages".into(), json!(skipped));
    record.insert("field_count".into(), json!(field_count));
    record.insert("empty_result".into(), json!(field_count == 0));
    record.insert("status".into(), json!(status));

    page_logger::append_log(&mut record).await;
}

fn null_if_zero(n: i64) -> Value {
    if n == 0 {
        Value::Null
    } else {
        json!(n)
    }
}

async fn db_recover_stale(
    pool: &PgPool,
    stage: &str,
    minutes: i64,
) -> augocr_common::error::AppResult<i64> {
    augocr_common::db::recover_stale_jobs(pool, stage, minutes).await
}

/// Sleep, or return early when shutdown is requested. `true` = shutting down.
async fn sleep_or_shutdown(duration: Duration, shutdown: &CancellationToken) -> bool {
    tokio::select! {
        () = tokio::time::sleep(duration) => false,
        () = shutdown.cancelled() => true,
    }
}

/// Stop the loop on SIGINT/SIGTERM so an in-flight job finishes first.
fn spawn_signal_handler(shutdown: CancellationToken) {
    tokio::spawn(async move {
        #[cfg(unix)]
        {
            use tokio::signal::unix::{signal, SignalKind};
            let mut term = match signal(SignalKind::terminate()) {
                Ok(s) => s,
                Err(e) => {
                    tracing::warn!("could not install SIGTERM handler: {e}");
                    return;
                }
            };
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = term.recv() => {}
            }
        }
        #[cfg(not(unix))]
        {
            if let Err(e) = tokio::signal::ctrl_c().await {
                tracing::warn!("could not listen for shutdown signal: {e}");
                return;
            }
        }
        tracing::info!("Shutdown signal received — finishing current job then exiting");
        shutdown.cancel();
    });
}

fn validate_stage(stage: &str) -> anyhow::Result<()> {
    const STAGES: [&str; 4] = ["normalize", "ocr", "llm", "postprocess"];
    if STAGES.contains(&stage) {
        Ok(())
    } else {
        anyhow::bail!("Unknown worker stage: {stage} (expected one of {STAGES:?})")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_four_pipeline_stages_are_accepted() {
        for stage in ["normalize", "ocr", "llm", "postprocess"] {
            assert!(validate_stage(stage).is_ok(), "{stage} should be valid");
        }
        assert!(validate_stage("").is_err());
        assert!(validate_stage("ocr2").is_err());
    }

    #[test]
    fn null_if_zero_distinguishes_unknown_from_none() {
        // A zero page count means "we never found out", which the billing log
        // records as null rather than a confident zero.
        assert_eq!(null_if_zero(0), Value::Null);
        assert_eq!(null_if_zero(3), json!(3));
    }

    #[tokio::test]
    async fn sleep_returns_immediately_when_already_shut_down() {
        let token = CancellationToken::new();
        token.cancel();
        let started = Instant::now();
        assert!(sleep_or_shutdown(Duration::from_secs(30), &token).await);
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[tokio::test]
    async fn sleep_completes_normally_when_not_cancelled() {
        let token = CancellationToken::new();
        assert!(!sleep_or_shutdown(Duration::from_millis(10), &token).await);
    }
}
