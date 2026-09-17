"""Anchor-scoped mutmut targeting (PLAN-qa-agent-substrate.md Tier 0 / B1).

Complements, and never duplicates, `collectors/mutation.py`: that collector
*consumes* a CI-produced kill-rate JSON (module-granularity) into the
compiled knowledge graph; this module helps *produce* a more precisely
scoped mutmut run in the first place, by deriving a deterministic
`only_mutate` value from an entity's compiled anchors. Its output is meant
to feed a `collectors/mutation.py`-shaped JSON summary at finer-than-module
granularity, not to be consumed by `kc validate-test` directly — the
compiled `mutation_kill_rate` signal (already surfaced through
`test_plan`/`coverage_for`) is `knowledge_compiler.validation`'s only
mutation-data source; see that package's `__init__.py`.

Known limitation, recorded rather than silently assumed away (vision DP 8):
`only_mutate` scopes mutmut to whole files (confirmed by
`.github/workflows/mutation-test.yaml`'s own comments on mutmut 3.x's
configuration surface), not to a line span. `AnchorScope.spans_by_file` is
carried through for a future line-level filter once mutmut's own per-mutant
output format is confirmed against a real run — attempting that filter now
would risk silently misreporting a kill rate rather than visibly failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from knowledge_compiler.ir import Anchor


@dataclass(frozen=True)
class AnchorScope:
    """Deterministic mutmut scope derived from an entity's anchors."""

    file_globs: tuple[str, ...]
    spans_by_file: dict[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)

    @property
    def only_mutate(self) -> str:
        """Ready for mutmut's `only_mutate` config
        (`.github/workflows/mutation-test.yaml`'s `paths_to_mutate` input).

        For a single-file scope (the common case: a rule/symbol anchored to
        one file) this is just that file path — verified-shape, matches the
        workflow's own default. For a multi-file scope this falls back to a
        comma-joined list; mutmut's own docs/CLI disagree with each other on
        `only_mutate`'s exact multi-pattern syntax (per this workflow's own
        comments) and there is no mutmut install in this environment to
        confirm comma-joining is honored rather than silently ignored — flag
        this explicitly rather than assume it. Run each file separately if a
        multi-file `only_mutate` value doesn't narrow the mutmut run as
        expected.
        """
        if len(self.file_globs) <= 1:
            return self.file_globs[0] if self.file_globs else ""
        return ",".join(self.file_globs)  # UNVERIFIED multi-pattern syntax — see docstring


def anchor_scope(anchors: tuple[Anchor, ...] | list[Anchor]) -> AnchorScope:
    """Pure, deterministic: no DB/tree-sitter access, just anchor data
    already carried by a compiled entity (`get_entity`'s `anchors` field)."""
    files = sorted({a.file_path for a in anchors if a.file_path})
    spans: dict[str, list[tuple[int, int]]] = {}
    for a in anchors:
        if a.file_path and a.span:
            spans.setdefault(a.file_path, []).append(a.span)
    frozen_spans = {f: tuple(sorted(set(s))) for f, s in spans.items()}
    return AnchorScope(file_globs=tuple(files), spans_by_file=frozen_spans)
