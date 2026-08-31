"""Items 1, 6, 7 of the QA-agent-grounding backlog (see BRAINSTORM-test-generation-*
and the ADR-018 stale-test-detection design): business/feature/risk context inlined
into `test_plan` recommendations, and stale-test detection via the already-denormalized
`last_compile_run_id` field (no schema change, no delta_changes join required —
see `linked_context`/`coverage_for` docstrings in mcp/queries.py for the reasoning).

Real Postgres, real git repos, FakeLLMProvider (no network) — same conventions as
test_llm_layer.py / test_retrieval_serve.py.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.llm.provider import FakeLLMProvider
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


def repo_id_of(session: Session, slug: str) -> int:
    from knowledge_compiler.mcp.queries import resolve_repo
    return resolve_repo(session, slug).id


PRICING_OUTPUT = {
    "business_rules": [{
        "name": "Pricing floor",
        "statement": "Total price must never go below zero.",
        "intent": "Prevent refund-abuse via negative totals.",
        "symbol_paths": ["billing.pricing.calculate_total"],
        "related_components": ["billing.pricing"],
    }],
    "features": [{
        "name": "Order pricing",
        "narrative": "Computes the total charge for a customer's order.",
        "symbol_paths": ["billing.pricing.calculate_total"],
        "related_components": ["billing.pricing"],
    }],
    "risks": [{
        "name": "Unbounded input",
        "description": "calculate_total has no upper bound check on item count.",
        "category": "input-validation",
        "symbol_paths": ["billing.pricing.calculate_total"],
        "related_components": ["billing.pricing"],
    }],
}
RULES_OUTPUT = {
    "business_rules": [{
        "name": "Discount cap",
        "statement": "Discounts are capped at 20 percent.",
        "intent": "Protect margins from excessive discounting.",
        "symbol_paths": ["billing.rules.apply_discount"],
        "related_components": ["billing.rules"],
    }],
    "features": [], "risks": [],
}


@pytest.fixture()
def repo(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    # uncovered on purpose — this is the coverage-gap target for context enrichment
    (repo / "billing" / "pricing.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rules.py").write_text(
        "from billing.rules import apply_discount\n\ndef test_cap():\n"
        "    assert apply_discount(50) == 20\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    from knowledge_compiler.compiler.bootstrap import init_repository
    slug = f"val-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    config = (repo / "kc.toml").read_text(encoding="utf-8").replace(
        'enabled = false\nprovider = "anthropic"', 'enabled = true\nprovider = "anthropic"')
    assert "enabled = true" in config, "kc.toml template changed — fix the toggle above"
    (repo / "kc.toml").write_text(config, encoding="utf-8")
    return repo, slug


def _provider():
    return FakeLLMProvider(responses={
        "billing/rules.py": RULES_OUTPUT,
        "billing/pricing.py": PRICING_OUTPUT,
    })


def test_linked_context_surfaces_business_rules_features_and_risks(repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    summary = compile_full(repo_dir, llm_provider=_provider())
    assert summary.warnings == []

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        ctx = queries.linked_context(session, rid, "component/billing-pricing")

    assert ctx["business_rules"] == [{
        "slug": "business-rule/pricing-floor", "name": "Pricing floor",
        "statement": "Total price must never go below zero.",
        "intent": "Prevent refund-abuse via negative totals.",
    }]
    assert ctx["features"] == [{
        "slug": "feature/order-pricing", "name": "Order pricing",
        "narrative": "Computes the total charge for a customer's order.",
    }]
    assert ctx["risks"] == [{
        "slug": "risk/unbounded-input", "name": "Unbounded input",
        "description": "calculate_total has no upper bound check on item count.",
        "category": "input-validation",
    }]


def test_test_plan_inlines_context_for_coverage_gaps(repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        plan = queries.test_plan(session, rid, "component/billing-pricing")

    assert "component/billing-pricing" in plan["coverage_gaps"]
    rec = next(r for r in plan["test_recommendations"] if r["component"] == "component/billing-pricing")
    assert rec["target_kind"] == "symbols"
    assert rec["context"]["business_rules"][0]["slug"] == "business-rule/pricing-floor"
    assert rec["context"]["features"][0]["slug"] == "feature/order-pricing"
    assert rec["context"]["risks"][0]["slug"] == "risk/unbounded-input"


def test_coverage_for_flags_fresh_test_as_not_stale(repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        cov = queries.coverage_for(session, rid, "component/billing-rules")

    assert cov["covered"] is True
    assert cov["stale"] is False
    assert cov["tests"][0]["stale"] is False


def test_stale_test_detected_when_component_changes_without_its_test(repo):
    """ADR-018: component/billing-rules changes in a later compile than its
    covering test — the test's last_compile_run_id (run 1) predates the
    component's (run 2), so it must be flagged stale, distinct from uncovered."""
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    compile_full(repo_dir, llm_provider=_provider())  # run 1: baseline

    # change billing/rules.py's deterministic surface (a new symbol changes the
    # component's `symbols` payload/content_hash) only — tests/test_rules.py is
    # untouched, so its last_compile_run_id must not move.
    (repo_dir / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n\n\n"
        "def apply_bulk_discount(pct, qty):\n    return min(pct, 20) * qty\n",
        encoding="utf-8")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add bulk discount helper")
    compile_full(repo_dir, llm_provider=_provider())  # run 2: component changes, test doesn't

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        cov = queries.coverage_for(session, rid, "component/billing-rules")
        plan = queries.test_plan(session, rid, "component/billing-rules")

    assert cov["covered"] is True
    assert cov["stale"] is True
    assert cov["tests"][0]["stale"] is True

    assert "component/billing-rules" not in plan["coverage_gaps"]  # distinct from "no coverage"
    assert "component/billing-rules" in plan["stale_coverage"]
    stale_rec = next(r for r in plan["test_recommendations"]
                     if r["component"] == "component/billing-rules"
                     and r["target_kind"] == "stale_retest")
    assert stale_rec["targets"] == ["test-coverage/tests-test-rules-py-test-cap"]
    assert stale_rec["context"]["business_rules"][0]["slug"] == "business-rule/discount-cap"


