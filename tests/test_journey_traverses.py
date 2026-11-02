"""Journey `traverses` edges survive a PR that leaves some steps untouched.

A journey is re-observed on every compile, so Diff treats its outgoing edges as
authoritative. Normalize used to emit `traverses` only to steps observed in the
compile's own slice, so a PR touching one step's file deleted the edges to all
other steps, and the next `kc verify` diverged (normalize.md §7).

kc-covers:
  - component/knowledge-compiler-compiler-normalize

Real git repo, real Postgres, FakeForge for the merged-PR metadata.
"""

import json
import subprocess
import uuid
from datetime import datetime, timezone
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

JOURNEY = "user-journey/apply-discount"
STEPS = ["component/billing-rules", "component/billing-api"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def _traverses(slug: str) -> set[str]:
    from knowledge_compiler.mcp import queries

    with Session(kcdb.make_engine()) as session:
        repo_id = queries.resolve_repo(session, slug).id
        entity = queries.get_entity(session, repo_id, JOURNEY)
    return {r["to"] for r in entity["relationships"] if r["relation"] == "traverses"}


def test_pr_touching_one_step_keeps_edges_to_the_others(tmp_path: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full, compile_pr, verify

    repo = tmp_path / "repo"
    files = {
        "billing/__init__.py": "",
        "billing/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
        "billing/api.py": ("from billing.rules import apply_discount\n\n"
                           "def handle(x):\n    return apply_discount(x)\n"),
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
    with (repo / "kc.toml").open("a", encoding="utf-8") as f:
        f.write(f'\n[[journeys]]\nname = "Apply discount"\nsteps = {json.dumps(STEPS)}\n')
    compile_full(repo)
    assert _traverses(slug) == set(STEPS)

    # touches billing/rules.py only: billing/api.py is out of the PR's scope
    (repo / "billing/rules.py").write_text("def apply_discount(pct):\n    return min(pct, 25)\n",
                                           encoding="utf-8")
    git(repo, "commit", "-qam", "PR 1")
    pr = MergedPR(number=1, title="pr 1", body="",
                  merged_at=datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc),
                  merge_commit_sha=git(repo, "rev-parse", "HEAD"),
                  files=(PRFile(path="billing/rules.py", change="modified"),))
    compile_pr(repo, FakeForge(prs=[pr]), expect_pr=1)

    assert _traverses(slug) == set(STEPS)
    assert verify(repo).equivalent
