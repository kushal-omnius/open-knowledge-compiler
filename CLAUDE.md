# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Knowledge Compiler** — an open-source system that continuously compiles software engineering artifacts (Git repos, PRs, Jira tickets, docs, OpenAPI specs) into a structured, persistent knowledge base queryable by AI agents. The compiler metaphor is intentional: raw artifacts go in, structured engineering knowledge comes out.

**Architecture v1.0 is FROZEN (2026-07-18).** The spec set: [docs/vision.md](docs/vision.md), [docs/architecture.md](docs/architecture.md), ADR-001…ADR-010 ([docs/decisions/index.md](docs/decisions/index.md)), [docs/ir.md](docs/ir.md), [docs/data-model.md](docs/data-model.md), [docs/pipeline.md](docs/pipeline.md), [docs/normalize.md](docs/normalize.md). ADRs are immutable — changing a decision requires a superseding ADR; living specs accept additive clarifications only (implementation findings are folded in as marked clarifications). Do not create new architecture documents unless implementation reveals a genuine gap. New (non-superseding) architectural decisions discovered post-freeze are still recorded as new ADRs — see [ADR-011](docs/decisions/ADR-011-cross-repo-dependency-resolution.md) (cross-repo dependency resolution, 2026-07-20). [INITIAL-Brainstorm.md](INITIAL-Brainstorm.md) is the superseded exploratory draft.

**Current phase: V1 pipeline + milestone 2 implemented; dogfooded on two real team repos (frida, omnius_llmlib), now with real semantic enrichment.** Working: `kc init/compile(--full|--pr|--no-llm)/reconcile/verify/inspect/serve`, Python + TypeScript analyzers, the ADR-004 identity cascade, atomic persist + append-only delta log, OKF wiki emission, loop-safe branch publisher, opt-in LLM semantic layer (anthropic/openai/azure-openai/cloudflare providers, content-addressed cache), opt-in embeddings + hybrid retrieval ([docs/retrieval.md](docs/retrieval.md)), read-only stdio MCP server (`search_knowledge`/`get_entity`/`impact_plan`/`test_plan`/`resolve_dependency`/…), a Jira collector (opt-in `[jira]`, `collectors/jira.py`: fetches issues linked from a merged PR's title/body, mints `jira_story` entities + `motivates` edges to the PR), and streamed compile progress (`[llm]`/`[embed]` lines to stderr — the LLM/embeddings stages are network-bound and were previously silent until the final summary). `[llm]`/`[embeddings]` (azure-openai, `gpt-4o-mini` + `text-embedding-3-small` — see [llm-usage.md](llm-usage.md)) have now run for real on both dogfood repos: frida has 50 business rules, 517 features, 245 risks (2765 entities total); omnius-llmlib has 18/131/83 (967 entities total); both `kc verify`-clean. Dogfooding on frida surfaced two real internal-import-resolution gaps (Python import root ≠ repo root; TypeScript tsconfig path aliases) — both fixed, see [normalize.md](docs/normalize.md) §7. Deferred by design: entry-point plugin activation (built-ins wired directly until plugin-sdk.md's trigger fires), HNSW index activation. Open: validating `--pr`/reconcile against real merged-PR history (needs a GitHub token + a PR-count check first — full-history-since-epoch reconcile risk, see conversation notes). See BRAINSTORM-test-generation-eval.md's next-steps for the fuller planner/eval roadmap `impact_plan`/`test_plan` are steps 3–4 of.

Key V1 commitments (see vision.md for rationale): the **Living Wiki is the V1 wedge** (MCP/Q&A is milestone 2, test generation milestone 3); **Python + TypeScript** language analyzers to keep the plugin interface honest; **dogfood on the team's real repo** before generalizing; **deterministic-first extraction** (AST/git/parsers for structure, LLM only for semantics, provenance on every fact).

## Development Commands

