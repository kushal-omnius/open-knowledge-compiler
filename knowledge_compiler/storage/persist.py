"""Persist stage (pipeline.md §3.5): apply the delta in ONE transaction (ADR-003).

State mutation + delta append + run success are atomic — a crash leaves compiled
state untouched and the run re-runnable. Provenance is snapshotted only for
entities in the delta (data-model.md §2).
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from knowledge_compiler.compiler.diff import CompileScope  # noqa: F401  (re-export for runner)
from knowledge_compiler.compiler.normalize import CandidateState, CurrentState
from knowledge_compiler.ir import Anchor, Delta, Entity, Relationship
from knowledge_compiler.storage.schema import (
    CompileRun, DeltaChangeRow, DeltaRelationshipChangeRow, EntityRow,
    ProvenanceRow, RelationshipRow,
)


def load_current_state(session: Session, repo_id: int, repo_slug: str) -> CurrentState:
    """Rehydrate Knowledge IR from rows (the state Normalize matches against)."""
    rows = session.execute(select(EntityRow).where(EntityRow.repo_id == repo_id)).scalars().all()
    entities = [
        Entity(slug=r.slug, entity_type=r.entity_type, repo_id=repo_slug, name=r.name,
               payload=r.payload, content_hash=r.content_hash,
               anchors=tuple(Anchor(**a) for a in (r.anchors or [])))
        for r in rows
    ]
    id_to_slug = {r.id: r.slug for r in rows}
    rel_rows = session.execute(
        select(RelationshipRow).where(RelationshipRow.repo_id == repo_id)).scalars().all()
    relationships = [
        Relationship(relation_type=r.relation_type,
                     from_slug=id_to_slug[r.from_entity_id], to_slug=id_to_slug[r.to_entity_id])
        for r in rel_rows
    ]
    return CurrentState(entities=entities, relationships=relationships)


def persist_compile(session: Session, repo_id: int, run: CompileRun,
                    candidate: CandidateState, delta: Delta) -> None:
    """The atomic commit. Caller opens the transaction; everything here is one unit."""
    changed_slugs = {c.slug for c in delta.entity_changes}

    # -- entities: upsert observed, delete removed --------------------------------
    existing = {r.slug: r for r in session.execute(
        select(EntityRow).where(EntityRow.repo_id == repo_id)).scalars()}

    for entity in candidate.entities:
        row = existing.get(entity.slug)
        anchors = [a.model_dump() for a in entity.anchors]
        if row is None:
            row = EntityRow(repo_id=repo_id, slug=entity.slug, entity_type=entity.entity_type,
                            name=entity.name, payload=entity.payload,
                            content_hash=entity.content_hash, anchors=anchors,
                            first_compile_run_id=run.id, last_compile_run_id=run.id)
            session.add(row)
        elif entity.slug in changed_slugs:
            row.name = entity.name
            row.payload = entity.payload
            row.content_hash = entity.content_hash
            row.anchors = anchors
            row.last_compile_run_id = run.id
    session.flush()

    removed = [c.slug for c in delta.entity_changes if c.op == "removed"]
    if removed:
        session.execute(delete(EntityRow).where(
            EntityRow.repo_id == repo_id, EntityRow.slug.in_(removed)))

    slug_to_id = {r.slug: r.id for r in session.execute(
        select(EntityRow).where(EntityRow.repo_id == repo_id)).scalars()}

    # -- relationships: apply the delta's edge diff --------------------------------
    for rc in delta.relationship_changes:
        if rc.op == "added":
            session.add(RelationshipRow(repo_id=repo_id,
                                        from_entity_id=slug_to_id[rc.from_slug],
                                        relation_type=rc.relation_type,
                                        to_entity_id=slug_to_id[rc.to_slug]))
        else:
            f, t = slug_to_id.get(rc.from_slug), slug_to_id.get(rc.to_slug)
            if f is not None and t is not None:  # CASCADE already handled removed entities
                session.execute(delete(RelationshipRow).where(
                    RelationshipRow.repo_id == repo_id,
                    RelationshipRow.from_entity_id == f,
                    RelationshipRow.relation_type == rc.relation_type,
                    RelationshipRow.to_entity_id == t))

    # -- provenance: snapshots for delta entities only ------------------------------
    for change in delta.entity_changes:
        if change.op == "removed":
            continue
        evidence = candidate.evidence.get(change.slug)
        match_evidence = ({"rule": evidence.rule, "signals": evidence.signals}
                          if evidence else None)
        for snap in candidate.provenance.get(change.slug, []):
            session.add(ProvenanceRow(repo_id=repo_id, entity_id=slug_to_id[change.slug],
                                      compile_run_id=run.id, fact_type=snap["fact_type"],
                                      extraction=snap["extraction"],
                                      artifact_refs=snap["artifact_refs"],
                                      anchors=snap["anchors"], match_evidence=match_evidence))

    # -- the append-only delta log ----------------------------------------------------
    for change in delta.entity_changes:
        session.add(DeltaChangeRow(repo_id=repo_id, compile_run_id=run.id, op=change.op,
                                   entity_id=slug_to_id.get(change.slug),
                                   slug=change.slug, entity_type=change.entity_type,
                                   change_summary=change.change_summary,
                                   evidence=change.evidence))
    for rc in delta.relationship_changes:
        session.add(DeltaRelationshipChangeRow(repo_id=repo_id, compile_run_id=run.id,
                                               op=rc.op, relation_type=rc.relation_type,
                                               from_slug=rc.from_slug, to_slug=rc.to_slug))

    run.status = "succeeded"
