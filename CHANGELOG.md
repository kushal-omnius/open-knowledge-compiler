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

- `kc eval` no longer counts test modules as changed or affected components.
  kc compiles test files as components, but labels name the source modules
  whose behavior changed, so every test file a PR edited was scored as a
  false positive: on frida, 108 of 132 change-detection false positives.
  A component is a test module when one of its files is a test file by its
  own analyzer's convention (`test_*.py`, `*.test.ts`, …). Such components
  are dropped from `change_detection` and `impact` predictions and listed
  per item as `excluded_test_modules` in `--json`, except components the
  label itself names: the convention can misfire (frida's production
  workflow `cause_agent_test.py`), and the label is the ground truth.
  Coverage scoring is unchanged.

## [1.6.0] — 2026-11-02

### Added

- Rename lineage for path-keyed entities (normalize.md §4.1). A PR that
  renames files still removes and re-adds the components, test coverage,
  state models and wiki pages keyed by those paths, because their slugs are
  derived from the path (ADR-004). The delta now pairs them: `renamed_to` on
  the removed row, `renamed_from` on the added one. A stale slug resolves to
  its successor: MCP `get_entity`/`impact_plan`/`test_plan` (the result is
  marked `renamed_from`), `kc validate-test` (`renamed_claims` instead of a
  nonexistent-slug failure) and `[[journeys]]` steps (warned, recorded in
  `renamed_steps`). Found replaying frida PR #468, a 43-file pure rename.
  PR compiles only: direct-commit and full compiles see no rename report.

### Fixed

- Import resolution no longer crosses languages. The import-root fallback
  (a bare import matching the unique dotted suffix of a component path) also
  matched components in other languages, so Python's stdlib `import types`
  resolved to a lone TypeScript `widgets/types.ts`: a bogus `depends_on` edge
  and an import-`covers` edge from every Python test importing `types`. On
  frida this was the last incremental-vs-full divergence after #36/#37 (6
  components, 28 test coverages, 36 relationship diffs in `kc verify`). The
  fallback now only considers components of the importer's language family
  (Python; TypeScript + JavaScript). This also resolves imports that a
  same-named module in the other language used to make ambiguous: on frida,
  `import utils` under the `backend/` import root now links to `backend.utils`
  instead of staying unresolved next to `frontend/src/lib/utils.ts`, adding 20
  `depends_on` and 24 `covers` edges to a full compile (none removed).
  **Upgrade note:** one `kc compile --full` drops any wrong cross-language edges
  already persisted and adds the newly resolved ones.

- PR and commit compiles now resolve TypeScript path aliases (`@/*` ->
  `./src/*`) the way a full compile does. They used to read `tsconfig*.json`
  only when the change touched it, so every aliased import in a touched
  `.ts`/`.tsx` file landed in `external_dependencies`, `depends_on` edges went
  missing, and `kc verify` diverged after any incremental compile (frida
  dogfood: 259 components). Incremental compiles with TypeScript now also
  read every `tsconfig*.json` at the compile's commit as alias config, without
  adding them to the compile scope.
- Full and incremental compiles no longer disagree on Windows checkouts with
  `core.autocrlf=true`. A full compile (and `kc verify`) read the CRLF working
  tree; PR and commit compiles read LF git blobs. So an unchanged file got a
  different artifact `content_hash` depending on the path. Any symbol with a
  string token spanning lines (docstrings, multi-line strings, template
  literals) got a different `body_hash`. Every incremental compile churned
  those symbols, `kc verify` reported drift, and LLM extraction missed the
  cache between the two paths. The Git collector now converts CRLF to LF on
  both read paths before hashing (pipeline.md §3.1). Lone CRs are kept, as git
  keeps them. Line numbers and anchors do not move.
  **Upgrade note:** on an existing Windows knowledge base, run one
  `kc compile --full` after upgrading. It reports symbols in CRLF files as
  changed (`~N`), once; those deltas are line endings, not code. LLM answers
  that earlier full compiles cached under the CRLF bytes are still used: on a
  cache miss the extractor retries with the CRLF variant of the content and
  copies a hit to the new key. So the upgrade compile makes no new LLM calls
  for unchanged files. Linux/macOS checkouts and repos without autocrlf are
  unaffected. No schema change.

- A PR compile no longer drops a journey's `traverses` edges to steps whose
  files the PR didn't touch. P5 emitted the edges only to entities observed
  in that compile, and a re-observed journey's outgoing edges are
  authoritative, so every such PR deleted them and the next `kc verify`
  diverged (normalize.md §7).

