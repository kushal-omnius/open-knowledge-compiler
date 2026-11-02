"""Rename lineage (normalize.md §4.1): a PR that only `git mv`s files.

Components and test coverage are keyed by path (ADR-004), so a renamed file
still yields a removed and an added entity with different slugs. Each side
must name the other in the delta (`renamed_to` / `renamed_from`), and a stale
slug must keep resolving: get_entity/test_plan, kc validate-test, journeys.

kc-covers:
  - component/knowledge-compiler-compiler-normalize
  - component/knowledge-compiler-compiler-diff
  - component/knowledge-compiler-storage-persist
  - component/knowledge-compiler-mcp-queries
  - component/knowledge-compiler-validation

Real git repo, real Postgres, FakeForge for the merged-PR metadata.
"""

import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
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
JOURNEY = "user-journey/apply-discount"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture()
def env(tmp_path: Path):
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
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"inc-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    # billing-api is renamed by every PR below; billing-rules is never touched
    steps = ["component/billing-api", "component/billing-rules"]
    with (repo / "kc.toml").open("a", encoding="utf-8") as f:
        f.write(f'\n[[journeys]]\nname = "Apply discount"\nsteps = {json.dumps(steps)}\n')
    compile_full(repo)

    prs: list[MergedPR] = []

    def merge_pr(number: int, renames: dict[str, str],
                 changes: dict[str, str] | None = None) -> None:
        """git mv each rename (plus optional writes), commit, compile the PR."""
        from knowledge_compiler.compiler.run import compile_pr

        pr_files = []
        for old, new in renames.items():
            git(repo, "mv", old, new)
            pr_files.append(PRFile(path=new, change="renamed", old_path=old))
        for path, content in (changes or {}).items():
            (repo / path).write_text(content, encoding="utf-8")
            git(repo, "add", path)
            pr_files.append(PRFile(path=path, change="added"))
        git(repo, "commit", "-qm", f"PR {number}")
        prs.append(MergedPR(number=number, title=f"pr {number}", body="",
                            merged_at=T0 + timedelta(minutes=number),
                            merge_commit_sha=git(repo, "rev-parse", "HEAD"),
                            files=tuple(pr_files)))
        compile_pr(repo, FakeForge(prs=list(prs)), expect_pr=number)

    return repo, slug, merge_pr


def _session_repo(slug: str):
    from knowledge_compiler.mcp.queries import resolve_repo

    session = Session(kcdb.make_engine())
    return session, resolve_repo(session, slug).id


def _last_delta(session: Session, repo_id: int) -> dict[tuple[str, str], dict]:
    rows = session.execute(text(
        "SELECT op, slug, evidence FROM delta_changes WHERE repo_id = :r AND compile_run_id = "
        "(SELECT max(id) FROM compile_runs WHERE repo_id = :r AND status = 'succeeded')"),
        {"r": repo_id}).all()
    return {(op, slug): evidence for op, slug, evidence in rows}


def test_pure_rename_pr_records_lineage_and_stale_slugs_resolve(env, tmp_path):
    from knowledge_compiler.compiler.run import verify
    from knowledge_compiler.mcp import queries
    from knowledge_compiler.validation import score_test

    repo, slug, merge_pr = env
    # modules nothing imports, so the rename leaves no dangling import behind
    merge_pr(1, {"billing/api.py": "billing/handlers.py",
                 "tests/test_rules.py": "tests/test_discount_rules.py"})

    session, repo_id = _session_repo(slug)
    with session:
        delta = _last_delta(session, repo_id)
        assert delta[("removed", "component/billing-api")] == {
            "renamed_to": "component/billing-handlers"}
        assert delta[("added", "component/billing-handlers")]["renamed_from"] == \
            "component/billing-api"
        assert delta[("removed", "wiki-page/component-billing-api")] == {
            "renamed_to": "wiki-page/component-billing-handlers"}
        # a pure rename removes nothing without naming its successor, and every
        # entity it adds (other than the PR record) names its predecessor
        removed = {s: ev for (op, s), ev in delta.items() if op == "removed"}
        added = {s: ev for (op, s), ev in delta.items() if op == "added"}
        assert removed and all(ev.get("renamed_to") for ev in removed.values())
        assert {ev["renamed_to"] for ev in removed.values()} == \
            {s for s in added if s != "pull-request/1"}
        assert any(s.startswith("test-coverage/") for s in removed)

        # stale slug -> successor, for agents and for the planners built on get_entity
        entity = queries.get_entity(session, repo_id, "component/billing-api")
        assert entity["slug"] == "component/billing-handlers"
        assert entity["renamed_from"] == "component/billing-api"
        assert queries.test_plan(session, repo_id, "component/billing-api")["slug"] == \
            "component/billing-handlers"
        untouched = queries.get_entity(session, repo_id, "component/billing-rules")
        assert untouched["slug"] == "component/billing-rules" and "renamed_from" not in untouched

        # a kc-covers citation of the old slug is scored as its successor, not as fake
        test_file = tmp_path / "test_generated.py"
        test_file.write_text('"""\nkc-covers:\n  - component/billing-api\n"""\n\n'
                             "def test_x():\n    assert True\n", encoding="utf-8")
        report = score_test(session, repo_id, test_file, "component/billing-handlers")
        assert report.renamed_claims == {"component/billing-api": "component/billing-handlers"}
        assert report.nonexistent_claims == []

        journey = queries.get_entity(session, repo_id, JOURNEY)["payload"]
        assert journey["steps"] == ["component/billing-handlers", "component/billing-rules"]
        assert journey["status"] == "complete"
        assert journey["renamed_steps"] == {"component/billing-api": "component/billing-handlers"}

    assert verify(repo).equivalent

    # lineage chains across compiles: the original slug follows both renames
    merge_pr(2, {"billing/handlers.py": "billing/endpoints.py"})
    session, repo_id = _session_repo(slug)
    with session:
        assert queries.get_entity(session, repo_id, "component/billing-api")["slug"] == \
            "component/billing-endpoints"
        journey = queries.get_entity(session, repo_id, JOURNEY)["payload"]
        assert journey["steps"] == ["component/billing-endpoints", "component/billing-rules"]
        assert journey["status"] == "complete"
    assert verify(repo).equivalent


def test_path_recreated_after_rename_keeps_its_own_slug(env):
    from knowledge_compiler.mcp import queries

    repo, slug, merge_pr = env
    # rename, and in the same PR put a new module at the old path: the old slug is
    # still observed, so it is not removed and there is nothing to pair
    merge_pr(1, {"billing/rules.py": "billing/discount_rules.py"},
             changes={"billing/rules.py": "def other():\n    return 1\n"})

    session, repo_id = _session_repo(slug)
    with session:
        delta = _last_delta(session, repo_id)
        assert ("removed", "component/billing-rules") not in delta
        assert "renamed_from" not in delta[("added", "component/billing-discount-rules")]
        assert queries.get_entity(session, repo_id, "component/billing-rules")["slug"] == \
            "component/billing-rules"
        assert "renamed_from" not in queries.get_entity(session, repo_id, "component/billing-rules")