# -- item 2: mutation-kill rate ingestion + surfacing --------------------------------

def test_mutation_scores_attach_to_matching_component_and_surface_in_coverage_for(repo):
    """Item 2: opt-in mutation-score ingestion (collectors/mutation.py) never
    executes the target repo's code — it reads a JSON summary a CI job already
    produced and attaches it to the matching Component, surfaced inline in
    coverage_for/test_plan instead of only in a separate CI artifact."""
    import json

    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    config = (repo_dir / "kc.toml").read_text(encoding="utf-8").replace(
        "enabled = false\nscores_file", "enabled = true\nscores_file")
    assert "enabled = true\nscores_file" in config, "kc.toml template changed — fix the toggle above"
    (repo_dir / "kc.toml").write_text(config, encoding="utf-8")
    (repo_dir / "mutation-scores.json").write_text(json.dumps({
        "billing.rules": {"killed": 3, "survived": 7, "timeout": 0},   # 30% — low
    }), encoding="utf-8")

    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        cov = queries.coverage_for(session, rid, "component/billing-rules")
        plan = queries.test_plan(session, rid, "component/billing-rules")

    assert cov["mutation_kill_rate"] == 0.3
    assert cov["low_mutation_kill"] is True

    assert "component/billing-rules" in plan["low_mutation_kill"]
    rec = next(r for r in plan["test_recommendations"]
              if r["component"] == "component/billing-rules"
              and r["target_kind"] == "low_mutation_kill")
    assert rec["mutation_kill_rate"] == 0.3
    assert rec["targets"] == ["test-coverage/tests-test-rules-py-test-cap"]
    assert rec["context"]["business_rules"][0]["slug"] == "business-rule/discount-cap"


def test_mutation_scores_for_unobserved_module_are_silently_skipped(repo):
    """A scores-file entry naming a module not observed this compile (typo, or
    a module removed since) must not raise — same silent-skip class as the
    external-dependency/unresolved-coverage-target precedents in normalize.py."""
    import json

    from knowledge_compiler.compiler.run import compile_full

    repo_dir, slug = repo
    config = (repo_dir / "kc.toml").read_text(encoding="utf-8").replace(
        "enabled = false\nscores_file", "enabled = true\nscores_file")
    (repo_dir / "kc.toml").write_text(config, encoding="utf-8")
    (repo_dir / "mutation-scores.json").write_text(json.dumps({
        "billing.does_not_exist": {"killed": 1, "survived": 1, "timeout": 0},
    }), encoding="utf-8")

    summary = compile_full(repo_dir, llm_provider=_provider())
    assert summary.warnings == []


# -- items 3+4: deterministic UserJourney entity + end-to-end coverage --------------

def _add_second_covered_component(repo_dir: Path) -> None:
    """billing/checkout.py + a test that imports BOTH rules and checkout —
    the only test in this fixture that can satisfy end-to-end journey coverage."""
    (repo_dir / "billing" / "checkout.py").write_text(
        "from billing.rules import apply_discount\n\n"
        "def checkout(pct):\n    return 100 - apply_discount(pct)\n", encoding="utf-8")
    (repo_dir / "tests" / "test_checkout.py").write_text(
        "from billing.checkout import checkout\nfrom billing.rules import apply_discount\n\n"
        "def test_checkout_applies_discount():\n    assert checkout(10) == 90\n",
        encoding="utf-8")


