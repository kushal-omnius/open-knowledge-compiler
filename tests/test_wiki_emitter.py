"""Wiki emitter tests: OKF conformance, cross-links, dirty-only regeneration,
byte-determinism. Rendering is pure — tested without a database."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash
from knowledge_compiler.wiki.emitter import RunDelta, WikiContext, WikiEmitter, rel_link

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402

FINISHED_AT = datetime(2026, 8, 5, 14, 31, 43, tzinfo=timezone.utc)
CTX = WikiContext(repo_slug="repo-a", compile_run_id=7, commit_sha="abc123def456",
                 finished_at=FINISHED_AT)


@pytest.fixture()
def state():
    files = {
        "billing/__init__.py": "",
        "billing/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
        "billing/api.py": (
            "from fastapi import FastAPI\nfrom billing.rules import apply_discount\n"
            "app = FastAPI()\n\n"
            "@app.get(\"/discounts/{id}\")\ndef read_discount(id: int):\n"
            "    return apply_discount(id)\n"
        ),
        "tests/test_rules.py": (
            "from billing.rules import apply_discount\n\n"
            "def test_cap():\n    assert apply_discount(50) == 20\n"
        ),
    }
    artifacts = [Artifact(artifact_type="source_file", source_ref=r,
                          content_hash=content_hash({"c": c}), content=c)
                 for r, c in files.items()]
    facts = PythonAnalyzer().analyze(artifacts)
    return normalize(facts, CurrentState(), Thresholds(), repo_slug="repo-a")


def emit_all(state, tmp_path: Path, dirty=None) -> list[Path]:
    return WikiEmitter(tmp_path).emit(state.entities, state.relationships,
                                      dirty if dirty is not None else set(),
                                      [RunDelta(7, "abc123def456",
                                                (("added", "component/billing", "component"),),
                                                finished_at=FINISHED_AT)],
                                      CTX)


def test_pages_written_at_slug_paths(state, tmp_path):
    emit_all(state, tmp_path)
    assert (tmp_path / "component" / "billing-rules.md").is_file()
    assert (tmp_path / "api" / "get-discounts.md").is_file()
    assert (tmp_path / "index.md").is_file()
    assert (tmp_path / "log.md").is_file()
    assert (tmp_path / "recent-changes.md").is_file()


def test_okf_frontmatter(state, tmp_path):
    emit_all(state, tmp_path)
    text = (tmp_path / "component" / "billing-rules.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---", 2)[1]
    for line in ("type: component", "slug: component/billing-rules", "repo: repo-a",
                 "compile_run: 7", "by: process:knowledge-compiler/",
                 "at: 2026-08-05T14:31:43+00:00"):
        assert line in head
    assert "files:" in head and "- billing/rules.py" in head
    assert "do not edit" in text  # ADR-010 hand-edit warning


def test_index_has_no_general_frontmatter(state, tmp_path):
    """OKF v0.2 §8: index.md is a reserved filename — no frontmatter except the
    optional bundle-root okf_version."""
    emit_all(state, tmp_path)
    text = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---", 2)[1].strip()
    assert head.startswith("okf_version:")
    for forbidden in ("type: index", "compile_run:", "commit:", "title:"):
        assert forbidden not in head


def test_log_md_has_no_frontmatter_and_is_date_grouped(state, tmp_path):
    """OKF v0.2 §9: log.md is a reserved filename — no frontmatter, date-grouped
    ISO 8601 headings, prose entries."""
    emit_all(state, tmp_path)
    text = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert not text.startswith("---")
    assert "## 2026-08-05" in text
    assert "**Creation** `component/billing` (component)" in text


def test_recent_changes_scoped_to_last_compile(state, tmp_path):
    """recent-changes.md is a KC-specific convenience view (not a reserved OKF
    filename) — frontmatter is fine, scope is just the latest compile."""
    emit_all(state, tmp_path)
    text = (tmp_path / "recent-changes.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "Compile 7" in text
    assert "[log.md](log.md)" in text


def test_cross_links_are_relative_and_resolve(state, tmp_path):
    emit_all(state, tmp_path)
    api_page = (tmp_path / "api" / "get-discounts.md").read_text(encoding="utf-8")
    assert "(../component/billing-api.md)" in api_page  # defined_in link
    # every link target the emitter generates must exist on disk
    assert (tmp_path / "component" / "billing-api.md").is_file()


def test_component_page_content(state, tmp_path):
    emit_all(state, tmp_path)
    page = (tmp_path / "component" / "billing-rules.md").read_text(encoding="utf-8")
    assert "`billing.rules.apply_discount`" in page       # symbols table
    assert "## Covered by tests" in page                  # incoming covers edge
    assert "`tests/test_rules.py::test_cap`" in page      # non-page entity rendered as code


def test_dirty_only_regeneration(state, tmp_path):
    emit_all(state, tmp_path)  # bootstrap: everything
    rules_page = tmp_path / "component" / "billing-rules.md"
    api_page = tmp_path / "api" / "get-discounts.md"
    rules_page.write_text("STALE", encoding="utf-8")
    api_page.write_text("STALE", encoding="utf-8")

    emit_all(state, tmp_path, dirty={"component/billing-rules"})
    assert rules_page.read_text(encoding="utf-8") != "STALE"   # dirty: regenerated
    assert api_page.read_text(encoding="utf-8") == "STALE"     # clean: untouched


def test_emission_is_byte_deterministic(state, tmp_path):
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    emit_all(state, dir_a)
    emit_all(state, dir_b)
    files_a = sorted(p.relative_to(dir_a) for p in dir_a.rglob("*.md"))
    files_b = sorted(p.relative_to(dir_b) for p in dir_b.rglob("*.md"))
    assert files_a == files_b
    for rel in files_a:
        assert (dir_a / rel).read_bytes() == (dir_b / rel).read_bytes()


def test_rel_link_helper():
    assert rel_link("api/get-x", "component/y") == "../component/y.md"
    assert rel_link("component/a", "component/b") == "b.md"
