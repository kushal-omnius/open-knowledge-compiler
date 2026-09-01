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

- **`gh` CLI token fallback now targets the right host for GHE**: the fallback added in 1.1.1
  called `gh auth token` unconditionally, which returns the token for `gh`'s *default* host —
  silently wrong (or empty) whenever `KC_GITHUB_API` pointed at a GitHub Enterprise instance
  instead of `api.github.com`. `GitHubGateway` now parses the hostname out of `KC_GITHUB_API`
  and passes `gh auth token --hostname <host>` whenever it isn't the default, matching how `gh`
  itself scopes multi-host authentication (`gh auth login --hostname`). No change for the
  default `api.github.com` case. New test `test_falls_back_to_gh_cli_token_for_ghe_host`.

## [1.2.0] — 2026-08-26

### Added

- **`state_model` entity** (ADR-023): closes the "Behavioral Contract" gap named in this project's
  north star — a new deterministic entity type representing one resource's own lifecycle (states
  plus structurally-inferred transitions), distinct from a static invariant (`business_rule`) or a
  cross-resource traversal (`user_journey`). Python analyzer only for V1. New
  `state_transition_observed` fact type and `models` relationship; `test_plan` gains a
  `transition_gap` recommendation kind. Extraction never fabricates a transition across two
  mutually-exclusive branch outcomes — `if`/`elif`/`else` and `try`/`except` branches reset to
  unknown (`from_state: null`) after the branch construct rather than merging — verified against a
  real state machine in dogfood code (a job-status lifecycle) and pinned by a regression test.
- **Escaped-defect trust score** (ADR-020): a new, forward-looking, PR-triggered, outcome-based
  test-quality signal — correlates bug-fix PRs (title/body regex, or a linked Jira issue typed
  `Bug`) against whether the component they touched already had compiled test coverage at the time.
  Cumulative per-component `escaped_defect_fix_count` / `escaped_defect_covered_fix_count` /
  `escaped_defect_trust_score`, surfaced via `coverage_for`/`test_plan`'s new
  `low_escaped_defect_trust`, informational only (never gates anything). PR-triggered compiles only
  — resolves against the compiled `covers` relationship, not the `kc-covers:` declared-coverage
  header, which is never durably persisted.
- **Shared OKF rules file** (ADR-014): a new `wiki/okf_rules.py` module is now the single source of
  truth both `okf_conformance.py`'s validator and `emitter.py`'s renderer consult, closing a
  previously-latent drift risk between the two independently hard-coded encodings. Three points in
  the emitter now self-check against the same rules and fail loudly at emission time on drift,
  instead of only being caught downstream by a separate `kc validate-okf` run.
- **Compile completeness signal** (dogfood-review finding): `compile_runs` gains `files_seen` /
  `files_parsed` / `files_failed` / `failed_files` (migration 0007). A parser failure still skips
  the file, never the compile (ADR-006) — but the three language analyzers now isolate per-file
  exceptions themselves (previously unhandled, so a single bad file could in fact crash the whole
  Extract stage despite the documented contract) and report what they skipped. Surfaced via
  `knowledge_stats()`'s new `knowledge_completeness` field and a new `kc inspect` line. NULL on
  runs predating this migration and on `kc compile --emit-only` reruns.
- **User journey fail-closed status** (dogfood-review finding, extends ADR-017): a `user_journey`
  entity now carries `status` (`complete` | `partial` | `invalid`) and `unresolved_steps`. A
  `kc.toml [[journeys]]` step that doesn't resolve to a compiled entity was already dropped with a
  compile warning, not a hard failure — but the warning only reached the transient compile-run log,
  so the resulting journey entity looked identical to a fully-resolved one everywhere downstream.
  `journey_coverage()` and the journey's wiki page now both surface the status and the exact
  unresolved slugs.
- **`get_entity` relationship limit** (dogfood-review finding): the MCP tool and underlying query
  gain `max_neighbors` (default 50) — previously every relationship touching an entity was returned
  with no limit, so a hub entity could return an unbounded edge set to an agent. `relationship_count`
  and `relationships_truncated` report the true total when the cap is hit.

### Fixed

- **ADR-013's status corrected** from Proposed to Accepted — no code change; an audit found its
  OKF spec-version-tracking mechanism (`OKF_SPEC_VERSION`, `compile_runs.okf_spec_version`,
  `index.md`'s `okf_version` field, `kc compile --emit-only`) was already fully implemented, only
  the ADR's own status line was stale.

## [1.1.1] — 2026-08-20

### Added

- **`gh` CLI token fallback** for the forge gateway: `GitHubGateway` now tries `gh auth token`
  when neither `KC_GITHUB_TOKEN` nor `GITHUB_TOKEN` is set, so a machine with `gh auth login`
  already done doesn't also need a separately-provisioned PAT to unblock `kc reconcile`/
  `kc compile --pr`. Interactive-developer convenience only — there's no non-interactive
  credential behind the `gh` CLI's own auth store, so CI must keep setting the env var
  explicitly (same reasoning as ADR-021's Jira file-gateway scoping). Explicit env vars still
  take precedence and skip the `gh` call entirely. 4 new tests in `tests/test_forge_gateway.py`.

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
- **`journeys_file` config key**: journey definitions can now be declared in
  an external TOML file instead of (or alongside) inline `[[journeys]]`
  entries. `journeys_file` accepts a single path string or an array of path
  strings, each resolved relative to the repo directory. Useful when a
  separate QA repo owns the journey definitions for another repo — set
  `journeys_file = "../qa-repo/frida-journeys.toml"` in the source repo's
  `kc.toml` and keep the canonical definitions there. Inline `[[journeys]]`
  and `journeys_file` entries are merged (inline first). A missing or
  unreadable file fails loudly at compile time.
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
- Every DB-touching entry point (`init`, `compile`/`reconcile`/`verify`/
  `--emit-only`, `inspect`, `validate-test`, `serve`) would either hang
  indefinitely or surface a raw driver traceback when Postgres was
  unreachable (Docker not running, wrong `KC_DATABASE_URL`, network
  black-hole) — no bounded wait, no actionable message. `storage/db.py`
  gains a `connect_timeout` (`KC_DB_CONNECT_TIMEOUT_SECONDS`, default 120s)
  applied via `psycopg`'s `connect_args`, shared with `alembic/env.py` so
  `kc init`'s migration step gets the same bound, plus a
  `check_connection()` helper that fails fast with `is Docker running? Try:
  docker compose up -d` instead of a bare `OperationalError`. Every entry
  point above now calls it before doing any real work and translates the
  result into its existing exception idiom (`CompileError` /
  `click.ClickException`). `kc serve` checks at startup rather than on the
  first tool call.
- `GitHubGateway`'s error message for a 404 from the GitHub API (`kc reconcile`
  / `kc compile --pr`) just echoed the raw `HTTPError`, which reads as "this
  repo doesn't exist" — but GitHub returns 404, not 403, for a repo the token
  can't see, specifically so a caller can't distinguish "doesn't exist" from
  "no access." The message now explicitly names both real causes (private
  repo the token lacks access to, or a stale/wrong `forge_ref` in `kc.toml`)
  and points at what to check. Other HTTP error codes are unaffected.

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
