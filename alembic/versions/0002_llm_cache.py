"""LLM content-addressed cache (ADR-008; data-model.md §2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18
"""
from alembic import op

from knowledge_compiler.storage.schema import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["llm_cache"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("llm_cache")