- PR compiles now see every file a large PR changed. The GitHub gateway read
  only the first page (100 files) of a PR's file list, so `kc compile --pr`
  and `kc reconcile` silently compiled a partial scope for bigger PRs: on
  frida, PR #384 changed 240 files and only 77 source files were compiled,
  leaving its new components missing until the next full compile. The list
  is now paged in full. A PR at GitHub's 3000-file listing cap fails with a
  `ForgeError` asking for `kc compile --full`, instead of compiling a scope
  that may be incomplete.
  **Upgrade note:** a knowledge base built by `--pr`/`reconcile` across a
  PR with more than 100 changed files may be missing entities; `kc verify`
  shows the drift, and one `kc compile --full` heals it.
- Jira→feature matching (the per-issue LLM pass after file-level semantic
  extraction) streams `[llm] i/n jira:<KEY> --changed|--failed` progress
  lines, reports issues whose call failed (e.g. exhausted HTTP 429 retries)
  as one compile warning instead of dropping their feature links silently,
  and counts its calls toward `[llm] max_calls_per_run` and the `llm:`
  call/token totals (previously uncounted). A run whose file pass leaves no
  budget for these calls now fails resumably; raise `max_calls_per_run` by
  the number of linked Jira issues if needed.
- The per-repo compile advisory lock is held on a dedicated connection.
  Locking and unlocking through the pooled Session could hit different
  connections once the Jira pass opened a second one, leaking the lock so
  the next compile of that repo in the same process waited forever.
- Journey steps resolve against the post-compile state. A compile that
  itself removed a `[[journeys]]` step's target (e.g. an API slug renamed
  by 1.5.0 router-prefix composition) still counted the step as resolved
  against the pre-compile snapshot, persisted the journey as `complete`
  with no warning, and made the next `kc verify` report it `changed`. Such
  a compile now drops the step with the usual "does not resolve" warning
  and persists `partial`/`invalid`. Unobserved entities that Diff's
  removal-evidence rule keeps (files a PR slice didn't touch) still count.

## [1.5.0] — 2026-11-01

### Added

- `kc eval`: scores `covers`, `stale_coverage`, per-run change detection and
  `impact_plan` against hand labels (`kc-eval.toml`) as micro-averaged
  precision/recall/F1, with `--json` per-label detail. Establishes the
  baseline for the QA-signal fixes below. (Its strict-xfail test for the
  body-only change-detection gap now passes; see Fixed.)

### Changed

- Docs honesty pass: README tagline and CLAUDE.md overview now list only the
  inputs that ship (Git history, PRs, Jira, source code, tests). Prose docs
  (README/Markdown) and OpenAPI specs are listed as planned — no collector or
  extractor exists for either yet.

### Fixed

- Change detection now sees logic-only edits (QA-signal review finding 1).
  The Python, TypeScript and JavaScript analyzers add a per-symbol `body_hash`
  to `symbol_observed` facts: the first 16 hex chars of sha256 over the
  definition's syntax tree, skipping comments. Normalize carries it into each
  entry of the component `symbols` payload. A function-body-only edit now
  produces a component delta, so `kc eval` change detection, `stale_coverage`
  and `impact_plan` see it. Comment and whitespace-only edits still produce no
  delta. `test_body_only_change_is_detected` is no longer xfail.
  **Upgrade note:** the first compile after upgrading reports every component
  as changed (`~N`), once, because each `symbols` entry gains `body_hash`.
  Expect a full wiki republish on that run. There is no re-embedding: the
  embedding text uses only string payload fields, and `symbols` is a list.
  There is no schema migration; `symbols` is JSON payload.
- API routes now include router prefixes (QA-signal review finding 6).
  FastAPI `APIRouter(prefix=...)` + `include_router(..., prefix=...)` and
  Express `app.use('/x', router)` (ESM imports, CommonJS `require`,
  `export default` / `module.exports`) compose into the full served route.
  This covers nesting, a router mounted more than once (one API per mount),
  routers mounted from another file, and an import root below the repo root.
  The analyzers emit new per-file `router_observed` / `router_mount_observed`
  facts and a `router` ref on `api_endpoint_observed`. Normalize composes
  them. PR and commit compiles that touch routing now re-collect every known
  routing file, so an incremental compile composes the same routes as a full
  one (`kc verify` stays clean). Also fixed: a FastAPI empty path
  (`@router.post("")`) was not detected.
  **Upgrade note:** in repos that use prefixes, API routes and therefore
  API slugs change (`api/get-items` becomes `api/get-api-v1-items`). The
  first compile reports the old APIs removed and the new ones added, and a
  `kc-covers:` header citing an old API slug will fail `kc validate-test`
  until it is updated from `test_plan`. Repos without prefixes keep their
  routes and slugs. Components that declare routers or mounts gain a
  `routing` payload field. There is no schema migration.
- `impact_plan` is now transitive (QA-signal review finding 4). A
  recursive CTE walks `depends_on`/`governs`/`implemented_by`/`affects` up
  to `max_depth` hops: default 3, range 1-10, exposed on the MCP tool as
  `impact_plan(slug, max_depth=3)`. Each `affected` entry gains `depth`
  (shortest hop count) and `via` (the previous hop). Ties at the same depth
  resolve to the smallest `(relation, via)`. Cycles are never re-entered.
  `coverage_gaps` / `stale_coverage` / … now cover components at every
  depth, so `test_plan` and `kc eval`'s `impact` metric see the wider blast
  radius. Depth-1 entries keep their old fields and order; `max_depth=1`
  reproduces the old result. No schema change.
- Coverage now distinguishes tests that exercise a component from tests
  that merely import it (QA-signal review finding 2). Python, TypeScript and
  JavaScript test files emit per-test `call` evidence (a test body calls an
  imported name) and `http` evidence (a test-client or supertest request).
  Calls in fixtures, `beforeEach` setup, local helpers and `conftest.py` are
  excluded. Normalize matches `http` paths to API routes and attributes them
  to the defining component. It records the strongest evidence per covered
  component in a new `coverage` field on the `test_coverage` payload: http
  0.8, call 0.7, import 0.3. `covers` edges are unchanged; there is no
  schema migration and no edge attributes. `coverage_for` reports
  `mechanism`/`confidence` per test plus `weakly_covered` and
  `max_confidence`. `covered` (and therefore `impact_plan`/`test_plan` gaps,
  `stale`, `low_mutation_kill` and `low_escaped_defect_trust`) now requires
  confidence ≥ 0.5. `kc eval`'s `covers` metric scores the same set and
  lists weak tests in `--json`.
  **Upgrade note:** components covered only by import-level tests become
  coverage gaps, so expect more `test_plan` recommendations. Tests compiled
  before this change have no evidence recorded and keep counting as
  coverage until the next full compile. Known limitation: a function passed
  rather than called (`CliRunner().invoke(main)`, `pytest.raises(E, f)`)
  is import-level evidence.
- API routes whose slugs collide are no longer silently dropped. `GET /users`,
  `GET /users/{id}` and `GET /users/` all slugify to `api/get-users`, and
  Normalize kept only the last one. It now applies ADR-004's dedup-suffix
  rule: an API that already exists keeps its slug, and a newly seen colliding
  route gets `-2`, `-3`, … in sorted order. No existing slug changes; the
  previously missing routes appear as new API entities.

## [1.4.1] — 2026-10-29

## [1.4.0] — 2026-10-27

### Fixed

- **A full compile now seeds the reconcile watermark** (ADR-002 addendum, 2026-10-27): a `full` run
  used to store no timestamp, so a repo bootstrapped with `kc compile --full` had no watermark and
  its first `kc reconcile` replayed every merged PR since the start of history — rewinding touched
  files to old versions until it caught up, and paying an LLM call per historical file version.
  A full compile now records HEAD's committer timestamp in `compile_runs.commit_timestamp` (no
  migration — the column exists since 0006), so the next reconcile resumes after the compiled
  commit. Only set when HEAD is reachable from `<default_branch>@{upstream}`; a full compile of
  another branch, an unpushed commit, or a repo with no upstream keeps the previous NULL behaviour,
  since a watermark there could make reconcile skip PRs whose code was never compiled. The compile
  summary gains a `reconcile watermark:` line saying which happened. 4 new tests in
  `tests/test_full_compile_watermark.py`.

