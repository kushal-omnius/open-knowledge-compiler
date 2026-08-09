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


def _is_test_component(payload: dict) -> bool:
    """A component consisting entirely of test file(s) is not itself something
    that needs test coverage — recommending a test-of-a-test is nonsensical.
    Mirrors the naming heuristics python_analyzer/typescript_analyzer already
    use to decide whether a *file* is a test file (they don't tag the
    resulting component_observed fact with is_test, so this is re-derived
    from `files` here rather than duplicating extractor internals)."""
    files = payload.get("files") or []
    if not files:
        return False
    return all(
        name.startswith("test_") or name.endswith("_test.py")
        or ".test." in name or ".spec." in name
        for f in files
        for name in (f.rsplit("/", 1)[-1],)
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
    e.g. `repoB.core.predict` for a `from repoB.core.predict
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


def linked_context(session: Session, repo_id: int, component_slug: str) -> dict:
    """Business rules / features / risks that `governs`/`implemented_by`/`affects`
    the given component (normalize.py only wires these edges component-directed —
    see normalize.py's feature/business_rule/risk handling — so this is the real,
    currently-compiled reach of that context, not the aspirational per-API `exposes`
    edge which isn't wired yet). Used to enrich `test_plan` recommendations with
    the business intent behind a coverage gap, instead of a bare structural target."""
    component = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo_id, EntityRow.slug == component_slug)).scalar_one_or_none()
    if component is None:
        return {"business_rules": [], "features": [], "risks": []}

    incoming = session.execute(
        select(EntityRow, RelationshipRow.relation_type)
        .join(RelationshipRow, RelationshipRow.from_entity_id == EntityRow.id)
        .where(RelationshipRow.repo_id == repo_id,
               RelationshipRow.to_entity_id == component.id,
               RelationshipRow.relation_type.in_(("governs", "implemented_by", "affects")))
        .order_by(EntityRow.slug)).all()

    business_rules = [{"slug": e.slug, "name": e.name, "statement": e.payload.get("statement"),
                       "intent": e.payload.get("intent")}
                      for e, rel in incoming if rel == "governs"]
    features = [{"slug": e.slug, "name": e.name, "narrative": e.payload.get("narrative")}
                for e, rel in incoming if rel == "implemented_by"]
    risks = [{"slug": e.slug, "name": e.name, "description": e.payload.get("description"),
             "category": e.payload.get("category")}
             for e, rel in incoming if rel == "affects"]
    return {"business_rules": business_rules, "features": features, "risks": risks}


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
    # Stale-test detection (ADR-018): a test "covers" a component structurally, but
    # last_compile_run_id (the common entity envelope field every entity already
    # carries, ir.md §3.1) tells us the compile run that last touched each side.
    # If the component changed (in a *later* compile run) more recently than a
    # given test was itself touched, the test may be asserting stale behavior —
    # a cheaper, equivalent read of the same signal a delta_changes join would
    # give, with no new schema and no new query surface.
    test_list = [{"slug": t.slug, "node_id": t.payload.get("node_id"),
                 "framework": t.payload.get("framework"),
                 "stale": t.last_compile_run_id < component.last_compile_run_id}
                for t in tests]
    # Mutation-kill surfacing (item 2, ADR-012's named trigger condition): when
    # a component already looks "covered" but its mutation-kill rate (opt-in,
    # collectors/mutation.py) is low, that's the concrete, execution-based
    # signal that a test may be calling the code without verifying its
    # invariant — surfaced here, inline, instead of only in a separate CI
    # artifact nobody consuming test_plan/coverage_for is looking at.
    mutation_kill_rate = component.payload.get("mutation_kill_rate")
    low_mutation_kill = (bool(tests) and mutation_kill_rate is not None
                        and mutation_kill_rate <= 0.4)
    return {"component": component_slug,
            "covered": bool(tests),
            "stale": bool(test_list) and all(t["stale"] for t in test_list),
            "mutation_kill_rate": mutation_kill_rate,
            "low_mutation_kill": low_mutation_kill,
            "tests": test_list}


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

    candidate_targets = ({s for _rel, s in affected if s.startswith("component/")}
                         | ({slug} if entity["entity_type"] == "component" else set()))
    candidate_rows = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo_id, EntityRow.slug.in_(candidate_targets))).scalars().all() \
        if candidate_targets else []
    test_component_slugs = {r.slug for r in candidate_rows if _is_test_component(r.payload)}
    coverage_targets = sorted(candidate_targets - test_component_slugs)
    coverage = {t: coverage_for(session, repo_id, t) for t in coverage_targets}

    return {
        "slug": slug, "entity_type": entity["entity_type"],
        "affected": [{"relation": rel, "slug": s} for rel, s in affected],
        "coverage_gaps": sorted(t for t, cov in coverage.items() if cov and not cov["covered"]),
        # Distinct from coverage_gaps: covered, but every covering test predates
        # the component's most recent change (ADR-018) — a different condition
        # from "no coverage," surfaced separately so it isn't mistaken for one.
        "stale_coverage": sorted(t for t, cov in coverage.items() if cov and cov["covered"] and cov["stale"]),
        # Distinct from both of the above: covered, fresh (not stale), but the
        # mutation-kill rate says the covering test(s) may not verify the real
        # invariant (item 2, ADR-012's trigger condition).
        "low_mutation_kill": sorted(t for t, cov in coverage.items()
                                    if cov and cov["covered"] and not cov["stale"] and cov["low_mutation_kill"]),
        "coverage_detail": coverage,
        "cross_repo_dependencies": entity.get("cross_repo_dependencies", []),
    }


def _journey_step_components(session: Session, repo_id: int, step_slug: str) -> set[str]:
    """Resolve one journey step slug down to the underlying component(s) it
    actually reaches, so end-to-end coverage can be checked in component
    terms regardless of whether the step names a component directly or an
    api/business_rule/feature/risk that governs/implements/exposes one."""
    if step_slug.startswith("component/"):
        return {step_slug}
    prefix_to_relation = {
        "api/": "defined_in", "business_rule/": "governs",
        "feature/": "implemented_by", "risk/": "affects",
    }
    relation = next((rel for prefix, rel in prefix_to_relation.items()
                     if step_slug.startswith(prefix)), None)
    if relation is None:
        return set()
    step = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo_id, EntityRow.slug == step_slug)).scalar_one_or_none()
    if step is None:
        return set()
    targets = session.execute(
        select(EntityRow.slug)
        .join(RelationshipRow, RelationshipRow.to_entity_id == EntityRow.id)
        .where(RelationshipRow.repo_id == repo_id,
               RelationshipRow.from_entity_id == step.id,
               RelationshipRow.relation_type == relation,
               EntityRow.entity_type == "component")).scalars().all()
    return set(targets)


def journey_coverage(session: Session, repo_id: int, journey_slug: str) -> dict | None:
    """ADR-017 (items 3+4): whether any *single* test exercises every
    component a journey's steps reach — the structural fix for a test-slip
    where each step scores 100% coverage individually but the chain as a
    whole is never proven end-to-end. `component/api etc. -> covering test`
    reuses the same `covers` relationship coverage_for already reads."""
    journey = session.execute(select(EntityRow).where(
        EntityRow.repo_id == repo_id, EntityRow.slug == journey_slug,
        EntityRow.entity_type == "user_journey")).scalar_one_or_none()
    if journey is None:
        return None

    step_components: set[str] = set()
    for step_slug in journey.payload.get("steps", []):
        step_components |= _journey_step_components(session, repo_id, step_slug)

    if not step_components:
        return {"journey": journey_slug, "steps": journey.payload.get("steps", []),
               "step_components": [], "covered_end_to_end": False, "covering_tests": []}

    component_ids = dict(session.execute(select(EntityRow.slug, EntityRow.id).where(
        EntityRow.repo_id == repo_id, EntityRow.slug.in_(step_components))).all())
    covers_rows = session.execute(
        select(RelationshipRow.from_entity_id, RelationshipRow.to_entity_id)
        .where(RelationshipRow.repo_id == repo_id,
               RelationshipRow.relation_type == "covers",
               RelationshipRow.to_entity_id.in_(component_ids.values()))).all()
    covered_by: dict[int, set[int]] = {}
    for test_id, component_id in covers_rows:
        covered_by.setdefault(test_id, set()).add(component_id)

    needed = set(component_ids.values())
    covering_test_ids = sorted(t for t, comps in covered_by.items() if comps >= needed)
    covering_tests = ([] if not covering_test_ids else
                      [r.slug for r in session.execute(select(EntityRow).where(
                          EntityRow.id.in_(covering_test_ids))).scalars().all()])
    return {"journey": journey_slug, "steps": journey.payload.get("steps", []),
           "step_components": sorted(step_components),
           "covered_end_to_end": bool(covering_tests),
           "covering_tests": sorted(covering_tests)}


def _journeys_containing(session: Session, repo_id: int, slug: str) -> list[str]:
    return sorted(session.execute(
        select(EntityRow.slug)
        .join(RelationshipRow, RelationshipRow.from_entity_id == EntityRow.id)
        .where(RelationshipRow.repo_id == repo_id,
               EntityRow.entity_type == "user_journey",
               RelationshipRow.relation_type == "traverses",
               RelationshipRow.to_entity_id == (
                   select(EntityRow.id).where(EntityRow.repo_id == repo_id,
                                              EntityRow.slug == slug).scalar_subquery()
               ))).scalars().all())


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
        # Business rules / features / risks that govern this gap (items 1 & 6 of
        # the QA-agent-grounding backlog): inlined here so an agent doesn't have
        # to make a separate get_entity call per recommendation to learn *why*
        # a gap matters, not just what to call.
        context = linked_context(session, repo_id, gap_slug)
        if apis:
            recommendations.append({
                "component": gap_slug, "target_kind": "api",
                "targets": [{"slug": a.slug, "method": a.payload.get("method"),
                            "route": a.payload.get("route")} for a in apis],
                "context": context,
            })
        else:
            component = session.execute(select(EntityRow).where(
                EntityRow.id == component_id)).scalar_one()
            symbols = [s["symbol_path"] for s in component.payload.get("symbols", [])
                      if s.get("kind") in ("function", "class")]
            recommendations.append({
                "component": gap_slug, "target_kind": "symbols", "targets": symbols,
                "context": context,
            })

    # Stale-test recommendations (item 7, ADR-018): a distinct target_kind from
    # the coverage-gap recommendations above — the component IS covered, but
    # every covering test predates its most recent change. Names the existing
    # tests to review/refresh rather than a fresh coverage target.
    for stale_slug in plan["stale_coverage"]:
        cov = plan["coverage_detail"][stale_slug]
        recommendations.append({
            "component": stale_slug, "target_kind": "stale_retest",
            "targets": [t["slug"] for t in cov["tests"]],
            "context": linked_context(session, repo_id, stale_slug),
        })

    # Low-mutation-kill recommendations (item 2): a third, distinct condition
    # from an uncovered gap or a stale test — the covering test is current,
    # but the execution-based signal says it may not verify the real
    # invariant. Names the existing tests to strengthen, plus the mutation
    # rate itself so an agent can prioritize.
    for weak_slug in plan["low_mutation_kill"]:
        cov = plan["coverage_detail"][weak_slug]
        recommendations.append({
            "component": weak_slug, "target_kind": "low_mutation_kill",
            "targets": [t["slug"] for t in cov["tests"]],
            "mutation_kill_rate": cov["mutation_kill_rate"],
            "context": linked_context(session, repo_id, weak_slug),
        })

    # Journey-level recommendations (items 3+4, ADR-017): a fourth, distinct
    # condition from all of the above — every step of a journey can be
    # individually covered (even fresh, even high-mutation-kill) while no
    # single test exercises the chain end-to-end. Covers both directions:
    # `slug` naming the journey itself, and `slug` naming a step that
    # participates in one or more journeys.
    journey_slugs = ([slug] if plan["entity_type"] == "user_journey" else []) \
        + _journeys_containing(session, repo_id, slug)
    for journey_slug in dict.fromkeys(journey_slugs):  # de-dup, preserve order
        jc = journey_coverage(session, repo_id, journey_slug)
        if jc is None or jc["covered_end_to_end"]:
            continue
        recommendations.append({
            "component": journey_slug, "target_kind": "journey",
            "targets": jc["step_components"],
            "steps": jc["steps"],
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
