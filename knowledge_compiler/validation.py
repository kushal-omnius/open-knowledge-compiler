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
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from knowledge_compiler.mcp import queries

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


def score_test(session: Session, repo_id: int, test_file: Path, for_entity: str,
               dep_map: dict[str, str] | None = None) -> ValidationReport | None:
    """Score `test_file`'s declared kc-covers: header against test_plan(for_entity).

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
    report.score_pct = round(pr_avg * 100.0 * existence_penalty, 1)

    return report
