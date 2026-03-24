import uuid
from datetime import datetime
from sqlalchemy import Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from core.database import Base


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    # Relationships
    templates = relationship("SemanticTemplate", back_populates="vendor", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="vendor")
    extraction_results = relationship("ExtractionResult", back_populates="vendor")
