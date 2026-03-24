from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://ocr:ocr@postgres:5432/ocrdb"
    SYNC_DATABASE_URL: str = "postgresql://ocr:ocr@postgres:5432/ocrdb"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # MinIO
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "ocr-documents"
    MINIO_SECURE: bool = False

    # vLLM (local WSL2 via host.docker.internal)
    VLLM_SERVER_URL: str = "http://host.docker.internal:8001"
    VLLM_MODEL: str = "PaddlePaddle/PaddleOCR-VL-1.5"
    VLLM_TIMEOUT: int = 60

    # Celery
    CELERY_CONCURRENCY: int = 4

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
