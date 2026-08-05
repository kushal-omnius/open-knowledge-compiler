# Contributing to Open Knowledge Compiler

Thanks for considering a contribution. This project compiles software engineering artifacts into a structured, OKF-conformant knowledge base — contributions that extend what gets compiled, how it's extracted, or where it's served are all welcome.

## Before you start

- Read [CLAUDE.md](CLAUDE.md) for the project's architecture, module layout, and conventions.
- Read [docs/decisions/index.md](docs/decisions/index.md) for the ADRs — architectural decisions are immutable once Accepted; changing one requires a superseding ADR, not a silent PR.
- Check [docs/vision.md](docs/vision.md) for the project's non-goals (V1 explicitly excludes distributed architecture, microservices, graph databases, multi-tenancy, and real-time indexing).

## Ways to contribute

### Plugins (the primary extension point)

Every compiler stage — collectors, extractors, storage backends, retrieval strategies — is a pluggable interface ([ADR-007](docs/decisions/ADR-007-plugin-architecture.md)). Plugins register via standard Python entry points and are activated only by explicit `kc.toml` configuration — installing a package never silently changes compilation output.

Interfaces are defined in `knowledge_compiler/interfaces.py` (stage Protocols). To add:

- **A collector** (new artifact source): implement the `Collector` protocol; see `collectors/git.py`, `collectors/forge.py`, `collectors/jira.py` for the pattern, and `FakeForge`/`FakeJira` for how they're tested without a real network.
- **A language analyzer**: implement the `LanguageAnalyzer` protocol; see `extractors/python_analyzer.py` and `extractors/typescript_analyzer.py` (both tree-sitter-based, in-process, no external runtime — [ADR-006](docs/decisions/ADR-006-language-analyzers.md)).
- **An LLM provider**: implement the provider interface in `llm/provider.py`; see the anthropic/openai/azure-openai/cloudflare implementations for the pattern ([ADR-008](docs/decisions/ADR-008-llm-abstraction-caching.md)).
- **A retrieval or embedding provider**: see `retrieval/` and `llm/embeddings.py` ([ADR-005](docs/decisions/ADR-005-embeddings-pgvector.md)).

Built-in plugins have no privileged path — they register through the exact same entry-point mechanism a third-party plugin would use.

### Bug reports and fixes

Open an issue with: the `kc` command you ran, the `kc.toml` you're using (redact secrets), and the actual vs. expected output. `kc verify` and `kc inspect` are usually the fastest way to characterize a compiled-state bug before filing.

### Documentation

Living specs (`docs/ir.md`, `docs/data-model.md`, `docs/pipeline.md`, `docs/normalize.md`) accept additive clarifications discovered during real use. If you hit something the docs don't explain, a PR that adds the clarification (not a rewrite) is welcome.

## Development setup

```bash
git clone https://github.com/kushal-omnius/open-knowledge-compiler.git
cd open-knowledge-compiler
python -m venv .venv && .venv\Scripts\activate   # Windows; source .venv/bin/activate elsewhere
pip install -e .[all]
docker compose up -d          # Postgres 16 + pgvector
pytest                        # integration tests skip loudly if Postgres is down
```

## Testing conventions

No mocks for the core pipeline — tests use real git repos, real Postgres, real tree-sitter parsing. LLM tests use `FakeLLMProvider`; embedding tests use `FakeEmbedder`. If you're adding a test that touches the LLM cache or embeddings table, give it a unique `model_id` — both caches are shared/repo-agnostic by design, so tests asserting call counts need isolation.

Integration tests mint throwaway repos with unique slugs against the shared test database; `tests/conftest.py` cleans these up automatically at the end of the test session — you don't need to clean up manually, but keep new throwaway-repo slugs prefixed consistently with the existing convention (see `_TEST_SLUG_RE` in that file) so they get swept.

## Pull requests

- Keep PRs scoped to one change. A bug fix doesn't need surrounding refactors.
- Run `pytest` locally before opening — CI runs the same suite.
- If your change touches an ADR's stated invariant, open the ADR discussion first (as an issue) rather than sending a PR that silently reverses a documented decision.
- Follow the existing code style: no comments explaining *what* code does (names should do that); comments only for non-obvious *why*.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you're expected to uphold it.

## License

By contributing, you agree your contributions are licensed under the project's [Apache License 2.0](LICENSE).
