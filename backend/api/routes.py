import uuid
import logging
import re
import io
import fitz  # PyMuPDF
from fastapi import APIRouter, File, UploadFile, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db

# File upload limits
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
from models.vendor import Vendor
from models.document import Document
from models.extraction_result import ExtractionResult
from schemas.extract import ExtractionRequest, TemplateConfirmRequest
from schemas.vendor import VendorCreate, VendorResponse, SemanticTemplateResponse
from schemas.job import JobStatusResponse, JobResultResponse
from services.storage import upload_file as minio_upload, presign_url
from services.template_service import (
    get_vendor_templates,
    templates_to_anchors,
    upsert_semantic_template,
    delete_template,
    confirm_template_page,
)
from worker.tasks import process_document_vqa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "application/pdf",
}


# ── Upload ────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload file → MinIO, returns s3_key."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. "
                   f"Accepted: {', '.join(ALLOWED_CONTENT_TYPES)}",
        )

    file_bytes = await file.read()

    # Validate file size
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size: {MAX_FILE_SIZE / (1024 * 1024):.0f}MB",
        )

    s3_key = f"documents/{uuid.uuid4()}/{file.filename}"
    minio_upload(s3_key, file_bytes, file.content_type)

    return {
        "s3_key": s3_key,
        "filename": file.filename,
        "size": len(file_bytes),
        "presigned_url": presign_url(s3_key),
    }


# ── Extract ───────────────────────────────────────────────────────────

@router.post("/extract", status_code=202, response_model=JobStatusResponse)
async def extract(payload: ExtractionRequest, db: AsyncSession = Depends(get_db)):
    """
    Submit extraction job (202 Accepted).
    Rule #6: Returns 202 in < 200ms — never await inference.
    """
    vendor_id = payload.vendor_id
    anchors = [a.model_dump() for a in payload.anchors]

    # Check vendor exists
    vendor = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    if not vendor.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Zero-touch path: no anchors provided → use stored templates
    if not anchors:
        templates = await get_vendor_templates(db, vendor_id)
        if not templates:
            raise HTTPException(
                status_code=422,
                detail="No anchors provided and no stored templates for this vendor. "
                       "Annotation required for first document.",
            )
        anchors = await templates_to_anchors(templates)
    else:
        # Add vendor_id to each anchor
        for a in anchors:
            a["vendor_id"] = str(vendor_id)

    # Create document record
    doc = Document(
        id=uuid.uuid4(),
        vendor_id=vendor_id,
        s3_key=payload.s3_key,
        filename=payload.s3_key.split("/")[-1] if "/" in payload.s3_key else payload.s3_key,
        status="queued",
    )
    db.add(doc)
    await db.flush()

    # Push Celery task
    task = process_document_vqa.delay(
        document_id=str(doc.id),
        s3_key=payload.s3_key,
        anchors=anchors,
        save_rules=payload.save_rules,
    )

    doc.celery_task_id = task.id
    await db.commit()

    return JobStatusResponse(
        job_id=str(doc.id),
        document_id=doc.id,
        status="queued",
    )


