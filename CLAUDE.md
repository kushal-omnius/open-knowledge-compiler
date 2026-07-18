# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Knowledge Compiler** — an open-source system that continuously compiles software engineering artifacts (Git repos, PRs, Jira tickets, docs, OpenAPI specs) into a structured, persistent knowledge base queryable by AI agents. The compiler metaphor is intentional: raw artifacts go in, structured engineering knowledge comes out.

**Architecture v1.0 is FROZEN (2026-07-18).** The spec set: [docs/vision.md](docs/vision.md), [docs/architecture.md](docs/architecture.md), ADR-001…ADR-010 ([docs/decisions/index.md](docs/decisions/index.md)), [docs/ir.md](docs/ir.md), [docs/data-model.md](docs/data-model.md), [docs/pipeline.md](docs/pipeline.md), [docs/normalize.md](docs/normalize.md). ADRs are immutable — changing a decision requires a superseding ADR; living specs accept additive clarifications only. Do not create new architecture documents unless implementation reveals a genuine gap. [INITIAL-Brainstorm.md](INITIAL-Brainstorm.md) is the superseded exploratory draft. Current phase: implementation.

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

Core code map: `knowledge_compiler/ir.py` (two-layer IR models, ADR-009), `knowledge_compiler/interfaces.py` (stage Protocols + entry-point groups, ADR-007), `knowledge_compiler/cli.py` (run modes, pipeline.md §1).

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
