# `kc` CLI Reference

Knowledge Compiler command-line interface. Compiles engineering artifacts (Git, PRs, docs, code) into a structured, queryable knowledge base.

```
kc [--version] [--help] <command> [options]
```

---

## Commands

### `kc init`

Register a repository: runs DB migrations, inserts the repo row, and writes `kc.toml` into the target directory.

```bash
kc init --slug <slug> --forge-ref <ref> [--default-branch <branch>] [--dir <path>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--slug` | yes | — | Unique identifier for this repo in the knowledge base (e.g. `knowledge-compiler`). |
| `--forge-ref` | yes | — | Canonical forge reference (e.g. `github.com/org/repo`). |
| `--default-branch` | no | `main` | Branch the compiler tracks. |
| `--dir` | no | `.` | Directory to write `kc.toml` into. |

**Run once per repo, before any `kc compile`.**

```bash
kc init --slug my-service --forge-ref github.com/acme/my-service --dir /path/to/repo
```

---

### `kc compile`

Compile the repository. Exactly one of `--full`, `--pr`, or `--emit-only` is required.

```bash
kc compile (--full | --pr <number> | --emit-only) [--dir <path>] [--no-llm]
```

| Option | Description |
|--------|-------------|
| `--full` | Bootstrap / escape-hatch. Always re-runs the full pipeline against HEAD. No up-to-date check — use this for first-time compilation or to force a clean pass. LLM and embedding caches make repeated runs cheap for unchanged files. |
| `--pr <N>` | Incremental compilation of one merged PR. Idempotent: skipped if PR N already has a `succeeded` row in `compile_runs`. |
| `--emit-only` | Re-render the wiki bundle from already-compiled Knowledge IR only — no Collect/Extract/Normalize, no new `compile_runs` row. Requires at least one prior successful compile. The cheap OKF-spec-version rollout path ([ADR-013](decisions/ADR-013-open-source-okf-conformance.md)): a spec bump is an emitter code change plus a re-render, never a data migration. Cannot be combined with `--full` or `--pr`. |
| `--no-llm` | Skip LLM extraction — deterministic pass only. Run is marked `degraded`. LLM-derived entities (features, business rules, risks) are never removed by degraded runs. |
| `--dir` | Repository directory containing `kc.toml` (default: `.`). |

```bash
kc compile --full                     # first compile or forced full pass
kc compile --full --no-llm            # fast re-index without LLM calls
kc compile --pr 142                   # compile one PR incrementally
kc compile --emit-only                # re-render the wiki only, no new compile
```

**Progress** is streamed to stderr (`[llm]` and `[embed]` lines with counts and real token usage per request) so long-running stages are no longer silent. The final summary reports each run's LLM/embedding call and token totals; `kc reconcile`/`kc compile --pr` also print a grand total across every PR/commit walked.

---

### `kc reconcile`

Catch up on merged PRs and direct commits missed since the last successful compile. Runs two passes: PR-based (existing path) then commit-fill covering direct pushes, squash commits, and repos without a PR workflow. Items are processed in timestamp order; already-succeeded items are skipped (idempotent). Uses a unified watermark (`max(COALESCE(commit_timestamp, merged_at))` over succeeded non-full runs) so it never re-examines old history.

```bash
kc reconcile [--dir <path>]
```

**Requires a `GITHUB_TOKEN` env var** (or `~/.config/gh/hosts.yml` via `gh auth login`) — it queries the GitHub API for PR metadata.

```bash
kc reconcile                          # catch up after several merged PRs
kc reconcile --dir /path/to/repo
```

Output: one summary line per PR compiled, or `"up to date — no merged PRs after the watermark"`.

**`kc compile` requires exactly one of `--full`, `--pr`, or `--emit-only` — there is no bare incremental mode.**

**When to use `reconcile` vs `compile --full`:**

Both produce the same final entity state. The difference is cost and history:

| | `kc reconcile` | `kc compile --full` |
|---|---|---|
| Files extracted | Only files changed in each PR | Every file in the repo, every time |
| LLM calls (1–3 PRs, 5–10 files) | ~5–10 calls | ~50+ calls |
| LLM calls (20 accumulated PRs) | More expensive — same files hit once per touching commit | Always ~50+ calls |
| PR attribution | Preserved — `which_pr_introduced` works, delta history per-PR | Lost — only current state recorded |
| Design intent | Run after every merge (CI-triggered, ADR-002) | First compile, suspected drift, or big restructure |

