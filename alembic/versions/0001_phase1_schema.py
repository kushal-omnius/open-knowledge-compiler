"""Phase-1 schema: deterministic compiler tables (docs/data-model.md §2).

Deferred to later phases (additive): embeddings (ADR-005), llm_cache (ADR-008).

Revision ID: 0001
Revises:
Create Date: 2026-07-18
"""
from alembic import op

from knowledge_compiler.storage.schema import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tables come from the single source of truth: storage/schema.py metadata.
    Base.metadata.create_all(op.get_bind())

    # Generated FTS column + GIN index (data-model.md §2 entities, §5 keyword search).
    # Raw SQL: generated tsvector columns have no portable ORM form.
    op.execute(
        """
        ALTER TABLE entities ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english', coalesce(name, '') || ' ' || coalesce(payload::text, ''))
        ) STORED
        """
    )
    op.execute("CREATE INDEX ix_entities_search ON entities USING GIN (search_vector)")


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
