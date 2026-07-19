"""kc init: create/upgrade schema, register the repository, write kc.toml (pipeline.md §1)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_compiler.storage.db import make_engine
from knowledge_compiler.storage.schema import Repository

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

KC_TOML_TEMPLATE = """\
# Knowledge Compiler repository configuration (ADR-007: activation is explicit).
[repository]
slug = "{slug}"
forge_ref = "{forge_ref}"
default_branch = "{default_branch}"

# Plugin activation lands with the first built-in plugins (phase 1).

[wiki]
# Local publication directory; the branch publisher ships it when enabled (ADR-010).
output_dir = "kc-wiki"

[publisher]
# Ship the wiki to a dedicated branch (ADR-010: knowledge/wiki). Explicit opt-in.
enabled = false
branch = "knowledge/wiki"
remote = "origin"
push = true

[llm]
# Semantic layer (ADR-008). Explicit opt-in. The deterministic compiler is fully
# functional without it. Credentials come from the environment, never this file:
#   provider = "anthropic" -> ANTHROPIC_API_KEY, pip install knowledge-compiler[llm]
#   provider = "openai"    -> OPENAI_API_KEY,    pip install knowledge-compiler[llm-openai]
enabled = false
provider = "anthropic"
# model = "claude-opus-4-8"   # per-provider default applies when omitted
max_calls_per_run = 200

[embeddings]
# Semantic vectors for retrieval (ADR-005). Explicit opt-in; without them,
# search runs keyword-only (FTS) — fully functional, just not semantic.
#   provider = "openai"       -> OPENAI_API_KEY
#   provider = "azure-openai" -> OPENAI_AZURE_* env vars (embedding deployment)
enabled = false
provider = "openai"
# model = "text-embedding-3-small"
"""


def upgrade_schema() -> None:
    """Run migrations to head. URL comes from KC_DATABASE_URL (storage/db.py)."""
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


def register_repository(slug: str, forge_ref: str, default_branch: str, config_ref: str) -> int:
    """Idempotent: registering an existing slug updates its refs and returns its id."""
    engine = make_engine()
    with Session(engine) as session, session.begin():
        repo = session.execute(select(Repository).where(Repository.slug == slug)).scalar_one_or_none()
        if repo is None:
            repo = Repository(slug=slug, forge_ref=forge_ref,
                              default_branch=default_branch, config_ref=config_ref)
            session.add(repo)
            session.flush()
        else:
            repo.forge_ref = forge_ref
            repo.default_branch = default_branch
            repo.config_ref = config_ref
        return repo.id


def write_config(target_dir: Path, slug: str, forge_ref: str, default_branch: str) -> Path:
    path = target_dir / "kc.toml"
    path.write_text(
        KC_TOML_TEMPLATE.format(slug=slug, forge_ref=forge_ref, default_branch=default_branch),
        encoding="utf-8",
    )
    return path


def init_repository(target_dir: Path, slug: str, forge_ref: str, default_branch: str) -> tuple[int, Path]:
    upgrade_schema()
    config_path = write_config(target_dir, slug, forge_ref, default_branch)
    repo_id = register_repository(slug, forge_ref, default_branch, str(config_path))
    return repo_id, config_path
