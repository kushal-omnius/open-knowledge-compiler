"""
A full compile seeds the reconcile watermark (ADR-002 addendum, 2026-10-27),
so the next reconcile resumes after the compiled commit instead of replaying
every merged PR since the start of history — guarded to commits that are on
the default branch's upstream.

kc-covers:
  - component/knowledge-compiler-compiler-run
  - component/knowledge-compiler-collectors-git
"""

import subprocess
import uuid
from datetime import datetime, timedelta
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
    not _db_available(), reason="Postgres unreachable — integration skipped")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def _make_repo(tmp_path: Path, *, with_upstream: bool) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("")
    (repo / "billing" / "rules.py").write_text("def apply_discount(pct):\n    return min(pct, 20)\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    if with_upstream:
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")

    from knowledge_compiler.compiler.bootstrap import init_repository
    slug = f"wm-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    return repo, slug


def _latest_run(slug: str):
    from knowledge_compiler.storage.schema import CompileRun, Repository
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        return session.execute(select(CompileRun).where(CompileRun.repo_id == repo_id)
                               .order_by(CompileRun.id.desc()).limit(1)).scalar_one()


def _head_timestamp(repo: Path) -> datetime:
    return datetime.fromisoformat(git(repo, "log", "-1", "--format=%cI", "HEAD"))


def test_full_compile_on_pushed_default_branch_sets_watermark(tmp_path):
    from knowledge_compiler.compiler.run import compile_full

    repo, slug = _make_repo(tmp_path, with_upstream=True)
    summary = compile_full(repo)

    run = _latest_run(slug)
    assert run.scope == "full"
    assert run.commit_timestamp == _head_timestamp(repo)
    assert summary.watermark_note.startswith("set to ")
    assert summary.warnings == []


def test_reconcile_after_full_compile_skips_prs_merged_before_it(tmp_path):
    """The bug this fixes: with only a full compile, reconcile used to replay
    every merged PR from the start of history."""
    from knowledge_compiler.compiler.run import compile_full, reconcile

    repo, _ = _make_repo(tmp_path, with_upstream=True)
    compile_full(repo)
    full_ts = _head_timestamp(repo)
    baseline_sha = git(repo, "rev-parse", "HEAD")

    (repo / "billing" / "extra.py").write_text("def bonus():\n    return 1\n")
    git(repo, "add", "billing/extra.py")
    git(repo, "commit", "-qm", "Add bonus")
    new_sha = git(repo, "rev-parse", "HEAD")

    old_pr = MergedPR(number=1, title="Baseline", merged_at=full_ts - timedelta(days=1),
                      merge_commit_sha=baseline_sha,
                      files=(PRFile(path="billing/rules.py", change="added"),))
    new_pr = MergedPR(number=2, title="Add bonus", merged_at=full_ts + timedelta(minutes=1),
                      merge_commit_sha=new_sha,
                      files=(PRFile(path="billing/extra.py", change="added"),))

    summaries = reconcile(repo, FakeForge(prs=[old_pr, new_pr]))
    assert [s.pr_number for s in summaries] == [2]


def test_no_upstream_leaves_watermark_null(tmp_path):
    """Same as before this fix — reported in the summary, not as a warning,
    since a NULL watermark is the pre-existing behaviour, not a failure."""
    from knowledge_compiler.compiler.run import compile_full

    repo, slug = _make_repo(tmp_path, with_upstream=False)
    summary = compile_full(repo)

    assert _latest_run(slug).commit_timestamp is None
    assert summary.watermark_note.startswith("not set (default branch 'main' has no upstream")
    assert summary.warnings == []


def test_unpushed_head_leaves_watermark_null(tmp_path):
    """A watermark from a commit that isn't on the default branch's upstream
    could make reconcile skip default-branch PRs whose code was never
    compiled — so none is set."""
    from knowledge_compiler.compiler.run import compile_full

    repo, slug = _make_repo(tmp_path, with_upstream=True)
    (repo / "billing" / "local.py").write_text("X = 1\n")
    git(repo, "add", "billing/local.py")
    git(repo, "commit", "-qm", "local only")
    summary = compile_full(repo)

    assert _latest_run(slug).commit_timestamp is None
    assert "is not on origin/main" in summary.watermark_note
