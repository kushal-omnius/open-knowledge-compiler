"""tsconfig path aliases in incremental compiles (frida dogfood finding): a
PR/commit compile that touches only a .ts file still resolves its "@/..."
imports as internal, so incremental state matches a full compile.

kc-covers:
  - component/knowledge-compiler-compiler-run
  - component/knowledge-compiler-extractors-typescript-analyzer

Integration only: real git, real Postgres, real tree-sitter, FakeForge. A full
compile, then a PR compile and a direct-commit compile that each touch only
src/pages/page.ts (never tsconfig.json). The aliased import must stay an
internal dependency, and `kc verify` must stay clean after each step.
"""

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_typescript")

TSCONFIG = ('{\n  // JSONC, like most real tsconfigs\n'
            '  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}},\n}\n')
UTIL = "export function slugify(s: string): string {\n  return s.toLowerCase();\n}\n"
PAGE = ("import { slugify } from '@/lib/util';\n\n"
        "export function title(s: string): string {\n  return slugify(s);\n}\n")


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _page_deps(slug: str) -> tuple[list[str], list[str]]:
    from knowledge_compiler.mcp.queries import resolve_repo
    from knowledge_compiler.storage.schema import EntityRow

    with Session(kcdb.make_engine()) as session:
        repo_id = resolve_repo(session, slug).id
        payload = next(p for p in session.execute(select(EntityRow.payload).where(
            EntityRow.repo_id == repo_id, EntityRow.entity_type == "component")).scalars()
            if p.get("path") == "src.pages.page")
    return payload["internal_dependencies"], payload["external_dependencies"]


@pytest.mark.skipif(not _db_available(), reason="Postgres unreachable — integration skipped")
def test_pr_and_commit_compiles_touching_only_ts_keep_alias_imports_internal(tmp_path):
    from knowledge_compiler.collectors.forge import CommitInfo, FakeForge, MergedPR, PRFile
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full, compile_pr, reconcile, verify

    repo = tmp_path / "repo"
    for rel, src in {"tsconfig.json": TSCONFIG, "src/lib/util.ts": UTIL,
                     "src/pages/page.ts": PAGE}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(src, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    slug = f"ts-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)
    assert _page_deps(slug) == (["src.lib.util"], [])

    # PR touching only page.ts — tsconfig.json is not in its file list
    (repo / "src/pages/page.ts").write_text(
        PAGE + "\nexport function upper(s: string): string {\n  return s.toUpperCase();\n}\n",
        encoding="utf-8")
    _git(repo, "commit", "-qam", "add upper")
    pr = MergedPR(number=3, title="add upper", body="",
                  merged_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                  merge_commit_sha=_git(repo, "rev-parse", "HEAD"),
                  files=(PRFile(path="src/pages/page.ts", change="modified"),))
    compile_pr(repo, FakeForge(prs=[pr]), expect_pr=3)
    assert _page_deps(slug) == (["src.lib.util"], [])
    report = verify(repo)
    assert report.equivalent, report.entity_divergences

    # direct commit touching only page.ts
    (repo / "src/pages/page.ts").write_text(
        PAGE + "\nexport function lower(s: string): string {\n  return s.toLowerCase();\n}\n",
        encoding="utf-8")
    _git(repo, "commit", "-qam", "add lower")
    commit = CommitInfo(sha=_git(repo, "rev-parse", "HEAD"),
                        timestamp=datetime(2026, 11, 2, tzinfo=timezone.utc),
                        message="add lower", files=("src/pages/page.ts",))
    reconcile(repo, FakeForge(prs=[pr], commits=[commit]))
    assert _page_deps(slug) == (["src.lib.util"], [])
    report = verify(repo)
    assert report.equivalent, report.entity_divergences
