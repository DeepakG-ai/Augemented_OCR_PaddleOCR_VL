import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from core.database import Base


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    vendor_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    candidate_log: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    extracted_at: Mapped[datetime] = mapped_column(default=func.now())

    __table_args__ = (
        Index("idx_extraction_results_vendor", "vendor_id"),
        Index("idx_extraction_results_jsonb", "raw_data", postgresql_using="gin"),
    )

    # Relationships
    document = relationship("Document", back_populates="extraction_result")
    vendor = relationship("Vendor", back_populates="extraction_results")
