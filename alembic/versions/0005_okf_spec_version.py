"""Track the OKF spec version targeted by each compile's wiki emission (ADR-013),
alongside the existing fact_vocabulary_version/knowledge_model_version pair (ir.md §5).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: migration 0001's create_all() (schema.py is the live source of
    # truth) already creates this column on a fresh database, since schema.py now
    # declares it — matches the idempotent pattern 0002-0004 already established.
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS okf_spec_version TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS okf_spec_version")
