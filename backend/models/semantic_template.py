import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import Text, Float, Integer, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from core.database import Base


class SemanticTemplate(Base):
    __tablename__ = "semantic_templates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    norm_x: Mapped[float] = mapped_column(Float, nullable=False)
    norm_y: Mapped[float] = mapped_column(Float, nullable=False)
    page_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("vendor_id", "field_name"),
    )

    # Relationships
    vendor = relationship("Vendor", back_populates="templates")
