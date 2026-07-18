# Vision: Knowledge Compiler

> Status: Living document. Supersedes the exploratory `INITIAL-Brainstorm.md` as the project's committed direction.
> Last updated: 2026-07-17

---

## The Problem

AI agents and engineers alike treat repositories as collections of files to be searched — grep, embeddings, agentic file-reading. Every question re-derives the same understanding from scratch: what features exist, what business rules govern them, which tests cover what, what changed last sprint. This understanding is expensive to rebuild, immediately stale, and never shared between the agent that built it and the next one that needs it.

Meanwhile, the knowledge itself is already *in* the artifacts — code, PRs, commit messages, Jira tickets, OpenAPI specs, existing tests. It's just uncompiled.

## The Bet

**Treat the repository as source code and engineering knowledge as its compilation target.**

A compiler doesn't re-read the world on every query. It transforms source into a persistent, structured, incrementally-updated artifact — and consumers use *that*. The Knowledge Compiler does the same for engineering knowledge:

```
Source code + PRs + commits + Jira + docs + tests
        │
        ▼
  Knowledge Compiler          (collect → extract → normalize → persist)
        │
        ▼
  Compiled Engineering Knowledge   (structured entities + relationships)
        │
        ├──▶ Living Wiki           (human-readable build artifact)
        ├──▶ Embeddings + search   (retrieval layer)
        └──▶ MCP server            (AI agent consumption)
```

This is not another RAG system. RAG retrieves fragments of raw source at query time. A compiler produces normalized, deduplicated, cross-linked knowledge *ahead* of query time, and keeps it synchronized through incremental compilation.

**The three-layer truth model:**

| Layer | Role |
|---|---|
| Repository (+ Jira, PRs) | Source of truth |
| Knowledge base | Compiled knowledge |
| Wiki | Human-readable output |

The wiki and the database are build artifacts. They are never edited by hand; they are regenerated from source. If the wiki is wrong, the compiler is wrong — fix the compiler.

## V1 Wedge: The Living Wiki

**The one thing that must work end-to-end first: a continuously updated Markdown wiki compiled from a real repository's artifacts.**

Why the wiki leads:

1. **It's the honest test of compilation quality.** If the compiled knowledge can't produce a wiki a human engineer finds accurate and useful, it won't produce good agent context either. Humans are the strictest evaluators we have.
2. **It's immediately valuable without any agent in the loop.** Onboarding, sprint reviews, "what changed since release" — value lands before the harder retrieval and test-generation problems are solved.
3. **Everything else stacks on it.** Repo Q&A is retrieval over the same compiled entities the wiki renders. Test generation is reasoning over the same business rules and coverage maps the wiki displays. The wiki forces the knowledge model to exist and be correct.

Agent consumption (MCP, Q&A) is the *second* milestone. Test generation — the long-term killer use case — is the third, and it inherits its quality from the first two.

## Use Cases, In Priority Order

1. **Living documentation** *(V1 wedge)* — a wiki that always reflects the latest merged state: features, components, APIs, business rules, test coverage, recent changes, risks.
2. **Repository Q&A** — agents (via MCP) answer engineering questions without scanning the repo: "Which business rules changed recently?", "Which tests cover this component?", "What changed since the last release?"
3. **Knowledge discovery** — provenance queries: "Which PR introduced this rule?", "Which Jira ticket added this endpoint?"
4. **Test generation** — agents generate implementation-aware regression tests from compiled business rules, API contracts, coverage gaps, and recent deltas.

## Core Mechanic: The Knowledge Delta

Every merged PR produces a **knowledge delta** — the smallest unit of incremental compilation:

- Features changed · Business rules added/modified · APIs modified · Components affected · Tests impacted · Risks introduced

Deltas are what keep the knowledge base synchronized without full recompilation, and they are first-class output: a human-readable "what did this PR actually change, in engineering terms" record with provenance links back to PR, commits, and Jira.

Full recompilation must always remain possible and cheap enough to run (the escape hatch when incremental state drifts).

## Extraction Philosophy: Deterministic-First

**Everything that can be extracted deterministically, is. LLMs are reserved for semantics.**

| Deterministic (AST, git, parsers) | LLM-assisted |
|---|---|
| Components, modules, symbols, dependencies | Feature narratives ("what is this *for*") |
| API surfaces (routes, OpenAPI, signatures) | Business rules and their intent |
| Test inventory and structural coverage mapping | Risk assessment |
| Commit/PR/Jira metadata and linkage | Summarization for wiki prose |
| Diffs and change surfaces | Conflict resolution between sources |

Rationale: deterministic extraction is reproducible, cheap, and debuggable — the compiler's skeleton must not hallucinate. LLM extraction is layered on top where meaning genuinely requires reasoning, and every LLM-derived fact carries provenance (which artifacts it was derived from) so it can be audited and recompiled.

