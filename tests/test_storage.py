"""Storage-layer tests.

Unit tests run everywhere. Integration tests need the compose Postgres and skip
(loudly, with reason) when it's unreachable — skipped is surfaced, never silent.
"""

import pytest
from sqlalchemy import inspect as sa_inspect, text

from knowledge_compiler.storage import db as kcdb
from knowledge_compiler.storage.schema import Base

EXPECTED_TABLES = {
    "repositories", "compile_runs", "artifacts", "facts", "entities",
    "relationships", "provenance", "delta_changes", "delta_relationship_changes",
    "llm_cache",
}


def test_schema_declares_exactly_expected_tables():
    # Intent: data-model.md §2 catalog, minus embeddings (deferred to its phase).
    # A new table appearing here must be a conscious act.
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_table_carries_repo_id():
    # ADR-001 invariant: no schema decision may assume single-repo.
    # llm_cache is exempt by design (data-model.md §2): content-addressed and
    # repo-agnostic — identical (template, model, input) is the same answer
    # in any repo, so sharing entries across repos is correct and saves cost.
    for name, table in Base.metadata.tables.items():
        if name in ("repositories", "llm_cache"):
            continue
        assert "repo_id" in table.columns, f"{name} is missing repo_id"


def test_advisory_lock_key_is_stable_and_slug_derived():
    # Intent: the lock must be acquirable before the repo row exists (bootstrap),
    # so it derives from the slug, deterministically.
    assert kcdb.repo_lock_key("repo-a") == kcdb.repo_lock_key("repo-a")
    assert kcdb.repo_lock_key("repo-a") != kcdb.repo_lock_key("repo-b")


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable (docker compose up -d) — integration skipped"
)


@requires_db
def test_migration_creates_catalog_and_fts_column():
    from knowledge_compiler.compiler.bootstrap import upgrade_schema

    upgrade_schema()
    engine = kcdb.make_engine()
    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES <= tables
    entity_cols = {c["name"] for c in inspector.get_columns("entities")}
    assert "search_vector" in entity_cols  # generated FTS column (migration raw SQL)


@requires_db
def test_init_is_idempotent():
    from knowledge_compiler.compiler.bootstrap import register_repository, upgrade_schema

    upgrade_schema()
    id1 = register_repository("kc-test-repo", "github.com/test/repo", "main", "kc.toml")
    id2 = register_repository("kc-test-repo", "github.com/test/repo2", "main", "kc.toml")
    assert id1 == id2  # same slug => same repo, refs updated, no duplicate
