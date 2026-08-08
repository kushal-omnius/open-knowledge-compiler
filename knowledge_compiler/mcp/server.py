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
from knowledge_compiler.storage.db import make_engine

ENTITY_TYPES_HINT = ("component, api, business_rule, feature, risk, test_coverage, "
                     "pull_request, project")


def build_server(repo_dir: Path):
    from mcp.server.fastmcp import FastMCP

    from knowledge_compiler.compiler.run import read_config

    config = read_config(repo_dir)
    repo_slug = config["repository"]["slug"]
    dep_map = config.get("dependencies", {})
    engine = make_engine()

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
                     "updated by CI-triggered compilation, never by this server.")

    @mcp.tool()
    def search_knowledge(query: str, entity_type: str | None = None, limit: int = 10) -> list[dict]:
        """Search the knowledge base (hybrid keyword+semantic when embeddings exist,
        keyword otherwise). entity_type filters to one of: component, api,
        business_rule, feature, risk, test_coverage, pull_request, project."""
        with repo_session() as (session, repo_id):
            results = retrieval_search(session, repo_id, query, embedder=embedder,
                                       entity_types=[entity_type] if entity_type else None,
                                       limit=limit)
            return [{"slug": r.slug, "entity_type": r.entity_type, "name": r.name,
                     "score": r.score, "payload": r.payload, "anchors": r.anchors}
                    for r in results]

    @mcp.tool()
    def get_entity(slug: str) -> dict:
        """Full detail for one entity: payload, source anchors, relationships,
        and provenance (how the compiler derived it). For components, also
        resolves any external_dependencies configured in kc.toml's
        [dependencies] as links to other repos compiled into this database."""
        with repo_session() as (session, repo_id):
            return (queries.get_entity(session, repo_id, slug, dep_map=dep_map)
                    or {"error": f"no entity '{slug}'"})

    @mcp.tool()
    def impact_plan(slug: str) -> dict:
        """Given a changed entity, what would be affected (one-hop, this repo),
        which affected components have test-coverage gaps, and what it reaches
        across repos. Not transitive; cross-repo *inbound* impact is out of
        scope (see queries.impact_plan's docstring)."""
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
        Also returns three additional recommendation kinds, each tagged via
        'target_kind': 'stale_retest' (a test exists but the component
        changed more recently than the test was last touched, ADR-018),
        'low_mutation_kill' (declared coverage exists but the mutation-kill
        rate is <=40%, carries a 'mutation_kill_rate' field, ADR-012's named
        trigger — only populated when the repo's kc.toml [mutation] section
        is enabled), and 'journey' (every step of a kc.toml-declared
        [[journeys]] end-to-end journey is individually covered, but no
        single test proves the whole chain — ADR-017; only populated when
        the repo declares [[journeys]])."""
        with repo_session() as (session, repo_id):
            return (queries.test_plan(session, repo_id, slug, dep_map=dep_map)
                    or {"error": f"no entity '{slug}'"})

    @mcp.tool()
    def linked_context(component_slug: str) -> dict:
        """The business rules, features, and risks that govern / are
        implemented by / affect this component (slug form:
        component/<path-slug>) — the same context test_plan inlines into
        its recommendations, exposed standalone for a direct lookup."""
        with repo_session() as (session, repo_id):
            return queries.linked_context(session, repo_id, component_slug)

    @mcp.tool()
    def journey_coverage(journey_slug: str) -> dict:
        """Whether a kc.toml-declared [[journeys]] end-to-end journey
        (slug form: user_journey/<name-slug>) is covered end-to-end by a
        single test that exercises every step's component, versus each step
        only being covered individually (ADR-017)."""
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
        """List entities of one type: component, api, business_rule, feature,
        risk, test_coverage, pull_request, project."""
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
        """Which tests cover this component (slug form: component/<path-slug>).
        Each test also reports 'stale' (true if the component changed more
        recently than the test's own last_compile_run_id, ADR-018 — zero
        schema change) and, when kc.toml's [mutation] section is enabled,
        'mutation_kill_rate'/'low_mutation_kill' (ADR-012's <=40% trigger)."""
        with repo_session() as (session, repo_id):
            return (queries.coverage_for(session, repo_id, component_slug)
                    or {"error": f"no component '{component_slug}'"})

    @mcp.tool()
    def knowledge_stats() -> dict:
        """Entity counts by type and the last successful compile."""
        with repo_session() as (session, repo_id):
            return queries.knowledge_stats(session, repo_id)

    return mcp


def serve(repo_dir: Path) -> None:
    build_server(repo_dir).run()  # stdio transport
