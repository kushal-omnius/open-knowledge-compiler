"""`kc eval` (knowledge_compiler/evaluation.py): the baseline harness that turns
hand labels into precision/recall for covers, stale_coverage, change detection
and impact.

The harness is only useful if it can *disagree* with the compiler, so the
integration tests below are built around cases where compiled knowledge and
ground truth differ — an import-only test the compiler counts as coverage, and
a body-only edit the compiler's change detection can't see.

Pure parsing/scoring tests need nothing; integration tests use real Postgres,
real git, real tree-sitter (same conventions as test_qa_grounding.py).
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.evaluation import LabelError, Metric, evaluate, load_labels
from knowledge_compiler.storage import db as kcdb


# -- pure: labels + scoring --------------------------------------------------------

def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "kc-eval.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_metric_micro_averages_and_reports_undefined_as_none():
    m = Metric()
    m.add({"a", "b"}, {"a", "c"})   # tp a, fp b, fn c
    m.add(set(), {"d"})             # fn d
    assert (m.tp, m.fp, m.fn) == (1, 1, 2)
    assert m.precision == 0.5
    assert m.recall == pytest.approx(1 / 3)
    # nothing predicted and nothing expected is "no evidence", not a perfect score
    assert Metric().precision is None and Metric().recall is None and Metric().f1 is None


def test_change_label_needs_exactly_one_of_pr_or_commit(tmp_path):
    with pytest.raises(LabelError, match="exactly one"):
        load_labels(_write(tmp_path, '[[change]]\npr = 1\ncommit = "abc"\n'
                                     'changed = []\naffected = []\n'))
    with pytest.raises(LabelError, match="exactly one"):
        load_labels(_write(tmp_path, '[[change]]\nchanged = []\naffected = []\n'))


def test_changed_must_be_subset_of_affected(tmp_path):
    """A changed component always needs re-testing; letting a labeler omit it
    from `affected` would silently inflate impact recall."""
    with pytest.raises(LabelError, match="also be in `affected`"):
        load_labels(_write(tmp_path, '[[change]]\npr = 1\nchanged = ["a"]\naffected = ["b"]\n'))


def test_empty_or_missing_labels_fail_loud(tmp_path):
    with pytest.raises(LabelError, match="no \\[\\[coverage\\]\\]"):
        load_labels(_write(tmp_path, "# nothing labeled yet\n"))
    with pytest.raises(LabelError, match="not found"):
        load_labels(tmp_path / "missing.toml")


# -- integration: real compile, real labels ----------------------------------------

def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable (docker compose up -d) — integration skipped")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit_all(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path):
    """billing.rules is truly exercised by test_rules.py. test_smoke.py imports
    billing.rules but asserts nothing about it — import-based `covers` counts it
    anyway, which is exactly the false positive the eval must surface."""
    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rules.py").write_text(
        "from billing.rules import apply_discount\n\ndef test_cap():\n"
        "    assert apply_discount(50) == 20\n", encoding="utf-8")
    (repo / "tests" / "test_smoke.py").write_text(
        "import billing.rules\n\ndef test_imports():\n    assert True\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    commit_all(repo, "baseline")

    from knowledge_compiler.compiler.bootstrap import init_repository
    slug = f"val-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    return repo, slug


def _evaluate(repo_dir: Path, slug: str, labels_toml: str):
    from knowledge_compiler.mcp.queries import resolve_repo
    labels = load_labels(_write(repo_dir, labels_toml))
    with Session(kcdb.make_engine()) as session:
        return evaluate(session, resolve_repo(session, slug).id, labels)


@needs_db
def test_import_only_test_is_scored_as_a_covers_false_positive(repo):
    from knowledge_compiler.compiler.run import compile_full
    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=None)

    report = _evaluate(repo_dir, slug, '[[coverage]]\ncomponent = "billing.rules"\n'
                                       'tests = ["tests/test_rules.py"]\nstale = false\n')
    covers = report.metrics["covers"]
    assert (covers.tp, covers.fp, covers.fn) == (1, 1, 0)
    assert covers.precision == 0.5
    assert report.items[0]["predicted_tests"] == ["tests/test_rules.py", "tests/test_smoke.py"]


@needs_db
def test_symbol_level_change_is_detected_and_scored(repo):
    from knowledge_compiler.compiler.run import compile_full
    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=None)
    (repo_dir / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n\n\n"
        "def apply_bulk_discount(pct, qty):\n    return min(pct, 20) * qty\n", encoding="utf-8")
    sha = commit_all(repo_dir, "add bulk discount")
    compile_full(repo_dir, llm_provider=None)

    report = _evaluate(repo_dir, slug, f'[[change]]\ncommit = "{sha[:12]}"\n'
                                       'changed = ["billing.rules"]\naffected = ["billing.rules"]\n')
    assert report.metrics["change_detection"].recall == 1.0
    assert report.metrics["impact"].recall == 1.0


@needs_db
def test_body_only_change_is_detected(repo):
    """Review finding 1, fixed by per-symbol body_hash: a logic-only edit (same
    symbols, same imports) must still produce a component delta."""
    from knowledge_compiler.compiler.run import compile_full
    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=None)
    (repo_dir / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 30)\n", encoding="utf-8")
    sha = commit_all(repo_dir, "raise discount cap")
    compile_full(repo_dir, llm_provider=None)

    report = _evaluate(repo_dir, slug, f'[[change]]\ncommit = "{sha[:12]}"\n'
                                       'changed = ["billing.rules"]\naffected = ["billing.rules"]\n')
    assert report.metrics["change_detection"].recall == 1.0


@needs_db
def test_unknown_component_and_bootstrap_run_are_label_errors(repo):
    from knowledge_compiler.compiler.run import compile_full
    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=None)
    head = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()

    with pytest.raises(LabelError, match="aren't compiled"):
        _evaluate(repo_dir, slug, '[[coverage]]\ncomponent = "billing.rulez"\ntests = []\n')
    with pytest.raises(LabelError, match="bootstrap"):
        _evaluate(repo_dir, slug, f'[[change]]\ncommit = "{head[:12]}"\n'
                                  'changed = []\naffected = []\n')


@needs_db
def test_cli_prints_metric_table(repo):
    from knowledge_compiler.cli import main
    from knowledge_compiler.compiler.run import compile_full
    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=None)
    _write(repo_dir, '[[coverage]]\ncomponent = "billing.rules"\ntests = ["tests/test_rules.py"]\n')

    result = CliRunner().invoke(main, ["eval", "--dir", str(repo_dir)])
    assert result.exit_code == 0, result.output
    assert "covers" in result.output and "0.50" in result.output
