"""Incremental compilation integration tests (FakeForge + real git + real Postgres).

The scenarios pin pipeline.md §4–5: reconcile ordering, idempotence, PR-scoped
removal evidence, and edge authority under partial observation.
"""

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.collectors.forge import FakeForge, MergedPR, PRFile
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

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture()
def env(tmp_path: Path):
    """Fixture repo bootstrapped with a full compile; returns helpers to 'merge PRs'."""
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    repo = tmp_path / "repo"
    repo.mkdir()
    files = {
        "billing/__init__.py": "",
        "billing/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
        "billing/api.py": ("from billing.rules import apply_discount\n\n"
                           "def handle(x):\n    return apply_discount(x)\n"),
        "tests/test_rules.py": ("from billing.rules import apply_discount\n\n"
                                "def test_cap():\n    assert apply_discount(50) == 20\n"),
    }
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"inc-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo)

    counter = {"n": 0}

    def merge_pr(number: int, changes: dict[str, str | None], renames: dict[str, str] | None = None):
        """Apply changes ({path: content or None=delete}), commit, return MergedPR."""
        pr_files = []
        for old, new in (renames or {}).items():
            git(repo, "mv", old, new)
            pr_files.append(PRFile(path=new, change="renamed", old_path=old))
        for path, content in changes.items():
            if content is None:
                git(repo, "rm", "-q", path)
                pr_files.append(PRFile(path=path, change="removed"))
            else:
                existed = (repo / path).exists()
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(content, encoding="utf-8")
                git(repo, "add", path)
                pr_files.append(PRFile(path=path, change="modified" if existed else "added"))
        git(repo, "commit", "-qm", f"PR {number}")
        counter["n"] += 1
        return MergedPR(number=number, title=f"pr {number}",
                        merged_at=T0 + timedelta(minutes=counter["n"]),
                        merge_commit_sha=git(repo, "rev-parse", "HEAD"),
                        files=tuple(pr_files))

    return repo, slug, merge_pr


def current_slugs(slug: str) -> set[str]:
    from knowledge_compiler.storage.schema import EntityRow, Repository

    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        return set(session.execute(
            select(EntityRow.slug).where(EntityRow.repo_id == repo_id)).scalars())


def test_pr_compile_produces_precise_delta(env):
    from knowledge_compiler.compiler.run import compile_pr

    repo, slug, merge_pr = env
    # structural change (new symbol). Note deliberately: a body-only edit (e.g. a
    # constant 20 -> 25) produces an EMPTY deterministic delta — bodies are the
    # LLM layer's knowledge (business rules), not structure. That's by design.
    pr = merge_pr(101, {"billing/rules.py":
                        "def apply_discount(pct):\n    return min(pct, 25)\n\n"
                        "def audit_discount(pct):\n    return pct\n"})
    summaries = compile_pr(repo, FakeForge(prs=[pr]), expect_pr=101)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.pr_number == 101
    assert s.removed == 0
    assert s.changed >= 1 and s.added >= 1        # component changed; PR entity added
    # out-of-scope entities untouched
    assert "component/billing-api" in current_slugs(slug)
    assert "pull-request/101" in current_slugs(slug)


def test_reconcile_processes_backlog_in_order_and_is_idempotent(env):
    from knowledge_compiler.compiler.run import reconcile

    repo, slug, merge_pr = env
    pr1 = merge_pr(201, {"billing/rules.py":
                         "def apply_discount(pct):\n    return min(pct, 30)\n"})
    pr2 = merge_pr(202, {"billing/extra.py": "def bonus():\n    return 1\n"})
    forge = FakeForge(prs=[pr2, pr1])  # deliberately out of order

    first = reconcile(repo, forge)
    assert [s.pr_number for s in first] == [201, 202]  # merge order, not arrival order

    second = reconcile(repo, forge)
    assert second == []  # watermark advanced; nothing to do

    assert "component/billing-extra" in current_slugs(slug)


def test_pr_deletion_removes_only_in_scope_entities(env):
    from knowledge_compiler.compiler.run import compile_pr

    repo, slug, merge_pr = env
    pr = merge_pr(301, {"tests/test_rules.py": None})  # PR deletes the test file
    compile_pr(repo, FakeForge(prs=[pr]), expect_pr=301)

    slugs = current_slugs(slug)
    assert not any(s.startswith("test-coverage/") for s in slugs)   # in scope + gone => removed
    assert "component/tests-test-rules" not in slugs
    assert "component/billing-rules" in slugs                        # out of scope => survives


def test_dropped_import_removes_edge_despite_target_out_of_scope(env):
    from knowledge_compiler.compiler.run import compile_pr
    from knowledge_compiler.storage.schema import (
        EntityRow, RelationshipRow, Repository,
    )

    repo, slug, merge_pr = env
    # api.py stops importing billing.rules; rules.py itself is NOT in the PR
    pr = merge_pr(401, {"billing/api.py": "def handle(x):\n    return x\n"})
    compile_pr(repo, FakeForge(prs=[pr]), expect_pr=401)

    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        id_to_slug = dict(session.execute(
            select(EntityRow.id, EntityRow.slug).where(EntityRow.repo_id == repo_id)).all())
        edges = {(id_to_slug[r.from_entity_id], r.relation_type, id_to_slug[r.to_entity_id])
                 for r in session.execute(select(RelationshipRow)
                                          .where(RelationshipRow.repo_id == repo_id)).scalars()}
    # the from-side (billing-api) was observed => its outgoing edges are authoritative
    assert ("component/billing-api", "depends_on", "component/billing-rules") not in edges
    # but rules' own edges (tests covering it) survive — its coverage wasn't in scope
    assert ("test-coverage/tests-test-rules-py-test-cap", "covers",
            "component/billing-rules") in edges


def test_pr_slice_links_into_out_of_scope_components(env):
    from knowledge_compiler.compiler.run import compile_pr
    from knowledge_compiler.storage.schema import EntityRow, Repository

    repo, slug, merge_pr = env
    # new module imports billing.rules, which is out of the PR's scope
    pr = merge_pr(501, {"billing/report.py":
                        "from billing.rules import apply_discount\n\n"
                        "def report():\n    return apply_discount(5)\n"})
    compile_pr(repo, FakeForge(prs=[pr]), expect_pr=501)

    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        payload = session.execute(
            select(EntityRow.payload).where(EntityRow.repo_id == repo_id,
                                            EntityRow.slug == "component/billing-report")
        ).scalar_one()
    # billing.rules resolved as INTERNAL even though out of scope (combined-map fix)
    assert payload["internal_dependencies"] == ["billing.rules"]
    assert "billing.rules" not in payload["external_dependencies"]
