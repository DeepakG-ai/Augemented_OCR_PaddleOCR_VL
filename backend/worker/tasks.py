import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime

import redis as sync_redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from worker.celery_app import celery_app
from worker.inference.pdf_utils import pdf_to_numpy_pages, image_bytes_to_numpy
from worker.inference.layout import run_layout_analysis, format_layout_summary
from worker.inference.vqa_client import run_vqa
from services.storage import fetch_from_minio
from core.config import settings
from models.document import Document
from models.extraction_result import ExtractionResult
from models.semantic_template import SemanticTemplate

logger = logging.getLogger(__name__)

# Sync engine for Celery worker (Celery doesn't support async natively)
_sync_engine = None
_sync_session_factory = None
_redis_client = None


def _get_sync_session() -> Session:
    global _sync_engine, _sync_session_factory
    if _sync_engine is None:
        _sync_engine = create_engine(settings.SYNC_DATABASE_URL, pool_pre_ping=True)
        _sync_session_factory = sessionmaker(bind=_sync_engine)
    return _sync_session_factory()


@contextmanager
def _db_session():
    """Context manager for database sessions with proper cleanup."""
    session = _get_sync_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = sync_redis.from_url(settings.REDIS_URL)
    return _redis_client


def _update_document_status(document_id: str, status: str, completed: bool = False):
    """Update document status in DB."""
    with _db_session() as session:
        doc = session.query(Document).filter(Document.id == uuid.UUID(document_id)).first()
        if doc:
            doc.status = status
            if completed:
                doc.completed_at = datetime.utcnow()


def _save_extraction_result(
    document_id: str,
    vendor_id: str | None,
    raw_data: dict,
    candidate_log: dict | None = None,
):
    """Save extraction result to DB."""
    with _db_session() as session:
        result = ExtractionResult(
            id=uuid.uuid4(),
            document_id=uuid.UUID(document_id),
            vendor_id=uuid.UUID(vendor_id) if vendor_id else None,
            raw_data=raw_data,
            candidate_log=candidate_log,
            is_verified=False,
        )
        session.add(result)


