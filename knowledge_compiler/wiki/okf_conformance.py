"""OKF v0.2 conformance check over an already-emitted wiki bundle (ADR-013).

Never compiles — a downstream check over disk state, same posture as
`kc validate-test` (BRAINSTORM-test-generation-mechanism.md): the compiler's
job ends at emission; conformance checking is a consumer, not a stage.

Deliberately not a full YAML parser (ADR-007 boring-infra: PyYAML is not a
project dependency) — frontmatter here is checked at exactly the level OKF
v0.2's own conformance rules (SPEC.md §11) require: presence, a non-empty
`type` key for concept files, and the absence/restriction of frontmatter on
the two reserved filenames (`index.md`, `log.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from knowledge_compiler import OKF_SPEC_VERSION

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


def check_bundle(wiki_dir: Path) -> ConformanceReport:
    """Check every `.md` file in `wiki_dir` against OKF v0.2's conformance
    rules (SPEC.md §11): non-reserved files need parseable frontmatter with a
    non-empty `type`; `index.md` may carry only `okf_version`; `log.md` may
    carry no frontmatter at all."""
    report = ConformanceReport(okf_spec_version=OKF_SPEC_VERSION)
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = str(path.relative_to(wiki_dir))
        report.files_checked += 1
        text = path.read_text(encoding="utf-8")
        block = _frontmatter_block(text)

        if path.name == "log.md":
            if block is not None:
                report.issues.append(ConformanceIssue(
                    rel, "log.md-frontmatter",
                    "log.md is a reserved filename and must carry no frontmatter (SPEC.md §9)"))
            continue

        if path.name == "index.md":
            if block is not None:
                extra = [k for k in _top_level_pairs(block) if k != "okf_version"]
                if extra:
                    report.issues.append(ConformanceIssue(
                        rel, "index.md-frontmatter",
                        f"index.md may carry only 'okf_version' in frontmatter, "
                        f"found: {extra} (SPEC.md §8)"))
            continue

        if block is None:
            report.issues.append(ConformanceIssue(
                rel, "missing-frontmatter",
                "non-reserved concept file has no parseable YAML frontmatter "
                "block (SPEC.md §11 rule 1)"))
            continue

        if not _top_level_pairs(block).get("type"):
            report.issues.append(ConformanceIssue(
                rel, "missing-type",
                "frontmatter has no non-empty 'type' field (SPEC.md §11 rule 2)"))

    return report
