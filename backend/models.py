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
    user_id: str | None = Field(
        None,
        description="Owner user_id — admin-only override; clients always own vendors they create.",
    )


# -- Auth ------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=256)
    password: str = Field(..., min_length=1, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    role: str
    is_active: bool = True
    subscription_limit: int | None = None
    created_at: datetime | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=256)
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field("client", pattern=r"^(admin|client)$")


class UserResetPassword(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=256)


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    status: str
    created_at: datetime
    user_id: str | None = None


class VendorAliasCreate(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=256)
    weight: int = Field(1, ge=1, le=10)


class VendorAliasOut(BaseModel):
    id: int
    vendor_id: str
    pattern: str
    weight: int
    source: str
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
    user_prompt: str | None = None
    system_prompt_page1: str | None = None
    user_prompt_page1: str | None = None
    system_prompt_page2: str | None = None
    user_prompt_page2: str | None = None
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
    document_id: int | None = None
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
    field_locations: Any | None = None
    ocr_data: Any | None = None
    corrected_result: Any | None = None
    correction_meta: Any | None = None
    export_object_key: str | None = None
    progress: Any | None = None
    cancel_requested: bool = False
    status: str
    error: str | None
    duration_ms: int | None
    created_at: datetime
    updated_at: datetime | None = None


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


class JobOut(BaseModel):
    id: int
    extraction_id: int | None = None
    document_id: int | None = None
    job_type: str
    status: str
    payload: Any | None = None
    progress: Any | None = None
    attempts: int
    max_attempts: int
    priority: int
    locked_by: str | None = None
    locked_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class JobStatusOut(BaseModel):
    job: JobOut
    extraction: ExtractionOut | None = None


class ExtractionJobStartOut(BaseModel):
    job_id: int
    extraction_id: int
    status: str
    detected_vendor: Any | None = None
    usage_warning: Any | None = None