def _set_journeys(repo_dir: Path, steps: list[str]) -> None:
    import json

    config = (repo_dir / "kc.toml").read_text(encoding="utf-8")
    config += ('\n[[journeys]]\nname = "Apply discount at checkout"\nsteps = '
              + json.dumps(steps) + "\n")
    (repo_dir / "kc.toml").write_text(config, encoding="utf-8")


def test_journey_entity_compiled_from_kc_toml(repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    _add_second_covered_component(repo_dir)
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add checkout")
    _set_journeys(repo_dir, ["component/billing-rules", "component/billing-checkout"])

    summary = compile_full(repo_dir, llm_provider=_provider())
    assert summary.warnings == []

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        journey = queries.get_entity(session, rid, "user-journey/apply-discount-at-checkout")

    assert journey is not None
    assert journey["payload"]["steps"] == ["component/billing-rules", "component/billing-checkout"]
    assert {"relation": "traverses", "from": "user-journey/apply-discount-at-checkout",
            "to": "component/billing-rules"} in journey["relationships"]


def test_journey_gap_when_no_single_test_covers_every_step(repo):
    """Both steps are individually covered — by test_rules.py and
    test_checkout.py separately — but no ONE test covers both: the
    structural test-slip this feature exists to catch."""
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    (repo_dir / "billing" / "checkout.py").write_text(
        "from billing.rules import apply_discount\n\n"
        "def checkout(pct):\n    return 100 - apply_discount(pct)\n", encoding="utf-8")
    # deliberately narrow: only imports checkout, not rules — the gap stays real
    (repo_dir / "tests" / "test_checkout.py").write_text(
        "from billing.checkout import checkout\n\n"
        "def test_checkout():\n    assert checkout(10) == 90\n", encoding="utf-8")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add checkout")
    _set_journeys(repo_dir, ["component/billing-rules", "component/billing-checkout"])

    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        jc = queries.journey_coverage(session, rid, "user-journey/apply-discount-at-checkout")
        plan = queries.test_plan(session, rid, "user-journey/apply-discount-at-checkout")

    assert jc["step_components"] == ["component/billing-checkout", "component/billing-rules"]
    assert jc["covered_end_to_end"] is False

    journey_recs = [r for r in plan["test_recommendations"] if r["target_kind"] == "journey"]
    assert len(journey_recs) == 1
    assert journey_recs[0]["component"] == "user-journey/apply-discount-at-checkout"
    assert set(journey_recs[0]["targets"]) == {"component/billing-checkout", "component/billing-rules"}


def test_journey_satisfied_when_one_test_covers_every_step(repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    _add_second_covered_component(repo_dir)  # test_checkout.py imports BOTH modules here
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add checkout")
    _set_journeys(repo_dir, ["component/billing-rules", "component/billing-checkout"])

    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        jc = queries.journey_coverage(session, rid, "user-journey/apply-discount-at-checkout")
        # querying from a STEP's own slug must also surface the journey's state
        plan = queries.test_plan(session, rid, "component/billing-checkout")

    assert jc["covered_end_to_end"] is True
    assert jc["covering_tests"] == ["test-coverage/tests-test-checkout-py-test-checkout-applies-discount"]
    assert not [r for r in plan["test_recommendations"] if r["target_kind"] == "journey"]


def _write_journey_file(path: Path, journeys: list[dict]) -> None:
    """Write a standalone [[journeys]] TOML file (for journeys_file tests)."""
    import json
    lines = []
    for j in journeys:
        lines.append(f'\n[[journeys]]\nname = {json.dumps(j["name"])}\nsteps = {json.dumps(j["steps"])}\n')
    path.write_text("".join(lines), encoding="utf-8")


def _prepend_root_key(repo_dir: Path, line: str) -> None:
    """Prepend a root-level TOML key before any [section] headers.

    Bare key = value lines appended to the end of kc.toml fall inside the
    last active table ([mutation]) rather than root. Prepending before the
    first [section] ensures the key is parsed at root scope."""
    existing = (repo_dir / "kc.toml").read_text(encoding="utf-8")
    (repo_dir / "kc.toml").write_text(line + "\n" + existing, encoding="utf-8")


def test_journeys_file_single_path_loads_external_journeys(repo, tmp_path):
    """journeys_file = "path" loads journeys from an external TOML file."""
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    _add_second_covered_component(repo_dir)
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add checkout")

    ext = tmp_path / "shared-journeys.toml"
    _write_journey_file(ext, [{"name": "Apply discount at checkout",
                               "steps": ["component/billing-rules", "component/billing-checkout"]}])
    # Use as_posix() for forward-slash paths — TOML literal strings preserve
    # backslashes verbatim, so Windows paths via repr() would double them.
    _prepend_root_key(repo_dir, f'journeys_file = "{ext.as_posix()}"')

    summary = compile_full(repo_dir, llm_provider=_provider())
    assert summary.warnings == []

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        journey = queries.get_entity(session, rid, "user-journey/apply-discount-at-checkout")

    assert journey is not None
    assert journey["payload"]["steps"] == ["component/billing-rules", "component/billing-checkout"]


def test_journeys_file_array_merges_multiple_files(repo, tmp_path):
    """journeys_file = ["a.toml", "b.toml"] merges all files."""
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    _add_second_covered_component(repo_dir)
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add checkout")

    file_a = tmp_path / "journeys-a.toml"
    file_b = tmp_path / "journeys-b.toml"
    _write_journey_file(file_a, [{"name": "Apply discount at checkout",
                                  "steps": ["component/billing-rules", "component/billing-checkout"]}])
    _write_journey_file(file_b, [{"name": "Discount only",
                                  "steps": ["component/billing-rules"]}])
    _prepend_root_key(repo_dir,
                      f'journeys_file = ["{file_a.as_posix()}", "{file_b.as_posix()}"]')

    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        j1 = queries.get_entity(session, rid, "user-journey/apply-discount-at-checkout")
        j2 = queries.get_entity(session, rid, "user-journey/discount-only")

    assert j1 is not None
    assert j2 is not None


def test_journeys_file_and_inline_are_merged(repo, tmp_path):
    """Inline [[journeys]] and journeys_file entries coexist and are both compiled."""
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    _add_second_covered_component(repo_dir)
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "add checkout")

    ext = tmp_path / "extra-journeys.toml"
    _write_journey_file(ext, [{"name": "Discount only", "steps": ["component/billing-rules"]}])
    _prepend_root_key(repo_dir, f'journeys_file = "{ext.as_posix()}"')
    # Inline entry appended after sections — [[journeys]] is an array-of-tables
    # which always starts at root scope, so appending here is safe.
    existing = (repo_dir / "kc.toml").read_text(encoding="utf-8")
    existing += ('\n[[journeys]]\nname = "Apply discount at checkout"\n'
                 'steps = ["component/billing-rules", "component/billing-checkout"]\n')
    (repo_dir / "kc.toml").write_text(existing, encoding="utf-8")

    compile_full(repo_dir, llm_provider=_provider())

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        j1 = queries.get_entity(session, rid, "user-journey/apply-discount-at-checkout")
        j2 = queries.get_entity(session, rid, "user-journey/discount-only")

    assert j1 is not None
    assert j2 is not None


def test_journeys_file_missing_file_fails_loudly(repo, tmp_path):
    """journeys_file pointing to a nonexistent file raises CompileError at compile time."""
    from knowledge_compiler.compiler.run import compile_full, CompileError

    repo_dir, _ = repo
    _prepend_root_key(repo_dir, 'journeys_file = "does-not-exist.toml"')

    with pytest.raises(CompileError, match="journeys_file not found"):
        compile_full(repo_dir, llm_provider=_provider())


def test_journey_step_unresolvable_slug_is_dropped_with_warning(repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = repo
    _set_journeys(repo_dir, ["component/billing-rules", "component/does-not-exist"])

    summary = compile_full(repo_dir, llm_provider=_provider())
    assert any("does-not-exist" in w and "dropped" in w for w in summary.warnings)

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        journey = queries.get_entity(session, rid, "user-journey/apply-discount-at-checkout")
    assert journey["payload"]["steps"] == ["component/billing-rules"]


# --- ADR-023: state_model / transition_gap --------------------------------------


@pytest.fixture()
def jobs_repo(tmp_path: Path):
    """A component whose code carries a real structural state machine, for
    end-to-end state_model -> test_plan(transition_gap) verification."""
    repo = tmp_path / "jobs-repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "jobs.py").write_text(
        'def _run(job):\n    job.status = "running"\n'
        '    try:\n        job.status = "succeeded"\n'
        '    except Exception:\n        job.status = "failed"\n',
        encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    from knowledge_compiler.compiler.bootstrap import init_repository
    slug = f"jobs-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    return repo, slug


def test_plan_surfaces_transition_gap_for_a_modeled_component(jobs_repo):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo_dir, slug = jobs_repo
    compile_full(repo_dir, llm_provider=FakeLLMProvider(responses={}))

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        sm = queries.get_entity(session, rid, "state-model/pkg-jobs-status")
        plan = queries.test_plan(session, rid, "component/pkg-jobs")

    assert sm is not None
    assert sm["payload"]["states"] == ["failed", "running", "succeeded"]

    rec = next(r for r in plan["test_recommendations"] if r["target_kind"] == "transition_gap")
    assert rec["component"] == "state-model/pkg-jobs-status"
    edges = {(t["from"], t["to"]) for t in rec["targets"]}
    assert edges == {(None, "running"), ("running", "succeeded"), ("running", "failed")}
    assert all(t["confidence"] == "structural" for t in rec["targets"])
