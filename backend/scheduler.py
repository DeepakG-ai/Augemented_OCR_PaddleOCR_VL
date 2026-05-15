"""
scheduler.py — APScheduler 3.11.2 wrapper for per-user PDF ingestion schedules.

AsyncIOScheduler + SQLAlchemyJobStore (psycopg2). APScheduler manages its own
apscheduler_jobs table. Our user_schedules table stores user-facing metadata.
No circular imports: pool and ingest callback are injected via set_context().
"""
from __future__ import annotations

import logging
import pathlib
import shutil

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_ctx: dict = {}   # {"pool": pool, "ingest_cb": async callable}


def set_context(pool, ingest_cb) -> None:
    """Inject pool and ingest callback after app startup (avoids circular import)."""
    _ctx["pool"] = pool
    _ctx["ingest_cb"] = ingest_cb


def _sa_url(database_url: str) -> str:
    """Strip +asyncpg dialect so SQLAlchemy uses the psycopg2 driver."""
    return database_url.replace("+asyncpg", "")


def _job_id(schedule_id: int) -> str:
    return f"user_sched_{schedule_id}"


async def init_scheduler(database_url: str) -> AsyncIOScheduler:
    """Build, start, and return the scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    jobstores = {"default": SQLAlchemyJobStore(url=_sa_url(database_url))}
    _scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="UTC")
    _scheduler.start()
    logger.info("APScheduler started (SQLAlchemy jobstore)")
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
    _scheduler = None


def sync_job(row: dict) -> None:
    """Add or replace one APScheduler job from a user_schedules row."""
    if _scheduler is None:
        return
    jid = _job_id(row["id"])

    if not row.get("enabled", True):
        try:
            _scheduler.remove_job(jid)
        except Exception:
            pass
        return

    tz = row.get("timezone") or "UTC"
    parts = (row.get("cron_expr") or "").split()
    if len(parts) != 5:
        logger.warning("scheduler: invalid cron_expr=%r schedule_id=%s", row.get("cron_expr"), row["id"])
        return

    trigger = CronTrigger(
        minute=parts[0], hour=parts[1], day=parts[2],
        month=parts[3], day_of_week=parts[4],
        timezone=tz,
    )
    _scheduler.add_job(
        _run_schedule,
        trigger=trigger,
        id=jid,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        kwargs={"schedule_id": row["id"], "user_id": str(row["user_id"])},
        name=row.get("label") or f"schedule-{row['id']}",
    )
    logger.info("scheduler: synced job_id=%s cron=%s tz=%s", jid, row.get("cron_expr"), tz)


def remove_job(schedule_id: int) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(schedule_id))
    except Exception:
        pass


def get_next_run_times(schedule_id: int, count: int = 5) -> list[str]:
    if _scheduler is None:
        return []
    try:
        job = _scheduler.get_job(_job_id(schedule_id))
        if job is None or job.next_run_time is None:
            return []
        results = []
        t = job.next_run_time
        trig = job.trigger
        for _ in range(count):
            results.append(t.isoformat())
            t = trig.get_next_fire_time(t, t)
            if t is None:
                break
        return results
    except Exception:
        return []


def _move_pdf(src: pathlib.Path, dest_folder: str, label: str) -> None:
    """Move a PDF file to dest_folder. Creates the folder if needed. Logs but never raises."""
    try:
        if not dest_folder:
            return
        dest = pathlib.Path(dest_folder)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest / src.name))
        logger.info("scheduler: moved %s → %s (%s)", src.name, dest_folder, label)
    except Exception as exc:
        logger.warning("scheduler: failed to move %s to %s: %s", src.name, dest_folder, exc)


async def _run_schedule(schedule_id: int, user_id: str) -> None:
    """Called by APScheduler. Scans user's input_folder and ingests every PDF."""
    pool = _ctx.get("pool")
    ingest_cb = _ctx.get("ingest_cb")
    if pool is None or ingest_cb is None:
        logger.error("scheduler: context not set, cannot run schedule_id=%s", schedule_id)
        return

    try:
        from . import db as db_mod

        config = await db_mod.get_user_config(pool, user_id)
        input_folder = config.get("input_folder", "")
        if not input_folder:
            logger.info("scheduler: no input_folder user=%s schedule_id=%s", user_id, schedule_id)
            return

        folder = pathlib.Path(input_folder)
        if not folder.is_dir():
            logger.warning("scheduler: folder missing path=%s user=%s", input_folder, user_id)
            return

        pdfs = list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))
        if not pdfs:
            logger.info("scheduler: no PDFs found in %s schedule_id=%s", input_folder, schedule_id)
            return

        # Mark executing — prevents UI uploads from starting while we run
        await db_mod.set_schedule_executing(pool, schedule_id, True)

        success_folder = config.get("success_folder", "")
        failed_folder  = config.get("failed_folder",  "")

        ok = fail = 0
        for pdf in pdfs:
            try:
                await ingest_cb(user_id, str(pdf))
                _move_pdf(pdf, success_folder, "success")
                ok += 1
            except Exception as exc:
                logger.warning("scheduler: ingest failed for %s: %s", pdf.name, exc)
                _move_pdf(pdf, failed_folder, "failed")
                fail += 1

        await db_mod.mark_schedule_ran(pool, schedule_id)
        logger.info(
            "scheduler: fired schedule_id=%s user=%s pdfs=%d ok=%d fail=%d",
            schedule_id, user_id, len(pdfs), ok, fail,
        )
    except Exception as exc:
        logger.exception("scheduler: error schedule_id=%s: %s", schedule_id, exc)
    finally:
        try:
            from . import db as db_mod
            await db_mod.set_schedule_executing(pool, schedule_id, False)
        except Exception:
            pass


async def reload_all_schedules(pool) -> None:
    """Called at startup to re-register all enabled schedules from DB."""
    try:
        from . import db as db_mod
        rows = await db_mod.get_all_schedules_enabled(pool)
        for row in rows:
            sync_job(row)
        logger.info("scheduler: loaded %d enabled schedules from DB", len(rows))
    except Exception as exc:
        logger.warning("scheduler: failed to reload schedules: %s", exc)
