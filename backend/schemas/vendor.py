from typing import Optional
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class VendorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)


class VendorResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SemanticTemplateResponse(BaseModel):
    id: UUID
    vendor_id: UUID
    field_name: str
    semantic_prompt: str
    norm_x: float
    norm_y: float
    page_index: Optional[int]
    sample_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
