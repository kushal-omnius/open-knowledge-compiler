# Architecture Decision Records

This directory records the significant architectural decisions of the Knowledge Compiler, per the vision's design principle: *decisions are recorded*.

## Process

1. Copy [adr-template.md](adr-template.md) to `ADR-XXX-<short-slug>.md` (three-digit number, next in sequence).
2. Write the ADR with status **Proposed**. The Considered Options section must contain genuinely distinct options with honest pros/cons — an ADR that documents only the winner is a changelog entry, not a decision record.
3. Discussion happens in the PR that adds the ADR. When merged with agreement, set status to **Accepted**.
4. ADRs are immutable once Accepted. To change a decision, write a new ADR that supersedes the old one and set the old ADR's status to **Superseded** with a link forward.

## Status lifecycle

`Proposed → Accepted → Superseded`

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-postgresql.md) | PostgreSQL as the single knowledge store | Accepted |
| [ADR-002](ADR-002-ci-trigger.md) | CI-invoked CLI trigger | Accepted |
| [ADR-003](ADR-003-current-state-delta-log.md) | Current state + append-only delta log | Accepted |
| [ADR-004](ADR-004-entity-identity.md) | Stable entity identity | Accepted |
| [ADR-005](ADR-005-embeddings-pgvector.md) | Embedding storage in pgvector | Accepted |
| [ADR-006](ADR-006-language-analyzers.md) | tree-sitter as the language-analyzer backbone | Accepted |
| [ADR-007](ADR-007-plugin-architecture.md) | Plugin discovery via entry points, activation via config | Accepted |
| [ADR-008](ADR-008-llm-abstraction-caching.md) | LLM provider abstraction + content-addressed cache | Accepted |
| [ADR-009](ADR-009-two-layer-ir.md) | Two-layer IR: Facts and Knowledge | Accepted |
| [ADR-010](ADR-010-wiki-destination.md) | Wiki destination: dedicated branch in the compiled repo | Accepted |
| [ADR-011](ADR-011-cross-repo-dependency-resolution.md) | Cross-repo dependency resolution: query-time config map | Accepted |
| [ADR-012](ADR-012-defer-verification-requirement-entity.md) | Defer VerificationRequirement entity; mutation-kill is the V1 signal | Accepted |

See [index.md](index.md) for summaries, dependencies, the dependency graph, and the list of unresolved decisions deferred to future design documents.
