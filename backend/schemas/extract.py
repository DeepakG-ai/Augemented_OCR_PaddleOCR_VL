from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class AnchorInput(BaseModel):
    """Single anchor point from the canvas annotation."""
    field: str = Field(..., description="Field name, e.g. 'invoice_total'")
    prompt: str = Field(..., description="Semantic prompt for the VLM")
    x_pct: float = Field(..., ge=0.0, le=1.0, description="Normalized X coordinate")
    y_pct: float = Field(..., ge=0.0, le=1.0, description="Normalized Y coordinate")
    page_index: Optional[int] = Field(None, description="Known page index (None = search all)")


class ExtractionRequest(BaseModel):
    """POST /api/extract request body."""
    s3_key: str
    vendor_id: UUID
    anchors: list[AnchorInput] = Field(default_factory=list)
    save_rules: bool = True


class TemplateConfirmRequest(BaseModel):
    """POST /api/templates/confirm — human resolves page_index ambiguity."""
    vendor_id: UUID
    field_name: str
    confirmed_page_index: int
    confirmed_value: str