## [1.3.3] — 2026-09-17

## [1.3.2] — 2026-09-17

### Added

- **`kc validate-test` verification tier**: alongside the existing targeting
  tier (citation precision/recall), a new tier checks whether the test
  actually asserts anything — assertion-presence (always checked,
  synchronous, AST-based) and, when available, the compiled
  `mutation_kill_rate` signal `[mutation]`/`collectors/mutation.py` already
  attaches to Component payloads (read from `test_plan`'s own
  `coverage_detail`, not a new query or CLI flag). `score_pct` is now the
  product of targeting × assertion-presence × mutation (each defaulting to
  a 1.0 multiplier when unchecked/unavailable), not an average, so a
  perfectly-cited test with zero assertions now scores 0.0% instead of
  hiding behind a perfect citation score. New `targeting_pct`,
  `mutation_kill_rate`, `mutation_source_component`, and `score_version`
  fields on `ValidationReport`.
- **`kc mutation-scope <slug>`**: prints an anchor-derived mutmut
  `only_mutate` scope for a compiled entity. Complements, not replaces,
  the existing `[mutation]` collector — that collector consumes a
  pre-existing kill-rate JSON at module granularity; this command helps
  produce a more precisely scoped one in the first place, from the
  entity's own compiled anchors.
- `knowledge_compiler.validation` is now a package
  (`knowledge_compiler/validation/`), with a new `validation/mutation.py`
  submodule for anchor-scoped mutmut targeting. Existing
  `from knowledge_compiler.validation import ...` call sites are
  unaffected — the package re-exports the same names.

