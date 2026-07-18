"""Diff stage (pipeline.md §3.4): candidate state vs current state -> knowledge delta.

Owns the removal-evidence rule (pipeline.md §5, ir.md §4.2.1): absence is evidence
only within the compile's scope, and only when the extractor family that produced
an entity actually ran. Pure function — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge_compiler.compiler.normalize import CandidateState, CurrentState
from knowledge_compiler.ir import (
    LLM_DERIVED_TYPES, Delta, Entity, EntityChange, RelationshipChange,
)


@dataclass(frozen=True)
class CompileScope:
    """What this compile observed (pipeline.md §5)."""

    full: bool                       # full repo vs PR slice (PR slices arrive phase 3)
    ran_families: frozenset[str]     # e.g. {"deterministic"} for a --no-llm compile


def _removable(entity: Entity, scope: CompileScope) -> bool:
    """Removal evidence: in scope AND the producing extractor family ran.

    LLM-derived entities (and their derived wiki pages) are only removable when
    the llm family ran — a deterministic-only compile can never mass-remove them
    (pipeline.md §6.1)."""
    if not scope.full:
        return False  # PR-scope removal logic lands with incremental compilation
    if entity.entity_type in LLM_DERIVED_TYPES:
        return "llm" in scope.ran_families
    if entity.entity_type == "wiki_page":
        # derived identity: follows its owner's family; owner slug is in the payload
        owner = entity.payload.get("owner_slug", "")
        owner_type = owner.split("/", 1)[0].replace("-", "_")
        if owner_type in LLM_DERIVED_TYPES:
            return "llm" in scope.ran_families
    return "deterministic" in scope.ran_families


def _payload_diff(old: dict, new: dict) -> dict:
    """Top-level payload paths with old->new values (data-model.md: backward-replayable)."""
    changed = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changed[key] = {"old": old.get(key), "new": new.get(key)}
    return changed


def _anchor_files(entity: Entity) -> tuple[str, ...]:
    return tuple(sorted({a.file_path for a in entity.anchors}))


def compute_diff(candidate: CandidateState, current: CurrentState,
                 scope: CompileScope) -> tuple[Delta, set[str]]:
    """Returns (delta, dirty_slugs). Dirty = entity changes ∪ relationship endpoints
    (ir.md §3.4 dirty-entity rule)."""
    cur = {e.slug: e for e in current.entities}
    cand = {e.slug: e for e in candidate.entities}

    entity_changes: list[EntityChange] = []

    for slug in sorted(cand):
        e = cand[slug]
        old = cur.get(slug)
        if old is None:
            entity_changes.append(EntityChange(
                op="added", slug=slug, entity_type=e.entity_type,
                change_summary={}, evidence=_evidence(candidate, slug)))
        elif old.content_hash != e.content_hash or old.name != e.name:
            summary = _payload_diff(old.payload, e.payload)
            if old.name != e.name:
                summary["name"] = {"old": old.name, "new": e.name}
            entity_changes.append(EntityChange(
                op="changed", slug=slug, entity_type=e.entity_type,
                change_summary=summary, evidence=_evidence(candidate, slug)))
        elif _anchor_files(old) != _anchor_files(e):
            entity_changes.append(EntityChange(
                op="moved", slug=slug, entity_type=e.entity_type,
                change_summary={"anchors": {"old": list(_anchor_files(old)),
                                            "new": list(_anchor_files(e))}},
                evidence=_evidence(candidate, slug)))

    # survivors: current entities absent from the candidate that removal evidence spares
    survivors: set[str] = set()
    for slug in sorted(set(cur) - set(cand)):
        if _removable(cur[slug], scope):
            entity_changes.append(EntityChange(
                op="removed", slug=slug, entity_type=cur[slug].entity_type,
                change_summary={}, evidence={}))
        else:
            survivors.add(slug)

    # relationships: set diff, protecting edges that touch surviving unobserved entities
    cur_edges = {(r.relation_type, r.from_slug, r.to_slug) for r in current.relationships}
    cand_edges = {(r.relation_type, r.from_slug, r.to_slug) for r in candidate.relationships}
    relationship_changes: list[RelationshipChange] = []
    for rel, f, t in sorted(cand_edges - cur_edges):
        relationship_changes.append(RelationshipChange(op="added", relation_type=rel,
                                                       from_slug=f, to_slug=t))
    for rel, f, t in sorted(cur_edges - cand_edges):
        if f in survivors or t in survivors:
            continue  # the entity wasn't observed; its edges aren't evidence either
        relationship_changes.append(RelationshipChange(op="removed", relation_type=rel,
                                                       from_slug=f, to_slug=t))

    delta = Delta(entity_changes=tuple(entity_changes),
                  relationship_changes=tuple(relationship_changes))

    dirty = {c.slug for c in entity_changes if c.op != "removed"}
    for rc in relationship_changes:
        dirty.update((rc.from_slug, rc.to_slug))
    dirty &= set(cand)  # dirty is about what exists now (emission input)
    return delta, dirty


def _evidence(candidate: CandidateState, slug: str) -> dict:
    ev = candidate.evidence.get(slug)
    if ev is None:
        return {}
    return {"rule": ev.rule, **ev.signals}
