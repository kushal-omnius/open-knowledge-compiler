"""kc serve — read-only MCP server over the compiled knowledge base (milestone 2).

ADR-002 invariant: serve NEVER compiles. It reads whatever state the last
CI-invoked compile produced. Transport: stdio (V1 agents — Claude Code, etc.).
Requires `pip install 'open-knowledge-compiler[serve]'`.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from knowledge_compiler.mcp import queries
from knowledge_compiler.retrieval.search import search as retrieval_search
from knowledge_compiler.storage.db import check_connection, make_engine

ENTITY_TYPES_HINT = ("component, api, business_rule, feature, risk, test_coverage, "
                     "pull_request, project, user_journey, state_model")


def build_server(repo_dir: Path):
    from mcp.server.fastmcp import FastMCP

    from knowledge_compiler.compiler.run import read_config

    config = read_config(repo_dir)
    repo_slug = config["repository"]["slug"]
    dep_map = config.get("dependencies", {})
    engine = make_engine()
    check_connection(engine)  # fail at startup, not on the first tool call

    embedder = None
    emb_cfg = config.get("embeddings", {})
    if emb_cfg.get("enabled", False):
        from knowledge_compiler.llm.embeddings import build_embedder
        from knowledge_compiler.llm.provider import LLMProviderError

        try:
            embedder = build_embedder(emb_cfg)  # hybrid search; absent => FTS-only
        except LLMProviderError:
            embedder = None

    @contextmanager
    def repo_session():
        with Session(engine) as session:
            yield session, queries.resolve_repo(session, repo_slug).id

    mcp = FastMCP(
        "knowledge-compiler",
        instructions=f"Compiled engineering knowledge for repository '{repo_slug}'. "
                     "Every answer carries provenance (anchors into source, extraction "
                     "method, compile run). The knowledge base is read-only here; it is "
                     "updated by CI-triggered compilation, never by this server.\n\n"
                     "## Recommended workflows\n\n"
                     "**Writing or improving tests (QA agent workflow):**\n"
                     "1. Call `test_plan(slug)` on the changed component — it returns "
                     "concrete test targets (APIs or symbols), governing business rules/"
                     "features/risks pre-joined as `context`, staleness flags, optional "
                     "mutation-kill rates, and end-to-end journey gaps. This is the "
                     "single call that answers 'what to test and why'.\n"
                     "2. Call `coverage_for(component_slug)` to see what tests already "
                     "exist and which are stale — avoids creating duplicates.\n"
                     "3. Use `get_entity(slug)` to get the source file path "
                     "(`payload.path`) so you know exactly which file to read.\n"
                     "4. Use `search_knowledge(query)` when you don't yet know the "
                     "slug — it returns ranked entity slugs you can pass to the tools "
                     "above.\n\n"
                     "**Understanding what a component does:**\n"
                     "Call `linked_context(component_slug)` for the business rules, "
                     "features, and risks that govern it. Call `get_entity(slug)` for "
                     "full relationships and provenance.\n\n"
                     "**Planning the impact of a change:**\n"
                     "Call `impact_plan(slug)` for one-hop affected entities and "
                     "coverage gaps. `test_plan` is a superset of this.\n\n"
                     "**Exploring what's compiled:**\n"
                     "Call `knowledge_stats()` for entity counts and last compile "
                     "timestamp. Call `list_entities(entity_type)` to enumerate all "
                     "entities of one type (component, api, business_rule, feature, "
                     "risk, test_coverage, pull_request, project, user_journey, "
                     "state_model).\n\n"
                     "**Slug format:** every entity has a canonical slug of the form "
                     "`<entity_type>/<identifier>` (e.g. `component/billing-rules`, "
                     "`api/post-claims`, `business_rule/discount-cap-rule`). Slugs are "
                     "stable across compiles unless the entity is moved or renamed.")

    @mcp.tool()
    def search_knowledge(query: str, entity_type: str | None = None, limit: int = 10) -> list[dict]:
        """Discover entity slugs by keyword or natural-language query. Use this
        when you don't already know the slug for a component, API, business rule,
        or other entity. Returns ranked results with slug, name, score, payload,
        and source anchors.

        Search mode: hybrid keyword+semantic when embeddings are configured,
        keyword-only (Postgres FTS) otherwise. Both modes return the same fields.

        entity_type filters results to one kind — pass it to reduce noise:
        component, api, business_rule, feature, risk, test_coverage,
        pull_request, project, user_journey, state_model.

        Typical next step: pass the returned slug to test_plan(), get_entity(),
        linked_context(), or coverage_for() for structured detail."""
        with repo_session() as (session, repo_id):
            results = retrieval_search(session, repo_id, query, embedder=embedder,
                                       entity_types=[entity_type] if entity_type else None,
                                       limit=limit)
            return [{"slug": r.slug, "entity_type": r.entity_type, "name": r.name,
                     "score": r.score, "payload": r.payload, "anchors": r.anchors}
                    for r in results]

    @mcp.tool()
    def get_entity(slug: str, max_neighbors: int = 50) -> dict:
        """Full detail for one entity by its canonical slug
        (e.g. 'component/billing-rules', 'api/post-claims',
        'business_rule/discount-cap-rule').

        Returns: payload (structured fields including path, description, symbols),
        anchors (exact source file locations the compiler extracted this from),
        relationships (one-hop graph edges — governs, covers, implemented_by,
        traverses, etc.), and provenance (extraction method, model, compile run).

        relationships is capped at max_neighbors (default 50) — a hub entity
        (e.g. a Project touching thousands of Components) can carry far more
        edges than one call should return. relationship_count is the true total
        and relationships_truncated is true whenever the cap was hit; raise
        max_neighbors if you need the rest.

        For components, payload.path is the importable module path — use it to
        locate the source file. For APIs, payload includes the HTTP method, route,
        and owning component slug.

        For components with cross-repo dependencies, also resolves any
        external_dependencies via kc.toml's [dependencies] map."""
        with repo_session() as (session, repo_id):
            return (queries.get_entity(session, repo_id, slug, dep_map=dep_map,
                                       max_neighbors=max_neighbors)
                    or {"error": f"no entity '{slug}'"})

    @mcp.tool()
    def impact_plan(slug: str) -> dict:
        """Given a changed entity's slug, returns: one-hop affected entities
        (components, APIs, features that depend on or implement this entity),
        which of those have test-coverage gaps, and what this entity reaches
        across configured cross-repo dependencies.

        Not transitive — only direct relationships. Cross-repo *inbound* impact
        (other repos that depend on this one) is out of scope.

        For test-writing purposes, prefer test_plan() — it is a superset of
        this tool and also provides concrete test targets and context."""
        with repo_session() as (session, repo_id):
            return (queries.impact_plan(session, repo_id, slug, dep_map=dep_map)
                    or {"error": f"no entity '{slug}'"})

    @mcp.tool()
    def test_plan(slug: str) -> dict:
        """Deterministic test recommendations for a changed entity: everything
        impact_plan returns, plus concrete targets (APIs or symbols) to write
        tests against for each coverage gap. Names what needs a test; writing
        it stays a separate, later step.

        Each 'api'/'symbols' recommendation inlines a 'context' field (the
        governing business rules, features, and risks linked to that
        component — see linked_context) so no separate lookup is needed.
        Also returns four additional recommendation kinds, each tagged via
        'target_kind': 'stale_retest' (a test exists but the component
        changed more recently than the test was last touched, ADR-018),
        'low_mutation_kill' (declared coverage exists but the mutation-kill
        rate is <=40%, carries a 'mutation_kill_rate' field, ADR-012's named
        trigger — only populated when the repo's kc.toml [mutation] section
        is enabled), 'journey' (every step of a kc.toml-declared
        [[journeys]] end-to-end journey is individually covered, but no
        single test proves the whole chain — ADR-017; only populated when
        the repo declares [[journeys]]), 'transition_gap' (the
        component has a compiled state_model — states plus structurally-
        inferred transitions, ADR-023, Python only — listing each known
        transition with a 'confidence' field; a review surface, not a
        per-edge covered/uncovered verdict), and 'low_escaped_defect_trust'
        (ADR-020: bug-fix PRs on this component kept landing on code that
        was already 'covered' — an outcome-based signal, distinct from
        every proxy above, carrying 'escaped_defect_trust_score' and
        'escaped_defect_fix_count'; only populated on PR-triggered
        compiles, withheld below a minimum fix-count sample; informational
        only, never a hard gate)."""
        with repo_session() as (session, repo_id):
            return (queries.test_plan(session, repo_id, slug, dep_map=dep_map)
                    or {"error": f"no entity '{slug}'"})

    @mcp.tool()
    def linked_context(component_slug: str) -> dict:
        """The business rules, features, and risks linked to a component
        (slug form: component/<path-slug>).

        Returns three groups: 'governs' (business rules that constrain this
        component's behaviour), 'implements' (features this component realises),
        and 'affects' (risks associated with it). Each entry includes the entity's
        slug, name, and description so no follow-up get_entity() call is needed
        for basic context.

        This is the same context that test_plan() inlines into each
        recommendation's 'context' field. Call this standalone when you want
        the governing context without a full test-plan computation."""
        with repo_session() as (session, repo_id):
            return queries.linked_context(session, repo_id, component_slug)

    @mcp.tool()
    def journey_coverage(journey_slug: str) -> dict:
        """End-to-end coverage status for a user journey declared in kc.toml
        (slug form: user_journey/<name-slug>, e.g. user_journey/checkout-flow).

        Distinguishes two states: each step's component is covered individually
        by unit/integration tests (step-level coverage), versus a single test
        proves the entire chain in sequence (end-to-end coverage). Returns the
        journey's ordered steps, which components cover each step, and whether
        a journey-level gap exists.

        Check `status` before trusting the result: 'complete' means every
        declared kc.toml step resolved to a compiled entity; 'partial' or
        'invalid' means one or more steps in `unresolved_steps` didn't resolve
        and were dropped — `covered_end_to_end: true` on a partial journey only
        proves the resolved portion of the chain, not the one you declared.

        A 'journey' recommendation in test_plan() points here when every step
        is individually covered but no single test proves the whole chain.
        Use list_entities('user_journey') to discover available journey slugs."""
        with repo_session() as (session, repo_id):
            return (queries.journey_coverage(session, repo_id, journey_slug)
                    or {"error": f"no journey '{journey_slug}'"})

    @mcp.tool()
    def resolve_dependency(coordinate: str) -> dict:
        """Resolve an external dependency coordinate (e.g. a package/import name
        from a component's external_dependencies) to another repo compiled into
        this same database, via kc.toml's [dependencies] config map."""
        with repo_session() as (session, _repo_id):
            return (queries.resolve_dependency(session, coordinate, dep_map)
                    or {"error": f"'{coordinate}' has no configured [dependencies] mapping"})

    @mcp.tool()
    def list_entities(entity_type: str, limit: int = 200) -> list[dict]:
        """List all entities of one type. Valid entity_type values: component,
        api, business_rule, feature, risk, test_coverage, pull_request, project,
        user_journey, state_model. Returns slug, name, and summary payload for
        each entity.

        Use this for enumeration and discovery — e.g. list all business rules,
        all user journeys, or all APIs. For full detail on one entity, follow
        up with get_entity(slug). Default limit is 200."""
        with repo_session() as (session, repo_id):
            return queries.list_entities(session, repo_id, entity_type, limit)

    @mcp.tool()
    def recent_changes(runs: int = 10) -> list[dict]:
        """Knowledge deltas of the most recent compiles: what was added, changed,
        removed, or moved — with old->new values."""
        with repo_session() as (session, repo_id):
            return queries.recent_changes(session, repo_id, runs)

    @mcp.tool()
    def which_pr_introduced(slug: str) -> dict:
        """Which PR (or bootstrap compile) first added this entity."""
        with repo_session() as (session, repo_id):
            return (queries.which_pr_introduced(session, repo_id, slug)
                    or {"error": f"no 'added' record for '{slug}'"})

    @mcp.tool()
    def coverage_for(component_slug: str) -> dict:
        """Which tests cover a component (slug form: component/<path-slug>).

        Returns: 'covered' (bool), 'tests' (list of covering test entities).
        Each test entry includes: node_id (pytest-style, e.g.
        tests/test_rules.py::test_cap), slug, and two quality signals —
        'stale' (true when the component changed more recently than the test
        was last touched — the test may not exercise the new behaviour, ADR-018)
        and, when kc.toml's [mutation] section is enabled, 'mutation_kill_rate'
        and 'low_mutation_kill' (true when kill rate <= 40%, meaning the test
        passes but misses many code mutations — ADR-012). Also returns
        'escaped_defect_fix_count', 'escaped_defect_trust_score', and
        'low_escaped_defect_trust' (ADR-020): whether bug-fix PRs on this
        component found it already covered — an outcome-based signal,
        populated only on PR-triggered compiles, trust_score null below a
        minimum fix-count sample.

        Use this to check whether writing a new test would duplicate existing
        coverage, or whether an existing test just needs updating (stale=true)."""
        with repo_session() as (session, repo_id):
            return (queries.coverage_for(session, repo_id, component_slug)
                    or {"error": f"no component '{component_slug}'"})

    @mcp.tool()
    def knowledge_stats() -> dict:
        """Entity counts by type and metadata about the last successful compile
        (timestamp, degraded flag, fact/knowledge model versions, OKF spec
        version). Call this first to understand the knowledge base size and
        freshness before running more specific queries."""
        with repo_session() as (session, repo_id):
            return queries.knowledge_stats(session, repo_id)

    return mcp


def serve(repo_dir: Path) -> None:
    build_server(repo_dir).run()  # stdio transport
