"""Compile completeness signal on compile_runs (dogfood-review finding): a
parser failure must never fail the compile (ADR-006), but a compile that
silently loses coverage in, say, an auth module previously looked identical
to a clean one — knowledge_stats() reported entity counts only. Adds
files_seen / files_parsed / files_failed / failed_files, populated by the
Extract stage (compiler/run.py `_extract`) on every full/PR/commit run.
NULL on rows predating this migration and on emit-only reruns (no Extract
stage runs) — distinguishes "not measured" from "measured, zero failures".

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-22
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: schema.py's create_all() (migration 0001) already creates
    # these columns on a fresh database, since schema.py now declares them —
    # matches the idempotent pattern 0002-0006 already established.
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS files_seen INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS files_parsed INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS files_failed INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS failed_files JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS failed_files")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS files_failed")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS files_parsed")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS files_seen")