```bash
.venv\Scripts\activate               # Windows venv (created; deps installed)
pip install -e .[dev]                # install package + dev deps
pytest                               # run all tests
pytest tests/test_smoke.py -k hash   # run a single test
docker compose up -d                 # Postgres 16 + pgvector (user: kc / kc, db: kc_wiki)
kc --help                            # CLI (init/compile/reconcile/verify/inspect/serve)
```

Core code map: `ir.py` (two-layer IR models, ADR-009) · `interfaces.py` (stage Protocols, ADR-007) · `collectors/git.py` + `collectors/forge.py` + `collectors/jira.py` (Collect; FakeForge/FakeJira for tests) · `extractors/python_analyzer.py` + `typescript_analyzer.py` (tree-sitter, ADR-006) · `extractors/llm_extractor.py` + `llm/` (semantic layer, ADR-008; FakeLLMProvider for tests) · `compiler/normalize.py` (identity cascade, normalize.md — determinism checklist §9 is the review gate) · `compiler/diff.py` (removal evidence) · `storage/persist.py` (the atomic commit, ADR-003) · `compiler/run.py` (pipeline orchestration, reconcile, verify) · `wiki/emitter.py` + `wiki/publisher.py` (OKF wiki, ADR-010 branch publishing) · `llm/embeddings.py` + `retrieval/` (embeddings emitter, hybrid search, ADR-005; FakeEmbedder for tests) · `mcp/queries.py` + `mcp/server.py` (read-only MCP serve, never compiles).

Testing conventions: no mocks — real git repos, real Postgres (integration tests skip loudly when it's down), real tree-sitter; LLM tests use `FakeLLMProvider`, embedding tests use `FakeEmbedder`. Both caches (`llm_cache`, `embeddings`) are keyed in part by `model_id` and are shared/repo-agnostic (`llm_cache`) or persist across recompiles (`embeddings`) by design, so tests asserting call counts must use a unique `model_id` per test.

## Test generation: kc-covers header (required)

Every test file generated or materially rewritten by an AI agent — in this
repo or in any repo consuming Knowledge Compiler's compiled knowledge — MUST
carry a `kc-covers:` block in its module-level docstring, naming the exact
compiled entity slug(s) it targets (BRAINSTORM-test-generation-eval.md's
declared-coverage convention; `knowledge_compiler/validation.py` is the
checker; scoring granularity is component/API level per ADR-012). Not
optional, not a comment — must be parseable by `ast.get_docstring()`.

Format (exact):
```python
"""
<optional one-line description of what this test covers>

kc-covers:
  - <entity-slug-1>
  - <entity-slug-2>
"""
```

Rules:
- **Get the slugs from `test_plan`, never invent or guess them.** Call the
  `test_plan` MCP tool (or CLI equivalent) against the target repo first;
  its `test_recommendations` name the exact citable slugs for the gap
  being closed.
- **One citable slug per recommendation, not the raw targets list:**
  - `target_kind: "api"` → cite each API's own slug individually
    (e.g. `api/post-claims`), not the surrounding component.
  - `target_kind: "symbols"` → cite the gap **component's own slug**
    (e.g. `component/billing-rules`) — bare symbol paths are never real
    compiled entities and can never appear here.
- **Never claim a slug you didn't verify exists** — `get_entity` first if
  unsure. A claimed-but-fake slug is a hard failure, not a minor inaccuracy.
- **No header at all is an automatic 0% score** — never skip it, even for a
  throwaway or exploratory test.
- The test file being scored can live in a different repo from the code it
  targets — the header travels with the file itself, not with the target
  repo (see BRAINSTORM-test-generation-mechanism.md's cross-repo note).

After writing the test, validate it before considering the task done:
```bash
kc validate-test <path-to-test-file> --for-entity <originating-slug> --dir <target-repo-dir>
```
A clean run exits 0 (header found, no nonexistent-slug claims). Fix and
re-run if it doesn't — don't hand back a test with an unvalidated header.

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
