"""
models.py -- Pydantic request/response models for Augmented OCR API.

Fields are split into header_fields (scalar) and line_item_fields (columns).
Users define these freely in the UI.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# -- Shared field-list limits ---------------------------------------------
# Caps to keep user-defined field/rule lists sane (and the LLM prompt bounded).
MAX_FIELD_COUNT = 200
MAX_FIELD_NAME_LEN = 128
MAX_RULE_LEN = 512
RESERVED_FIELD_NAMES = {"line_items", "fields", "boxes", "_page", "_error", "_raw"}


def validate_field_name_list(
    values: Any,
    *,
    max_len: int = MAX_FIELD_NAME_LEN,
    check_reserved: bool = True,
    check_duplicates: bool = True,
) -> list[str]:
    """Validate a list of user-supplied field names / rules.

    Rejects non-lists, oversized lists, non-string items, and overly long
    items. Returns the list with each item stripped.
    """
    if not isinstance(values, list):
        raise ValueError("must be a list")
    if len(values) > MAX_FIELD_COUNT:
        raise ValueError(f"too many entries (max {MAX_FIELD_COUNT})")
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("each entry must be a string")
        item = item.strip()
        if not item:
            raise ValueError("entries must not be blank")
        if len(item) > max_len:
            raise ValueError(f"entry too long (max {max_len} chars)")
        lowered = item.lower()
        if check_reserved and (lowered in RESERVED_FIELD_NAMES or lowered.startswith("_")):
            raise ValueError(f"{item!r} is a reserved field name")
        if check_duplicates and lowered in {existing.lower() for existing in cleaned}:
            raise ValueError(f"duplicate field name: {item!r}")
        cleaned.append(item)
    return cleaned


# -- Vendor ---------------------------------------------------------------

class VendorCreate(BaseModel):
    # id is issued server-side from a global sequence; clients never supply it.
    # Accepted-but-ignored if a legacy caller still sends it.
    id: str | None = Field(None, max_length=64, description="(ignored) server-issued")
    name: str = Field(..., min_length=1, max_length=256, description="Display name")
    user_id: str | None = Field(
        None,
        description="Owner user_id — admin-only override; clients always own vendors they create.",
    )


    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("control characters are not allowed")
        return v


# -- Auth ------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr = Field(..., max_length=256)
    password: str = Field(..., min_length=1, max_length=256)

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return str(v).strip().lower()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    role: str
    is_active: bool = True
    subscription_limit: int | None = None
    created_at: datetime | None = None
    # Per-user current period summary (admin list view). All optional so old
    # callers/tests keep working.
    period_start: datetime | None = None
    period_end: datetime | None = None
    base_limit: int | None = None
    topup_total: int | None = None
    effective_limit: int | None = None
    pages_used: int | None = None
    pages_remaining: int | None = None
    period_status: str | None = None  # 'active' | 'expired' | 'cancelled' | 'none'


class SubscriptionCreate(BaseModel):
    page_limit: int = Field(..., ge=0, description="Base pages granted for the period.")
    period_start: datetime | None = Field(
        None,
        description="ISO timestamp. Defaults to now() when omitted.",
    )
    period_end: datetime = Field(
        ...,
        description="ISO timestamp. Must be after period_start.",
    )
    note: str | None = Field(None, max_length=500)


class TopupCreate(BaseModel):
    pages: int = Field(..., gt=0, description="Extra pages to grant in addition to base.")
    note: str | None = Field(None, max_length=500)


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    page_limit: int
    period_start: datetime
    period_end: datetime
    status: str
    note: str | None = None
    created_at: datetime
    created_by_email: str | None = None
    topup_total: int = 0
    pages_used: int = 0


class TopupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subscription_id: int
    pages: int
    note: str | None = None
    created_at: datetime
    created_by_email: str | None = None
    sub_period_start: datetime | None = None
    sub_period_end: datetime | None = None
    sub_status: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserCreate(BaseModel):
    email: EmailStr = Field(..., max_length=256)
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field("client", pattern=r"^(admin|client)$")

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return str(v).strip().lower()


class UserResetPassword(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=256)


class ApiKeyCreate(BaseModel):
    label: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Human-readable name for this key (e.g. ap_automation, client.6)",
    )
    owner_user_id: UUID = Field(
        ...,
        description="UUID of the existing client user who owns this key.",
    )
    expires_days: Literal[30, 90, 365] | None = Field(
        None,
        description="Expiry in days from now. 30, 90, 365, or null for no expiry.",
    )


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: str | None = None
    label: str
    prefix: str
    is_active: bool = True
    owner_email: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_documents: int = 0
    total_pages: int = 0
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    status: str
    created_at: datetime
    user_id: str | None = None
    client_seq: int | None = None


class VendorAliasCreate(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=256)
    weight: int = Field(1, ge=1, le=10)

    @field_validator("pattern")
    @classmethod
    def _clean_pattern(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("pattern must not be blank")
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("control characters are not allowed")
        return v


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
        max_length=256,
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
        None, max_length=4000, description="Free-text layout hints from user"
    )
    extraction_rules: list[str] = Field(
        default_factory=list, description="Extraction rules — free-text, max 512 chars each"
    )

    @field_validator("header_fields", "line_item_fields")
    @classmethod
    def _validate_field_names(cls, v: list[str]) -> list[str]:
        return validate_field_name_list(v)

    @field_validator("extraction_rules")
    @classmethod
    def _validate_rules(cls, v: list[str]) -> list[str]:
        cleaned = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("each rule must be a string")
            item = item.strip()
            if not item:
                continue
            if len(item) > MAX_RULE_LEN:
                raise ValueError(f"rule too long (max {MAX_RULE_LEN} chars): {item[:40]!r}...")
            cleaned.append(item)
        return cleaned


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
    vendor_id: str | None = None
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


# -- Top-up requests -------------------------------------------------------

class TopupRequestCreate(BaseModel):
    """User-submitted top-up page request.

    Any positive integer is accepted for requested_pages — the admin decides
    what to actually grant. requested_period is a free-text label (e.g.
    '1 month', '6 months', '2 years', 'Q3 extension') — no enumeration is
    enforced so admins can use any period that makes sense for the client.
    """

    requested_pages: int = Field(
        ...,
        gt=0,
        le=10_000_000,
        description="Number of extra pages requested. Any positive integer.",
    )
    requested_period: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Free-text period label, e.g. '3 months', '1 year', 'Q4 extension'.",
    )
    note: str | None = Field(None, max_length=500)

    @field_validator("requested_pages", mode="before")
    @classmethod
    def coerce_pages(cls, v):
        """Accept numeric strings from form-style callers; reject floats with decimals."""
        if isinstance(v, str):
            try:
                v = int(v)
            except ValueError:
                raise ValueError("requested_pages must be a whole number")
        if isinstance(v, float) and not v.is_integer():
            raise ValueError("requested_pages must be a whole number, not a decimal")
        return int(v)

    @field_validator("requested_period", mode="before")
    @classmethod
    def strip_period(cls, v):
        if isinstance(v, str):
            v = v.strip()
        return v


class TopupRequestResolve(BaseModel):
    resolution_note: str | None = Field(None, max_length=500)


# -- Admin typed request bodies -------------------------------------------

class SubscriptionLimitUpdate(BaseModel):
    subscription_limit: int = Field(..., ge=0)


class VendorOwnerAssign(BaseModel):
    user_id: UUID


class CorrectionFieldLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(..., ge=1)
    # box is nullable: line-item column cells the model couldn't anchor are
    # saved with box=None (strategy "qwen_column_header_missing"), and they
    # round-trip through the Review save unchanged.
    box: list[float] | None = Field(None, min_length=4, max_length=4)
    matched_text: str | None = Field(None, max_length=5000)
    score: float | None = Field(None, ge=0, le=1)
    strategy: str | None = Field(None, max_length=128)
    confidence: str | None = Field(None, max_length=32)
    word_boxes: list[Any] | None = Field(None, max_length=500)

    @field_validator("box")
    @classmethod
    def _valid_box(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        if any(not math.isfinite(coord) for coord in v):
            raise ValueError("box coordinates must be finite numbers")
        x0, y0, x1, y1 = v
        if x1 <= x0 or y1 <= y0:
            raise ValueError("box must have positive width and height")
        return v

    @field_validator("strategy", "confidence")
    @classmethod
    def _no_control_text(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("control characters are not allowed")
        return v.strip()


class CorrectionSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrected_result: dict[str, Any] | list[dict[str, Any]]
    field_locations: dict[str, CorrectionFieldLocation] | list[dict[str, CorrectionFieldLocation]] = Field(
        default_factory=dict
    )
    actor: str = Field("ui", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    reason_code: str = Field(
        "manual_review",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    note: str | None = Field(None, max_length=500)


class VendorMappingSave(BaseModel):
    """ERP field-mapping save body: {source_field: canonical_target} dicts."""

    model_config = ConfigDict(extra="forbid")

    header_map: dict[str, str] = Field(default_factory=dict)
    line_map: dict[str, str] = Field(default_factory=dict)
    schema_id: int | None = Field(None, ge=1)

    @field_validator("header_map", "line_map", mode="before")
    @classmethod
    def _bounded_map(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            raise ValueError("mapping must be an object")
        if len(v) > MAX_FIELD_COUNT:
            raise ValueError(f"too many mappings (max {MAX_FIELD_COUNT})")
        cleaned: dict[str, str] = {}
        for key, value in v.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("mapping keys and values must be strings")
            key = key.strip()
            value = value.strip()
            if not key or not value:
                raise ValueError("mapping keys and values must not be blank")
            if len(key) > MAX_FIELD_NAME_LEN or len(value) > MAX_FIELD_NAME_LEN:
                raise ValueError(f"field name too long (max {MAX_FIELD_NAME_LEN} chars)")
            if any(ord(ch) < 32 for ch in key + value):
                raise ValueError("control characters are not allowed")
            cleaned[key] = value
        return cleaned


class TopupRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: str
    user_email: str | None = None
    requested_pages: int
    requested_period: str
    note: str | None = None
    status: str
    resolution_note: str | None = None
    resolved_by: str | None = None
    resolved_by_email: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
