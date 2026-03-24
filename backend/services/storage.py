import io
import logging
import re
import threading
from minio import Minio
from minio.error import S3Error
from core.config import settings

logger = logging.getLogger(__name__)

_client: Minio | None = None
_client_lock = threading.Lock()


def _validate_s3_key(s3_key: str) -> bool:
    """Validate s3_key format to prevent path traversal."""
    if not s3_key:
        return False
    # Block path traversal sequences
    if '..' in s3_key:
        return False
    # Block null bytes
    if '\x00' in s3_key:
        return False
    return True


def get_minio_client() -> Minio:
    """Get or create MinIO client singleton (thread-safe)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = Minio(
                    settings.MINIO_ENDPOINT,
                    access_key=settings.MINIO_ACCESS_KEY,
                    secret_key=settings.MINIO_SECRET_KEY,
                    secure=settings.MINIO_SECURE,
                )
                _ensure_bucket()
    return _client


def _ensure_bucket():
    """Auto-create bucket on first use — Rule #11."""
    client = _client
    bucket = settings.MINIO_BUCKET
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info(f"Created MinIO bucket: {bucket}")
        else:
            logger.info(f"MinIO bucket already exists: {bucket}")
    except S3Error as e:
        logger.error(f"Failed to ensure MinIO bucket: {e}")
        raise


def upload_file(s3_key: str, file_bytes: bytes, content_type: str) -> str:
    """Upload file bytes to MinIO. Returns the s3_key."""
    if not _validate_s3_key(s3_key):
        raise ValueError(f"Invalid s3_key format: {s3_key}")

    client = get_minio_client()
    data = io.BytesIO(file_bytes)
    client.put_object(
        settings.MINIO_BUCKET,
        s3_key,
        data,
        length=len(file_bytes),
        content_type=content_type,
    )
    logger.info(f"Uploaded to MinIO: {s3_key} ({len(file_bytes)} bytes)")
    return s3_key


def download_file(s3_key: str) -> bytes:
    """Download file bytes from MinIO."""
    if not _validate_s3_key(s3_key):
        raise ValueError(f"Invalid s3_key format: {s3_key}")

    client = get_minio_client()
    response = client.get_object(settings.MINIO_BUCKET, s3_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def presign_url(s3_key: str, expires_hours: int = 1) -> str:
    """Generate a presigned URL for temporary access."""
    from datetime import timedelta

    if not _validate_s3_key(s3_key):
        raise ValueError(f"Invalid s3_key format: {s3_key}")

    client = get_minio_client()
    return client.presigned_get_object(
        settings.MINIO_BUCKET,
        s3_key,
        expires=timedelta(hours=expires_hours),
    )


def fetch_from_minio(s3_key: str) -> tuple[bytes, str]:
    """Fetch file from MinIO. Returns (file_bytes, filename)."""
    if not _validate_s3_key(s3_key):
        raise ValueError(f"Invalid s3_key format: {s3_key}")

    file_bytes = download_file(s3_key)
    filename = s3_key.split("/")[-1] if "/" in s3_key else s3_key
    return file_bytes, filename