# ── Job status ────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}", response_model=JobResultResponse)
async def get_job_status(job_id: str, db: AsyncSession = Depends(get_db)):
    """Poll job status + result."""
    # Find document by id (job_id = document_id for WS channel consistency)
    try:
        doc_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    result = await db.execute(
        select(Document).where(Document.id == doc_uuid)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")

    response = JobResultResponse(
        job_id=job_id,
        document_id=doc.id,
        status=doc.status,
    )

    # If completed or needs_review, include extraction data
    if doc.status in ("completed", "needs_review"):
        ext_result = await db.execute(
            select(ExtractionResult).where(ExtractionResult.document_id == doc.id)
        )
        ext = ext_result.scalar_one_or_none()
        if ext:
            response.data = ext.raw_data
            if ext.candidate_log:
                response.candidates = ext.candidate_log

    elif doc.status == "failed":
        response.error = "Extraction failed. Check worker logs."

    return response


# ── Vendors ───────────────────────────────────────────────────────────

@router.get("/vendors", response_model=list[VendorResponse])
async def list_vendors(db: AsyncSession = Depends(get_db)):
    """List all vendors."""
    result = await db.execute(select(Vendor).order_by(Vendor.name))
    return list(result.scalars().all())


@router.post("/vendors", response_model=VendorResponse, status_code=201)
async def create_vendor(payload: VendorCreate, db: AsyncSession = Depends(get_db)):
    """Create a new vendor."""
    # Check for duplicate
    existing = await db.execute(select(Vendor).where(Vendor.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Vendor already exists")

    vendor = Vendor(id=uuid.uuid4(), name=payload.name)
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    return vendor


# ── Vendor Templates ──────────────────────────────────────────────────

@router.get("/vendors/{vendor_id}/templates", response_model=list[SemanticTemplateResponse])
async def get_templates(vendor_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get all semantic templates for a vendor."""
    templates = await get_vendor_templates(db, vendor_id)
    return templates


@router.delete("/vendors/{vendor_id}/templates/{field_name}", status_code=204)
async def delete_vendor_template(
    vendor_id: uuid.UUID,
    field_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a specific template rule."""
    deleted = await delete_template(db, vendor_id, field_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return None


# ── Template Confirmation ─────────────────────────────────────────────

@router.post("/templates/confirm", response_model=SemanticTemplateResponse)
async def confirm_template(
    payload: TemplateConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    """Human confirms correct candidate → resolves page_index."""
    template = await confirm_template_page(
        db,
        vendor_id=payload.vendor_id,
        field_name=payload.field_name,
        confirmed_page_index=payload.confirmed_page_index,
    )
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


# ── Document presign ──────────────────────────────────────────────────

def validate_s3_key(s3_key: str) -> bool:
    """Validate s3_key format to prevent path traversal attacks."""
    if not s3_key:
        return False
    if '..' in s3_key:
        return False
    if '\x00' in s3_key:
        return False
    if not s3_key.startswith("documents/"):
        return False
    return True


@router.get("/documents/{s3_key:path}/presign")
async def get_presigned_url(s3_key: str):
    """Get presigned URL for viewing a document image."""
    if not validate_s3_key(s3_key):
        raise HTTPException(status_code=400, detail="Invalid s3_key format")
    try:
        url = presign_url(s3_key)
        return {"presigned_url": url}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── PDF Page Rendering ────────────────────────────────────────────────

@router.get("/pages")
async def get_page_image(
    s3_key: str = Query(..., description="S3 key of the uploaded document"),
    page: int = Query(0, ge=0, description="Zero-indexed page number"),
    dpi: int = Query(200, ge=72, le=400, description="Render DPI"),
):
    """
    Convert a PDF page (or return an image as-is) as a PNG.
    The frontend calls this to render documents on the Konva canvas.
    """
    from services.storage import download_file

    if not validate_s3_key(s3_key):
        raise HTTPException(status_code=400, detail="Invalid s3_key format")

    try:
        file_bytes = download_file(s3_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {e}")

    # Detect file type from the s3_key extension
    lower_key = s3_key.lower()

    if lower_key.endswith(".pdf"):
        # Convert PDF page to PNG using PyMuPDF
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            total_pages = len(doc)
            if page >= total_pages:
                doc.close()
                raise HTTPException(
                    status_code=400,
                    detail=f"Page {page} out of range. Document has {total_pages} pages.",
                )
            pix = doc[page].get_pixmap(dpi=dpi)
            png_bytes = pix.tobytes("png")
            doc.close()
            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "X-Page-Count": str(total_pages),
                },
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"PDF rendering failed: {e}")

    elif lower_key.endswith((".jpg", ".jpeg", ".png")):
        # Return image as-is
        content_type = "image/png" if lower_key.endswith(".png") else "image/jpeg"
        return Response(
            content=file_bytes,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    else:
        raise HTTPException(status_code=400, detail="Unsupported file type")


@router.get("/pages/count")
async def get_page_count(
    s3_key: str = Query(..., description="S3 key of the uploaded document"),
):
    """Return the number of pages for a PDF (1 for images)."""
    from services.storage import download_file

    if not validate_s3_key(s3_key):
        raise HTTPException(status_code=400, detail="Invalid s3_key format")

    try:
        file_bytes = download_file(s3_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {e}")

    lower_key = s3_key.lower()
    if lower_key.endswith(".pdf"):
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        count = len(doc)
        doc.close()
        return {"page_count": count}
    else:
        return {"page_count": 1}
