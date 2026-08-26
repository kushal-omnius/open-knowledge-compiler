"""OKF conformance check over an already-emitted wiki bundle (ADR-013).

Never compiles — a downstream check over disk state, same posture as
`kc validate-test` (BRAINSTORM-test-generation-mechanism.md): the compiler's
job ends at emission; conformance checking is a consumer, not a stage.

Deliberately not a full YAML parser (ADR-007 boring-infra: PyYAML is not a
project dependency) — frontmatter here is checked at exactly the level OKF's
own conformance rules (SPEC.md §11) require: presence, non-empty required
fields for concept files, and the absence/restriction of frontmatter on
reserved filenames.

`check_bundle()` is a generic interpreter over `okf_rules.OKFRules` (ADR-014)
— it hard-codes no filenames or field names itself; every structural fact it
checks comes from the shared rules object also consulted by `emitter.py`, so
the two can no longer silently drift out of sync with each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from knowledge_compiler import OKF_SPEC_VERSION
from knowledge_compiler.wiki.okf_rules import OKF_V0_2_RULES, OKFRules

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


@dataclass
class ConformanceIssue:
    file: str
    rule: str
    detail: str


@dataclass
class ConformanceReport:
    okf_spec_version: str
    files_checked: int = 0
    issues: list[ConformanceIssue] = field(default_factory=list)

    @property
    def conformant(self) -> bool:
        return not self.issues


def _frontmatter_block(text: str) -> str | None:
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _top_level_pairs(block: str) -> dict[str, str]:
    """Top-level `key: value` lines only — skips nested/list lines (indented
    or `-`-prefixed), which is all this check needs (no full YAML parse)."""
    pairs: dict[str, str] = {}
    for line in block.splitlines():
        if not line or line[0] in " \t-":
            continue
        m = _KEY_RE.match(line)
        if m:
            pairs[m.group(1)] = m.group(2).strip().strip("\"'")
    return pairs


def check_bundle(wiki_dir: Path, rules: OKFRules = OKF_V0_2_RULES) -> ConformanceReport:
    """Check every `.md` file in `wiki_dir` against `rules` (ADR-014;
    defaults to the current OKF v0.2 structural rules, SPEC.md §11): a
    reserved filename's frontmatter is checked against its declared
    `allowed_keys`; every other file needs parseable frontmatter carrying
    all of `rules.concept_required_fields`, non-empty."""
    report = ConformanceReport(okf_spec_version=OKF_SPEC_VERSION)
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = str(path.relative_to(wiki_dir))
        report.files_checked += 1
        text = path.read_text(encoding="utf-8")
        block = _frontmatter_block(text)

        reserved = rules.reserved_files.get(path.name)
        if reserved is not None:
            if reserved.allowed_keys:
                if block is not None:
                    extra = sorted(set(_top_level_pairs(block)) - reserved.allowed_keys)
                    if extra:
                        report.issues.append(ConformanceIssue(
                            rel, f"{path.name}-frontmatter",
                            f"{path.name} may carry only {sorted(reserved.allowed_keys)} "
                            f"in frontmatter, found extra: {extra} (SPEC.md §8)"))
            elif block is not None:
                report.issues.append(ConformanceIssue(
                    rel, f"{path.name}-frontmatter",
                    f"{path.name} is a reserved filename and must carry no frontmatter (SPEC.md §9)"))
            continue

        if block is None:
            report.issues.append(ConformanceIssue(
                rel, "missing-frontmatter",
                "non-reserved concept file has no parseable YAML frontmatter "
                "block (SPEC.md §11 rule 1)"))
            continue

        pairs = _top_level_pairs(block)
        for required in rules.concept_required_fields:
            if not pairs.get(required):
                report.issues.append(ConformanceIssue(
                    rel, f"missing-{required}",
                    f"frontmatter has no non-empty '{required}' field (SPEC.md §11 rule 2)"))

    return report
