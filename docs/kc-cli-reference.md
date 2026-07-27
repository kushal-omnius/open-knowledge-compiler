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
| `--slug` | yes | — | Unique identifier for this repo in the knowledge base (e.g. `frida`). |
| `--forge-ref` | yes | — | Canonical forge reference (e.g. `github.com/org/repo`). |
| `--default-branch` | no | `main` | Branch the compiler tracks. |
| `--dir` | no | `.` | Directory to write `kc.toml` into. |

**Run once per repo, before any `kc compile`.**

```bash
kc init --slug my-service --forge-ref github.com/acme/my-service --dir /path/to/repo
```

---

### `kc compile`

Compile the repository. Exactly one of `--full` or `--pr` is required.

```bash
kc compile (--full | --pr <number>) [--dir <path>] [--no-llm]
```

| Option | Description |
|--------|-------------|
| `--full` | Bootstrap / escape-hatch. Always re-runs the full pipeline against HEAD. No up-to-date check — use this for first-time compilation or to force a clean pass. LLM and embedding caches make repeated runs cheap for unchanged files. |
| `--pr <N>` | Incremental compilation of one merged PR. Idempotent: skipped if PR N already has a `succeeded` row in `compile_runs`. |
| `--no-llm` | Skip LLM extraction — deterministic pass only. Run is marked `degraded`. LLM-derived entities (features, business rules, risks) are never removed by degraded runs. |
| `--dir` | Repository directory containing `kc.toml` (default: `.`). |

```bash
kc compile --full                     # first compile or forced full pass
kc compile --full --no-llm            # fast re-index without LLM calls
kc compile --pr 142                   # compile one PR incrementally
```

**Progress** is streamed to stderr (`[llm]` and `[embed]` lines with counts) so long-running stages are no longer silent.

---

### `kc reconcile`

Catch up on all merged PRs missed since the last successful compile. Runs `compile_pr` on each one in merge order, skipping any already-succeeded PR (idempotent). Uses a watermark (`max(merged_at)` over succeeded runs) so it never re-examines old history.

```bash
kc reconcile [--dir <path>]
```

**Requires a `GITHUB_TOKEN` env var** (or `~/.config/gh/hosts.yml` via `gh auth login`) — it queries the GitHub API for PR metadata.

```bash
kc reconcile                          # catch up after several merged PRs
kc reconcile --dir /path/to/repo
```

Output: one summary line per PR compiled, or `"up to date — no merged PRs after the watermark"`.

**When to use `reconcile` vs `--full`:**
- Normal steady-state (PRs merged to main): `kc reconcile`
- First compile, direct pushes to main, or suspected drift: `kc compile --full`

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
repository: frida
entities: 2765
  component: 312
  feature: 517
  business_rule: 50
  risk: 245
  api: 98
  test_coverage: 201
  pull_request: 1342
relationships: 8410
last compile: run 47 @ a3f9c1d20b12 — delta add:14  change:3  remove:1
```

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

Example output:
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

**Score formula:** `((precision + recall) / 2) × 100 × existence_penalty`
- Precision: fraction of claimed slugs that are citable recommendations
- Recall: fraction of citable recommendations that were claimed
- Existence penalty: proportional reduction for any nonexistent-slug claims

**Exit codes:**
- `0` — header found, all claimed slugs exist (imperfect precision/recall is informational)
- `1` — header missing OR any claimed slug doesn't exist in the knowledge base

See [CLAUDE.md](../CLAUDE.md) for the `kc-covers:` header format and slug-sourcing rules.

---

### `kc serve`

Start a read-only MCP server over the compiled knowledge base (stdio transport). **Never compiles** — state updates come from CI-triggered `kc compile`.

```bash
kc serve [--dir <path>]
```

Requires: `pip install 'knowledge-compiler[serve]'`

```bash
kc serve                              # serve the repo in the current directory
kc serve --dir /path/to/repo
```

One `kc serve` process per repo. To serve multiple repos, run multiple processes.

#### MCP Tools exposed by `kc serve`

| Tool | Description |
|------|-------------|
| `search_knowledge(query, entity_type?, limit?)` | Hybrid keyword+semantic search (falls back to keyword-only without embeddings). `entity_type` filters to one of: `component`, `api`, `business_rule`, `feature`, `risk`, `test_coverage`, `pull_request`, `project`. |
| `get_entity(slug)` | Full detail for one entity: payload, source anchors, relationships, provenance. Resolves cross-repo dependencies via `kc.toml [dependencies]`. |
| `impact_plan(slug)` | One-hop impact analysis for a changed entity: what's affected in this repo, which affected components have test-coverage gaps, and what it reaches across repos. |
| `test_plan(slug)` | Everything `impact_plan` returns, plus concrete test targets (APIs or symbols) for each coverage gap. Use to drive test generation before writing tests. |
| `resolve_dependency(coordinate)` | Resolve an external import/package coordinate to another repo compiled into the same database, via `kc.toml [dependencies]`. |
| `list_entities(entity_type, limit?)` | List all entities of one type. |
| `recent_changes(runs?)` | Knowledge deltas of the N most recent compiles: what was added, changed, removed, or moved. |
| `which_pr_introduced(slug)` | Which PR (or bootstrap compile) first added this entity. |
| `coverage_for(component_slug)` | Which tests cover this component. |
| `knowledge_stats()` | Entity counts by type and last successful compile metadata. |

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
| `DATABASE_URL` | all commands | Postgres connection string. Default: `postgresql+psycopg://kc:kc@localhost:5432/kc_wiki`. |

---

## kc.toml — per-repo configuration

Written by `kc init`. Sections:

```toml
[repository]
slug = "my-service"
forge_ref = "github.com/acme/my-service"
default_branch = "main"

[llm]
enabled = false
provider = "anthropic"          # anthropic | openai | azure-openai | cloudflare

[embeddings]
enabled = false
provider = "openai"             # openai | azure-openai

[wiki]
enabled = true
output_dir = "kc-wiki"
publish_branch = "kc-wiki"

[jira]
enabled = false

[dependencies]
# Cross-repo: map import prefixes to other compiled repo slugs
# omnius_llmlib = "omnius-llmlib"
```
