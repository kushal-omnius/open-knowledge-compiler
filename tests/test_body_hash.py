"""Per-symbol body_hash (review finding 1): logic edits must change a component's
content_hash, while comment and whitespace edits must not.

kc-covers:
  - component/knowledge-compiler-extractors-body-hash

Analyzer tests use real tree-sitter on fixture strings. The no-delta test runs
a real compile twice against real git and Postgres (same conventions as
test_evaluation.py).
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.ir import Artifact, content_hash
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.javascript_analyzer import JavaScriptAnalyzer  # noqa: E402
from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer  # noqa: E402


def _hashes(analyzer, ref: str, source: str) -> dict[str, str]:
    art = Artifact(artifact_type="source_file", source_ref=ref,
                   content_hash=content_hash({"c": source}), content=source)
    return {f.payload["symbol_path"]: f.payload["body_hash"]
            for f in analyzer.analyze([art]) if f.fact_type == "symbol_observed"}


# -- Python ------------------------------------------------------------------------

PY = "def cap(pct):\n    return min(pct, 20)\n\n\ndef other():\n    return 1\n"


def test_python_body_hash_is_16_hex_and_tracks_logic_only():
    base = _hashes(PythonAnalyzer(), "billing/rules.py", PY)
    assert len(base["billing.rules.cap"]) == 16
    int(base["billing.rules.cap"], 16)

    edited = _hashes(PythonAnalyzer(), "billing/rules.py", PY.replace("20", "30"))
    assert edited["billing.rules.cap"] != base["billing.rules.cap"]
    # an untouched sibling keeps its hash: churn is per symbol, not per file
    assert edited["billing.rules.other"] == base["billing.rules.other"]


def test_python_comments_and_whitespace_do_not_change_body_hash():
    noisy = ("# leading comment\n"
             "def cap(pct):   # trailing\n"
             "    # explain the cap\n"
             "    return min( pct,20 )\n\n\n\n"
             "def other():\n    return 1\n")
    assert _hashes(PythonAnalyzer(), "billing/rules.py", noisy) == \
        _hashes(PythonAnalyzer(), "billing/rules.py", PY)


def test_python_dedent_changes_body_hash():
    """Indentation is structure in Python, and moving b() out of the `if` is a
    real behavior change, even though the leaf tokens are identical."""
    inside = "def f(x):\n    if x:\n        a()\n        b()\n"
    outside = "def f(x):\n    if x:\n        a()\n    b()\n"
    assert _hashes(PythonAnalyzer(), "m.py", inside)["m.f"] != \
        _hashes(PythonAnalyzer(), "m.py", outside)["m.f"]


def test_python_method_edit_changes_method_and_enclosing_class():
    src = "class Svc:\n    def total(self, xs):\n        return sum(xs)\n"
    base = _hashes(PythonAnalyzer(), "svc.py", src)
    edited = _hashes(PythonAnalyzer(), "svc.py", src.replace("sum(xs)", "sum(xs) + 1"))
    assert edited["svc.Svc.total"] != base["svc.Svc.total"]
    assert edited["svc.Svc"] != base["svc.Svc"]


# -- TypeScript / JavaScript --------------------------------------------------------

TS = ("export function cap(pct: number): number {\n  return Math.min(pct, 20);\n}\n"
      "export const fmt = (x: number) => `${x}`;\n"
      "export class Svc {\n  total(xs: number[]) { return xs.length; }\n}\n")


def test_typescript_body_hash_tracks_logic_not_comments():
    base = _hashes(TypeScriptAnalyzer(), "src/rules.ts", TS)
    assert set(base) == {"src.rules.cap", "src.rules.fmt", "src.rules.Svc", "src.rules.Svc.total"}

    noisy = TS.replace("{\n  return", "{ // why\n  /* block */ return  ")
    assert _hashes(TypeScriptAnalyzer(), "src/rules.ts", noisy) == base

    edited = _hashes(TypeScriptAnalyzer(), "src/rules.ts", TS.replace("20", "30"))
    assert edited["src.rules.cap"] != base["src.rules.cap"]
    assert edited["src.rules.fmt"] == base["src.rules.fmt"]


JS = ("function cap(pct) {\n  return Math.min(pct, 20);\n}\n"
      "module.exports = { cap, total: function (xs) { return xs.length; } };\n")


def test_javascript_body_hash_tracks_logic_not_comments():
    base = _hashes(JavaScriptAnalyzer(), "src/rules.js", JS)
    assert "src.rules.cap" in base and "src.rules.total" in base

    noisy = "// header\n" + JS.replace("return Math", "/* c */\n    return Math")
    assert _hashes(JavaScriptAnalyzer(), "src/rules.js", noisy) == base

    edited = _hashes(JavaScriptAnalyzer(), "src/rules.js", JS.replace("xs.length", "xs.length + 1"))
    assert edited["src.rules.total"] != base["src.rules.total"]
    assert edited["src.rules.cap"] == base["src.rules.cap"]


# -- integration: comment/whitespace-only commit produces no delta ------------------

def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable (docker compose up -d) — integration skipped")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


RULES = ("from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
         "def apply_discount(pct):\n    return min(pct, 20)\n\n\n"
         "@app.get('/discount')\ndef discount():\n    return apply_discount(5)\n")

# same tree, but with comments added and whitespace and line numbers changed:
# every span below the header shifts
RULES_NOISY = ("# Billing rules.\n"
               "from fastapi import FastAPI\n\napp = FastAPI()  # the app\n\n\n\n"
               "def apply_discount(pct):\n    # cap discounts at 20%\n"
               "    return min(pct,   20)\n\n\n"
               "@app.get('/discount')\ndef discount():\n    return apply_discount(5)  # demo\n")


@needs_db
def test_comment_and_whitespace_only_commit_produces_no_delta(tmp_path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp.queries import resolve_repo
    from knowledge_compiler.storage.schema import CompileRun, DeltaChangeRow

    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(RULES, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rules.py").write_text(
        "from billing.rules import apply_discount\n\ndef test_cap():\n"
        "    assert apply_discount(50) == 20\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "baseline")
    slug = f"bh-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)

    (repo / "billing" / "rules.py").write_text(RULES_NOISY, encoding="utf-8")
    _commit(repo, "comments only")
    compile_full(repo, llm_provider=None)

    with Session(kcdb.make_engine()) as session:
        repo_id = resolve_repo(session, slug).id
        runs = session.execute(select(CompileRun.id).where(
            CompileRun.repo_id == repo_id, CompileRun.status == "succeeded")
            .order_by(CompileRun.id)).scalars().all()
        assert len(runs) == 2
        changes = session.execute(select(DeltaChangeRow.slug).where(
            DeltaChangeRow.compile_run_id == runs[-1])).scalars().all()
    assert changes == []
