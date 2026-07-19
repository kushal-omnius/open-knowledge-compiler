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


def resolve_dependency(session: Session, coordinate: str,
                       dep_map: dict[str, str]) -> dict | None:
    """Cross-repo dependency resolution (query-time only, kc.toml `[dependencies]`
    config map — no compiled edge, no Normalize/schema involvement). Looks up
    `coordinate` (an external_dependencies string as observed by the analyzer,
    e.g. `omnius_llmlib.core.predict` for a `from omnius_llmlib.core.predict
    import ...`) against the map by exact match or dotted-prefix — the same
    submodule-import pattern normalize.py's internal resolver handles — and if
    it names another repo compiled into this same database, returns that
    repo's identity + entity counts."""
    target_slug = next((slug for key, slug in dep_map.items()
                       if coordinate == key or coordinate.startswith(key + ".")), None)
    if target_slug is None:
        return None
    repo = session.execute(select(Repository).where(
        Repository.slug == target_slug)).scalar_one_or_none()
    if repo is None:
        return None
    counts = dict(session.execute(
        select(EntityRow.entity_type, func.count()).where(EntityRow.repo_id == repo.id)
        .group_by(EntityRow.entity_type)).all())
    project = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo.id, EntityRow.entity_type == "project")).scalar_one_or_none()
    return {"coordinate": coordinate, "repo_slug": repo.slug,
            "project_slug": project.slug if project else None,
            "entity_counts": counts}


def get_entity(session: Session, repo_id: int, slug: str,
               dep_map: dict[str, str] | None = None) -> dict | None:
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
    result = {
        "slug": entity.slug, "entity_type": entity.entity_type, "name": entity.name,
        "payload": entity.payload, "anchors": entity.anchors or [],
        "relationships": relationships,
        "provenance": [{"fact_type": p.fact_type, "extraction": p.extraction,
                        "compile_run": p.compile_run_id,
                        "match_evidence": p.match_evidence} for p in prov],
    }
    if dep_map and entity.entity_type == "component":
        cross_repo = [resolved for dep in entity.payload.get("external_dependencies", [])
                     if (resolved := resolve_dependency(session, dep, dep_map)) is not None]
        if cross_repo:
            result["cross_repo_dependencies"] = cross_repo
    return result


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


def impact_plan(session: Session, repo_id: int, slug: str,
                dep_map: dict[str, str] | None = None) -> dict | None:
    """Composed planning query: given a changed entity, what would be affected
    (within this repo), which of the affected components have test-coverage
    gaps, and what this entity reaches across repos. Pure composition over
    existing queries (get_entity, coverage_for) plus one relationship-graph
    lookup — no new schema, no new relationship types. This is the
    "impact/coverage planner" from the post-ADR-011 roadmap
    (BRAINSTORM-test-generation-eval.md's next steps): step 1 of separating
    *what needs testing* (deterministic) from *writing the test* (LLM, later).

    "Affected" = one-hop incoming edges of type depends_on/governs/
    implemented_by/affects — i.e. anything that would need re-examination if
    `slug` changes: dependents (depends_on), governing business rules
    (governs), features it implements (implemented_by), risks that name it
    (affects). Not transitive in this first cut — a multi-hop blast radius is
    a natural follow-on once this shape is validated against real changes.

    Scope boundary (explicit, not a bug): cross-repo *inbound* impact ("who in
    another repo depends on this") is not answerable here. ADR-011's
    [dependencies] map lives in each *consuming* repo's own kc.toml, and
    `kc serve` only loads the repo it's serving — there is no registry of
    other repos' dependency maps to search. Only outbound resolution (this
    entity's own external_dependencies, via get_entity's cross_repo_dependencies)
    is available today."""
    entity = get_entity(session, repo_id, slug, dep_map=dep_map)
    if entity is None:
        return None

    slug_to_id = dict(session.execute(select(EntityRow.slug, EntityRow.id)
                                      .where(EntityRow.repo_id == repo_id)).all())
    id_to_slug = {v: k for k, v in slug_to_id.items()}
    entity_id = slug_to_id[slug]

    impact_relations = ("depends_on", "governs", "implemented_by", "affects")
    incoming = session.execute(select(RelationshipRow).where(
        RelationshipRow.repo_id == repo_id,
        RelationshipRow.to_entity_id == entity_id,
        RelationshipRow.relation_type.in_(impact_relations),
    )).scalars().all()
    affected = sorted({(r.relation_type, id_to_slug[r.from_entity_id]) for r in incoming})

    coverage_targets = sorted({s for _rel, s in affected if s.startswith("component/")}
                              | ({slug} if entity["entity_type"] == "component" else set()))
    coverage = {t: coverage_for(session, repo_id, t) for t in coverage_targets}

    return {
        "slug": slug, "entity_type": entity["entity_type"],
        "affected": [{"relation": rel, "slug": s} for rel, s in affected],
        "coverage_gaps": sorted(t for t, cov in coverage.items() if cov and not cov["covered"]),
        "coverage_detail": coverage,
        "cross_repo_dependencies": entity.get("cross_repo_dependencies", []),
    }


def test_plan(session: Session, repo_id: int, slug: str,
              dep_map: dict[str, str] | None = None) -> dict | None:
    """Deterministic test-recommendation layer over impact_plan (roadmap step 4,
    BRAINSTORM-test-generation-eval.md): for each coverage gap, name a concrete
    target to write a test against -- the component's own APIs when it defines
    any (method+route+handler is already a machine-readable unit), or its
    public function/class symbols otherwise. This decides *what* needs a test,
    not the test itself: writing it is a later, LLM step (roadmap step 6),
    scored against the declared target this function names (M3 methodology's
    declared-coverage check)."""
    plan = impact_plan(session, repo_id, slug, dep_map=dep_map)
    if plan is None:
        return None

    slug_to_id = dict(session.execute(select(EntityRow.slug, EntityRow.id)
                                      .where(EntityRow.repo_id == repo_id)).all())
    recommendations = []
    for gap_slug in plan["coverage_gaps"]:
        component_id = slug_to_id.get(gap_slug)
        if component_id is None:
            continue
        apis = session.execute(
            select(EntityRow)
            .join(RelationshipRow, RelationshipRow.from_entity_id == EntityRow.id)
            .where(RelationshipRow.repo_id == repo_id,
                   RelationshipRow.relation_type == "defined_in",
                   RelationshipRow.to_entity_id == component_id)
            .order_by(EntityRow.slug)).scalars().all()
        if apis:
            recommendations.append({
                "component": gap_slug, "target_kind": "api",
                "targets": [{"slug": a.slug, "method": a.payload.get("method"),
                            "route": a.payload.get("route")} for a in apis],
            })
        else:
            component = session.execute(select(EntityRow).where(
                EntityRow.id == component_id)).scalar_one()
            symbols = [s["symbol_path"] for s in component.payload.get("symbols", [])
                      if s.get("kind") in ("function", "class")]
            recommendations.append({
                "component": gap_slug, "target_kind": "symbols", "targets": symbols,
            })

    return {**plan, "test_recommendations": recommendations}


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
