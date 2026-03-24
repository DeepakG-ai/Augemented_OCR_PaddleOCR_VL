import logging
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router as api_router
from api.websockets import router as ws_router
from services.storage import get_minio_client

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan — startup and shutdown.
    Rule #12: Alembic migrations run on API startup.
    Rule #11: MinIO bucket auto-created on first init.
    """
    # ── Startup ──────────────────────────────────────────────────────
    logger.info("Running Alembic migrations...")
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Alembic migrations completed successfully")
        else:
            logger.error(f"Alembic migration failed: {result.stderr}")
    except Exception as e:
        logger.error(f"Failed to run Alembic migrations: {e}")

    # Ensure MinIO bucket exists
    logger.info("Ensuring MinIO bucket exists...")
    try:
        get_minio_client()
        logger.info("MinIO client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize MinIO: {e}")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info("Application shutting down")


app = FastAPI(
    title="Augmented OCR — Semantic VLM Extraction",
    description="Extract structured data from invoices, receipts, and scanned documents using Vision-Language Models.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router)
app.include_router(ws_router)


@app.get("/")
async def root():
    return {
        "service": "Augmented OCR — Semantic VLM Extraction",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
