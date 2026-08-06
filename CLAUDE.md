# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Knowledge Compiler** (package: `open-knowledge-compiler`, Apache-2.0) — an open-source system that continuously compiles software engineering artifacts (Git repos, PRs, Jira tickets, docs, OpenAPI specs) into a structured, persistent knowledge base queryable by AI agents. The compiler metaphor is intentional: raw artifacts go in, structured engineering knowledge comes out. The emitted wiki is a conformant [OKF](docs/okf-conformance.md) (Open Knowledge Format) bundle — see [ADR-013](docs/decisions/ADR-013-open-source-okf-conformance.md) for spec-version tracking and the open-source release rationale.

**Architecture v1.0 is FROZEN (2026-07-18).** The spec set: [docs/vision.md](docs/vision.md), [docs/architecture.md](docs/architecture.md), ADR-001…ADR-010 ([docs/decisions/index.md](docs/decisions/index.md)), [docs/ir.md](docs/ir.md), [docs/data-model.md](docs/data-model.md), [docs/pipeline.md](docs/pipeline.md), [docs/normalize.md](docs/normalize.md). ADRs are immutable — changing a decision requires a superseding ADR; living specs accept additive clarifications only (implementation findings are folded in as marked clarifications). Do not create new architecture documents unless implementation reveals a genuine gap. New (non-superseding) architectural decisions discovered post-freeze are still recorded as new ADRs — see [ADR-011](docs/decisions/ADR-011-cross-repo-dependency-resolution.md) (cross-repo dependency resolution, 2026-07-20), [ADR-012](docs/decisions/ADR-012-defer-verification-requirement-entity.md) (defer VerificationRequirement entity, 2026-07-29), [ADR-013](docs/decisions/ADR-013-open-source-okf-conformance.md) (open-source release + OKF spec-version conformance/migration, 2026-08-05, Proposed). [INITIAL-Brainstorm.md](INITIAL-Brainstorm.md) is the superseded exploratory draft.

