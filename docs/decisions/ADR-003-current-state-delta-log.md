# ADR-003: Current State + Append-Only Delta Log

## Status

Accepted

## Date

2026-07-17

## Context

Every compile produces knowledge. The system must decide what happens to the *previous* compile's knowledge: is it versioned immutably, overwritten, or something in between? This is the versioning model — it determines the shape of every query, the cost of every compile, and what "history" means for the Knowledge Compiler.

The decision is constrained by what consumers actually ask for. Every V1 consumer reads the **latest** state: the wiki renders "now," MCP Q&A answers about "now," test generation reasons over "now." The historical questions in the vision ("which business rules changed recently?", "what changed since last release?") are questions about *changes*, not about *reconstructing past states* — a critical distinction, because changes can be stored far more cheaply than full temporal state.

This ADR decides how compiled knowledge is versioned and how the knowledge delta — the vision's core mechanic — is persisted.

## Decision Drivers

- Simplicity — no temporal predicates taxing every query
- Incremental compilation — a PR compile must cheaply read and update state
- Accuracy — the delta log must faithfully answer "what changed" questions
- Performance — current-state reads dominate; they must hit small, indexed tables
- Maintainability — storage growth must be bounded and predictable
- Determinism — persist must be atomic; state and history must never disagree

## Considered Options

### Option A — Immutable snapshot per merge (temporal tables)

Every entity row carries `valid_from` / `valid_to`; a compile closes old rows and opens new ones. Point-in-time queries are `WHERE valid_from <= t < valid_to`.

**Pros**

- Time-travel queries are first-class: reconstruct the knowledge base at any merge.
- Nothing is ever lost; audit is trivial.

**Cons**

- **Every consumer query pays a temporal tax** (predicates, partial indexes) to answer questions no V1 consumer asks.
- Tables grow as merge count × changed-entity count; at PR-merge granularity on an active repo this dominates storage.
- The delta still has to be computed for the wiki/MCP "what changed" views — snapshots don't give it away; they just store its inputs redundantly.

### Option B — Current state + append-only delta log

Current-state tables (`entities`, `relationships`) are mutated transactionally per compile; a `deltas` table appends one immutable row per compile describing added/changed/removed entities and relationships, with PR/commit/Jira references.

**Pros**

- Consumer queries hit small current-state tables with no temporal machinery.
- "What changed" questions are direct reads of the delta log — the vision's delta-as-first-class-output falls out of the storage model itself.
- Storage growth is proportional to *change volume*, the minimum possible.
- Matches ADR-004 exactly: the identity cascade matches candidates against *current state*, which is a real, always-materialized table.

**Cons**

- Point-in-time reconstruction is not a query — it requires replaying deltas or recompiling at a historical commit (slow, but no V1 consumer needs it).
- State and log can theoretically diverge; atomicity of the persist step becomes an invariant that must be enforced, not assumed.

### Option C — Event sourcing (delta log as the only store)

Deltas are the *source of truth*; current state is a projection materialized from replay (or maintained as a cache).

**Pros**

- One authoritative store; state is derivable by construction.
- Elegant audit story.

**Cons**

- **The real source of truth is the repository, not the log.** Event sourcing earns its complexity when events are the primary record; here a full recompile already regenerates everything, so the log-as-truth adds replay machinery, projection versioning, and rebuild choreography for no gained capability.
- Replay cost grows with history; "disposable, rebuildable" state now has two rebuild paths (replay vs. recompile) that can disagree.
- Violates the vision's boring-infrastructure principle.

## Decision

**Option B — current-state tables plus an append-only delta log.**

State and delta are written in **one transaction** per compile (the Persist stage). The delta log is the system's history; current state is the system's answer surface. Point-in-time needs are served by `kc compile --full` at a historical commit — the repository is the source of truth, so the past is recompilable, not stored.

Why B over the alternatives: A stores history no consumer reads and taxes every query for it; C inverts source-of-truth (the repo already plays the role event sourcing gives the log). B is the only option where the storage model *is* the product mechanic — the delta log is simultaneously the history store and the "knowledge delta per PR" deliverable.

## Architectural Invariants

