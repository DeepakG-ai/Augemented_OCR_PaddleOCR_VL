import uuid
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.semantic_template import SemanticTemplate

logger = logging.getLogger(__name__)


async def get_vendor_templates(db: AsyncSession, vendor_id: uuid.UUID) -> list[SemanticTemplate]:
    """Get all semantic templates for a vendor."""
    result = await db.execute(
        select(SemanticTemplate).where(SemanticTemplate.vendor_id == vendor_id)
    )
    return list(result.scalars().all())


async def templates_to_anchors(templates: list[SemanticTemplate]) -> list[dict]:
    """Convert stored SemanticTemplates to anchor dicts for the worker."""
    return [
        {
            "field": t.field_name,
            "prompt": t.semantic_prompt,
            "x_pct": t.norm_x,
            "y_pct": t.norm_y,
            "page_index": t.page_index,
            "vendor_id": str(t.vendor_id),
        }
        for t in templates
    ]


async def upsert_semantic_template(
    db: AsyncSession,
    vendor_id: uuid.UUID,
    field_name: str,
    semantic_prompt: str,
    norm_x: float,
    norm_y: float,
    page_index: Optional[int] = None,
) -> SemanticTemplate:
    """Upsert a semantic template — increment sample_count on conflict."""
    from sqlalchemy import func

    stmt = pg_insert(SemanticTemplate).values(
        id=uuid.uuid4(),
        vendor_id=vendor_id,
        field_name=field_name,
        semantic_prompt=semantic_prompt,
        norm_x=norm_x,
        norm_y=norm_y,
        page_index=page_index,
        sample_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="semantic_templates_vendor_id_field_name_key",
        set_={
            "semantic_prompt": stmt.excluded.semantic_prompt,
            "norm_x": stmt.excluded.norm_x,
            "norm_y": stmt.excluded.norm_y,
            "page_index": stmt.excluded.page_index,
            "sample_count": SemanticTemplate.sample_count + 1,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()

    # Fetch the upserted record
    result = await db.execute(
        select(SemanticTemplate).where(
            SemanticTemplate.vendor_id == vendor_id,
            SemanticTemplate.field_name == field_name,
        )
    )
    template = result.scalar_one()
    logger.info(f"Upserted template: {vendor_id}/{field_name} page_index={page_index}")
    return template


async def delete_template(db: AsyncSession, vendor_id: uuid.UUID, field_name: str) -> bool:
    """Delete a specific template by vendor_id and field_name."""
    result = await db.execute(
        select(SemanticTemplate).where(
            SemanticTemplate.vendor_id == vendor_id,
            SemanticTemplate.field_name == field_name,
        )
    )
    template = result.scalar_one_or_none()
    if template:
        await db.delete(template)
        await db.commit()
        return True
    return False


async def confirm_template_page(
    db: AsyncSession,
    vendor_id: uuid.UUID,
    field_name: str,
    confirmed_page_index: int,
) -> Optional[SemanticTemplate]:
    """Human confirms the correct page_index — resolves ambiguity."""
    result = await db.execute(
        select(SemanticTemplate).where(
            SemanticTemplate.vendor_id == vendor_id,
            SemanticTemplate.field_name == field_name,
        )
    )
    template = result.scalar_one_or_none()
    if template:
        template.page_index = confirmed_page_index
        await db.commit()
        await db.refresh(template)
        logger.info(f"Confirmed page_index={confirmed_page_index} for {vendor_id}/{field_name}")
        return template
    return None
