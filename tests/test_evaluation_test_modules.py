"""`kc eval` scores change detection and impact on source modules only: kc
compiles test files as components, but labels name the source modules whose
behavior changed, so edited tests must not count as false positives (frida
dogfood: 108 of 132 change-detection FPs were test modules).

kc-covers:
  - component/knowledge-compiler-evaluation

The pure test pins the per-language rule. The integration tests compile a real
git repo into real Postgres (same conventions as test_evaluation.py).
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.evaluation import _is_test_module, evaluate, load_labels
from knowledge_compiler.storage import db as kcdb


def test_test_module_rule_follows_each_analyzers_convention():
    assert _is_test_module({"language": "python", "files": ["tests/test_rules.py"]})
    assert _is_test_module({"language": "python", "files": ["pkg/rules_test.py"]})
    assert not _is_test_module({"language": "python", "files": ["tests/conftest.py"]})
    assert not _is_test_module({"language": "python", "files": ["billing/rules.py"]})
    assert _is_test_module({"language": "typescript", "files": ["src/__tests__/Card.test.tsx"]})
    assert _is_test_module({"language": "javascript", "files": ["src/app.spec.js"]})
    assert not _is_test_module({"language": "typescript", "files": ["src/widgets/types.ts"]})
    assert not _is_test_module({"language": "go", "files": ["x_test.go"]})  # no analyzer


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not running (docker compose up -d)")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rules.py").write_text(
        "from billing.rules import apply_discount\n\ndef test_cap():\n"
        "    assert apply_discount(50) == 20\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    _commit_all(repo, "baseline")

    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full
    slug = f"evt-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)
    # one change that edits the source module and its test together
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 30)\n", encoding="utf-8")
    (repo / "tests" / "test_rules.py").write_text(
        "from billing.rules import apply_discount\n\ndef test_cap():\n"
        "    assert apply_discount(50) == 30\n", encoding="utf-8")
    sha = _commit_all(repo, "raise discount cap")
    compile_full(repo, llm_provider=None)
    return repo, slug, sha


def _evaluate(repo_dir: Path, slug: str, labels_toml: str):
    from knowledge_compiler.mcp.queries import resolve_repo
    path = repo_dir / "kc-eval.toml"
    path.write_text(labels_toml, encoding="utf-8")
    with Session(kcdb.make_engine()) as session:
        return evaluate(session, resolve_repo(session, slug).id, load_labels(path))


@needs_db
def test_edited_test_module_is_excluded_not_a_false_positive(repo):
    repo_dir, slug, sha = repo
    report = _evaluate(repo_dir, slug, f'[[change]]\ncommit = "{sha[:12]}"\n'
                                       'changed = ["billing.rules"]\naffected = ["billing.rules"]\n')
    item = report.items[0]
    assert item["predicted_changed"] == ["billing.rules"]
    assert "tests.test_rules" in item["excluded_test_modules"]
    assert "tests.test_rules" not in item["predicted_affected"]
    detection = report.metrics["change_detection"]
    assert (detection.tp, detection.fp, detection.fn) == (1, 0, 0)


@needs_db
def test_label_naming_a_convention_test_module_is_scored(repo):
    """The naming convention can be wrong (frida's production workflow
    `cause_agent_test.py`), so a module the label names is scored, not dropped."""
    repo_dir, slug, sha = repo
    report = _evaluate(repo_dir, slug, f'[[change]]\ncommit = "{sha[:12]}"\n'
                                       'changed = ["billing.rules", "tests.test_rules"]\n'
                                       'affected = ["billing.rules", "tests.test_rules"]\n')
    item = report.items[0]
    assert sorted(item["predicted_changed"]) == ["billing.rules", "tests.test_rules"]
    assert "tests.test_rules" not in item["excluded_test_modules"]
    detection = report.metrics["change_detection"]
    assert (detection.tp, detection.fp, detection.fn) == (2, 0, 0)
