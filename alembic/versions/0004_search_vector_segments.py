"""Rebuild entities.search_vector to index dotted-name segments (dogfood finding:
'pkg.mod' tokenizes as one host-token, so searching 'mod' matched nothing).
Generated columns cannot be altered — drop and re-add.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-19
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_NEW = ("to_tsvector('english', coalesce(name, '') || ' ' || "
        "replace(coalesce(name, ''), '.', ' ') || ' ' || coalesce(payload::text, ''))")
_OLD = "to_tsvector('english', coalesce(name, '') || ' ' || coalesce(payload::text, ''))"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entities_search")
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS search_vector")
    op.execute(f"ALTER TABLE entities ADD COLUMN search_vector tsvector "
               f"GENERATED ALWAYS AS ({_NEW}) STORED")
    op.execute("CREATE INDEX ix_entities_search ON entities USING GIN (search_vector)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entities_search")
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS search_vector")
    op.execute(f"ALTER TABLE entities ADD COLUMN search_vector tsvector "
               f"GENERATED ALWAYS AS ({_OLD}) STORED")
    op.execute("CREATE INDEX ix_entities_search ON entities USING GIN (search_vector)")
