"""Read-only knowledge queries — the substance behind the MCP tools.

Factored out of the server so they're directly testable and reusable (a future
`kc query` CLI rides these for free). Every answer carries provenance-grade
fields; nothing here ever writes (ADR-002: serve never compiles).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge_compiler.storage.schema import (
    CompileRun, DeltaChangeRow, EntityRow, ProvenanceRow, RelationshipRow, Repository,
)


def resolve_repo(session: Session, slug: str) -> Repository:
    repo = session.execute(select(Repository).where(Repository.slug == slug)).scalar_one_or_none()
    if repo is None:
        raise LookupError(f"repository '{slug}' is not registered")
    return repo


def get_entity(session: Session, repo_id: int, slug: str) -> dict | None:
    entity = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo_id, EntityRow.slug == slug)).scalar_one_or_none()
    if entity is None:
        return None

    id_to = dict(session.execute(select(EntityRow.id, EntityRow.slug)
                                 .where(EntityRow.repo_id == repo_id)).all())
    rels = session.execute(select(RelationshipRow).where(
        RelationshipRow.repo_id == repo_id,
        (RelationshipRow.from_entity_id == entity.id) | (RelationshipRow.to_entity_id == entity.id)
    )).scalars().all()
    edges = [{"relation": r.relation_type,
              "from": id_to[r.from_entity_id], "to": id_to[r.to_entity_id]}
             for r in rels
             if not (id_to[r.from_entity_id].startswith("wiki-page/")
                     or id_to[r.to_entity_id].startswith("wiki-page/"))]
    relationships = sorted(edges, key=lambda d: (d["relation"], d["from"], d["to"]))

    prov = session.execute(select(ProvenanceRow).where(ProvenanceRow.entity_id == entity.id)
                           .order_by(ProvenanceRow.compile_run_id.desc()).limit(5)).scalars().all()
    return {
        "slug": entity.slug, "entity_type": entity.entity_type, "name": entity.name,
        "payload": entity.payload, "anchors": entity.anchors or [],
        "relationships": relationships,
        "provenance": [{"fact_type": p.fact_type, "extraction": p.extraction,
                        "compile_run": p.compile_run_id,
                        "match_evidence": p.match_evidence} for p in prov],
    }


def list_entities(session: Session, repo_id: int, entity_type: str,
                  limit: int = 200) -> list[dict]:
    rows = session.execute(select(EntityRow)
                           .where(EntityRow.repo_id == repo_id,
                                  EntityRow.entity_type == entity_type)
                           .order_by(EntityRow.slug).limit(limit)).scalars().all()
    return [{"slug": r.slug, "name": r.name} for r in rows]


def recent_changes(session: Session, repo_id: int, runs: int = 10) -> list[dict]:
    recent = session.execute(select(CompileRun)
                             .where(CompileRun.repo_id == repo_id,
                                    CompileRun.status == "succeeded")
                             .order_by(CompileRun.id.desc()).limit(runs)).scalars().all()
    out = []
    for run in recent:
        changes = session.execute(
            select(DeltaChangeRow.op, DeltaChangeRow.slug, DeltaChangeRow.change_summary)
            .where(DeltaChangeRow.compile_run_id == run.id)
            .order_by(DeltaChangeRow.slug)).all()
        out.append({"compile_run": run.id, "commit": run.commit_sha,
                    "pr_number": run.pr_number, "scope": run.scope,
                    "degraded": run.degraded,
                    "changes": [{"op": op, "slug": slug, "summary": summary}
                                for op, slug, summary in changes]})
    return out


def which_pr_introduced(session: Session, repo_id: int, slug: str) -> dict | None:
    row = session.execute(
        select(DeltaChangeRow, CompileRun)
        .join(CompileRun, DeltaChangeRow.compile_run_id == CompileRun.id)
        .where(DeltaChangeRow.repo_id == repo_id, DeltaChangeRow.slug == slug,
               DeltaChangeRow.op == "added")
        .order_by(DeltaChangeRow.id.desc()).limit(1)).first()
    if row is None:
        return None
    change, run = row
    return {"slug": slug, "pr_number": run.pr_number, "commit": run.commit_sha,
            "compile_run": run.id, "scope": run.scope,
            "note": "full-compile bootstrap (no PR attribution)" if run.pr_number is None else None}


def coverage_for(session: Session, repo_id: int, component_slug: str) -> dict | None:
    component = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo_id, EntityRow.slug == component_slug)).scalar_one_or_none()
    if component is None:
        return None
    tests = session.execute(
        select(EntityRow)
        .join(RelationshipRow, RelationshipRow.from_entity_id == EntityRow.id)
        .where(RelationshipRow.repo_id == repo_id,
               RelationshipRow.relation_type == "covers",
               RelationshipRow.to_entity_id == component.id)
        .order_by(EntityRow.slug)).scalars().all()
    return {"component": component_slug,
            "covered": bool(tests),
            "tests": [{"slug": t.slug, "node_id": t.payload.get("node_id"),
                       "framework": t.payload.get("framework")} for t in tests]}


def knowledge_stats(session: Session, repo_id: int) -> dict:
    counts = dict(session.execute(
        select(EntityRow.entity_type, func.count()).where(EntityRow.repo_id == repo_id)
        .group_by(EntityRow.entity_type)).all())
    last = session.execute(select(CompileRun)
                           .where(CompileRun.repo_id == repo_id, CompileRun.status == "succeeded")
                           .order_by(CompileRun.id.desc()).limit(1)).scalar_one_or_none()
    return {"entity_counts": counts,
            "last_compile": None if last is None else
            {"compile_run": last.id, "commit": last.commit_sha, "degraded": last.degraded}}
