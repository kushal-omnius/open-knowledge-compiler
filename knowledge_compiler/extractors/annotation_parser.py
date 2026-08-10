"""Deterministic parser for kc: inline annotations in source files.

Supports:
    # kc:external-key: <key>
        Placed on the line(s) immediately before a ``def`` or ``class``
        statement (blank lines skipped; chained kc: annotations allowed).
        The annotated symbol's *local name* is returned so callers can match
        it against the qualified symbol_paths produced by the deterministic
        analyzer pass — no language-specific path logic lives here.

The external key is KC's Rule-1 identity cascade step (normalize.md §5.2):
an entity with a matching ``external_key`` in the persisted state is always
reused regardless of anchor drift or LLM stochasticity.  Annotations make
stable architectural concepts (compile entry points, long-lived invariants)
immune to re-extraction noise.
"""

from __future__ import annotations

import re

_ANNOTATION_RE = re.compile(r"#\s*kc:external-key:\s*(\S+)")
_DEF_RE = re.compile(r"^[ \t]*(async[ \t]+)?def[ \t]+(\w+)|^[ \t]*class[ \t]+(\w+)")
_DECORATOR_RE = re.compile(r"^[ \t]*@")


def parse_external_keys(source: str) -> dict[str, str]:
    """Return ``{local_name: external_key}`` for all annotated defs/classes.

    ``local_name`` is the bare function or class name (no module prefix).
    Blank lines between the annotation and the def/class are ignored.
    A non-blank, non-kc-annotation line between annotation and target
    cancels the pending annotation (the annotation did not refer to that
    target).
    """
    lines = source.splitlines()
    result: dict[str, str] = {}
    pending_key: str | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        annotation_match = _ANNOTATION_RE.search(stripped)
        if annotation_match:
            pending_key = annotation_match.group(1)
            continue

        # Decorators sit between annotation and def/class — skip them
        if pending_key is not None and _DECORATOR_RE.match(line):
            continue

        if pending_key is not None:
            def_match = _DEF_RE.match(line)
            if def_match:
                name = def_match.group(2) or def_match.group(3)
                result[name] = pending_key
            # whether or not we matched, the pending annotation is consumed
            pending_key = None

    return result
