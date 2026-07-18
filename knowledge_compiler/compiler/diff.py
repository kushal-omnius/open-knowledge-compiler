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

    full: bool                            # full repo vs PR slice
    ran_families: frozenset[str]          # e.g. {"deterministic"} for a --no-llm compile
    in_scope_files: frozenset[str] = frozenset()  # PR slice: the forge-reported file set


def _entity_files(entity: Entity) -> frozenset[str]:
    files = entity.payload.get("files")
    if files:
        return frozenset(files)
    if entity.payload.get("file"):
        return frozenset({entity.payload["file"]})
    return frozenset(a.file_path for a in entity.anchors)


def _removable(entity: Entity, scope: CompileScope) -> bool:
    """Removal evidence (pipeline.md §5, ir.md §4.2.1): the entity's evidence
    location was in scope, AND the producing extractor family ran.

    Wiki pages are handled separately (they follow their owner — see compute_diff).
    PR/Jira entities are records of *events*, not statements about current source:
    absence from any compile is never evidence against a merge that happened, so
    they are never removable (dogfood finding: a later PR touching the same files
    must not delete an earlier PR's record).
    """
    if entity.entity_type in ("pull_request", "jira_story"):
        return False
    family = "llm" if entity.entity_type in LLM_DERIVED_TYPES else "deterministic"
    if family not in scope.ran_families:
        return False  # pipeline.md §6.1: absence is not evidence if the family didn't run
    if scope.full:
        return True
    files = _entity_files(entity)
    # no file evidence (e.g. project) => never removable from a PR slice
    return bool(files) and files <= scope.in_scope_files


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
    removed_slugs: set[str] = set()
    unobserved = sorted(set(cur) - set(cand))
    for slug in unobserved:
        if cur[slug].entity_type == "wiki_page":
            continue  # decided after owners (below)
        if _removable(cur[slug], scope):
            removed_slugs.add(slug)
        else:
            survivors.add(slug)
    # wiki pages have derived identity: they follow their owner's fate, never their own files
    for slug in unobserved:
        e = cur[slug]
        if e.entity_type != "wiki_page":
            continue
        owner = e.payload.get("owner_slug", "")
        if owner in removed_slugs or (scope.full and owner not in cand and owner not in survivors):
            removed_slugs.add(slug)
        else:
            survivors.add(slug)
    for slug in sorted(removed_slugs):
        entity_changes.append(EntityChange(
            op="removed", slug=slug, entity_type=cur[slug].entity_type,
            change_summary={}, evidence={}))

    # relationships: set diff. An edge is removable only when its FROM side was
    # observed this compile (its outgoing edges are then authoritative) or removed;
    # a surviving-unobserved from-entity keeps all its edges.
    cur_edges = {(r.relation_type, r.from_slug, r.to_slug) for r in current.relationships}
    cand_edges = {(r.relation_type, r.from_slug, r.to_slug) for r in candidate.relationships}

    removed_edges: set[tuple[str, str, str]] = set()
    for edge in cur_edges - cand_edges:
        if edge[1] in survivors:
            continue
        removed_edges.add(edge)
    # edges touching removed entities: recorded explicitly so the append-only
    # delta log matches what the DB cascade deletes (silent-history gap otherwise)
    for edge in cur_edges:
        if edge[1] in removed_slugs or edge[2] in removed_slugs:
            removed_edges.add(edge)

    relationship_changes: list[RelationshipChange] = []
    for rel, f, t in sorted(cand_edges - cur_edges):
        if f in removed_slugs or t in removed_slugs:
            continue  # never add an edge to an entity being removed this compile
        relationship_changes.append(RelationshipChange(op="added", relation_type=rel,
                                                       from_slug=f, to_slug=t))
    for rel, f, t in sorted(removed_edges):
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
