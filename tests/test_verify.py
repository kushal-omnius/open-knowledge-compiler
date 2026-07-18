"""kc verify tests: the incremental ≡ full guarantee, and drift detection.

The headline test: after a chain of PR-incremental compiles, a shadow full
compile finds NOTHING to change — ADR-003/004's whole promise in one assertion.
"""

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
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
def repo_env(tmp_path: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"vf-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo)
    return repo, slug


def test_verify_equivalent_after_full_compile(repo_env):
    from knowledge_compiler.compiler.run import verify

    repo, _ = repo_env
    report = verify(repo)
    assert report.equivalent
    assert report.evidence_histogram.get("natural_key", 0) > 0


def test_verify_equivalent_after_incremental_chain(repo_env):
    """THE guarantee: PR-incremental history converges to full-compile state."""
    from knowledge_compiler.compiler.run import compile_pr, verify

    repo, _ = repo_env
    (repo / "billing" / "report.py").write_text(
        "from billing.rules import apply_discount\n\ndef report():\n    return apply_discount(1)\n",
        encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "PR 601")
    pr1 = MergedPR(601, "add report", T0 + timedelta(minutes=1), git(repo, "rev-parse", "HEAD"),
                   files=(PRFile(path="billing/report.py", change="added"),))

    git(repo, "rm", "-q", "billing/report.py")
    git(repo, "commit", "-qm", "PR 602")
    pr2 = MergedPR(602, "remove report", T0 + timedelta(minutes=2), git(repo, "rev-parse", "HEAD"),
                   files=(PRFile(path="billing/report.py", change="removed"),))

    compile_pr(repo, FakeForge(prs=[pr1, pr2]), expect_pr=602)
    assert verify(repo).equivalent  # add-then-remove chain converged exactly


def test_verify_detects_state_drift(repo_env):
    from knowledge_compiler.compiler.run import verify
    from knowledge_compiler.storage.schema import EntityRow, Repository

    repo, slug = repo_env
    # simulate drift: hand-corrupt a persisted entity (the thing verify exists to catch)
    with Session(kcdb.make_engine()) as session, session.begin():
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        session.execute(update(EntityRow)
                        .where(EntityRow.repo_id == repo_id,
                               EntityRow.slug == "component/billing-rules")
                        .values(content_hash="corrupted"))

    report = verify(repo)
    assert not report.equivalent
    assert ("changed", "component/billing-rules") in report.entity_divergences
