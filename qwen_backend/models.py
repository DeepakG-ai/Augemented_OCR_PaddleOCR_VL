"""
models.py -- Pydantic request/response models for Augmented OCR API.

Fields are split into header_fields (scalar) and line_item_fields (columns).
Users define these freely in the UI.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# -- Vendor ---------------------------------------------------------------

class VendorCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, description="Unique vendor slug")
    name: str = Field(..., min_length=1, max_length=256, description="Display name")


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    status: str
    created_at: datetime


# -- Template -------------------------------------------------------------

class TemplateCreate(BaseModel):
    format_type: str = Field(
        ...,
        pattern=r"^(single_po_multipage|po_per_page|single_page)$",
        description="Document layout type",
    )
    vendor_name: str | None = Field(
        None,
        description="Vendor display name — used to auto-upsert vendor if not in DB yet",
    )
    header_fields: list[str] = Field(
        default_factory=list,
        description="Scalar header field names (e.g. supplier, po_number, date)",
    )
    line_item_fields: list[str] = Field(
        default_factory=list,
        description="Line item column names (e.g. no, description, qty, amount)",
    )
    prompt_instructions: str | None = Field(
        None, description="Free-text layout hints from user"
    )
    extraction_rules: list[str] = Field(
        default_factory=list, description="Extraction rules"
    )


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    vendor_id: str
    format_type: str
    header_fields: list[str]
    line_item_fields: list[str]
    prompt_instructions: str | None
    extraction_rules: list[str]
    system_prompt: str | None
    prompt_hash: str | None
    created_at: datetime
    updated_at: datetime


class TemplateSaveResponse(BaseModel):
    template_id: int
    prompt_hash: str
    system_prompt_preview: str


# -- Extraction ------------------------------------------------------------

class ExtractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    vendor_id: str
    vendor_name: str | None = None
    template_id: int | None
    filename: str | None
    total_pages: int | None
    format_type: str | None
    header_fields: list[str] | None
    line_item_fields: list[str] | None
    result: Any | None
    page_results: Any | None
    status: str
    error: str | None
    duration_ms: int | None
    created_at: datetime


class ExtractionStartResponse(BaseModel):
    extraction_id: int
    result: Any | None
    total_pages: int
    duration_ms: int


class TemplateListOut(BaseModel):
    """Template with vendor_name for the saved-templates page."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    vendor_id: str
    vendor_name: str
    format_type: str
    header_fields: list[str]
    line_item_fields: list[str]
    prompt_instructions: str | None
    extraction_rules: list[str]
    prompt_hash: str | None
    created_at: datetime
    updated_at: datetime


# -- Health ----------------------------------------------------------------

class HealthOut(BaseModel):
    status: str
    db: str