The extractor interface does not hardcode this split; individual extractors declare their method, so the ratio can shift as models and costs change.

## Language Scope

**V1 analyzes two target languages: Python and TypeScript.** TypeScript is chosen because its toolchain is maximally different from Python's, which stress-tests the plugin interface.

One language would let the "pluggable analyzer" interface silently ossify around Python assumptions. Two forces the abstraction to be honest from day one. More than two is scope creep.

Note the distinction: the compiler *itself* is implemented in Python only. Target-language analysis (including TypeScript) runs in-process via tree-sitter — no Node.js runtime is required (see `docs/architecture.md`).

Language analyzers are plugins behind a stable interface: given source, produce components, APIs, symbols, and test mappings in the canonical model. Everything downstream of extraction is language-agnostic.

## Deployment Posture

**V1 is dogfooded against our team's real repository** — real PRs, real Jira, real messy history — before any generalization. Open-source polish (compile any public repo out of the box) is a later milestone, not a V1 constraint.

This ordering is deliberate: a knowledge compiler that works on a curated demo repo but chokes on real artifact noise (inconsistent Jira linkage, squash merges, half-written READMEs) has not proven anything. The dogfood repo is the benchmark.

## Canonical Knowledge Model

Compiled knowledge is a set of typed, cross-linked entities:

**Project · Feature · Component · API · Business Rule · Test Coverage · Risk · Pull Request · Jira Story · Wiki Page**

Principles:

- **Provenance everywhere.** Every entity and every fact links back to the artifacts it was compiled from.
- **Relationships are explicit** where useful (Feature ↔ Components, Business Rule ↔ Tests, PR ↔ Delta), not inferred at query time.
- The precise IR/schema is an architecture decision (see `docs/data-model.md` and ADRs), not a vision commitment.

## Design Principles

1. **Pluggable stages.** Collectors, extractors, normalizers, writers, wiki generators, retrieval providers, and language analyzers each sit behind a stable, independently testable interface.
2. **Incremental by default, recompilable always.** PR-triggered deltas keep knowledge fresh; full recompilation is the correctness escape hatch.
3. **Deterministic whenever possible.** LLM reasoning only when deterministic extraction is insufficient (see Extraction Philosophy).
4. **Boring infrastructure.** Single storage backend (PostgreSQL, per ADR-001), no distributed systems, no graph database, no microservices. Complexity must be earned by a demonstrated need.
5. **Compiled artifacts are disposable and reproducible.** The database and wiki can be deleted and rebuilt from source at any time — no hand-edits, no un-recompilable state. Reproducible means *semantically equivalent* on recompile, not byte-identical: LLM-assisted extraction is nondeterministic in wording, never in provenance or structure.
6. **Human-readable outputs.** The wiki and knowledge deltas are written for engineers first; agent-readability falls out of the same structure.
7. **Decisions are recorded.** Significant architectural choices become ADRs in `docs/decisions/`.
8. **When uncertain, split; never silently merge.** Visible, correctable noise beats silent corruption. Applies wherever the compiler must resolve ambiguity — entity identity matching, extraction, and conflict handling between sources (established in ADR-004).

## Non-Goals (V1)

- Distributed architecture, microservices, multi-tenancy
- Dedicated graph database or multiple storage backends
- Real-time indexing (PR-merge granularity is the freshness contract)
- IDE integrations
- CI/CD automation beyond triggering compilation
- Languages beyond the two V1 targets
- Compiling arbitrary public repos out of the box (post-V1)

## Success Criteria

V1 succeeds if, against our real repository:

1. **A merged PR automatically updates the wiki and knowledge base** with a correct knowledge delta.
2. **Engineers voluntarily use the wiki** — it answers onboarding and "what changed" questions accurately enough that people return to it.
3. **The knowledge stays synchronized** over weeks of real development without manual repair; full recompilation reproduces equivalent state.
4. **AI agents answer engineering questions through MCP** without scanning the repository *(milestone 2)*.
5. **The plugin architecture survives contact with a second language** — adding TypeScript did not require changes downstream of extraction.

The long-term criterion — agents generating measurably better regression tests from compiled knowledge — is the north star, evaluated after the wedge is proven.

## Open Questions

Tracked here until resolved into ADRs:

- **IR shape** — what exactly is the canonical intermediate representation between extraction and persistence?
- **Versioning** — is knowledge immutable/versioned per merge, or mutated in place with delta history?
- **Wiki strategy** — regenerate pages wholesale per compile, or incrementally patch? How are cross-links kept consistent?
- **Retrieval ranking** — keyword vs. semantic vs. hybrid, and how metadata filters influence ranking.
- **Conflict handling** — when Jira says one thing and code says another, who wins and how is the disagreement surfaced?
- **Test-generation evaluation** — what does "better test cases" mean measurably? (Must be answered before milestone 3.)
