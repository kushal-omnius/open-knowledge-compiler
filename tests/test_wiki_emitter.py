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
                                      dirty,
                                      [RunDelta(7, "abc123def456",
                                                (("added", "component/billing", "component", {}),),
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


def test_log_md_lists_multiple_changes_as_separate_bullets(state, tmp_path):
    """Regression: multiple change entries within one compile run must render
    as distinct Markdown list items, not bare consecutive lines — the latter
    collapses into one run-on paragraph under standard Markdown rendering."""
    recent = [RunDelta(7, "abc123def456",
                       (("added", "component/billing", "component", {}),
                        ("changed", "component/billing-rules", "component", {}),
                        ("removed", "feature/legacy-discount", "feature", {})),
                       finished_at=FINISHED_AT)]
    WikiEmitter(tmp_path).emit(state.entities, state.relationships, None, recent, CTX)
    text = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "- **Creation** `component/billing` (component)" in text
    assert "- **Update** `component/billing-rules` (component)" in text
    assert "- **Deprecation** `feature/legacy-discount` (feature)" in text


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


def test_empty_dirty_set_skips_every_page(state, tmp_path):
    """Regression: a genuinely empty dirty set (a real no-op compile, per
    compute_diff) must skip every page, not render everything. This is
    distinct from dirty=None ('no filter' — used by --emit-only), which
    must still force a full rerender below."""
    emit_all(state, tmp_path)  # bootstrap: everything
    rules_page = tmp_path / "component" / "billing-rules.md"
    api_page = tmp_path / "api" / "get-discounts.md"
    rules_page.write_text("STALE", encoding="utf-8")
    api_page.write_text("STALE", encoding="utf-8")

    emit_all(state, tmp_path, dirty=set())
    assert rules_page.read_text(encoding="utf-8") == "STALE"   # nothing dirty: untouched
    assert api_page.read_text(encoding="utf-8") == "STALE"     # nothing dirty: untouched


def test_none_dirty_forces_full_rerender(state, tmp_path):
    """dirty=None means 'no filter' (--emit-only's spec-version rollout path,
    ADR-013) — every page rerenders even though nothing is actually dirty."""
    emit_all(state, tmp_path)  # bootstrap: everything
    rules_page = tmp_path / "component" / "billing-rules.md"
    api_page = tmp_path / "api" / "get-discounts.md"
    rules_page.write_text("STALE", encoding="utf-8")
    api_page.write_text("STALE", encoding="utf-8")

    emit_all(state, tmp_path, dirty=None)
    assert rules_page.read_text(encoding="utf-8") != "STALE"   # forced rerender
    assert api_page.read_text(encoding="utf-8") != "STALE"     # forced rerender


def test_orphaned_page_pruned_when_owner_removed(state, tmp_path):
    """Regression: emission is otherwise dirty-only/additive (ir.md) — without
    explicit pruning, a removed entity's page survives on disk forever, frozen
    at whatever content it had when last written (e.g. a pre-migration
    frontmatter shape), since it's never regenerated (owner-is-None skip) and
    was never deleted either."""
    emit_all(state, tmp_path)  # bootstrap: everything, including component/billing-rules
    rules_page = tmp_path / "component" / "billing-rules.md"
    api_page = tmp_path / "api" / "get-discounts.md"
    assert rules_page.is_file()
    assert api_page.is_file()

    remaining_entities = [e for e in state.entities
                          if e.slug != "component/billing-rules"
                          and e.payload.get("owner_slug") != "component/billing-rules"]
    remaining_relationships = [r for r in state.relationships
                               if r.from_slug != "component/billing-rules"
                               and r.to_slug != "component/billing-rules"]
    WikiEmitter(tmp_path).emit(
        remaining_entities, remaining_relationships, None,
        [RunDelta(8, "def456abc789",
                  (("removed", "component/billing-rules", "component", {}),),
                  finished_at=FINISHED_AT)],
        CTX)
    assert not rules_page.is_file()   # orphan: pruned
    assert api_page.is_file()         # still current: untouched


def test_recent_history_section_is_bounded_and_entity_scoped(state, tmp_path):
    """Item 5 of the QA-agent-grounding backlog: a bounded 'Recent history'
    section on the entity's own page, sourced from the same `recent` delta
    window log.md/recent-changes.md already use — filtered to this entity's
    slug, capped, with a pointer to log.md for anything older."""
    recent = [
        RunDelta(9, "cafebabe0001",
                (("changed", "component/billing-rules", "component",
                  {"symbols": {"old": [], "new": ["x"]}}),),
                finished_at=datetime(2026, 8, 6, 9, 0, 0, tzinfo=timezone.utc)),
        RunDelta(7, "abc123def456",
                (("added", "component/billing-rules", "component", {}),
                 ("added", "component/billing", "component", {})),
                finished_at=FINISHED_AT),
    ]
    WikiEmitter(tmp_path).emit(state.entities, state.relationships, None, recent, CTX)
    page = (tmp_path / "component" / "billing-rules.md").read_text(encoding="utf-8")
    assert "## Recent history" in page
    assert "2026-08-06" in page and "**Update**" in page and "compile run 9" in page
    assert "2026-08-05" in page and "**Creation**" in page and "compile run 7" in page
    assert "`symbols`: [] → ['x']" in page
    # a page with fewer entries than the per-page cap has no "see log.md" pointer
    assert "Full chronological history" not in page

    # a page not touched by any change entry gets no section at all
    api_page = (tmp_path / "api" / "get-discounts.md").read_text(encoding="utf-8")
    assert "## Recent history" not in api_page


def test_recent_history_section_caps_and_points_to_log(state, tmp_path):
    many = [RunDelta(n, f"deadbeef{n:04d}",
                     (("changed", "component/billing-rules", "component", {}),),
                     finished_at=FINISHED_AT)
            for n in range(10, 0, -1)]  # newest-first, 10 entries touching this entity
    WikiEmitter(tmp_path).emit(state.entities, state.relationships, None, many, CTX)
    page = (tmp_path / "component" / "billing-rules.md").read_text(encoding="utf-8")
    assert page.count("compile run") == 5          # capped at _RECENT_HISTORY_LIMIT
    assert "compile run 10" in page                 # kept the newest 5 (10..6)
    assert "compile run 6" in page
    assert "compile run 5" not in page               # not the oldest
    assert "Full chronological history: [log.md](log.md)." in page


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


# --- ADR-014: emitter self-checks against the shared rules file -----------------


def test_frontmatter_self_check_catches_drift(state, tmp_path, monkeypatch):
    """If okf_rules.py ever gains a required concept field this emitter
    doesn't populate, emission must fail loudly here — not silently produce
    non-conformant pages only `kc validate-okf` would catch downstream."""
    from knowledge_compiler.wiki import emitter as emitter_mod
    from knowledge_compiler.wiki.okf_rules import OKFRules

    drifted = OKFRules(spec_version="0.2", reserved_files={},
                       concept_required_fields=("type", "provenance"))
    monkeypatch.setattr(emitter_mod, "OKF_V0_2_RULES", drifted)

    with pytest.raises(ValueError, match="missing required field"):
        emit_all(state, tmp_path)


def test_index_self_check_catches_drift(state, tmp_path, monkeypatch):
    """If okf_rules.py ever stops allowing 'okf_version' on index.md, the
    renderer that still writes it must fail loudly, not silently emit a key
    the validator would then reject."""
    from knowledge_compiler.wiki import emitter as emitter_mod
    from knowledge_compiler.wiki.okf_rules import OKFRules, ReservedFileRule

    drifted = OKFRules(spec_version="0.2",
                       reserved_files={"index.md": ReservedFileRule(allowed_keys=frozenset()),
                                       "log.md": ReservedFileRule(allowed_keys=frozenset())},
                       concept_required_fields=("type",))
    monkeypatch.setattr(emitter_mod, "OKF_V0_2_RULES", drifted)

    with pytest.raises(ValueError, match="not in okf_rules.py's allowed set"):
        emit_all(state, tmp_path)
