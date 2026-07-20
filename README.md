# Knowledge Compiler

Compiles software engineering artifacts (Git repos, PRs, Jira, docs, OpenAPI, tests) into a structured, persistent knowledge base — queryable by humans (living wiki) and AI agents (MCP). Not another RAG system: raw artifacts go in once, compiled knowledge stays synchronized through incremental, PR-triggered compilation.

**Status:** Architecture v1.0 frozen · V1 pipeline + milestone 2 implemented (deterministic compiler for Python + TypeScript, incremental compilation with reconcile + verify, OKF wiki with loop-safe branch publishing, opt-in LLM semantic layer, opt-in embeddings with hybrid retrieval, read-only MCP server). Dogfooded on two real team repos (`kc verify`-clean); next: enable enrichment on them and wire up incremental `--pr` compilation in CI.

- Design: [docs/vision.md](docs/vision.md) · [docs/architecture.md](docs/architecture.md) · [docs/decisions/index.md](docs/decisions/index.md)
- Contracts: [docs/ir.md](docs/ir.md) · [docs/data-model.md](docs/data-model.md) · [docs/pipeline.md](docs/pipeline.md) · [docs/normalize.md](docs/normalize.md) · [docs/retrieval.md](docs/retrieval.md)

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -e .[dev]
docker compose up -d                               # Postgres 16 + pgvector

kc init --slug my-repo --forge-ref github.com/org/my-repo --default-branch main
kc compile --full                                  # bootstrap: repo -> knowledge base + wiki
kc inspect                                         # entity/relationship counts, last delta
kc verify                                          # incremental state ≡ full compile?
```

No API keys required — the deterministic compiler produces components, APIs, dependencies, test coverage, and a browsable wiki (`kc-wiki/`) on its own.

## Run modes

| Command | What it does |
|---|---|
| `kc compile --full` | Bootstrap / escape-hatch full compilation |
| `kc compile --pr N` | Incremental: reconciles missed merged PRs first, in merge order, exactly once |
| `kc reconcile` | Catch up on merged PRs since the watermark (needs `KC_GITHUB_TOKEN` or `GITHUB_TOKEN`) |
| `kc verify` | Zero-write shadow compile; reports drift between incremental state and a full compile |
| `kc inspect` | The debugging surface: counts by type + last delta |
| `kc compile --no-llm` | Deterministic pass only; run marked degraded; semantic entities are never removed |
| `kc serve` | Read-only MCP server (stdio) over the knowledge base — never compiles (`pip install -e .[serve]`) |

## Configuration

All external configuration lives in env vars and `kc.toml` (written by `kc init`) — nothing is hardcoded:

- `KC_DATABASE_URL` — Postgres URL (default matches `docker-compose.yml`)
- `KC_GITHUB_TOKEN` / `GITHUB_TOKEN` — forge API for `--pr`/`reconcile`; `KC_GITHUB_API` for GHE
- `kc.toml [wiki]` — local publication directory
- `kc.toml [publisher]` — opt-in loop-safe publishing to a `knowledge/wiki` branch (ADR-010)
- `kc.toml [llm]` — opt-in semantic layer (ADR-008): business rules, features, risks
- `kc.toml [embeddings]` — opt-in semantic search vectors (ADR-005); without them `kc serve` search is keyword-only (fully functional)

## Semantic layer (optional)

```toml
[llm]
enabled = true
provider = "openai"        # "anthropic" (default) | "openai" | "azure-openai"
# model = "gpt-4o"         # per-provider default applies when omitted
max_calls_per_run = 200    # budget cap: exceeding fails the run resumably (cache keeps paid work)
```

```bash
pip install -e .[llm-openai]      # or .[llm] for Anthropic
# credentials come from the environment, never from config files:
#   OPENAI_API_KEY (openai) / ANTHROPIC_API_KEY (anthropic)
#   OPENAI_AZURE_ENDPOINT / OPENAI_AZURE_API_KEY / OPENAI_AZURE_DEPLOYMENT (azure-openai)
#   OPENAI_AZURE_DEPLOYMENT names a chat/completion deployment (e.g. gpt-4o-mini) —
#   a *separate* deployment from embeddings (see below); see llm-usage.md for why
#   Azure OpenAI needs two distinct deployments even on one resource/endpoint.
kc compile --full
```

LLM outputs are schema-validated before persistence, cached content-addressed in Postgres (unchanged files cost zero on recompile), and every semantic fact carries provenance (model, template version, anchors into source). Provider outage degrades the compile gracefully — the deterministic knowledge base is never blocked on an API. Test files are excluded from semantic extraction by default (`[llm] include_tests = true` to override).

## Retrieval and MCP server (optional)

```toml
[embeddings]
enabled = true
provider = "openai"        # "openai" | "azure-openai"
# model = "text-embedding-3-small"
```

```bash
pip install -e .[serve]           # mcp SDK for `kc serve`
# azure-openai credentials: OPENAI_AZURE_ENDPOINT / OPENAI_AZURE_API_KEY (shared with [llm]) +
#   OPENAI_AZURE_EMBEDDING_DEPLOYMENT — a dedicated embedding-model deployment
#   (e.g. text-embedding-3-small), distinct from [llm]'s OPENAI_AZURE_DEPLOYMENT
#   chat deployment. See llm-usage.md for the full two-deployment rationale.
kc compile --full                 # embeds dirty entities as a post-persist stage
kc serve --dir .                  # read-only MCP server, stdio transport
```

Without `[embeddings]`, `kc serve` still works — search runs keyword-only (Postgres FTS) and is fully functional; enabling embeddings adds semantic matching, fused with keyword results via reciprocal-rank fusion. `kc serve` **never compiles** — it only reads whatever the last `kc compile` produced. See [docs/retrieval.md](docs/retrieval.md) for the retrieval design and the full MCP tool list (`search_knowledge`, `get_entity`, `recent_changes`, `which_pr_introduced`, `coverage_for`, …).

Register with Claude Code: `claude mcp add kc -- kc serve --dir <repo>`.

## Development

```bash
pytest                             # full suite (integration tests skip without Postgres)
pytest tests/test_normalize.py     # the identity-cascade suite
```