- The delta log is append-only: delta rows are never updated or deleted.
- State mutation and delta append occur in a single transaction; a compile is either fully persisted or not at all.
- Current state is always reconstructible from the repository by full recompilation (per the vision's disposability principle); the delta log is *history*, never a required input to state reconstruction.
- Every delta row references its compile run and its source refs (PR, commits, Jira keys).
- Consumers never derive "what changed" by diffing snapshots; they read the delta log.

## Consequences

### Positive

- Fast, simple consumer queries; no temporal predicates anywhere.
- The delta log directly serves the vision's headline queries and the per-PR knowledge delta output.
- ADR-004's identity cascade has a concrete, cheap matching target (current state).
- Storage growth bounded by change volume.

### Negative

- No cheap point-in-time reconstruction (recompile-at-commit is the escape hatch).
- Atomic persist is a hard requirement on the Persist stage — a crash-safety obligation that Option A would not concentrate in one place.

### Tradeoffs Accepted

- **Time-travel is traded for query simplicity.** If a real time-travel requirement emerges, snapshots can be layered *on top of* the delta log later without breaking it (a new consumer, not a migration).
- History is expressed in delta terms (entity-level adds/changes/removes), not full past states; questions about past state require recompilation.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Crash mid-persist | State/log divergence | Single-transaction invariant; compile marked failed in `compile_runs`, safely re-runnable |
| Delta log unbounded growth | Storage pressure on active repos | Log is append-only but *archivable*: old rows can be exported/partitioned without touching current state; not a V1 feature, flagged for data-model.md |
| Incremental state drift (accumulated deltas diverge from what full compile would produce) | Wrong current state despite intact log | `kc verify` (cascade-based equivalence per ADR-004); periodic full recompile into the existing database re-grounds state |
| Delta semantics too coarse (consumer needs sub-entity change detail) | "What changed" answers lack precision | Delta document schema is data-model.md's decision; this ADR fixes only append-only entity-level granularity as the floor |

## Assumptions

- No V1 consumer requires point-in-time state reconstruction (validated against every use case in vision.md).
- PostgreSQL transactions span all persisted tables (single database, per ADR-001).
- Change volume per compile is small relative to total entities (incremental compiles touch a PR's slice).
- `compile_runs` bookkeeping exists (used here for atomicity accounting; used by ADR-002 for reconciliation).

## Open Questions

- Delta document schema (JSONB shape, granularity of "changed" descriptions) — deferred to `data-model.md`.
- Archival/retention policy for the delta log on long-lived repos — deferred until dogfood evidence of growth.
- Whether `entity_moved` (ADR-004) is a delta event type or an attribute of a change record — data-model.md.

## Impact

Affected documents:

- `architecture.md` §5 (storage), §4 (Diff/Persist stages)
- `ir.md` — delta document is an IR artifact
- `data-model.md` — `entities`, `deltas`, `compile_runs` schemas
- `pipeline.md` — Persist stage transactionality contract

Affected compiler stages:

- **Diff** — produces the delta against current state
- **Persist** — owns the single-transaction invariant
- **Emit** — dirty-entity selection reads the delta

## Alternatives Rejected

- **Temporal snapshots (A)** — permanent per-query tax for a capability no V1 consumer uses; delta must be computed anyway.
- **Event sourcing (C)** — duplicates the repository's source-of-truth role; adds replay/projection machinery the boring-infrastructure principle forbids.

## Future Reconsideration

Revisit if a consumer emerges that genuinely needs point-in-time state (e.g., "show me the knowledge base as of release 2.3" as an interactive query rather than an offline recompile), or if delta-log growth forces a retention design that snapshots would simplify.

## References

- `docs/vision.md` — Core Mechanic: the Knowledge Delta; Design Principles 2 (incremental, recompilable) and 5 (disposable artifacts)
- `docs/architecture.md` — §5 (A2), §4 (pipeline)
- [ADR-004](ADR-004-entity-identity.md) — Accepted; the identity cascade matches against the current state defined here
- ADR-001 — PostgreSQL (transactional persist spans all tables)
- ADR-002 — CI trigger (consumes `compile_runs` for reconciliation)

## Self-Review

- **Truly architectural?** Yes — it fixes the versioning model, query shape, and the delta's storage semantics; every stage downstream of Diff depends on it.
- **Already made by the existing architecture?** Yes — architecture.md §5/A2 recommends exactly this; the ADR adds the atomicity invariant and the event-sourcing comparison.
- **Reversible?** Partially — layering snapshots on top later is additive; *removing* the delta log would break the product mechanic. One-way door on append-only; two-way on retention details.
- **Dependent future documents:** data-model.md (schemas), pipeline.md (Persist contract), ir.md (delta document).
- **Exposes unresolved decisions:** delta document schema and retention policy (data-model.md), both listed above — neither invented here.
