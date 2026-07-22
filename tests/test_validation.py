"""Tests for knowledge_compiler.validation (kc validate-test): scoring a
generated test's kc-covers: header against compiled knowledge.

Fixture pattern copied from test_retrieval_serve.py's `compiled` fixture,
but this one is deliberately built with two real, uncovered coverage gaps
(one api-kind, one symbols-kind) so test_plan's recommendations have real
content to score against -- test_retrieval_serve.py's fixture is fully
covered and has none.

These tests are also the first coverage anywhere in the repo for
queries.test_plan / queries.impact_plan (confirmed zero coverage before
this file) -- a side effect of needing real recommendations to validate
against, not separate scope.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

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


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def gapped(tmp_path: Path):
    """A fixture repo with a real, uncovered api-kind gap (component/billing-api,
    which depends on component/billing-rules) and a real, uncovered
    symbols-kind gap (component/billing-rules itself) -- no test files at all."""
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    (repo / "billing" / "api.py").write_text(
        "from billing.rules import apply_discount\n\n"
        "app = None\n\n\n"
        "@app.get(\"/discount\")\n"
        "def get_discount():\n    return apply_discount(10)\n",
        encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"val-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    summary = compile_full(repo)
    assert summary.warnings == []
    return repo, slug


def repo_id_of(session: Session, slug: str) -> int:
    from knowledge_compiler.mcp.queries import resolve_repo
    return resolve_repo(session, slug).id


def _plan(session, rid):
    from knowledge_compiler.mcp import queries
    plan = queries.test_plan(session, rid, "component/billing-rules")
    assert plan is not None
    assert set(plan["coverage_gaps"]) == {"component/billing-rules", "component/billing-api"}
    return plan


def _write_test(repo: Path, name: str, covers: list[str]) -> Path:
    header = "\n".join(f"  - {slug}" for slug in covers)
    body = f'"""\nkc-covers:\n{header}\n"""\n\ndef test_placeholder():\n    assert True\n'
    path = repo / "tests"
    path.mkdir(exist_ok=True)
    f = path / name
    f.write_text(body, encoding="utf-8")
    return f


def test_no_header_scores_zero(gapped):
    from knowledge_compiler.validation import score_test

    repo, slug = gapped
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        _plan(session, rid)
        f = repo / "tests" / "test_no_header.py"
        f.parent.mkdir(exist_ok=True)
        f.write_text('"""just a plain test, no kc-covers block"""\n\n'
                     'def test_x():\n    assert True\n', encoding="utf-8")
        report = score_test(session, rid, f, "component/billing-rules")
    assert report.header_found is False
    assert report.score_pct == 0.0
    assert report.claimed_slugs == []


def test_nonexistent_slug_fails_existence(gapped):
    from knowledge_compiler.validation import score_test

    repo, slug = gapped
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        _plan(session, rid)
        f = _write_test(repo, "test_bad.py", ["component/billing-rules", "component/does-not-exist"])
        report = score_test(session, rid, f, "component/billing-rules")
    assert report.header_found is True
    assert report.nonexistent_claims == ["component/does-not-exist"]
    assert report.score_pct < 100.0


def test_symbols_gap_collapses_to_component_slug(gapped):
    from knowledge_compiler.validation import score_test

    repo, slug = gapped
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        _plan(session, rid)
        f = _write_test(repo, "test_symbols.py", ["component/billing-rules"])
        report = score_test(session, rid, f, "component/billing-rules")
    assert "component/billing-rules" in report.citable_recommended
    assert report.recall == pytest.approx(0.5)  # covers the symbols gap, misses the api gap
    assert len(report.missing_from_claims) == 1
    assert report.missing_from_claims[0].startswith("api/")


def test_api_gap_requires_its_own_slug(gapped):
    from knowledge_compiler.validation import score_test

    repo, slug = gapped
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        plan = _plan(session, rid)
        api_recs = [r for r in plan["test_recommendations"] if r["target_kind"] == "api"]
        assert api_recs, plan["test_recommendations"]
        api_slug = api_recs[0]["targets"][0]["slug"]
        assert api_slug.startswith("api/")

        f = _write_test(repo, "test_api.py", [api_slug])
        report = score_test(session, rid, f, "component/billing-rules")
    assert report.precision == pytest.approx(1.0)
    assert report.recall == pytest.approx(0.5)
    assert "component/billing-rules" in report.missing_from_claims


def test_perfect_header_scores_100(gapped):
    from knowledge_compiler.validation import score_test

    repo, slug = gapped
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        plan = _plan(session, rid)
        from knowledge_compiler.validation import citable_targets_for
        citable = sorted({s for rec in plan["test_recommendations"] for s in citable_targets_for(rec)})

        f = _write_test(repo, "test_perfect.py", citable)
        report = score_test(session, rid, f, "component/billing-rules")
    assert report.precision == pytest.approx(1.0)
    assert report.recall == pytest.approx(1.0)
    assert report.score_pct == 100.0


def test_cli_validate_test_exit_codes(gapped):
    from click.testing import CliRunner

    from knowledge_compiler.cli import main

    repo, slug = gapped
    runner = CliRunner()

    clean = _write_test(repo, "test_clean.py", ["component/billing-rules", "component/billing-api"])
    result = runner.invoke(main, ["validate-test", str(clean), "--for-entity", "component/billing-rules",
                                  "--dir", str(repo)])
    assert result.exit_code == 0, result.output

    no_header = repo / "tests" / "test_missing_header.py"
    no_header.write_text('"""no header"""\n\ndef test_x():\n    assert True\n', encoding="utf-8")
    result = runner.invoke(main, ["validate-test", str(no_header), "--for-entity", "component/billing-rules",
                                  "--dir", str(repo)])
    assert result.exit_code == 1, result.output