**Rule of thumb:**
- Run `kc reconcile` frequently — ideally after every merge. At that cadence (1–3 PRs at a time) it is 5–10× cheaper than `--full` because it only touches changed files.
- If reconcile has accumulated many commits (10+), `--full` becomes comparably cheaper and gives a cleaner baseline — but you lose per-PR lineage for those commits.
- Use `--full` for: first compile, after a large restructure, or when `kc verify` reports drift.

---

### `kc verify`

Shadow full compile + equivalence check. Runs the full pipeline (Collect → Extract → Normalize → Diff) with **no writes**, then compares the result against current DB state. Exits 1 if they diverge.

```bash
kc verify [--dir <path>]
```

```bash
kc verify                             # is DB in sync with HEAD?
```

Output on success:
```
identity evidence: {'git_sha': 12, 'name+kind': 4, ...}
VERIFIED: incremental state is equivalent to a full compile
```

Output on divergence:
```
DIVERGED: a full compile would produce the following delta:
  add: component/new-module
  remove: component/old-module
remedy: kc compile --full (slug-preserving)
```

Use this in CI to detect silent drift, or before reporting "no changes" when you're not sure.

---

### `kc inspect`

Entity/relationship counts and last delta — the first debugging surface. Reads directly from the DB; never compiles.

```bash
kc inspect [--dir <path>]
```

```bash
kc inspect
```

Example output:
```
repository: knowledge-compiler
entities: 2765
  component: 312
  feature: 517
  business_rule: 50
  risk: 245
  api: 98
  test_coverage: 201
  pull_request: 1342
relationships: 8410
last compile: run 47 @ a3f9c1d20b12 [2026-07-18 09:14:22] — delta add:14  change:3  remove:1
knowledge completeness: 1798/1800 files parsed (99.9%)
  failed: legacy/scanner.py, vendor/patched_lib.py
```

The completeness lines are omitted for compile runs that predate the signal, and for `kc compile --emit-only` reruns (no Extract stage runs).

---

### `kc validate-test`

Score a generated test's `kc-covers:` header against compiled knowledge. **Never compiles** — a downstream check over already-compiled state.

Exits 0 unless the header is missing entirely or any claimed slug doesn't exist in the knowledge base.

```bash
kc validate-test <test_file> --for-entity <slug> [--dir <path>]
```

| Argument/Option | Required | Description |
|-----------------|----------|-------------|
| `test_file` | yes | Path to the test file to score (any location on disk). |
| `--for-entity` | yes | Entity slug originally passed to `test_plan` for this test's recommendations. |
| `--dir` | no | Repo directory whose `kc.toml` defines the knowledge base to check against (default: `.`). Decoupled from `test_file` — the test can live anywhere. |

```bash
# Test lives in Repo C, knowledge is in Repo A
kc validate-test /path/to/repoC/tests/test_billing.py \
  --for-entity component/billing-rules \
  --dir /path/to/repoA

# Test in the same repo
kc validate-test tests/test_billing.py --for-entity component/billing-rules
```

Example output (pure api-kind gap — no ceiling):
```
kc-covers: tests/test_billing.py
  for entity: component/billing-rules

header:            FOUND
claimed slugs:     component/billing-rules, api/get-discount

existence:
  component/billing-rules          OK
  api/get-discount                 OK

precision/recall vs test_plan(component/billing-rules):
  citable recommended:  api/get-discount, component/billing-rules
  precision: 100.0%
  recall:    100.0%

mutation data:     not checked here -- see mutation-test.yaml for the execution-based signal

SCORE: 100.0%
```

Example output (mixed api-kind + symbols-kind gap — ceiling shown):
```
precision/recall vs test_plan(component/backend-storage-blob-utils):
  citable recommended:  api/delete-claims, api/get-claims, ..., component/backend-storage-blob-utils
  precision: 100.0%
  missed (recall gap):  component/backend-integrations-base, ...
  recall:    72.2%
  ceiling (black-box / api-kind only): 13/18 = 72.2%  [5 symbols-kind target(s) require unit tests]
  extraneous claims (exist, not recommended): component/unrelated-module
```

