"""
object_store.py -- MinIO-backed object storage helpers.

Artifacts are stored outside Postgres and referenced by object key.
"""
from __future__ import annotations

import io
from functools import lru_cache

from .config import (
    MINIO_DOCUMENTS_BUCKET as DOCUMENTS_BUCKET,
    MINIO_ARTIFACTS_BUCKET as ARTIFACTS_BUCKET,
    MINIO_ENDPOINT,
    MINIO_ACCESS_KEY,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
    LOCAL_OBJECT_STORE_DIR,
)


class StorageUnavailableError(OSError):
    """Raised when the object store backend is unreachable or returns an unexpected error."""


class ObjectNotFoundError(FileNotFoundError):
    """Raised when the requested object does not exist in the store."""


class StoragePermissionError(PermissionError):
    """Raised when the store rejects the operation due to access permissions."""


import logging as _log
_store_log = _log.getLogger(__name__)


class ObjectStore:
    def __init__(self) -> None:
        endpoint = MINIO_ENDPOINT
        access_key = MINIO_ACCESS_KEY
        secret_key = MINIO_SECRET_KEY
        secure = MINIO_SECURE
        self._local_root = LOCAL_OBJECT_STORE_DIR
        try:
            from minio import Minio
        except ModuleNotFoundError:
            self.client = None
            self._endpoint = None
        else:
            try:
                self.client = Minio(
                    endpoint,
                    access_key=access_key,
                    secret_key=secret_key,
                    secure=secure,
                )
            except Exception as exc:
                _store_log.warning(
                    "MinIO client init failed (%s) — falling back to local storage", exc
                )
                self.client = None

    @staticmethod
    def _validate_object_key(object_key: str) -> None:
        """Validate object_key to prevent path traversal attacks."""
        if not object_key:
            raise ValueError("object_key cannot be empty")
        if ".." in object_key or object_key.startswith("/"):
            raise ValueError(f"Invalid object_key: path traversal detected")
        if "\\" in object_key:
            raise ValueError(f"Invalid object_key: backslashes not allowed")

    def ensure_buckets(self) -> None:
        if self.client is None:
            for bucket in (DOCUMENTS_BUCKET, ARTIFACTS_BUCKET):
                (self._local_root / bucket).mkdir(parents=True, exist_ok=True)
            return
        for bucket in (DOCUMENTS_BUCKET, ARTIFACTS_BUCKET):
            try:
                if not self.client.bucket_exists(bucket):
                    self.client.make_bucket(bucket)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                if code in ("AccessDenied", "Forbidden"):
                    raise StoragePermissionError(
                        f"Object store denied access to bucket {bucket!r}: {exc}"
                    ) from exc
                raise StorageUnavailableError(
                    f"Object store unreachable while checking bucket {bucket!r}: {exc}"
                ) from exc

    def put_bytes(self, bucket: str, object_key: str, data: bytes, content_type: str) -> None:
        self._validate_object_key(object_key)
        if self.client is None:
            target = self._local_root / bucket / object_key
            # Resolve to absolute path and ensure it's within the local root
            target = target.resolve()
            if not str(target).startswith(str(self._local_root.resolve())):
                raise ValueError("object_key attempts to escape storage directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(data)
            except OSError as exc:
                raise OSError(
                    f"Failed to write object key={object_key!r} to {target}: {exc}"
                ) from exc
            return
        payload = io.BytesIO(data)
        try:
            self.client.put_object(
                bucket,
                object_key,
                payload,
                length=len(data),
                content_type=content_type,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code in ("AccessDenied", "Forbidden"):
                raise StoragePermissionError(
                    f"Object store denied write: key={object_key!r} bucket={bucket!r}"
                ) from exc
            raise StorageUnavailableError(
                f"Object store write failed: key={object_key!r} bucket={bucket!r}: {exc}"
            ) from exc

    def get_bytes(self, bucket: str, object_key: str) -> bytes:
        self._validate_object_key(object_key)
        if self.client is None:
            target = self._local_root / bucket / object_key
            target = target.resolve()
            if not str(target).startswith(str(self._local_root.resolve())):
                raise ValueError("object_key attempts to escape storage directory")
            try:
                return target.read_bytes()
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"Object not found: key={object_key!r} bucket={bucket!r} path={target}"
                ) from exc
            except OSError as exc:
                raise OSError(
                    f"Failed to read object: key={object_key!r} bucket={bucket!r} path={target}: {exc}"
                ) from exc
        try:
            response = self.client.get_object(bucket, object_key)
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFoundError(
                    f"Object not found: key={object_key!r} bucket={bucket!r}"
                ) from exc
            if code in ("AccessDenied", "Forbidden"):
                raise StoragePermissionError(
                    f"Object store denied read: key={object_key!r} bucket={bucket!r}"
                ) from exc
            raise StorageUnavailableError(
                f"Object store read failed: key={object_key!r} bucket={bucket!r}: {exc}"
            ) from exc
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete_object(self, bucket: str, object_key: str) -> None:
        """Delete an object if present. Missing objects are ignored."""
        self._validate_object_key(object_key)
        if self.client is None:
            target = (self._local_root / bucket / object_key).resolve()
            if not str(target).startswith(str(self._local_root.resolve())):
                raise ValueError("object_key attempts to escape storage directory")
            if target.exists():
                target.unlink()
            return
        try:
            self.client.remove_object(bucket, object_key)
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            if code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                return
            raise


@lru_cache(maxsize=1)
def get_store() -> ObjectStore:
    store = ObjectStore()
    store.ensure_buckets()
    return store
