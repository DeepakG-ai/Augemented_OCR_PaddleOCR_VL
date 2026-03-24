from typing import Optional, Any
from uuid import UUID
from pydantic import BaseModel


class JobStatusResponse(BaseModel):
    job_id: str
    document_id: UUID
    status: str


class JobResultResponse(BaseModel):
    job_id: str
    document_id: UUID
    status: str
    data: Optional[dict[str, Any]] = None
    candidates: Optional[dict[str, list[dict[str, Any]]]] = None
    error: Optional[str] = None