def _upsert_semantic_template(
    vendor_id: str,
    field_name: str,
    semantic_prompt: str,
    norm_x: float,
    norm_y: float,
    page_index: int | None = None,
):
    """Upsert a semantic template in the DB (sync version for worker)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy import func

    with _db_session() as session:
        stmt = pg_insert(SemanticTemplate).values(
            id=uuid.uuid4(),
            vendor_id=uuid.UUID(vendor_id),
            field_name=field_name,
            semantic_prompt=semantic_prompt,
            norm_x=norm_x,
            norm_y=norm_y,
            page_index=page_index,
            sample_count=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=['vendor_id', 'field_name'],
            set_={
                "semantic_prompt": stmt.excluded.semantic_prompt,
                "norm_x": stmt.excluded.norm_x,
                "norm_y": stmt.excluded.norm_y,
                "page_index": stmt.excluded.page_index,
                "sample_count": SemanticTemplate.sample_count + 1,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)
        logger.info(f"Upserted template: {vendor_id}/{field_name} page_index={page_index}")


def _publish_job_result(
    document_id: str,
    status: str,
    data: dict | None = None,
    candidates: dict | None = None,
    error: str | None = None,
):
    """Publish job result via Redis pub/sub for WebSocket delivery."""
    r = _get_redis()
    message = {
        "status": status,
        "job_id": document_id,
    }
    if data is not None:
        message["data"] = data
    if candidates is not None:
        message["candidates"] = candidates
    if error is not None:
        message["error"] = error

    r.publish(f"job:{document_id}", json.dumps(message))
    logger.info(f"Published job result: {document_id} → {status}")


@celery_app.task(bind=True, max_retries=3, queue="ocr", name="worker.tasks.process_document_vqa")
def process_document_vqa(
    self,
    document_id: str,
    s3_key: str,
    anchors: list[dict],
    save_rules: bool = True,
):
    """
    Core extraction task — multi-page with page_index resolution.

    Uses a single event loop for all async vLLM calls within the task,
    avoiding the RuntimeError from repeated asyncio.run() calls.

    anchors shape:
    [
      {
        "field": "invoice_total",
        "prompt": "Find the currency value associated with...",
        "x_pct": 0.85,
        "y_pct": 0.90,
        "page_index": null,    ← null = search all pages (first-time)
        "vendor_id": "uuid"    ← int  = go directly to this page (zero-touch)
      }
    ]
    """
    # Create a single event loop for the entire task — reused for all async calls
    loop = asyncio.new_event_loop()

    try:
        _update_document_status(document_id, "processing")
        _publish_job_result(document_id, "processing")

        # ── Step 1: Fetch file from MinIO ─────────────────────────────
        file_bytes, filename = fetch_from_minio(s3_key)
        logger.info(f"Fetched file from MinIO: {filename} ({len(file_bytes)} bytes)")

        # ── Step 2: Burst to pages ────────────────────────────────────
        if filename.lower().endswith(".pdf"):
            pages = pdf_to_numpy_pages(file_bytes, dpi=300)
        else:
            pages = image_bytes_to_numpy(file_bytes)
        logger.info(f"Document has {len(pages)} page(s)")

        final_results = {}
        candidate_log = {}  # populated when multiple candidates found → triggers needs_review

        # ── Step 3: Extract each field ────────────────────────────────
        for anchor in anchors:
            field = anchor["field"]
            known_page = anchor.get("page_index")  # None or int

            # ── Zero-touch path: page is known, go direct ─────────────
            if known_page is not None:
                if known_page < len(pages):
                    page_img = pages[known_page]
                    layout_map = run_layout_analysis(page_img)
                    layout_summary = format_layout_summary(layout_map)
                    value = loop.run_until_complete(run_vqa(page_img, anchor, layout_summary))
                    final_results[field] = value
                else:
                    logger.warning(f"Page index {known_page} out of range for {field}")
                    final_results[field] = None
                continue  # done — no page loop

            # ── First-time path: search ALL pages, collect ALL hits ───
            # Rule #4: Do NOT break on first non-null.
            candidates = []

            for page_idx, page_img in enumerate(pages):
                layout_map = run_layout_analysis(page_img)
                layout_summary = format_layout_summary(layout_map)
                value = loop.run_until_complete(run_vqa(page_img, anchor, layout_summary))

                if value is not None:
                    candidates.append({
                        "value": value,
                        "page_index": page_idx,
                    })
                # No break — always search all pages on first-time path

            if not candidates:
                final_results[field] = None

            elif len(candidates) == 1:
                # Unambiguous — auto-accept, record page
                final_results[field] = candidates[0]["value"]
                anchor["resolved_page_index"] = candidates[0]["page_index"]

            else:
                # Multiple candidates across pages — heuristic: take the LAST occurrence
                # Rationale: summary totals almost always appear on the last page
                best = candidates[-1]
                final_results[field] = best["value"]
                anchor["resolved_page_index"] = best["page_index"]
                candidate_log[field] = candidates  # flag for human review

        # ── Step 4: Persist results ───────────────────────────────────
        needs_review = bool(candidate_log)
        vendor_id = anchors[0].get("vendor_id") if anchors else None

        _save_extraction_result(
            document_id=document_id,
            vendor_id=vendor_id,
            raw_data=final_results,
            candidate_log=candidate_log if needs_review else None,
        )

        # ── Step 5: Upsert semantic templates (zero-touch flywheel) ───
        if save_rules and vendor_id:
            for anchor in anchors:
                resolved_page = anchor.get("resolved_page_index")
                _upsert_semantic_template(
                    vendor_id=anchor.get("vendor_id", vendor_id),
                    field_name=anchor["field"],
                    semantic_prompt=anchor["prompt"],
                    norm_x=anchor["x_pct"],
                    norm_y=anchor["y_pct"],
                    page_index=resolved_page,  # None if still ambiguous
                )

        # ── Step 6: Notify frontend via Redis pub/sub ─────────────────
        status = "needs_review" if needs_review else "completed"
        _update_document_status(document_id, status, completed=True)
        _publish_job_result(
            document_id,
            status,
            data=final_results,
            candidates=candidate_log if needs_review else None,
        )

        logger.info(f"Task completed: {document_id} → {status}")

    except Exception as exc:
        logger.error(f"Task failed: {document_id} — {exc}")
        try:
            raise self.retry(exc=exc, countdown=2 ** self.request.retries)
        except self.MaxRetriesExceededError:
            _update_document_status(document_id, "failed")
            _publish_job_result(document_id, "failed", error=str(exc))
            logger.error(f"Max retries exhausted for {document_id}")

    finally:
        loop.close()
