"""Downstream validation of generated tests against compiled knowledge.

Not a compiler pipeline stage (BRAINSTORM-test-generation-mechanism.md):
Knowledge Compiler's responsibility ends at producing compiled knowledge and
`test_plan` (mcp/queries.py); test *writing* is delegated to an external
coding agent. This module scores what comes back — the `kc-covers:` header
convention and its checks are defined in BRAINSTORM-test-generation-eval.md
§"Declared-coverage header format".

Note on `test_plan`'s two recommendation shapes (see `citable_targets_for`):
`target_kind: "api"` recommendations carry real per-target entity slugs;
`target_kind: "symbols"` recommendations only carry bare `symbol_path`
strings, which are never compiled entities and can never legally appear in a
`kc-covers:` header. This makes "recall" mean two different granularities —
per-API for api-gaps, per-component for symbols-gaps — an asymmetry inherent
to the underlying data, not an implementation shortcut.

Scoring model (PLAN-qa-agent-substrate.md Tier 0 / B2, Appendix A): two
independent signals, deliberately not collapsed into one number until the
final composite —

- **targeting** (`targeting_pct`): did the header cite the right things?
  Precision/recall against `test_plan`'s citable targets — the whole of
  `score_pct` before this revision, unchanged in formula.
- **verification**: did the test actually assert anything? Two
  sub-signals, each contributing a multiplier that defaults to 1.0 (not
  penalized) when unchecked:
    - `has_assertions` (always computed, synchronous, AST-based): a test
      with literally zero assertions is a hard failure — multiplier 0.0.
      This does NOT catch a vacuous `assert True`; that needs execution
      evidence, not presence, and is intentionally left to the mutation
      signal below.
    - `mutation_kill_rate` (read, never computed here): resolved from
      `test_plan`'s own `coverage_detail`, which already carries each
      covered component's compiled `mutation_kill_rate` (opt-in
      `[mutation]`, `collectors/mutation.py`) — the same field
      `coverage_for`/`test_plan`'s `low_mutation_kill` already surfaces.
      This module adds no new mutation-data source and no CLI flag for
      one: mutation-kill is a compiled-knowledge signal now, not a
      per-invocation input, and reusing it here means the composite is
      automatically current with whatever `[mutation]` last compiled —
      never stale in a way a manually-supplied report file could be.
      Absent (no `[mutation]` configured, or the claimed slugs' owning
      components were never scored) contributes no penalty, matching
      `low_mutation_kill`'s own "informational, not gating" posture
      (ADR-012).

`score_pct = round(targeting_pct * assertion_multiplier * mutation_multiplier, 1)`
— a product, not an average, so a perfectly-cited test that verifies
nothing cannot hide behind a good-looking single number.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from knowledge_compiler.mcp import queries

__all__ = [
    "SCORE_VERSION", "SlugCheck", "ValidationReport", "parse_kc_covers",
    "citable_targets_for", "has_assertions", "score_test",
]

SCORE_VERSION = "2"  # bump on any change to the score_pct formula or its inputs

_COVERS_LINE_RE = re.compile(r"^\s*-\s*(\S+)\s*$")


@dataclass
class SlugCheck:
    slug: str
    exists: bool


@dataclass
class ValidationReport:
    test_file: str
    for_entity: str
    header_found: bool
    claimed_slugs: list[str] = field(default_factory=list)
    existence: list[SlugCheck] = field(default_factory=list)
    precision: float | None = None
    recall: float | None = None
    citable_recommended: list[str] = field(default_factory=list)
    api_kind_citable: list[str] = field(default_factory=list)
    symbols_kind_citable: list[str] = field(default_factory=list)
    missing_from_claims: list[str] = field(default_factory=list)
    extraneous_claims: list[str] = field(default_factory=list)
    nonexistent_claims: list[str] = field(default_factory=list)
    # targeting tier (unchanged formula, now split out from the composite)
    targeting_pct: float = 0.0
    # verification tier (new)
    has_assertions: bool | None = None
    mutation_kill_rate: float | None = None  # 0-1 fraction, read from compiled state
    mutation_source_component: str | None = None  # which coverage_detail entry it came from
    # composite
    score_version: str = SCORE_VERSION
    score_pct: float = 0.0


def parse_kc_covers(source: str) -> list[str] | None:
    """Extract claimed slugs from the module docstring's `kc-covers:` block.

    Returns None if no such block exists at all — distinct from an empty
    list, which means a `kc-covers:` line existed with zero `- slug` entries
    beneath it. Both are scored as failures by score_test, but as different
    failure modes.
    """
    tree = ast.parse(source)
    doc = ast.get_docstring(tree)
    if doc is None:
        return None
    lines = doc.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "kc-covers:")
    except StopIteration:
        return None
    slugs = []
    for line in lines[start + 1:]:
        if line.strip() == "":
            break
        m = _COVERS_LINE_RE.match(line)
        if m is None:
            break
        slugs.append(m.group(1))
    return slugs


def citable_targets_for(recommendation: dict) -> list[str]:
    """The entity slugs a kc-covers: header could legitimately cite for one
    test_plan recommendation entry (see module docstring for the asymmetry)."""
    if recommendation["target_kind"] == "api":
        return sorted({t["slug"] for t in recommendation["targets"]})
    return [recommendation["component"]]


def has_assertions(source: str) -> bool:
    """Cheap, synchronous, presence-only check — does the test file contain
    at least one assertion anywhere at all? File-scoped, matching
    `kc-covers`'s own file-level (not per-function) claim convention.

    Catches the emptiest failure mode (zero assertions) but deliberately
    NOT a vacuous one like `assert True` — distinguishing "asserted
    something" from "asserted something meaningful" needs execution
    evidence (mutation-kill), not static presence. See module docstring.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name and "assert" in name.lower():
                return True
        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    fname = _call_name(item.context_expr)
                    if fname == "raises":
                        return True
    return False


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _resolve_mutation_kill_rate(plan: dict, claimed: list[str]) -> tuple[float | None, str | None]:
    """Reads mutation_kill_rate for the claimed slugs' owning component(s)
    straight out of `plan["coverage_detail"]` — already computed by
    `test_plan`/`coverage_for`, no new query. Maps a claimed slug back to
    its owning component the same way `citable_targets_for` maps forward:
    for a symbols-kind recommendation the citable slug IS the component
    slug; for an api-kind recommendation each cited API slug belongs to
    `rec["component"]`.

    Conservative aggregate: the minimum rate among all resolved components
    (a test isn't fully verified if even one claimed area kills few
    mutants). Returns (None, None) when nothing resolves — no
    `[mutation]` data, or none of the claimed slugs are coverage gaps for
    this `for_entity`.
    """
    slug_to_component: dict[str, str] = {}
    for rec in plan.get("test_recommendations", []):
        for slug in citable_targets_for(rec):
            slug_to_component[slug] = rec["component"]

    resolved: dict[str, float] = {}
    for slug in claimed:
        component = slug_to_component.get(slug)
        if component is None:
            continue
        detail = plan.get("coverage_detail", {}).get(component)
        rate = detail.get("mutation_kill_rate") if detail else None
        if rate is not None:
            resolved[component] = rate

    if not resolved:
        return None, None
    worst_component = min(resolved, key=lambda c: resolved[c])
    return resolved[worst_component], worst_component


