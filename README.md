# Knowledge Compiler

Compiles software engineering artifacts (Git repos, PRs, Jira, docs, OpenAPI, tests) into a structured, persistent knowledge base — queryable by humans (living wiki) and AI agents (MCP).

**Status:** Architecture v1.0 frozen; implementation in progress (deterministic compiler first — no API keys required to get a useful knowledge base).

- Design: [docs/vision.md](docs/vision.md) · [docs/architecture.md](docs/architecture.md) · [docs/decisions/index.md](docs/decisions/index.md)
- Contracts: [docs/ir.md](docs/ir.md) · [docs/data-model.md](docs/data-model.md) · [docs/pipeline.md](docs/pipeline.md) · [docs/normalize.md](docs/normalize.md)

## Development

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e .[dev]
docker compose up -d            # Postgres + pgvector
pytest                          # run tests
pytest tests/test_smoke.py -k hash   # run a single test
kc --help
```
