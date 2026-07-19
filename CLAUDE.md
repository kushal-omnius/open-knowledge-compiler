# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Knowledge Compiler** — an open-source system that continuously compiles software engineering artifacts (Git repos, PRs, Jira tickets, docs, OpenAPI specs) into a structured, persistent knowledge base queryable by AI agents. The compiler metaphor is intentional: raw artifacts go in, structured engineering knowledge comes out.

**Architecture v1.0 is FROZEN (2026-07-18).** The spec set: [docs/vision.md](docs/vision.md), [docs/architecture.md](docs/architecture.md), ADR-001…ADR-010 ([docs/decisions/index.md](docs/decisions/index.md)), [docs/ir.md](docs/ir.md), [docs/data-model.md](docs/data-model.md), [docs/pipeline.md](docs/pipeline.md), [docs/normalize.md](docs/normalize.md). ADRs are immutable — changing a decision requires a superseding ADR; living specs accept additive clarifications only (implementation findings are folded in as marked clarifications). Do not create new architecture documents unless implementation reveals a genuine gap. [INITIAL-Brainstorm.md](INITIAL-Brainstorm.md) is the superseded exploratory draft.

**Current phase: V1 pipeline + milestone 2 implemented; dogfood on the team's real repo is the open milestone.** Working: `kc init/compile(--full|--pr|--no-llm)/reconcile/verify/inspect/serve`, Python + TypeScript analyzers, the ADR-004 identity cascade, atomic persist + append-only delta log, OKF wiki emission, loop-safe branch publisher, opt-in LLM semantic layer (anthropic/openai/azure-openai providers, content-addressed cache), opt-in embeddings + hybrid retrieval ([docs/retrieval.md](docs/retrieval.md)), read-only stdio MCP server. Deferred by design: Jira collector, entry-point plugin activation (built-ins wired directly until plugin-sdk.md's trigger fires), HNSW index activation.

Key V1 commitments (see vision.md for rationale): the **Living Wiki is the V1 wedge** (MCP/Q&A is milestone 2, test generation milestone 3); **Python + TypeScript** language analyzers to keep the plugin interface honest; **dogfood on the team's real repo** before generalizing; **deterministic-first extraction** (AST/git/parsers for structure, LLM only for semantics, provenance on every fact).

## Development Commands

```bash
.venv\Scripts\activate               # Windows venv (created; deps installed)
pip install -e .[dev]                # install package + dev deps
pytest                               # run all tests
pytest tests/test_smoke.py -k hash   # run a single test
docker compose up -d                 # Postgres 16 + pgvector (user: kc / kc, db: knowledge)
kc --help                            # CLI (init/compile/reconcile/verify/inspect/serve)
```

Core code map: `ir.py` (two-layer IR models, ADR-009) · `interfaces.py` (stage Protocols, ADR-007) · `collectors/git.py` + `collectors/forge.py` (Collect; FakeForge for tests) · `extractors/python_analyzer.py` + `typescript_analyzer.py` (tree-sitter, ADR-006) · `extractors/llm_extractor.py` + `llm/` (semantic layer, ADR-008; FakeLLMProvider for tests) · `compiler/normalize.py` (identity cascade, normalize.md — determinism checklist §9 is the review gate) · `compiler/diff.py` (removal evidence) · `storage/persist.py` (the atomic commit, ADR-003) · `compiler/run.py` (pipeline orchestration, reconcile, verify) · `wiki/emitter.py` + `wiki/publisher.py` (OKF wiki, ADR-010 branch publishing) · `llm/embeddings.py` + `retrieval/` (embeddings emitter, hybrid search, ADR-005; FakeEmbedder for tests) · `mcp/queries.py` + `mcp/server.py` (read-only MCP serve, never compiles).

Testing conventions: no mocks — real git repos, real Postgres (integration tests skip loudly when it's down), real tree-sitter; LLM tests use `FakeLLMProvider`, embedding tests use `FakeEmbedder`. Both caches (`llm_cache`, `embeddings`) are keyed in part by `model_id` and are shared/repo-agnostic (`llm_cache`) or persist across recompiles (`embeddings`) by design, so tests asserting call counts must use a unique `model_id` per test.

## Intended Tech Stack

- **Implementation language:** Python only (the compiler itself)
- **Target languages analyzed:** Python and TypeScript (V1) — parsed via tree-sitter in-process, no Node.js runtime
- **Build system:** `pyproject.toml`
- **Storage:** PostgreSQL
- **AI agent interface:** MCP (Model Context Protocol)

## Planned Architecture

Pipeline stages (in order):
1. **Collect** — ingest artifacts from Git, PRs, Jira, README, OpenAPI, tests
2. **Extract** — derive engineering knowledge (LLM + deterministic methods)
3. **Normalize** — canonicalize into shared entity schema
4. **Persist** — write to PostgreSQL
5. **Wiki generation** — living Markdown documentation
6. **Embeddings** — semantic vectors for retrieval
7. **Serve** — MCP server for AI agent queries

Planned module layout:
```
compiler/     # core pipeline orchestration
collectors/   # artifact ingestion (Git, Jira, GitHub, etc.)
extractors/   # knowledge extraction
storage/      # persistence layer
retrieval/    # keyword, semantic, and hybrid search
wiki/         # Markdown wiki generator
mcp/          # MCP server for AI agents
tests/
scripts/
docs/         # ADRs go here
```

Canonical knowledge entities: `Project`, `Feature`, `Component`, `API`, `BusinessRule`, `TestCoverage`, `Risk`, `PullRequest`, `JiraStory`, `WikiPage`.

## Design Principles

- Plugin architecture for every compiler stage — collectors, extractors, storage backends, and retrieval strategies are all pluggable.
- Record architectural decisions as ADRs in `docs/`.
- Prefer simplicity and modularity; challenge abstractions before adding them.
- Make external things configurable using, env or config variable, do not hardcode any configs and document everything

## V1 Explicit Non-Goals

Distributed architecture, microservices, graph databases, multi-tenancy, and real-time indexing are out of scope for V1.
