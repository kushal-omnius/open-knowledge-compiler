"""End-to-end pipeline integration (real git, real Postgres, real tree-sitter).

Covers the phase-1 acceptance criteria:
  - kc compile --full produces a knowledge base from a real repo
  - recompiling an unchanged repo yields an EMPTY delta (end-to-end determinism)
  - source change => precise delta
  - two repositories in one database stay isolated (ADR-001 multi-repo invariant)
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable (docker compose up -d) — integration skipped")


BILLING_FILES = {
    "billing/__init__.py": "",
    "billing/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
    "billing/api.py": (
        "from fastapi import FastAPI\nfrom billing.rules import apply_discount\n"
        "app = FastAPI()\n\n"
        "@app.get(\"/discounts/{id}\")\ndef read_discount(id: int):\n"
        "    return apply_discount(id)\n"
    ),
    "tests/test_rules.py": (
        "from billing.rules import apply_discount\n\n"
        "def test_cap():\n    assert apply_discount(50) == 20\n"
    ),
}


def make_repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@e.st")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return repo


@pytest.fixture()
def compiled_repo(tmp_path: Path):
    """A registered + compiled fixture repo with a unique slug per test run."""
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    slug = f"it-{uuid.uuid4().hex[:8]}"
    repo = make_repo(tmp_path, "repo", BILLING_FILES)
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    summary = compile_full(repo)
    return repo, slug, summary


def test_full_compile_builds_knowledge_base(compiled_repo):
    _, slug, summary = compiled_repo
    assert summary.entities > 0 and summary.added == summary.entities  # bootstrap: all added
    assert summary.removed == 0 and summary.warnings == []

    from knowledge_compiler.storage.schema import EntityRow, Repository
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        types = dict(session.execute(
            select(EntityRow.entity_type, EntityRow.slug).where(EntityRow.repo_id == repo_id)
        ).all())
    # spot-check the knowledge actually landed
    slugs = set(types.values())
    assert {"project", "component", "api", "test_coverage", "wiki_page"} <= set(types)


def test_recompile_unchanged_repo_is_empty_delta(compiled_repo):
    from knowledge_compiler.compiler.run import compile_full

    repo, _, _ = compiled_repo
    second = compile_full(repo)
    assert (second.added, second.changed, second.removed, second.moved) == (0, 0, 0, 0)
    assert second.dirty == 0  # nothing to re-emit: end-to-end determinism


def test_source_change_produces_precise_delta(compiled_repo):
    from knowledge_compiler.compiler.run import compile_full

    repo, _, _ = compiled_repo
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 25)\n\n"
        "def new_helper():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-aqm", "change cap"], check=True,
                   capture_output=True)

    third = compile_full(repo)
    assert third.changed >= 1          # billing.rules component (symbols changed) + its page owner hash
    assert third.removed == 0
    assert third.dirty >= 1


def test_emit_only_reruns_emission_without_new_compile_run(compiled_repo):
    """ADR-013: emit-only re-renders the wiki from durable Knowledge IR without
    a new compile_runs row — the cheap OKF-spec-version rollout path."""
    from knowledge_compiler.compiler.run import emit_only
    from knowledge_compiler.storage.schema import CompileRun, Repository

    repo, slug, first = compiled_repo
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        run_count_before = session.execute(
            select(func.count()).select_from(CompileRun).where(CompileRun.repo_id == repo_id)
        ).scalar_one()

    summary = emit_only(repo)
    assert summary.compile_run_id == first.compile_run_id  # reuses last succeeded run's identity
    assert summary.entities == first.entities
    assert summary.wiki_pages_written > 0

    with Session(kcdb.make_engine()) as session:
        run_count_after = session.execute(
            select(func.count()).select_from(CompileRun).where(CompileRun.repo_id == repo_id)
        ).scalar_one()
    assert run_count_after == run_count_before  # no new compile_runs row


def test_two_repos_in_one_database_stay_isolated(tmp_path: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.storage.schema import EntityRow, Repository

    slug_a, slug_b = (f"it-a-{uuid.uuid4().hex[:6]}", f"it-b-{uuid.uuid4().hex[:6]}")
    repo_a = make_repo(tmp_path, "a", BILLING_FILES)
    repo_b = make_repo(tmp_path, "b", {"net/__init__.py": "", "net/client.py": "def retry():\n    pass\n"})
    init_repository(repo_a, slug_a, "github.com/t/a", "main")
    init_repository(repo_b, slug_b, "github.com/t/b", "main")

    sa, sb = compile_full(repo_a), compile_full(repo_b)
    assert sa.entities != sb.entities  # different repos, different knowledge

    with Session(kcdb.make_engine()) as session:
        def slugs_of(slug: str) -> set[str]:
            repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
            return set(session.execute(
                select(EntityRow.slug).where(EntityRow.repo_id == repo_id)).scalars())

        a_slugs, b_slugs = slugs_of(slug_a), slugs_of(slug_b)
    assert "component/billing" in a_slugs and "component/billing" not in b_slugs
    assert "component/net-client" in b_slugs and "component/net-client" not in a_slugs
    # same-named project entities coexist because every table carries repo_id (ADR-001)
    assert any(s.startswith("project/") for s in a_slugs)
    assert any(s.startswith("project/") for s in b_slugs)