The ceiling line appears only when the gap contains symbols-kind targets — components with no HTTP surface whose public functions can only be reached by unit tests importing them directly. A score at the ceiling is at the achievable maximum for a black-box test; closing the remaining recall gap requires switching to unit tests that import internal symbols.

**Score formula:** `((precision + recall) / 2) × 100 × existence_penalty`
- Precision: fraction of claimed slugs that are citable recommendations
- Recall: fraction of citable recommendations that were claimed
- Existence penalty: proportional reduction for any nonexistent-slug claims

**Exit codes:**
- `0` — header found, all claimed slugs exist (imperfect precision/recall is informational)
- `1` — header missing OR any claimed slug doesn't exist in the knowledge base

See [CLAUDE.md](../CLAUDE.md) for the `kc-covers:` header format and slug-sourcing rules. The scoring granularity decision (component/API level, not sub-component) is recorded in [ADR-012](decisions/ADR-012-defer-verification-requirement-entity.md).

---

### `kc validate-okf`

Check the emitted wiki bundle against OKF conformance rules. **Never compiles** — reads whatever the last `kc compile` wrote to the wiki output directory, same posture as `kc validate-test`.

```bash
kc validate-okf [--dir <path>]
```

| Option | Required | Description |
|--------|----------|--------------|
| `--dir` | no | Repository directory whose `kc.toml` locates the wiki output (default: `.`). |

```bash
kc validate-okf --dir /path/to/my-service
```

Example output (conformant):
```
okf spec version: 0.2
files checked:    247

CONFORMANT — kc-wiki satisfies OKF v0.2
```

Example output (issues found):
```
okf spec version: 0.2
files checked:    247

2 conformance issue(s):
  [missing-type] component/legacy-module.md: frontmatter has no non-empty 'type' field (SPEC.md §11 rule 2)
  [index.md-frontmatter] index.md: index.md may carry only 'okf_version' in frontmatter, found: ['title'] (SPEC.md §8)

NOT CONFORMANT
```

Checks (per [SPEC.md §11](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)): every non-reserved `.md` file has parseable frontmatter with a non-empty `type`; `index.md` carries no frontmatter beyond an optional `okf_version`; `log.md` carries no frontmatter at all. See [docs/okf-conformance.md](okf-conformance.md) for the full spec-to-emitter mapping and [ADR-013](decisions/ADR-013-open-source-okf-conformance.md) for the version-tracking/migration design.

**Exit codes:**
- `0` — bundle is conformant
- `1` — one or more conformance issues found

---

### `kc serve`

Start a read-only MCP server over the compiled knowledge base (stdio transport). **Never compiles** — state updates come from CI-triggered `kc compile`.

```bash
kc serve [--dir <path>]
```

Requires: `pip install 'open-knowledge-compiler[serve]'`

```bash
kc serve                              # serve the repo in the current directory
kc serve --dir /path/to/repo
```

One `kc serve` process per repo. To serve multiple repos, run multiple processes.

#### MCP Tools exposed by `kc serve`

