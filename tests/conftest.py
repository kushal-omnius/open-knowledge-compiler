"""Shared pytest fixtures.

Integration tests each mint a uniquely-slugged throwaway repo (`it-*`,
`it-a-*`/`it-b-*`, `inc-*`, `llm-*`, `val-*`, `m2-*`, `m2o-*`, `vf-*`, `dbg-*`)
directly against the real Postgres instance (docker-compose) and never delete
it afterward — a 2026-08-05 dogfood pass found ~790 such repos accumulated in
the shared database from repeated local test runs. This session-scoped,
autouse fixture deletes every repo matching those test-slug prefixes once the
whole test session finishes, regardless of pass/fail, so the DB never
accumulates test cruft across runs.

Not per-test teardown: several integration tests intentionally recompile the
same fixture repo across multiple assertions within one test function, so
cleanup happens once, after the full session, rather than fixture-by-fixture.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.storage import db as kcdb

_TEST_SLUG_RE = re.compile(r"^(it|inc|llm|val|m2o?|vf|dbg)-")

# Child tables in FK-dependency order (no ON DELETE CASCADE on repo_id in the
# schema — same order as the one-off manual cleanup this fixture replaces).
_CHILD_TABLES = (
    "provenance", "delta_relationship_changes", "delta_changes",
    "embeddings", "relationships", "entities", "facts", "artifacts",
    "compile_runs",
)


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_repos():
    yield
    if not _db_available():
        return
    with Session(kcdb.make_engine()) as session:
        ids = [row.id for row in session.execute(text("SELECT id, slug FROM repositories")).all()
               if _TEST_SLUG_RE.match(row.slug)]
        if not ids:
            return
        for table in _CHILD_TABLES:
            session.execute(text(f"DELETE FROM {table} WHERE repo_id = ANY(:ids)"), {"ids": ids})
        session.execute(text("DELETE FROM repositories WHERE id = ANY(:ids)"), {"ids": ids})
        session.commit()
