"""`kc eval` — score compiled QA signals against hand-labeled ground truth.

Why this exists: `covers`, `stale_coverage`, `impact_plan` and the per-run
delta are the signals test_plan builds on, and none of them had a measured
precision/recall. This module turns a small hand-labeled file (kc-eval.toml)
into those numbers so a change to extraction or normalization can be judged
against a baseline instead of by eye.

Labels are written in human terms — component module paths and test file
paths, never slugs — so a reviewer can label from the source tree alone:

    [[coverage]]
    component = "billing.rules"          # component payload `path`
    tests = ["tests/test_rules.py"]      # files that truly exercise it
    stale = false                        # optional: is every covering test stale?

    [[change]]
    commit = "a3f9c1d"                   # or: pr = 42 (one of the two)
    changed = ["billing.rules"]          # components whose behavior changed
    affected = ["billing.rules", "api.checkout"]  # everything needing re-test

Scoring is micro-averaged (TP/FP/FN summed across items). A label that names
a component or compile run the knowledge base doesn't have is an error, not a
miss: it is almost always a typo, and silently scoring it would corrupt the
baseline (fail loud).

Test modules are not components for `change_detection` and `impact`. kc
compiles test files as components too, but a reviewer labels the source
modules whose behavior changed, so a PR that touches a test would otherwise
count every edited test file as a false positive (frida dogfood: 108 of 132).
A component is a test module when one of its files is a test file by its own
analyzer's convention (`test_*.py`, `*.test.ts`, ...). Those are dropped from
the predicted sets and listed per item as `excluded_test_modules`, except any
the label itself names: a naming convention can be wrong (frida's production
workflow `cause_agent_test.py`), and the label is the ground truth. Removed
components, which have no current entity to classify, are kept as predicted.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_compiler.extractors import (
    javascript_analyzer,
    python_analyzer,
    typescript_analyzer,
)
from knowledge_compiler.mcp import queries
from knowledge_compiler.storage.schema import CompileRun, DeltaChangeRow, EntityRow

# Each analyzer owns its test-file convention (ADR-006); reuse it rather than a
# name pattern of our own, so the eval and the compiler agree on what a test is.
_TEST_FILE_RULES = {
    "python": python_analyzer._is_test_file,
    "typescript": typescript_analyzer._is_test_file,
    "javascript": javascript_analyzer._is_test_file,
}


class LabelError(ValueError):
    """The labels file is malformed or references things that don't exist."""


# -- labels ---------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageLabel:
    component: str
    tests: frozenset[str]
    stale: bool | None = None


@dataclass(frozen=True)
class ChangeLabel:
    key: str                      # "pr:42" or "commit:a3f9c1d" — for reporting
    pr: int | None
    commit: str | None
    changed: frozenset[str]
    affected: frozenset[str]


@dataclass(frozen=True)
class Labels:
    coverage: tuple[CoverageLabel, ...]
    changes: tuple[ChangeLabel, ...]


def load_labels(path: Path) -> Labels:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LabelError(f"labels file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LabelError(f"{path}: invalid TOML: {exc}") from exc

    coverage = []
    for i, item in enumerate(raw.get("coverage", [])):
        where = f"{path}: coverage[{i}]"
        if not isinstance(item.get("component"), str) or not isinstance(item.get("tests"), list):
            raise LabelError(f"{where}: needs `component` (string) and `tests` (list)")
        stale = item.get("stale")
        if stale is not None and not isinstance(stale, bool):
            raise LabelError(f"{where}: `stale` must be true/false when set")
        coverage.append(CoverageLabel(item["component"], frozenset(item["tests"]), stale))

    changes = []
    for i, item in enumerate(raw.get("change", [])):
        where = f"{path}: change[{i}]"
        pr, commit = item.get("pr"), item.get("commit")
        if (pr is None) == (commit is None):
            raise LabelError(f"{where}: set exactly one of `pr` or `commit`")
        if not isinstance(item.get("changed"), list) or not isinstance(item.get("affected"), list):
            raise LabelError(f"{where}: needs `changed` and `affected` lists")
        changed, affected = frozenset(item["changed"]), frozenset(item["affected"])
        if not changed <= affected:
            # a changed component always needs re-testing; keeping the invariant
            # explicit stops `affected` recall from being inflated by omission
            raise LabelError(f"{where}: every `changed` component must also be in `affected`")
        key = f"pr:{pr}" if pr is not None else f"commit:{commit}"
        changes.append(ChangeLabel(key, pr, commit, changed, affected))

    if not coverage and not changes:
        raise LabelError(f"{path}: no [[coverage]] or [[change]] entries")
    return Labels(tuple(coverage), tuple(changes))


# -- scoring --------------------------------------------------------------------

@dataclass
class Metric:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, predicted: set[str] | frozenset[str], expected: set[str] | frozenset[str]) -> None:
        self.tp += len(predicted & expected)
        self.fp += len(predicted - expected)
        self.fn += len(expected - predicted)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)

    def as_dict(self) -> dict:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn,
                "precision": self.precision, "recall": self.recall, "f1": self.f1}


@dataclass
class EvalReport:
    metrics: dict[str, Metric] = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)   # per-label detail for --json

    def as_dict(self) -> dict:
        return {"metrics": {k: m.as_dict() for k, m in self.metrics.items()},
                "items": self.items}


# -- predictions ----------------------------------------------------------------

def _component_paths(session: Session, repo_id: int) -> dict[str, str]:
    """path -> slug for every compiled component."""
    rows = session.execute(select(EntityRow.slug, EntityRow.payload).where(
        EntityRow.repo_id == repo_id, EntityRow.entity_type == "component")).all()
    return {payload["path"]: slug for slug, payload in rows}


