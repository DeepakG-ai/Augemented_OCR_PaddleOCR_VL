"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- vendors ---
    op.create_table(
        "vendors",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # --- semantic_templates ---
    op.create_table(
        "semantic_templates",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("semantic_prompt", sa.Text(), nullable=False),
        sa.Column("norm_x", sa.Float(), nullable=False),
        sa.Column("norm_y", sa.Float(), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("vendor_id", "field_name"),
    )
    op.create_index("idx_semantic_templates_vendor", "semantic_templates", ["vendor_id"])

    # --- documents ---
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"]),
    )
    op.create_index("idx_documents_vendor", "documents", ["vendor_id"])
    op.create_index("idx_documents_status", "documents", ["status"])

    # --- extraction_results ---
    op.create_table(
        "extraction_results",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_log", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("extracted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"]),
        sa.UniqueConstraint("document_id"),
    )
    op.create_index("idx_extraction_results_vendor", "extraction_results", ["vendor_id"])
    op.create_index(
        "idx_extraction_results_jsonb",
        "extraction_results",
        ["raw_data"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("extraction_results")
    op.drop_table("documents")
    op.drop_table("semantic_templates")
    op.drop_table("vendors")
