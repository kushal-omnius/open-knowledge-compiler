"""Shared OKF structural rules (ADR-014): the single source of truth for what
`okf_conformance.py`'s validator checks and what `emitter.py`'s renderer must
produce. A structural fact declared once here and consulted by both sides
never needs to be kept in sync by hand — the drift risk ADR-014 exists to
close.

Scope (ADR-014's Option A, deliberately not more): reserved-filename
frontmatter shape and required-field presence on concept pages.
Content-value computation — how a field's actual *value* is derived from an
Entity — stays out of this file; that remains `emitter.py`'s own domain
logic, unchanged by this ADR.

Format resolution (ADR-014 left this an open question): a Python module of
frozen dataclasses, not an external JSON/TOML file. This still gives one
shared, single-loaded source of truth — the actual goal — without the
packaging risk of a data file needing `package_data`/`importlib.resources`
wiring to survive a built wheel. Revisit only if OKF's rule set grows complex
enough to need a non-Python-native shape.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReservedFileRule:
    """A reserved OKF filename's frontmatter constraint.

    `allowed_keys` empty means the file may carry no frontmatter block at
    all (e.g. log.md); non-empty means frontmatter is permitted but limited
    to exactly these top-level keys (e.g. index.md's `okf_version`)."""

    allowed_keys: frozenset[str]


@dataclass(frozen=True)
class OKFRules:
    spec_version: str
    reserved_files: dict[str, ReservedFileRule]
    concept_required_fields: tuple[str, ...]


OKF_V0_2_RULES = OKFRules(
    spec_version="0.2",
    reserved_files={
        "log.md": ReservedFileRule(allowed_keys=frozenset()),
        "index.md": ReservedFileRule(allowed_keys=frozenset({"okf_version"})),
    },
    concept_required_fields=("type",),
)
