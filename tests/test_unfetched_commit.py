"""
A commit the forge lists but the local clone never fetched must fail the run,
not mass-remove the entities anchored to its in-scope files.

kc-covers:
  - component/knowledge-compiler-collectors-git
  - component/knowledge-compiler-compiler-run

Real git repos (the "forge" commit is made in a separate clone so its sha is
absent from the analyzed repo), real Postgres, FakeForge for the listing.
"""

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from knowledge_compiler.collectors.forge import CommitInfo, FakeForge, MergedPR, PRFile
from knowledge_compiler.collectors.git import GitCollector, GitCollectorError
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable — integration skipped")

T0 = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
MISSING_SHA = "0123456789abcdef0123456789abcdef01234567"
ITEMS = ("from fastapi import APIRouter\n"
         "router = APIRouter(prefix='/items')\n\n"
         "@router.get('/{item_id}')\ndef get_item(item_id: int): ...\n\n"
         "@router.post('')\ndef create_item(): ...\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "plain"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "gone.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    _commit_all(repo, "baseline")
    (repo / "pkg" / "gone.py").unlink()
    _commit_all(repo, "delete gone.py")
    return repo


# -- unit: GitCollector -----------------------------------------------------------------

def test_has_commit_true_and_false(plain_repo: Path):
    collector = GitCollector(plain_repo)
    assert collector.has_commit(collector.head_commit())
    assert not collector.has_commit(MISSING_SHA)


def test_collect_at_commit_skips_path_deleted_at_commit(plain_repo: Path):
    collector = GitCollector(plain_repo)
    artifacts = collector.collect_at_commit(
        collector.head_commit(), ["pkg/mod.py", "pkg/gone.py", "never/existed.py"])
    assert [a.source_ref for a in artifacts] == ["pkg/mod.py"]


def test_collect_at_commit_raises_for_missing_commit(plain_repo: Path):
    collector = GitCollector(plain_repo)
    with pytest.raises(GitCollectorError, match="not in the local clone"):
        collector.collect_at_commit(MISSING_SHA, ["pkg/mod.py"])


# -- integration: reconcile ---------------------------------------------------------------

@pytest.fixture()
def env(tmp_path: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "items.py").write_text(ITEMS, encoding="utf-8")
    (repo / "app" / "rules.py").write_text("def cap(x):\n    return min(x, 20)\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    _commit_all(repo, "baseline")
    slug = f"uf-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)

    # the "forge" commit lives only in a separate clone
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "t@e.st")
    git(clone, "config", "user.name", "t")
    (clone / "app" / "items.py").write_text(
        ITEMS + "\n@router.delete('/{item_id}')\ndef delete_item(item_id: int): ...\n",
        encoding="utf-8")
    (clone / "app" / "rules.py").write_text("def cap(x):\n    return min(x, 30)\n", encoding="utf-8")
    sha = _commit_all(clone, "tweak items and rules")
    assert not GitCollector(repo).has_commit(sha)
    return repo, clone, slug, sha


def _repo_id(session: Session, slug: str) -> int:
    from knowledge_compiler.storage.schema import Repository
    return session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()


def _entities(slug: str) -> dict[str, str]:
    from knowledge_compiler.storage.schema import EntityRow
    with Session(kcdb.make_engine()) as session:
        return dict(session.execute(
            select(EntityRow.slug, EntityRow.entity_type).where(
                EntityRow.repo_id == _repo_id(session, slug))).all())


def _watermark(slug: str):
    from knowledge_compiler.storage.schema import CompileRun
    with Session(kcdb.make_engine()) as session:
        stamp = func.coalesce(CompileRun.commit_timestamp, CompileRun.merged_at)
        return session.execute(
            select(func.max(stamp)).where(
                CompileRun.repo_id == _repo_id(session, slug),
                CompileRun.status == "succeeded", stamp.is_not(None))
        ).scalar_one_or_none()


def _run_statuses(slug: str, sha: str) -> list[str]:
    from knowledge_compiler.storage.schema import CompileRun
    with Session(kcdb.make_engine()) as session:
        return list(session.execute(
            select(CompileRun.status).where(
                CompileRun.repo_id == _repo_id(session, slug), CompileRun.commit_sha == sha
            ).order_by(CompileRun.id)).scalars())


def _assert_fails_then_recovers(env, forge: FakeForge):
    from knowledge_compiler.compiler.run import CompileError, reconcile

    repo, clone, slug, sha = env
    before = _entities(slug)
    assert "api" in before.values()  # the fixture really yields APIs
    watermark_before = _watermark(slug)

    with pytest.raises(CompileError, match="git fetch"):
        reconcile(repo, forge)

    assert _entities(slug) == before             # nothing removed
    assert _run_statuses(slug, sha) == ["failed"]
    assert _watermark(slug) == watermark_before  # watermark did not advance

    git(repo, "fetch", "-q", str(clone), "main")
    summaries = reconcile(repo, forge)
    assert len(summaries) == 1
    assert summaries[0].removed == 0
    assert set(before) <= set(_entities(slug))   # no mass removal
    assert _run_statuses(slug, sha) == ["failed", "succeeded"]


@needs_db
def test_unfetched_commit_fails_run_and_recovers_after_fetch(env):
    _, _, _, sha = env
    commit = CommitInfo(sha=sha, timestamp=T0, message="tweak items and rules",
                        files=("app/items.py", "app/rules.py"))
    _assert_fails_then_recovers(env, FakeForge(commits=[commit]))


@needs_db
def test_unfetched_pr_merge_commit_fails_run_and_recovers_after_fetch(env):
    _, _, _, sha = env
    pr = MergedPR(number=77, title="tweak items and rules", body="",
                  merged_at=T0 + timedelta(minutes=1), merge_commit_sha=sha,
                  files=(PRFile(path="app/items.py", change="modified"),
                         PRFile(path="app/rules.py", change="modified")))
    _assert_fails_then_recovers(env, FakeForge(prs=[pr]))
