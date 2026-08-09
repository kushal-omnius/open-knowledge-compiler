"""Add commit_timestamp to compile_runs for commit-fill reconcile.

The existing merged_at column records PR merge times; commit_timestamp records
the committer timestamp for direct-push compiles (scope='commit'). The unified
watermark query uses COALESCE(commit_timestamp, merged_at) to advance the
reconcile cursor across both PR and direct-commit compile runs.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-09
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE compile_runs "
        "ADD COLUMN IF NOT EXISTS commit_timestamp TIMESTAMPTZ"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_compile_runs_repo_commit_ts "
        "ON compile_runs (repo_id, commit_timestamp)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_compile_runs_repo_commit_ts")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS commit_timestamp")
