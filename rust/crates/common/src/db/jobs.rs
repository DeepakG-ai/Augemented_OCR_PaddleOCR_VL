//! jobs.rs — the Postgres job queue (db.py "Job queue" section).
//!
//! enqueue/ensure/claim/update-progress/complete/cancel/fail/recover plus the
//! latest-per-extraction lookups. `claim_job` keeps Python's exact
//! `FOR UPDATE SKIP LOCKED` semantics: one statement selects a queued row,
//! locks it, flips it to 'running' and returns it atomically, so concurrent
//! workers can never grab the same job.

use serde_json::{json, Value};
use sqlx::PgPool;

use super::util;
use crate::error::AppResult;

/// Verbatim job column projection used by every plain SELECT in db.py.
const JOB_COLS: &str = "id, extraction_id, document_id, job_type, status, payload, progress,
        attempts, max_attempts, priority, locked_by, locked_at,
        started_at, finished_at, error, created_at, updated_at";

/// Wrap an INSERT/UPDATE..RETURNING so its single result row arrives as one
/// jsonb payload (the DML sits as a top-level CTE member, which Postgres
/// requires).
fn returning_row_sql(statement: &str) -> String {
    format!("WITH _r AS ({statement}) SELECT to_jsonb(_r.*) AS row FROM _r")
}

