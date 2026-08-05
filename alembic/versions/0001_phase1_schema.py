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
    # Base.metadata.create_all() below creates every table storage/schema.py
    # currently declares (the single source of truth) — not a frozen snapshot of
    # what existed when this migration was written. Since schema.py now includes
    # embeddings.vector (pgvector), the extension must exist before create_all()
    # runs on a fresh database, not just by the time migration 0003 gets to it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Tables come from the single source of truth: storage/schema.py metadata —
    # including entities.search_vector, an ORM-declared Computed (generated) column.
    Base.metadata.create_all(op.get_bind())

    # GIN index for keyword search (data-model.md §5)
    op.execute("CREATE INDEX IF NOT EXISTS ix_entities_search ON entities USING GIN (search_vector)")


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
