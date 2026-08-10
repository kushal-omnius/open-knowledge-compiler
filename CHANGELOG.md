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

### Added

- **Commit-fill reconcile** (ADR-002 addendum): `kc reconcile` now runs two passes — the existing
  PR-based pass, then a commit-fill pass (`ForgeGateway.list_commits`) for direct pushes, squash
  commits, and repos without a PR workflow. New `CommitInfo` dataclass `(sha, timestamp, message,
  files)`. New `scope='commit'` value in `compile_runs`; new `commit_timestamp` column
  (migration 0006). Watermark unified to `max(COALESCE(commit_timestamp, merged_at))` across
  succeeded non-full runs. Jira issue keys extracted from commit messages for direct-commit runs.
  `FakeForge` gains a `commits` field and `list_commits()` method. 7 new tests in
  `tests/test_commit_reconcile.py`.

- **Jira→Feature enrichment**: new `_jira_feature_enrichment_facts()` pass in Extract — after
  file-level LLM extraction, calls the LLM once per `jira_observed` issue (template
  `jira-feature-match`) to match each Jira story to compiled feature candidates, emitting
  `jira_feature_link_observed` facts `{jira_key, feature_names}`. Full-compile path uses
  `_file_jira_all_keys()` to load all keys from the file cache so enrichment fires without a PR.
  Normalize resolves these via `_jira_stories()` + `_p5_relationships()` to emit `motivates →
  Feature` edges. Feature wiki pages gain a **"Motivated by Jira"** section listing each Jira
  story as `**KEY** — summary`. Skipped when LLM or Jira is disabled; per-issue failures silently
  skipped (best-effort, additive).

QA-agent test-grounding improvements (backlog items 1, 2, 5, 6, 7, 3, 4; items
8–10 explicitly deferred, see [ADR-019](docs/decisions/ADR-019-test-flakiness-signal.md)/[ADR-020](docs/decisions/ADR-020-escaped-defect-trust-score.md)):

- `test_plan`'s `api`/`symbols` recommendations now inline a `context` field
  (governing business rules, features, and risks linked to the target
  component via a new `linked_context` query) — an agent no longer needs a
  separate `get_entity` round-trip to see *why* a component needs a test.
- `coverage_for`/`test_plan` gain a `stale` flag: a test is stale if the
  component(s) it covers changed more recently than the test itself was last
  touched, computed at query time from the existing entity envelope's
  `last_compile_run_id` — no schema change ([ADR-018](docs/decisions/ADR-018-stale-test-detection.md)).
- New opt-in `[mutation]` config section + `collectors/mutation.py`: attaches
  a CI job's already-produced mutation-kill-rate summary to matching
  Component entities. `coverage_for`/`test_plan` surface `low_mutation_kill`
  when declared coverage exists but the kill rate is ≤40% (ADR-012's named
  trigger). KC never executes the target repo's tests itself.
- New `user_journey` entity type + `traverses` relationship (deterministic
  V1 scope, [ADR-017](docs/decisions/ADR-017-user-journey-entity.md)):
  declare an ordered, end-to-end step list via `kc.toml [[journeys]]`.
  `test_plan` gains a `journey` target kind, `journey_coverage` an MCP tool,
  distinguishing "every step individually covered" from "the whole chain is
  proven by one test." LLM-candidate and E2E-header/Jira-epic extraction
  sources are explicitly deferred, not built.
- Wiki: entity pages gain a bounded "Recent history" section (last 5
  changes, with a pointer to `log.md` for the full chronological record)
  so an agent reading one page sees what changed recently without a
  separate `recent_changes` call.
- Frozen vocabulary counts (`tests/test_smoke.py`) updated to 11 entity
  types / 12 relationship types — additive, non-breaking per ir.md §5, same
  precedent as ADR-011/012/015.

See [docs/decisions/index.md](docs/decisions/index.md) (ADR-017 through
ADR-020) for the full design rationale, including the two rejected/deferred
signals (test flakiness, escaped-defect trust score) not built in this round.

Jira collector: second gateway backend, `[jira] source = "rest" | "file"`
([ADR-021](docs/decisions/ADR-021-jira-gateway-source-abstraction.md)):

- `source = "file"` (`FileJiraGateway`) reads a pre-fetched JSON cache
  instead of calling the live API — for an agent-driven interactive compile
  where the agent already has its own Jira access (e.g. an Atlassian MCP
  connector) but the repo has no Jira API token configured. `cache_file` is
  resolved relative to the repo directory, matching `[wiki] output_dir` and
  `[mutation] scores_file`'s existing precedent — not the process's current
  working directory, which the documented `kc compile --dir /path/to/repo`
  usage need not match. Defaults to `source = "rest"` (the existing
  live-API gateway) when the key is absent, so every pre-existing
  `kc.toml` keeps working unchanged. An unrecognized `source` value fails
  loud (`JiraError`) at gateway-construction time. Interactive-only by
  design — never usable from the CI-triggered path (ADR-002), since
  there's no non-interactive credential behind the access pattern it
  exists to serve.

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
- Wiki emission was additive-only: a removed entity's page was skipped (never
  regenerated) but also never deleted, so it survived on disk — and in the
  published `knowledge/wiki` branch — indefinitely at a stale, pre-migration
  frontmatter shape. `WikiEmitter.emit` now prunes any on-disk page whose
  owner is no longer current, on every emit, self-healing for orphans already
  stranded by prior compiles.
- `kc verify`'s shadow compile (`compiler/run.py`) called only `_extract`,
  omitting `_journey_facts`/`_mutation_facts` — the `kc.toml`-sourced facts
  the real compile path appends — causing every `user_journey` entity and
  mutation score to appear as a spurious divergence whenever `[[journeys]]`
  or `[mutation]` was configured (frida dogfood finding, 2026-08-09). Fixed
  by including both calls in the shadow compile; the invariant (the shadow
  compile must include every fact source the real compile uses) is now
  documented in `pipeline.md` §7.

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