**Current phase: V1 pipeline + milestone 3 implemented; dogfooded on two real team repos (repo A, repo B used as a dependency in repo A), now with real semantic enrichment.** Working: `kc init/compile(--full|--pr|--no-llm|--emit-only)/reconcile/verify/inspect/validate-test/validate-okf/serve`, Python + TypeScript + JavaScript analyzers ([ADR-015](docs/decisions/ADR-015-javascript-language-analyzer.md) implemented: `.js`/`.jsx`/`.mjs`/`.cjs`, ESM + CJS, Express/Fastify routes, Jest tests), the ADR-004 identity cascade, atomic persist + append-only delta log, OKF v0.2-conformant wiki emission (`kc validate-okf` checks it; `docs/okf-conformance.md` maps every spec section to the emitter), loop-safe branch publisher ([ADR-010](docs/decisions/ADR-010-wiki-destination.md)), opt-in LLM semantic layer (anthropic/openai/azure-openai/cloudflare providers, content-addressed cache), opt-in embeddings + hybrid retrieval ([docs/retrieval.md](docs/retrieval.md)), read-only stdio MCP server (`search_knowledge`/`get_entity`/`impact_plan`/`test_plan`/`resolve_dependency`/…; multi-repo setup, worked examples, and known cross-repo limitations are in [docs/cross-repo-workflows.md](docs/cross-repo-workflows.md)), a Jira collector (opt-in `[jira]`, `collectors/jira.py`: fetches issues linked from a merged PR's title/body, mints `jira_story` entities + `motivates` edges to the PR), and streamed compile progress (`[llm]`/`[embed]` lines to stderr — the LLM/embeddings stages are network-bound and were previously silent until the final summary): `[llm]` lines are emitted only on cache misses (annotated `--changed`; cached files are silent); `[embed]` lines are emitted once per batch of 64 entities (annotated `N entities --changed`). `kc compile` and `kc reconcile` both accept `--verbose / -v` to print a per-slug breakdown of added/changed/removed/moved entities in the final summary. `kc compile --emit-only` re-renders the wiki from already-compiled Knowledge IR with no new `compile_runs` row — the cheap OKF-spec-version rollout path (ADR-013); every compile run now also records `okf_spec_version` alongside `fact_vocabulary_version`/`knowledge_model_version` (ir.md §5). `[llm]`/`[embeddings]` (azure-openai, `gpt-4o-mini` + `text-embedding-3-small` — see [llm-usage.md](llm-usage.md)) have now run for real on both dogfood repos: repoA has 50 business rules, 517 features, 245 risks (2765 entities total); repoB has 18/131/83 (967 entities total); both `kc verify`-clean. Dogfooding on repoA surfaced two real internal-import-resolution gaps (Python import root ≠ repo root; TypeScript tsconfig path aliases) — both fixed, see [normalize.md](docs/normalize.md) §7. Self-compiling this repo surfaced a dirty-set sentinel collision in wiki emission (an empty set was ambiguous between "no filter" and "nothing dirty", forcing a full-tree wiki republish on every no-op compile) plus a `log.md` formatting bug (multiple change entries per compile run rendered as one run-on paragraph, no list markers) — both fixed, see [pipeline.md](docs/pipeline.md) §3.6. Dogfooding further surfaced that emission was additive-only — a removed entity's page was never deleted, only ever skipped, so it survived on disk (and in the published wiki) indefinitely at a stale, pre-migration frontmatter shape; fixed by pruning orphaned pages against the live entity set on every emit, also documented in pipeline.md §3.6. Deferred by design: entry-point plugin activation (built-ins wired directly until plugin-sdk.md's trigger fires), HNSW index activation, database-less compile mode (`BRAINSTORM-db-less-compile-mode.md`, explicitly parked), a shared declarative OKF rules file unifying the emitter and `kc validate-okf` ([ADR-014](docs/decisions/ADR-014-shared-okf-rules-file.md), Proposed, unimplemented), Java language analyzer ([ADR-016](docs/decisions/ADR-016-java-language-analyzer.md), Proposed, unimplemented — scoped to structural extraction only, Spring/JAX-RS route detection explicitly deferred pending real dogfood evidence). Open: validating `--pr`/reconcile against real merged-PR history (needs a GitHub token + a PR-count check first — full-history-since-epoch reconcile risk, see conversation notes). See BRAINSTORM-test-generation-eval.md's next-steps for the fuller planner/eval roadmap `impact_plan`/`test_plan` are steps 3–4 of.

Key V1 commitments (see vision.md for rationale): the **Living Wiki is the V1 wedge** (MCP/Q&A is milestone 2, test generation milestone 3); **Python + TypeScript** language analyzers to keep the plugin interface honest; **dogfood on the team's real repo** before generalizing; **deterministic-first extraction** (AST/git/parsers for structure, LLM only for semantics, provenance on every fact).

## Development Commands

```bash
venv\Scripts\activate               # Windows venv (created; deps installed)
pip install -e .[all]                # install all deps
pytest                               # run all tests
pytest tests/test_smoke.py -k hash   # run a single test
docker compose up -d                 # Postgres 16 + pgvector (user: kc / kc, db: kc_wiki)
kc --help                            # CLI (init/compile/reconcile/verify/inspect/validate-test/validate-okf/serve)
```

Core code map: `ir.py` (two-layer IR models, ADR-009) · `interfaces.py` (stage Protocols, ADR-007) · `collectors/git.py` + `collectors/forge.py` + `collectors/jira.py` (Collect; FakeForge/FakeJira for tests) · `extractors/python_analyzer.py` + `typescript_analyzer.py` + `javascript_analyzer.py` (tree-sitter, ADR-006/ADR-015) · `extractors/llm_extractor.py` + `llm/` (semantic layer, ADR-008; FakeLLMProvider for tests) · `compiler/normalize.py` (identity cascade, normalize.md — determinism checklist §9 is the review gate) · `compiler/diff.py` (removal evidence) · `storage/persist.py` (the atomic commit, ADR-003) · `compiler/run.py` (pipeline orchestration, reconcile, verify, `emit_only` — ADR-013's cheap OKF-spec-version rollout path) · `wiki/emitter.py` + `wiki/publisher.py` (OKF wiki, ADR-010 branch publishing) · `wiki/okf_conformance.py` (`kc validate-okf`'s checker, ADR-013) · `validation.py` (`kc validate-test`'s scorer) · `llm/embeddings.py` + `retrieval/` (embeddings emitter, hybrid search, ADR-005; FakeEmbedder for tests) · `mcp/queries.py` + `mcp/server.py` (read-only MCP serve, never compiles).

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
- **Target languages analyzed:** Python and TypeScript (V1 commitment, vision.md) plus JavaScript (added post-freeze, ADR-015) — parsed via tree-sitter in-process, no Node.js runtime
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
