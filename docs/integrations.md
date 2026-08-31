# Integrations: LLM Providers and Jira Collector

Knowledge Compiler's optional integrations are all **explicit opt-in** — no external service is contacted unless the relevant section appears in `kc.toml`. Credentials always come from the environment (never from `kc.toml`).

---

## LLM Provider Integrations

The semantic layer uses one thin interface:

```
complete(prompt, schema) → validated JSON
```

Swapping providers is a `kc.toml` change plus an env-var swap — no code change. All four providers use this same interface; the extractor never imports a vendor SDK directly (ADR-008).

### When to enable

Without `[llm]`, `kc compile` is fully functional (deterministic AST extraction only). Add `[llm]` to unlock:
- Business rule / feature / risk extraction from source files
- Context-aware relationship inference

A compile that fails to reach the LLM falls back to **degraded mode** (deterministic facts only; labelled `degraded=true` in compile metadata) rather than aborting.

### Common kc.toml shape

```toml
[llm]
enabled = true
provider = "anthropic"   # or openai / azure-openai / cloudflare
max_calls_per_run = 200  # budget guard; omit to use all changed files
# model = "..."          # optional override; default per provider below
```

```toml
[embeddings]
# Semantic vectors for search. Without this, search falls back to Postgres FTS.
# Only openai and azure-openai are supported here.
enabled = true
provider = "openai"      # or azure-openai
```

---

### Anthropic

**Default provider.** Uses `claude-opus-4-8` structured outputs.

**Install:**
```bash
pip install 'open-knowledge-compiler[llm]'
```

**Env:**
```bash
ANTHROPIC_API_KEY=sk-ant-...
```

**kc.toml:**
```toml
[llm]
enabled = true
provider = "anthropic"
# model = "claude-opus-4-8"   # default; override with any claude model ID
```

No `[embeddings]` support — pair with OpenAI or Azure OpenAI for semantic search.

---

### OpenAI

**Install:**
```bash
pip install 'open-knowledge-compiler[llm-openai]'
```

**Env:**
```bash
OPENAI_API_KEY=sk-...
```

**kc.toml:**
```toml
[llm]
enabled = true
provider = "openai"
# model = "gpt-4o"   # default; gpt-4o-mini works too at lower cost

[embeddings]
enabled = true
provider = "openai"
# model = "text-embedding-3-small"   # default
```

The `[llm]` and `[embeddings]` sections share the same `OPENAI_API_KEY`.

---

### Azure OpenAI

Azure requires **two separate deployments**: one chat model for extraction, one embedding model for search. They share the same endpoint and API key but differ in deployment name.

**Install:**
```bash
pip install 'open-knowledge-compiler[llm-openai]'
```

**Azure portal steps:**
1. Deploy a chat model (e.g. `gpt-4o-mini`) — note the deployment name.
2. Deploy an embedding model (e.g. `text-embedding-3-small`) — note the deployment name.

**Env:**
```bash
OPENAI_AZURE_ENDPOINT=https://<your-resource>.openai.azure.com
OPENAI_AZURE_API_KEY=<your-key>
OPENAI_AZURE_DEPLOYMENT=gpt-4o-mini              # chat model — used by [llm]
OPENAI_AZURE_EMBEDDING_DEPLOYMENT=text-embedding-3-small   # embedding model — used by [embeddings]
# KC_AZURE_OPENAI_API_VERSION=2024-10-21         # optional; this is the default
```

`AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` are accepted as fallbacks if you already use the Azure SDK's canonical names.

**kc.toml:**
```toml
[llm]
enabled = true
provider = "azure-openai"
# model = "gpt-4o-mini"   # omit to read OPENAI_AZURE_DEPLOYMENT from env

[embeddings]
enabled = true
provider = "azure-openai"
```

> **Why two deployments?** A chat model produces JSON; an embedding model produces a fixed-size vector. Azure treats them as architecturally distinct deployments even under the same resource. `text-embedding-3-small` (1536 dims) is the cost-effective default; use `text-embedding-3-large` (3072 dims) only if you have a very large corpus requiring finer semantic discrimination.

---

### Cloudflare Workers AI

Uses Cloudflare's OpenAI-compatible endpoint. Schema enforcement is done via tool/function calling (rather than `response_format: json_schema`), which is the confirmed-supported mechanism on Workers AI.

**Install:**
```bash
pip install 'open-knowledge-compiler[llm-openai]'
```

**Env:**
```bash
CF_ACCOUNT_ID=<your-account-id>
CF_API_TOKEN=<your-api-token>
```

