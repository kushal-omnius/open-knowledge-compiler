# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`, where MAJOR is a breaking change to the CLI, the MCP
tool surface, or the compiled Knowledge IR contract; MINOR is a
backward-compatible feature (a new analyzer, a new MCP tool, a new opt-in
collector); PATCH is a fix with no contract change.

Release process: bump the version in `pyproject.toml` and
`knowledge_compiler/__init__.py`, add a dated entry below, then tag and
publish a GitHub Release from `main` (`gh release create vX.Y.Z --target main
--title vX.Y.Z --generate-notes` — this mints the tag as part of publishing,
so it works even where a raw `git push` of a tag ref is restricted).

## [Unreleased]

### Fixed

- Wiki emission: `dirty` was overloaded — `--emit-only` passed `set()` to mean
  "no filter, render everything," but a genuine zero-change compile also
  produces an empty set to mean "nothing is dirty." Both were treated
  identically, so a no-op compile silently re-rendered and republished the
  entire wiki (every page's `generated: at:` timestamp changes even when
  content doesn't). Now `dirty: set[str] | None`, with `None` meaning "no
  filter" and an empty set meaning "nothing dirty."
- `log.md`: multiple change entries within one compile run rendered as bare
  consecutive lines with no list marker, which Markdown collapses into one
  run-on paragraph. Now rendered as `-`-prefixed list items, matching
  `recent-changes.md`'s existing style.

## [1.0.0] — 2026-08-06

Initial tagged release, as the open-source project `open-knowledge-compiler`
(Apache-2.0).

### Added

- Core pipeline: `kc init` / `compile` (`--full` / `--pr` / `--no-llm` /
  `--emit-only`) / `reconcile` / `verify` / `inspect` / `validate-test` /
  `validate-okf` / `serve`.
- Language analyzers: Python and TypeScript (ADR-006, V1 commitment) and
  JavaScript — `.js`/`.jsx`/`.mjs`/`.cjs`, ESM + CommonJS, Express/Fastify
  routes, Jest tests (ADR-015).
- The ADR-004 stable-entity-identity cascade (external key → anchor overlap →
  name similarity), atomic persist + append-only delta log (ADR-003).
- OKF (Open Knowledge Format) v0.2-conformant wiki emission, checked by
  `kc validate-okf`; every compile run records `okf_spec_version` alongside
  `fact_vocabulary_version`/`knowledge_model_version` (ADR-013).
- Loop-safe wiki branch publisher (ADR-010).
- Opt-in LLM semantic layer — anthropic / openai / azure-openai / cloudflare
  providers, content-addressed cache (ADR-008).
- Opt-in embeddings + hybrid (keyword + semantic) retrieval (ADR-005,
  `docs/retrieval.md`).
- Read-only stdio MCP server (ADR-002: never compiles) exposing
  `search_knowledge`, `get_entity`, `impact_plan`, `test_plan`,
  `resolve_dependency`, `list_entities`, `recent_changes`,
  `which_pr_introduced`, `coverage_for`, `knowledge_stats`.
- Cross-repo dependency resolution — query-time `kc.toml [dependencies]`
  config map, no compiled schema changes (ADR-011); operational setup and
  known limitations documented in `docs/cross-repo-workflows.md`.
- Opt-in Jira collector (`[jira]`): fetches issues linked from a merged PR's
  title/body, mints `jira_story` entities + `motivates` edges.
- `kc-covers:` declared-coverage header convention for AI-generated tests,
  scored by `kc validate-test` (precision/recall against `test_plan`'s
  citable targets).
- Streamed compile progress (`[llm]` / `[embed]` lines to stderr) and
  `--verbose`/`-v` per-slug delta breakdown on `kc compile` / `kc reconcile`.
- `kc compile --emit-only` — re-render the wiki from already-compiled
  Knowledge IR with no new `compile_runs` row (ADR-013's cheap
  OKF-spec-version rollout path).
- Open-source project scaffolding: Apache-2.0 `LICENSE`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, GitHub Actions CI workflow.

### Fixed

- Migration chain for fresh databases (CI was failing on a clean bootstrap).
- A `test_plan` bug (see commit `5b489ee`).

### Proposed, not yet implemented

Recorded as ADRs, not shipped in this release: a shared declarative OKF
rules file unifying the emitter and `kc validate-okf` (ADR-014), and a Java
language analyzer scoped to structural extraction only (ADR-016).