| Tool | Description |
|------|-------------|
| `search_knowledge(query, entity_type?, limit?)` | Hybrid keyword+semantic search (falls back to keyword-only without embeddings). `entity_type` filters to one of: `component`, `api`, `business_rule`, `feature`, `risk`, `test_coverage`, `pull_request`, `project`, `jira_story`, `user_journey`, `state_model`. |
| `get_entity(slug, max_neighbors?)` | Full detail for one entity: payload, source anchors, relationships, provenance. `relationships` is capped at `max_neighbors` (default 50, dogfood-review finding — hub entities could otherwise return an unbounded edge set); `relationship_count`/`relationships_truncated` report the true total. Resolves cross-repo dependencies via `kc.toml [dependencies]`. |
| `impact_plan(slug)` | One-hop impact analysis for a changed entity: what's affected in this repo, which affected components have test-coverage gaps, and what it reaches across repos. |
| `test_plan(slug)` | Everything `impact_plan` returns, plus concrete test targets (APIs or symbols) for each coverage gap. Each `api`/`symbols` recommendation now inlines a `context` field (governing business rules, features, and risks linked to that component — item 1/6 of the QA-agent-grounding backlog, no extra round-trip needed). Also returns `stale_retest` recommendations (a test exists but the component changed since the test was last touched — ADR-018), `low_mutation_kill` recommendations (declared coverage exists but the mutation-kill rate is ≤40%, ADR-012's named trigger, carries a `mutation_kill_rate` field), `journey` recommendations (every step of a `kc.toml`-declared `[[journeys]]` end-to-end journey is individually covered, but no single test proves the whole chain — ADR-017), `transition_gap` recommendations (the component has a compiled `state_model` — a lifecycle with states and structurally-inferred transitions, ADR-023 — listing each known transition with a `confidence` field; a review surface, not a per-edge covered/uncovered verdict, since that needs semantic test-body analysis out of V1 scope), and `low_escaped_defect_trust` recommendations (declared coverage exists, but bug-fix PR history says it has repeatedly failed to prevent a regression — ADR-020's outcome-based signal, carries `escaped_defect_trust_score`/`escaped_defect_fix_count`, withheld below a minimum sample size; informational only, per the ADR's gate-later stance). Use to drive test generation before writing tests. |
| `resolve_dependency(coordinate)` | Resolve an external import/package coordinate to another repo compiled into the same database, via `kc.toml [dependencies]` (query-time only — [ADR-011](../docs/decisions/ADR-011-cross-repo-dependency-resolution.md)). |
| `list_entities(entity_type, limit?)` | List all entities of one type. |
| `recent_changes(runs?)` | Knowledge deltas of the N most recent compiles: what was added, changed, removed, or moved. |
| `which_pr_introduced(slug)` | Which PR (or bootstrap compile) first added this entity. |
| `coverage_for(component_slug)` | Which tests cover this component. Each test now also reports `stale` (true if the component changed more recently than the test's own `last_compile_run_id` — ADR-018, zero schema change, computed from the existing entity envelope) and, when a `[mutation]` scores file is configured, `mutation_kill_rate`/`low_mutation_kill` (ADR-012's ≤40% trigger). Also reports `escaped_defect_fix_count`/`escaped_defect_trust_score`/`low_escaped_defect_trust` (ADR-020): whether bug-fix PRs landing on this component found it already "covered" — an outcome-based signal, populated only on PR-triggered compiles, `trust_score` withheld (`null`) below a minimum fix-count sample. |
| `linked_context(component_slug)` | The business rules, features, and risks that govern/are-implemented-by/affect this component — the same context `test_plan` inlines, exposed standalone for direct lookups. |
| `journey_coverage(journey_slug)` | Whether a declared `[[journeys]]` end-to-end journey is covered by a single test that exercises every step's component, versus each step only being covered individually. Also returns `status` (`complete`\|`partial`\|`invalid`) and `unresolved_steps` — a step that didn't resolve at compile time is dropped, not failed, so `covered_end_to_end: true` on a non-`complete` journey only proves the resolved portion of the declared chain (dogfood-review finding). |
| `knowledge_stats()` | Entity counts by type and last successful compile metadata, plus `knowledge_completeness` (`files_seen`/`files_parsed`/`files_failed`/`parse_coverage`/`failed_files` — null on compiles predating this signal, dogfood-review finding). |

---

## Typical Workflows

### First-time setup

```bash
cd /path/to/my-repo
kc init --slug my-service --forge-ref github.com/acme/my-service
kc compile --full
```

### Steady-state CI (after each merge)

```bash
kc reconcile                          # catches all PRs since last compile
```

### Check for drift

```bash
kc verify
```

### Serve knowledge to an AI agent

```bash
kc serve                              # add to MCP config in Claude Code / agent
```

### Test generation workflow

```bash
# 1. Ask what tests are needed
#    (via MCP: test_plan("component/billing-rules"))
#    Each recommendation now carries inline governing-rule/feature/risk
#    context (no separate get_entity round-trip needed), and the plan
#    surfaces stale-retest, low-mutation-kill, and journey-gap
#    recommendations alongside plain coverage gaps.

# 2. Write the test with kc-covers: header
#    (external coding agent)

# 3. Validate the test header before committing
kc validate-test tests/test_billing.py --for-entity component/billing-rules
```

---

## Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | `kc compile --pr`, `kc reconcile` | GitHub API token for PR metadata. |
| `ANTHROPIC_API_KEY` | `kc compile` (LLM stage) | Required when `[llm] provider = "anthropic"` in `kc.toml`. |
| `OPENAI_API_KEY` | `kc compile` (LLM + embeddings) | Required when provider is `openai`. |
| `OPENAI_AZURE_ENDPOINT` | `kc compile` (LLM + embeddings) | Required when provider is `azure-openai`. Azure OpenAI resource endpoint URL. |
| `OPENAI_AZURE_API_KEY` | `kc compile` (LLM + embeddings) | Required when provider is `azure-openai`. |
| `OPENAI_AZURE_DEPLOYMENT` | `kc compile` (LLM stage) | Required when `[llm] provider = "azure-openai"`. Deployment name for the chat model. |
| `OPENAI_AZURE_EMBEDDING_DEPLOYMENT` | `kc compile` (embeddings) | Required when `[embeddings] provider = "azure-openai"`. Deployment name for the embedding model. |
| `CF_ACCOUNT_ID` | `kc compile` (LLM stage) | Required when `[llm] provider = "cloudflare"`. |
| `CF_API_TOKEN` | `kc compile` (LLM stage) | Required when `[llm] provider = "cloudflare"`. |
| `KC_DATABASE_URL` | all commands | Postgres connection string. Default: `postgresql+psycopg://kc:kc@localhost:5432/kc_wiki`. |
| `KC_DB_CONNECT_TIMEOUT_SECONDS` | all commands | How long to wait for Postgres before failing (default: 120s). |
| `JIRA_BASE_URL` | `kc compile` (Jira collector) | Atlassian Cloud base URL, e.g. `https://your-org.atlassian.net`. Required when `[jira] source = "rest"`. |
| `JIRA_EMAIL` | `kc compile` (Jira collector) | Atlassian account email for API token auth. |
| `JIRA_API_TOKEN` | `kc compile` (Jira collector) | Atlassian API token (generate at account settings → Security → API tokens). |

For provider setup guides and the Jira collector how-to, see [docs/integrations.md](integrations.md).

---

## kc.toml — per-repo configuration

Written by `kc init`. Sections:

```toml
[repository]
slug = "my-service"
forge_ref = "github.com/acme/my-service"
default_branch = "main"

[wiki]
# Local publication directory; the branch publisher ships it when enabled (ADR-010).
output_dir = "kc-wiki"

[publisher]
# Ship the wiki to a dedicated branch (ADR-010: knowledge/wiki). Explicit opt-in.
enabled = false
branch = "knowledge/wiki"
remote = "origin"
push = true

[llm]
enabled = false
provider = "anthropic"          # anthropic | openai | azure-openai | cloudflare
max_calls_per_run = 200
# model = "claude-opus-4-8"    # per-provider default applies when omitted

[embeddings]
enabled = false
provider = "openai"             # openai | azure-openai
# model = "text-embedding-3-small"

[jira]
enabled = false
# source = "rest" | "file"      # default "rest" (ADR-021); "file" reads
                                 # cache_file instead of the live API
# cache_file = "jira-cache.json"  # resolved relative to this repo's directory

[dependencies]
# Cross-repo: map import prefixes to other compiled repo slugs (ADR-011)
# repoB = "repoB"

[mutation]
# Attach mutation-kill-rate stats (from a CI job's already-produced JSON
# summary) to matching Component entities. KC never executes the target
# repo's tests itself. Explicit opt-in (ADR-012's named trigger, surfaced in
# test_plan/coverage_for).
enabled = false
scores_file = "mutation-scores.json"

# Deterministic-only V1 (ADR-017): declare an ordered, end-to-end step list
# of already-compiled entity slugs. No [[journeys]] table means no journeys
# are compiled — fully optional, additive.
#
# Inline declaration:
# [[journeys]]
# name = "Apply coupon at checkout"
# steps = ["api/post-cart-add-item", "api/post-cart-apply-coupon",
#          "api/post-checkout-submit"]
#
# External file(s) — resolved relative to this repo directory.
# Useful when another repo (e.g. a QA repo) owns the journey definitions.
# Inline [[journeys]] and journeys_file entries are merged; both may coexist.
# journeys_file = "../qa-repo/checkout-journeys.toml"          # single
# journeys_file = ["../qa-repo/checkout-journeys.toml",        # multiple
#                  "../qa-repo/admin-journeys.toml"]
# The referenced file(s) use the same [[journeys]] syntax as inline entries.
# A missing or unreadable file fails loudly at compile time.
```
