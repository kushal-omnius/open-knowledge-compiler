# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not open a public issue**. Instead, use GitHub's private vulnerability reporting (Security tab → "Report a vulnerability") on this repository, or contact the maintainers directly through a private channel.

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal repro repo/config is ideal)
- The `kc` version and provider configuration involved, if relevant

We'll acknowledge reports within a reasonable time and work with you on a fix before any public disclosure.

## Credential handling

Knowledge Compiler integrates with several external systems that require credentials. The project's design principle here is: **credentials live in environment variables only, never in `kc.toml` or any committed file.**

| Credential | Used for | Env var |
|---|---|---|
| Postgres connection | all commands | `KC_DATABASE_URL` |
| GitHub API token | `--pr`, `reconcile` | `KC_GITHUB_TOKEN` / `GITHUB_TOKEN` |
| Anthropic API key | LLM extraction | `ANTHROPIC_API_KEY` |
| OpenAI API key | LLM extraction, embeddings | `OPENAI_API_KEY` |
| Azure OpenAI credentials | LLM extraction, embeddings | `OPENAI_AZURE_ENDPOINT`, `OPENAI_AZURE_API_KEY`, `OPENAI_AZURE_DEPLOYMENT`, `OPENAI_AZURE_EMBEDDING_DEPLOYMENT` |
| Cloudflare credentials | LLM extraction | `CF_ACCOUNT_ID`, `CF_API_TOKEN` |
| Jira credentials | Jira collector | `JIRA_EMAIL`, `JIRA_API_TOKEN` |

`kc.toml` (written by `kc init`, checked into the analyzed repo) contains only non-secret configuration — provider *names*, feature flags, and output paths. If you ever see a real credential land in `kc.toml` or the emitted wiki, treat it as a bug and report it privately per the process above.

## Data handling notes

- **The LLM cache (`llm_cache` table) is content-addressed and repo-agnostic by design** ([ADR-008](docs/decisions/ADR-008-llm-abstraction-caching.md)) — it stores schema-validated model *outputs*, keyed by a hash of (prompt template version, model, input content), shared across every repo compiled into the same database. It does not store raw credentials, but it does store derived semantic content (feature descriptions, business rules, risks) extracted from your source code — treat the database as holding a copy of that derived content for as long as the cache retention policy keeps it.
- **The compiled knowledge base is only as private as your Postgres instance.** `kc serve` is read-only and has no authentication layer of its own — it assumes the MCP transport (stdio, one process per repo) and the database's own access controls are your security boundary. Don't expose `kc serve` over a network without your own authentication in front of it.
- **The published wiki branch** (`knowledge/wiki`, [ADR-010](docs/decisions/ADR-010-wiki-destination.md)) contains the same compiled knowledge as the database, rendered as Markdown. If your source repository is private, keep the wiki branch in the same private repository — the publisher does not change the repo's visibility.

## Supported versions

This project is pre-1.0 (see [CLAUDE.md](CLAUDE.md) for current status). Security fixes land on the latest release; there is no long-term-support branch yet.