pub async fn get_job(pool: &PgPool, job_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query(
        "SELECT id, extraction_id, document_id, job_type, status, payload, progress,
                attempts, max_attempts, priority, locked_by, locked_at,
                started_at, finished_at, error, created_at, updated_at
         FROM jobs
         WHERE id = $1",
    );
    let rec = sqlx::query(&sql)
        .bind(job_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Insert a queued job; returns None when an active job already exists for
/// (extraction_id, job_type) per the partial unique index.
pub async fn enqueue_job(
    pool: &PgPool,
    extraction_id: Option<i64>,
    document_id: Option<i64>,
    job_type: &str,
    payload: Option<&Value>,
    priority: i32,
    max_attempts: i32,
) -> AppResult<Option<Value>> {
    let payload_owned = payload.cloned().unwrap_or_else(|| json!({}));
    let sql = returning_row_sql(&format!(
        "INSERT INTO jobs (extraction_id, document_id, job_type, payload, priority, max_attempts)
         VALUES ($1, $2, $3, $4::jsonb, $5, $6)
         ON CONFLICT (extraction_id, job_type) WHERE status IN ('queued', 'running') AND extraction_id IS NOT NULL
         DO NOTHING
         RETURNING {JOB_COLS}"
    ));
    let rec = sqlx::query(&sql)
        .bind(extraction_id.map(|v| v as i32))
        .bind(document_id.map(|v| v as i32))
        .bind(job_type)
        .bind(payload_owned)
        .bind(priority)
        .bind(max_attempts)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// True when a queued or running job exists for this extraction+type.
pub async fn has_active_job(pool: &PgPool, extraction_id: i64, job_type: &str) -> AppResult<bool> {
    let value: bool = sqlx::query_scalar(
        "SELECT EXISTS(
            SELECT 1
            FROM jobs
            WHERE extraction_id = $1
              AND job_type = $2
              AND status IN ('queued', 'running')
        )",
    )
    .bind(extraction_id as i32)
    .bind(job_type)
    .fetch_one(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    Ok(value)
}

/// Enqueue unless a job of this type is already active for the extraction.
pub async fn ensure_job(
    pool: &PgPool,
    extraction_id: Option<i64>,
    document_id: Option<i64>,
    job_type: &str,
    payload: Option<&Value>,
    priority: i32,
    max_attempts: i32,
) -> AppResult<Option<Value>> {
    if let Some(eid) = extraction_id {
        if has_active_job(pool, eid, job_type).await? {
            return Ok(None);
        }
    }
    enqueue_job(pool, extraction_id, document_id, job_type, payload, priority, max_attempts).await
}

/// Atomically claim the oldest queued job of this type: marks it running on
/// behalf of `worker_name` via FOR UPDATE SKIP LOCKED, bumping attempts.
pub async fn claim_job(pool: &PgPool, job_type: &str, worker_name: &str) -> AppResult<Option<Value>> {
    let sql = "WITH candidate AS (
            SELECT id
            FROM jobs
            WHERE job_type = $1
              AND status = 'queued'
            ORDER BY priority ASC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        ),
        _r AS (
            UPDATE jobs j
            SET status = 'running',
                locked_by = $2,
                locked_at = NOW(),
                started_at = COALESCE(started_at, NOW()),
                attempts = attempts + 1,
                updated_at = NOW()
            FROM candidate
            WHERE j.id = candidate.id
            RETURNING j.id, j.extraction_id, j.document_id, j.job_type, j.status, j.payload, j.progress,
                      j.attempts, j.max_attempts, j.priority, j.locked_by, j.locked_at,
                      j.started_at, j.finished_at, j.error, j.created_at, j.updated_at
        )
        SELECT to_jsonb(_r.*) AS row FROM _r";
    let rec = sqlx::query(sql)
        .bind(job_type)
        .bind(worker_name)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Overwrite the progress payload of a running job.
pub async fn update_job_progress(pool: &PgPool, job_id: i64, progress: &Value) -> AppResult<()> {
    sqlx::query(
        "UPDATE jobs
         SET progress = $2::jsonb,
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(job_id)
    .bind(progress)
    .execute(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    Ok(())
}

/// Mark a job done; keeps the previous progress payload when none is given.
pub async fn complete_job(pool: &PgPool, job_id: i64, progress: Option<&Value>) -> AppResult<()> {
    sqlx::query(
        "UPDATE jobs
         SET status = 'done',
             progress = COALESCE($2::jsonb, progress),
             finished_at = NOW(),
             updated_at = NOW(),
             error = NULL
         WHERE id = $1",
    )
    .bind(job_id)
    .bind(progress)
    .execute(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    Ok(())
}

/// Cancel a job, preserving any prior error message.
pub async fn cancel_job(
    pool: &PgPool,
    job_id: i64,
    progress: Option<&Value>,
    error: Option<&str>,
) -> AppResult<()> {
    sqlx::query(
        "UPDATE jobs
         SET status = 'cancelled',
             progress = COALESCE($2::jsonb, progress),
             finished_at = NOW(),
             updated_at = NOW(),
             error = COALESCE($3, error, 'Cancelled')
         WHERE id = $1",
    )
    .bind(job_id)
    .bind(progress)
    .bind(error)
    .execute(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    Ok(())
}

/// Requeue a failed job when attempts remain, else mark it failed permanently.
pub async fn fail_job(pool: &PgPool, job_id: i64, error: &str, retryable: bool) -> AppResult<()> {
    let attempts_sql = util::row_query("SELECT attempts, max_attempts FROM jobs WHERE id = $1");
    let rec = sqlx::query(&attempts_sql)
        .bind(job_id)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let Some(row) = rec.map(util::row_value) else {
        return Ok(());
    };
    let attempts = row.get("attempts").map(util::int_or_zero).unwrap_or(0);
    let max_attempts = row.get("max_attempts").map(util::int_or_zero).unwrap_or(0);
    let next_status = if retryable && attempts < max_attempts {
        "queued"
    } else {
        "failed"
    };
    sqlx::query(
        "UPDATE jobs
         SET status = $2,
             error = $3,
             locked_by = NULL,
             locked_at = NULL,
             finished_at = CASE WHEN $2 = 'failed' THEN NOW() ELSE finished_at END,
             updated_at = NOW()
         WHERE id = $1",
    )
    .bind(job_id)
    .bind(next_status)
    .bind(error)
    .execute(pool)
    .await
    .map_err(crate::error::AppError::from)?;
    Ok(())
}

/// Reset running jobs stuck longer than stale_minutes back to queued (or
/// failed if exhausted). Called on worker startup and periodically in the poll
/// loop to reclaim orphaned jobs left by crashed workers. Also purges expired
/// idempotency claims. Returns the number of jobs recovered.
///
/// For exhausted OCR jobs the parent extraction gets a review-unavailable
/// progress note and a postprocess job is enqueued when the LLM JSON already
/// succeeded; for other stages the extraction itself is marked failed.
pub async fn recover_stale_jobs(pool: &PgPool, stage: &str, stale_minutes: i64) -> AppResult<i64> {
    match sqlx::query("DELETE FROM idempotency_claims WHERE created_at < NOW() - INTERVAL '24 hours'")
        .execute(pool)
        .await
    {
        Ok(_) => {}
        Err(exc) => {
            tracing::warn!(stage, error = %exc, "recover_stale_jobs: idempotency prune failed");
        }
    }

    let mut tx = pool.begin().await.map_err(crate::error::AppError::from)?;
    let sql = returning_row_sql(
        "UPDATE jobs
         SET status     = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
             locked_by  = NULL,
             locked_at  = NULL,
             error      = CASE
                              WHEN attempts >= max_attempts
                              THEN COALESCE(error, 'Worker crash — max attempts reached')
                              ELSE 'Worker crash — requeued'
                          END,
             updated_at = NOW()
         WHERE job_type = $1
           AND status   = 'running'
           AND updated_at < NOW() - ($2 * INTERVAL '1 minute')
         RETURNING id, extraction_id, status",
    );
    let rows = sqlx::query(&sql)
        .bind(stage)
        .bind(stale_minutes as f64)
        .fetch_all(&mut *tx)
        .await
        .map_err(crate::error::AppError::from)?;

    let recovered = rows.len() as i64;
    let failed_ext_ids: Vec<i32> = rows
        .into_iter()
        .map(util::row_value)
        .filter(|r| r.get("status").and_then(Value::as_str) == Some("failed"))
        .filter_map(|r| r.get("extraction_id").and_then(Value::as_i64))
        .map(|id| id as i32)
        .collect();

    if !failed_ext_ids.is_empty() {
        if stage == "ocr" {
            sqlx::query(
                "UPDATE extractions
                 SET progress = jsonb_build_object(
                         'stage', 'ocr',
                         'message', 'JSON extraction may still complete, but OCR-backed review is unavailable.',
                         'review_available', false,
                         'ocr_error', 'Worker crash - max attempts reached'
                     ),
                     updated_at = NOW()
                 WHERE id = ANY($1::int[])
                   AND status NOT IN ('done', 'failed', 'partial', 'cancelled')",
            )
            .bind(&failed_ext_ids)
            .execute(&mut *tx)
            .await
            .map_err(crate::error::AppError::from)?;
            sqlx::query(
                "INSERT INTO jobs (extraction_id, document_id, job_type, payload)
                 SELECT e.id, e.document_id, 'postprocess', jsonb_build_object('extraction_id', e.id)
                 FROM extractions e
                 WHERE e.id = ANY($1::int[])
                   AND e.status = 'processing'
                   AND e.result IS NOT NULL
                 ON CONFLICT (extraction_id, job_type)
                 WHERE status IN ('queued', 'running') AND extraction_id IS NOT NULL
                 DO NOTHING",
            )
            .bind(&failed_ext_ids)
            .execute(&mut *tx)
            .await
            .map_err(crate::error::AppError::from)?;
            tx.commit().await.map_err(crate::error::AppError::from)?;
            return Ok(recovered);
        }
        sqlx::query(
            "UPDATE extractions
             SET status     = 'failed',
                 error      = 'Worker crash — max attempts reached',
                 progress   = '{\"stage\":\"failed\",\"message\":\"Worker crash — max attempts reached\"}'::jsonb,
                 updated_at = NOW()
             WHERE id = ANY($1::int[])
               AND status NOT IN ('done', 'failed', 'partial', 'cancelled')",
        )
        .bind(&failed_ext_ids)
        .execute(&mut *tx)
        .await
        .map_err(crate::error::AppError::from)?;
    }
    tx.commit().await.map_err(crate::error::AppError::from)?;
    Ok(recovered)
}

/// Cancel queued jobs outright and flag running ones as cancelling.
/// Returns counts: cancelled, cancelling, updated.
pub async fn cancel_jobs_for_extraction(pool: &PgPool, extraction_id: i64) -> AppResult<Value> {
    let sql = returning_row_sql(
        "UPDATE jobs
         SET status = CASE
                 WHEN status = 'queued' THEN 'cancelled'
                 WHEN status = 'running' THEN 'cancelling'
                 ELSE status
             END,
             error = CASE
                 WHEN status = 'queued' THEN 'Cancelled before execution'
                 WHEN status = 'running' THEN 'Cancellation requested during execution'
                 ELSE error
             END,
             finished_at = CASE WHEN status = 'queued' THEN NOW() ELSE finished_at END,
             updated_at = NOW()
         WHERE extraction_id = $1
           AND status IN ('queued', 'running')
         RETURNING status",
    );
    let rows = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    let updated = rows.len() as i64;
    let mut cancelled = 0i64;
    let mut cancelling = 0i64;
    for rec in rows {
        match util::row_value(rec).get("status").and_then(Value::as_str) {
            Some("cancelled") => cancelled += 1,
            Some("cancelling") => cancelling += 1,
            _ => {}
        }
    }
    Ok(json!({
        "cancelled": cancelled,
        "cancelling": cancelling,
        "updated": updated,
    }))
}

/// Most recent job for an extraction regardless of type.
pub async fn get_latest_job_for_extraction(pool: &PgPool, extraction_id: i64) -> AppResult<Option<Value>> {
    let sql = util::row_query(&format!(
        "SELECT {JOB_COLS}
         FROM jobs
         WHERE extraction_id = $1
         ORDER BY created_at DESC
         LIMIT 1"
    ));
    let rec = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// Most recent job of one type for an extraction (newest created_at, then id).
pub async fn get_latest_job_for_extraction_type(
    pool: &PgPool,
    extraction_id: i64,
    job_type: &str,
) -> AppResult<Option<Value>> {
    let sql = util::row_query(&format!(
        "SELECT {JOB_COLS}
         FROM jobs
         WHERE extraction_id = $1
           AND job_type = $2
         ORDER BY created_at DESC, id DESC
         LIMIT 1"
    ));
    let rec = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .bind(job_type)
        .fetch_optional(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rec.map(util::row_value))
}

/// True when the latest job of this type ended in failure.
pub async fn latest_job_failed(pool: &PgPool, extraction_id: i64, job_type: &str) -> AppResult<bool> {
    Ok(match get_latest_job_for_extraction_type(pool, extraction_id, job_type).await? {
        Some(job) => job.get("status").and_then(Value::as_str) == Some("failed"),
        None => false,
    })
}

/// All jobs for an extraction, oldest first.
pub async fn list_jobs_for_extraction(pool: &PgPool, extraction_id: i64) -> AppResult<Vec<Value>> {
    let sql = util::row_query(&format!(
        "SELECT {JOB_COLS}
         FROM jobs
         WHERE extraction_id = $1
         ORDER BY created_at ASC"
    ));
    let rows = sqlx::query(&sql)
        .bind(extraction_id as i32)
        .fetch_all(pool)
        .await
        .map_err(crate::error::AppError::from)?;
    Ok(rows.into_iter().map(util::row_value).collect())
}
