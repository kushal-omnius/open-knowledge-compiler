"""Track the OKF spec version targeted by each compile's wiki emission (ADR-013),
alongside the existing fact_vocabulary_version/knowledge_model_version pair (ir.md §5).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("compile_runs", sa.Column("okf_spec_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("compile_runs", "okf_spec_version")
