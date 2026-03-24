import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import Text, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from core.database import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vendor_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="queued")
    celery_task_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    __table_args__ = (
        Index("idx_documents_vendor", "vendor_id"),
        Index("idx_documents_status", "status"),
    )

    # Relationships
    vendor = relationship("Vendor", back_populates="documents")
    extraction_result = relationship("ExtractionResult", back_populates="document", uselist=False)
