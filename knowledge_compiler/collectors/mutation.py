"""Opt-in mutation-score collector (item 2 of the QA-agent-grounding backlog).

Reads a JSON summary a mutation-testing CI job already produces (the existing
`mutation-test.yaml` workflow referenced in BRAINSTORM-test-generation-eval.md)
and turns it into deterministic `mutation_score_observed` facts that Normalize
attaches to matching Component entities. This closes the gap between
`test_plan`'s declared-coverage recommendations and whether the existing test
suite actually kills mutants — surfaced inline where an agent is already
looking, instead of buried in a separate CI artifact (the exact trigger
condition ADR-012 names for revisiting deferred test-precision work).

KC never executes the target repo's code itself (pipeline.md's operating
model boundary — Collect/Extract/Normalize/Diff/Persist all read
already-produced artifacts). This collector only reads a summary another
process already produced; it does not run mutmut or any other tool.

Opt-in via `kc.toml` `[mutation] enabled = true` (ADR-007: activation is
explicit). `scores_file` is relative to the repo directory.

File shape:
    {"<dotted.module.path>": {"killed": N, "survived": N, "timeout": N}, ...}

A module path absent from the file, or a file that doesn't exist, produces no
facts — mutation scoring is inherently partial (not every module has been
scored yet), and that's data, not a collector outage.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_mutation_scores(repo_dir: Path, scores_file: str) -> dict[str, dict[str, int]]:
    path = repo_dir / scores_file
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        module: {
            "killed": int(stats.get("killed", 0)),
            "survived": int(stats.get("survived", 0)),
            "timeout": int(stats.get("timeout", 0)),
        }
        for module, stats in data.items()
    }
