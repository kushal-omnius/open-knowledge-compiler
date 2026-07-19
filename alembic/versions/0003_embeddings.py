"""Embeddings: pgvector extension + per-generation vectors (ADR-005).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-19
"""
from alembic import op

from knowledge_compiler.storage.schema import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.tables["embeddings"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("embeddings")
