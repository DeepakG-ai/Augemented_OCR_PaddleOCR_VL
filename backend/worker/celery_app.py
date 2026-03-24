import os
import sys

# Ensure /app (backend root) is on Python path for sibling package imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from celery import Celery
from core.config import settings

celery_app = Celery(
    "ocr_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "worker.tasks.process_document_vqa": {"queue": "ocr"},
    },
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # Task time limits (5 minutes hard, 4 minutes soft)
    task_time_limit=300,
    task_soft_time_limit=240,
    # Result expiration (1 day)
    result_expires=86400,
)

# Auto-discover tasks
celery_app.autodiscover_tasks(["worker"])