- **Per-item reconcile progress**: `[item] i/n PR #<number> merged <date>` / `[item] i/n commit
  <short-sha> @ <date>` now streams to stderr right before each item in a `kc reconcile`/
  `kc compile --pr` run starts — before Collect, so it fires even for an item with no
  LLM-eligible changes (those previously produced zero output until their final summary block,
  making a long reconcile look silent/stuck between summaries). `i`/`n` count every fetched item
  for that invocation (including idempotence-skipped ones) and reset on each call.

- **Jira key-resolution progress**: `[jira] i/n <scope>: R/N keys resolved (missing: ...)` now
  streams to stderr whenever a compile item extracts at least one Jira issue key — `<scope>` is
  `PR #<number>`, `commit <short-sha>`, or `full compile`. Jira linking was previously silent in
  both directions: a clean resolution and a silently-missing key produced identical (no) output,
  so there was no way to tell from the terminal whether a PR's cited ticket actually linked or
  quietly didn't. Missing keys still aren't an error (unchanged "absence is data, not an outage"
  contract) — they're just visible now.

### Fixed

- **`user_journey` steps no longer spuriously drop on incremental compiles**: found dogfooding
  repoA — a genuinely fresh `kc reconcile` dropped every declared step of a journey, even though
  all of them were real, queryable compiled entities. Root cause: `_user_journeys()` resolved each
  step against `self.entities` only, which is Normalize's per-run slice (only what this pass's
  facts touched) — an incremental `reconcile` only extracts facts for files that changed in that
  pass, so a step whose defining file simply wasn't touched this time looked identical to one that
  was actually removed, and got dropped (with the ADR-017 fail-closed `status`/`unresolved_steps`
  field correctly, but misleadingly, reporting the journey as `invalid`). Steps now also resolve
  against `self.current.entities` (the full accumulated state), matching how other Normalize
  methods already handle cross-run continuity (e.g. the identity-cascade matching pool, `_project()`).
  New regression test reproduces the bug end-to-end (full compile, then an incremental reconcile
  touching an unrelated file, asserting the journey stays `complete`) and fails without the fix.

## [1.3.1] — 2026-09-01

### Added

- **Persisted LLM/embedding token usage**: 1.3.0 surfaced real token usage in `CompileSummary`
  and stderr progress lines only — nothing landed in the database, so the numbers vanished once
  the terminal scrolled past them. New migration (`0008`) adds `llm_calls`/`llm_input_tokens`/
  `llm_output_tokens`/`embedding_calls`/`embedding_input_tokens` to `compile_runs` (nullable,
  same "NULL = not measured" convention as `files_seen`/0007), written by `_compile_one`. Rows
  from before this migration — including anything compiled on 1.3.0 — stay `NULL` permanently;
  there's no way to recover token usage that was never written down.

## [1.3.0] — 2026-09-01

### Added

- **Real LLM/embedding token usage tracking**: `usage` from every provider's API response
  (Anthropic/OpenAI/Azure OpenAI/Cloudflare for `[llm]`, OpenAI/Azure OpenAI for `[embeddings]`)
  was previously discarded, making cost estimates for a compile or reconcile run pure guesswork.
  `[llm]`/`[embed]` stderr progress lines now show that request's actual token usage; each run's
  `CompileSummary` carries real totals (`llm_calls`/`llm_input_tokens`/`llm_output_tokens`,
  `embedding_calls`/`embedding_input_tokens`, cache hits excluded since they cost nothing), printed
  by `kc compile`/`kc reconcile`; a multi-PR `kc reconcile`/`kc compile --pr` run also prints a
  grand total across every item walked, not just the last one.

- **`gh` CLI token fallback** for the forge gateway: `GitHubGateway` now tries `gh auth token`
  when neither `KC_GITHUB_TOKEN` nor `GITHUB_TOKEN` is set, so a machine with `gh auth login`
  already done doesn't also need a separately-provisioned PAT to unblock `kc reconcile`/
  `kc compile --pr`. Interactive-developer convenience only — there's no non-interactive
  credential behind the `gh` CLI's own auth store, so CI must keep setting the env var
  explicitly (same reasoning as ADR-021's Jira file-gateway scoping). Explicit env vars still
  take precedence and skip the `gh` call entirely. Correctly targets non-default hosts too:
  `GitHubGateway` parses the hostname out of `KC_GITHUB_API` and passes
  `gh auth token --hostname <host>` whenever it isn't `api.github.com`, matching how `gh` itself
  scopes multi-host authentication (`gh auth login --hostname`) — needed for GitHub Enterprise.
  5 new tests in `tests/test_forge_gateway.py`.

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
  `journeys_file = "../qa-repo/user-journeys.toml"` in the source repo's
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
  or `[mutation]` was configured (repoA dogfood finding, 2026-08-09). Fixed
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