def score_test(session: Session, repo_id: int, test_file: Path, for_entity: str,
               dep_map: dict[str, str] | None = None) -> ValidationReport | None:
    """Score `test_file`'s declared kc-covers: header against test_plan(for_entity),
    plus the verification tier: assertion presence (always checked) and
    mutation kill rate (read from already-compiled state — see module
    docstring; never a separate input to this function).

    Returns None if `for_entity` doesn't resolve (mirrors test_plan's own
    None contract). A missing/malformed kc-covers block is never an
    exception — it is a scored failure (score_pct == 0.0).
    """
    plan = queries.test_plan(session, repo_id, for_entity, dep_map=dep_map)
    if plan is None:
        return None

    source = test_file.read_text(encoding="utf-8")
    claimed = parse_kc_covers(source)
    report = ValidationReport(test_file=str(test_file), for_entity=for_entity,
                               header_found=claimed is not None,
                               claimed_slugs=claimed or [])
    if claimed is None:
        return report

    report.existence = [SlugCheck(s, queries.get_entity(session, repo_id, s, dep_map=dep_map) is not None)
                        for s in claimed]
    report.nonexistent_claims = [c.slug for c in report.existence if not c.exists]

    api_citable = sorted({s for rec in plan["test_recommendations"]
                          if rec["target_kind"] == "api"
                          for s in citable_targets_for(rec)})
    syms_citable = sorted({s for rec in plan["test_recommendations"]
                           if rec["target_kind"] != "api"
                           for s in citable_targets_for(rec)})
    citable = sorted(set(api_citable) | set(syms_citable))
    report.citable_recommended = citable
    report.api_kind_citable = api_citable
    report.symbols_kind_citable = syms_citable

    claimed_set, citable_set = set(claimed), set(citable)
    report.extraneous_claims = sorted((claimed_set - citable_set) - set(report.nonexistent_claims))
    report.missing_from_claims = sorted(citable_set - claimed_set)

    report.precision = (len(claimed_set & citable_set) / len(claimed_set)) if claimed_set else 0.0
    report.recall = (len(claimed_set & citable_set) / len(citable_set)) if citable_set else 1.0

    existence_penalty = 1 - (len(report.nonexistent_claims) / len(claimed)) if claimed else 1.0
    pr_avg = (report.precision + report.recall) / 2
    report.targeting_pct = round(pr_avg * 100.0 * existence_penalty, 1)

    report.has_assertions = has_assertions(source)
    assertion_multiplier = 1.0 if report.has_assertions else 0.0

    report.mutation_kill_rate, report.mutation_source_component = _resolve_mutation_kill_rate(plan, claimed)
    mutation_multiplier = report.mutation_kill_rate if report.mutation_kill_rate is not None else 1.0

    report.score_pct = round(report.targeting_pct * assertion_multiplier * mutation_multiplier, 1)

    return report