**kc.toml:**
```toml
[llm]
enabled = true
provider = "cloudflare"
# model = "@cf/google/gemma-4-26b-a4b-it"   # default; any Workers AI chat model
```

No `[embeddings]` support — pair with OpenAI or Azure OpenAI if semantic search is needed.

---

### Choosing a provider

| Provider | `[llm]` extraction | `[embeddings]` search | Notes |
|---|---|---|---|
| `anthropic` | ✓ | — | Best structured output quality; recommended default |
| `openai` | ✓ | ✓ | One key covers both; simplest setup |
| `azure-openai` | ✓ | ✓ | Enterprise/compliance use; two deployments required |
| `cloudflare` | ✓ | — | Edge/serverless; schema via tool call |

---

### LLM cache

Every provider call is content-addressed in Postgres (`llm_cache` table). Keyed by `hash(template_version + model_id + input_content)` — re-compiling an unchanged file never hits the API. The cache is shared across repos and survives restarts.

`[llm] max_calls_per_run` is a budget guard per compile run; set it to limit cost during initial rollout.

---

## Jira Collector

Fetches Jira issues linked from merged PRs' titles or bodies (e.g. `DCA-1234`), mints `jira_story` entities, and creates `motivates` edges to the PRs that closed them.

**Scope:** not full Jira ingestion — only issue keys that appear in a PR's title or body are fetched. This keeps the collector strictly PR-scoped, consistent with how forge PR facts work.

### What gets compiled

For each linked issue key found in a merged PR:
- A `jira_story` entity is created (slug: `jira_story/DCA-1234`)
- Payload includes: key, summary, status, description (plain text extracted from Atlassian Document Format), issue type
- A `motivates` edge is added from `jira_story/DCA-1234` to the `pull_request/<pr-number>` entity

### Backend: `rest` (CI-compatible)

Calls the live Atlassian Cloud REST API. The only backend usable from an unattended CI compile (ADR-002) — it requires only env vars, no interactive session.

**Env:**
```bash
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_EMAIL=ci-bot@your-org.com
JIRA_API_TOKEN=<api-token>
```

The API token is per-user. Generate one at: Atlassian account settings → Security → API tokens.

**kc.toml:**
```toml
[jira]
enabled = true
# source = "rest"   # default; no need to specify explicitly
```

**Full example with explicit source:**
```toml
[jira]
enabled = true
source = "rest"
```

### Backend: `file` (interactive agent workflow)

For interactive compiles where an AI agent already has its own Jira access (e.g. via an Atlassian MCP connector) but no API token is configured. The agent pre-fetches the relevant issues to a JSON cache file; KC reads the cache without touching Jira directly (ADR-021).

This backend is **interactive-only** — never use it for a CI-triggered compile, since there is no non-interactive credential behind the access pattern.

**Cache file format:**
```json
{
  "AOC-1234": {
    "summary": "Add discount cap for enterprise tier",
    "status": "Done",
    "description": "When an enterprise customer ...",
    "issue_type": "Story"
  },
  "AOC-1235": {
    "summary": "Fix rounding on invoice totals",
    "status": "In Progress",
    "description": "",
    "issue_type": "Bug"
  }
}
```

All fields except the key itself are optional (they default to empty string). A key present in a PR but absent from the cache is silently omitted — the same behaviour as the REST backend when a key genuinely doesn't exist in Jira.

**kc.toml:**
```toml
[jira]
enabled = true
source = "file"
cache_file = "jira-cache.json"   # resolved relative to the repo directory
```

`cache_file` defaults to `jira-cache.json` if omitted.

**Agent workflow example (using Atlassian MCP):**
1. Agent calls Atlassian MCP to fetch PR-linked issue keys and their fields.
2. Agent writes them to `jira-cache.json` in the repo directory.
3. Agent runs `kc compile` — KC reads the cache, mints entities, no API token needed.

### Error handling

- A key that doesn't exist in Jira (REST 404, or absent from cache file) is **silently omitted** — typos in PR titles are data, not an outage.
- A source that can't be reached at all (bad credentials, network, unreadable cache file) **fails loudly** (ADR-007 fail-loud posture) — the compile does not silently skip Jira and produce an incomplete knowledge base.
- A bad `source` value (not `"rest"` or `"file"`) fails immediately at startup with a clear error, rather than silently falling back.

### MCP queries

Once compiled, Jira stories are queryable through the MCP server like any other entity:

```
search_knowledge("DCA-1234")
get_entity("jira_story/DCA-1234")
```

The `motivates` edge is visible in `get_entity` relationships and traversed by `impact_plan`.
