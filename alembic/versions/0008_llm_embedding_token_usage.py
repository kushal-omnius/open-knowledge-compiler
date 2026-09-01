"""Real LLM/embedding token usage on compile_runs: usage.* from every provider's
API response was previously discarded, making cost estimates for a compile or
reconcile run pure guesswork. Adds llm_calls / llm_input_tokens /
llm_output_tokens / embedding_calls / embedding_input_tokens, populated by
compiler/run.py `_compile_one` (cache hits excluded — they cost nothing).
NULL on rows predating this migration and on emit-only reruns (no Extract/Embed
stage runs) — same "not measured" convention as files_seen (0007).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-01
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: schema.py's create_all() (migration 0001) already creates
    # these columns on a fresh database, since schema.py now declares them —
    # matches the idempotent pattern 0002-0007 already established.
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS llm_calls INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS llm_input_tokens INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS llm_output_tokens INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS embedding_calls INTEGER")
    op.execute("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS embedding_input_tokens INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS embedding_input_tokens")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS embedding_calls")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS llm_output_tokens")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS llm_input_tokens")
    op.execute("ALTER TABLE compile_runs DROP COLUMN IF EXISTS llm_calls")