def _is_test_module(payload: dict) -> bool:
    rule = _TEST_FILE_RULES.get(payload.get("language", ""))
    return rule is not None and any(rule(f) for f in payload.get("files", []))


def _test_modules(session: Session, repo_id: int) -> set[str]:
    """Paths of compiled components that are test modules."""
    rows = session.execute(select(EntityRow.payload).where(
        EntityRow.repo_id == repo_id, EntityRow.entity_type == "component")).scalars()
    return {payload["path"] for payload in rows if _is_test_module(payload)}


def _find_run(session: Session, repo_id: int, label: ChangeLabel) -> CompileRun:
    q = select(CompileRun).where(CompileRun.repo_id == repo_id, CompileRun.status == "succeeded")
    if label.pr is not None:
        q = q.where(CompileRun.pr_number == label.pr)
    else:
        q = q.where(CompileRun.commit_sha.startswith(label.commit))
    runs = session.execute(q.order_by(CompileRun.id)).scalars().all()
    if not runs:
        raise LabelError(f"{label.key}: no succeeded compile run — compile it first "
                         f"(`kc compile --pr N`, or `kc compile --full` at that commit)")
    if label.commit is not None and len({r.commit_sha for r in runs}) > 1:
        raise LabelError(f"{label.key}: commit prefix is ambiguous — use more characters")
    run = runs[0]  # first run at that point is the one whose delta reflects the change
    first_id = session.execute(select(CompileRun.id).where(
        CompileRun.repo_id == repo_id, CompileRun.status == "succeeded")
        .order_by(CompileRun.id).limit(1)).scalar_one()
    if run.id == first_id:
        # the bootstrap delta marks every component "added" — scoring it would
        # report perfect change recall for free
        raise LabelError(f"{label.key}: matches the bootstrap compile, whose delta is "
                         f"everything — label a later change")
    return run


def evaluate(session: Session, repo_id: int, labels: Labels) -> EvalReport:
    path_to_slug = _component_paths(session, repo_id)
    slug_to_path = {s: p for p, s in path_to_slug.items()}

    unknown = sorted({c.component for c in labels.coverage} - path_to_slug.keys())
    if unknown:
        raise LabelError(f"labels name components that aren't compiled: {unknown}")

    report = EvalReport()
    covers, stale = Metric(), Metric()
    for label in labels.coverage:
        cov = queries.coverage_for(session, repo_id, path_to_slug[label.component])
        # score what impact_plan treats as coverage: tests at or above the
        # confidence bar (or with no recorded evidence: pre-upgrade compiles).
        # Weak, import-only tests are listed separately in --json.
        counted = {t["slug"]: t for t in cov["tests"]
                   if t["confidence"] is None or t["confidence"] >= queries.COVERAGE_GAP_CONFIDENCE}
        rows = session.execute(select(EntityRow.slug, EntityRow.payload).where(
            EntityRow.repo_id == repo_id,
            EntityRow.slug.in_([t["slug"] for t in cov["tests"]]))).all()
        file_of = {slug: payload["file"] for slug, payload in rows}
        predicted = {file_of[s] for s in counted}
        covers.add(predicted, label.tests)
        item = {"kind": "coverage", "component": label.component,
                "predicted_tests": sorted(predicted), "expected_tests": sorted(label.tests),
                "weak_tests": sorted({file_of[t["slug"]] for t in cov["tests"]
                                     if t["slug"] not in counted}),
                "mechanisms": {file_of[t["slug"]]: t["mechanism"] for t in cov["tests"]},
                "predicted_stale": cov["stale"]}
        if label.stale is not None:
            # stale is a binary judgment per component: positive class = "stale"
            stale.add({label.component} if cov["stale"] else set(),
                      {label.component} if label.stale else set())
            item["expected_stale"] = label.stale
        report.items.append(item)

    changed, impact = Metric(), Metric()
    test_modules = _test_modules(session, repo_id) if labels.changes else set()
    for label in labels.changes:
        run = _find_run(session, repo_id, label)
        delta_slugs = set(session.execute(select(DeltaChangeRow.slug).where(
            DeltaChangeRow.compile_run_id == run.id,
            DeltaChangeRow.entity_type == "component")).scalars())
        # removed components have no current entity; their path lives only in the
        # delta's denormalized slug, so map what we can and keep the rest as slugs
        predicted_changed = {slug_to_path.get(s, s) for s in delta_slugs}

        predicted_affected = set(predicted_changed)
        for slug in delta_slugs & slug_to_path.keys():
            plan = queries.impact_plan(session, repo_id, slug)
            predicted_affected |= {slug_to_path[a["slug"]] for a in plan["affected"]
                                   if a["slug"] in slug_to_path}

        # the label wins over the naming convention for anything it names
        unscored = test_modules - label.changed - label.affected
        excluded = (predicted_changed | predicted_affected) & unscored
        predicted_changed -= unscored
        predicted_affected -= unscored

        changed.add(predicted_changed, label.changed)
        impact.add(predicted_affected, label.affected)
        report.items.append({
            "kind": "change", "key": label.key, "compile_run": run.id,
            "predicted_changed": sorted(predicted_changed), "expected_changed": sorted(label.changed),
            "predicted_affected": sorted(predicted_affected),
            "expected_affected": sorted(label.affected),
            "excluded_test_modules": sorted(excluded),
        })

    if labels.coverage:
        report.metrics["covers"] = covers
        if any(c.stale is not None for c in labels.coverage):
            report.metrics["stale_coverage"] = stale
    if labels.changes:
        report.metrics["change_detection"] = changed
        report.metrics["impact"] = impact
    return report
