# ADR-009: Two-Layer IR — Facts and Knowledge

## Status

Accepted

## Date

2026-07-18

## Context

The canonical IR is the representation between extraction and persistence (vision open question; architecture.md §4–5). The pipeline already produces two distinct representations — per-compile **facts** (extraction output) and durable **entities** (ADR-004's definition: canonical knowledge that persists across compilations) — with Normalize converting one into the other. The question is whether the specified, versioned IR covers facts, knowledge, or both.

## Decision Drivers

- Extensibility — extractor output is the most plugin-exposed contract in the system (ADR-006 invariant: analyzers emit canonical facts only)
- Determinism — identity must exist only where ADR-004 assigns it (Normalize), never before
- Accuracy — provenance must stay fine-grained (entity → facts → artifacts)
- Simplicity — consumers must never ask "which kind of object am I holding?"
- Maintainability — fact vocabulary and entity model evolve at different rates

## Considered Options

### Option A — Facts only

Specify extraction output; let `data-model.md` implicitly define entities.

Pros: one specified model.
Cons: the entity model is the *semantic* contract consumed by wiki, MCP, and deltas — letting a storage document define semantics inverts the dependency.

### Option B — Knowledge (entities) only

Specify entities; extractor output stays informal.

Pros: one specified model, at the consumer surface.
Cons: leaves the most plugin-exposed contract unspecified — every extractor invents its own output shape, Normalize becomes N bespoke adapters, and the ADR-006/007 plugin boundary becomes a fiction.

### Option C — One unified model

Extractors emit "candidate entities" directly; no separate fact layer.

Pros: superficially simpler — one vocabulary.
Cons: a candidate without identity is not an entity (ADR-004: only Normalize assigns identity), and the unified type invites extractors to bypass the identity cascade; facts are fine-grained and many-to-one to entities, so conflation destroys provenance precision; two lifecycles (per-compile vs. durable) in one model burden every consumer.

### Option D — Two-layer IR with a directional boundary

Specify both layers as distinct, versioned models: **Fact IR** (extraction output — per-compile, disposable, identity-free, anchored, provenance-carrying) and **Knowledge IR** (canonical entities + explicit relationships — durable, slug-bearing). Normalize is the only crossing point.

Pros: each plugin-exposed contract is specified; identity boundary is enforced by the type system; provenance granularity preserved; mirrors standard compiler practice (multiple IRs).
Cons: two versioned contracts to maintain.

## Decision

**Option D — a two-layer IR.** The knowledge delta (ADR-003) is a third, *derived* artifact expressed in Knowledge IR vocabulary, not a separate layer.

A and B each leave one load-bearing contract unspecified; C erases the identity boundary ADR-004 exists to enforce. D is the only option consistent with all accepted ADRs.

## Architectural Invariants

- Extractors and language analyzers emit Fact IR only; they never produce entities.
- Only Normalize produces Knowledge IR; identity (slugs) exists only in the Knowledge IR.
- Consumers (Diff, Persist, wiki, MCP, embeddings) read Knowledge IR only; they never read facts.
- Every fact carries provenance (artifact refs) and, for LLM-derived candidates, anchors (ADR-004 hard requirement).
- Fact-vocabulary *additions* are non-breaking; Knowledge IR *changes* are breaking (versioning policy detail in `ir.md`).

## Consequences

### Positive

- The plugin contract (facts) and the consumer contract (entities) are each explicit, testable, and independently versioned.
- The identity cascade's boundary is structural, not conventional.
- Entity → facts → artifacts provenance chains stay fine-grained.

### Negative

- Two models to specify, version, and document in `ir.md`.

### Tradeoffs Accepted

- Specification overhead is accepted to keep the fastest-churning contract (facts) evolvable without breaking consumers.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| A plugin smuggles entity-like objects through the fact layer | Identity cascade bypassed | Fact types carry no slug field; Normalize rejects unknown shapes; fail loudly (ADR-007 policy) |
| Fact vocabulary gap (an extractor's observation has no fact type) | Knowledge silently unexpressed | Vocabulary additions are non-breaking by policy — add the type, don't shoehorn |
| Layer drift (Knowledge IR changes without fact-mapping updates) | Normalize breakage | Both layers versioned together in `ir.md`; mapping rules are part of the spec |

## Assumptions

- The fact vocabulary can be enumerated per extractor family (deterministic, LLM, analyzer) — validated as `ir.md` is written.
- Facts need no durable storage contract beyond the compile (staging is a `data-model.md` concern, not an IR concern).

## Open Questions

- The concrete fact vocabulary, entity schemas, anchor representation, and mapping rules — all deferred to `ir.md`, which implements this ADR.

## Impact

Affected documents: `ir.md` (implements this ADR), `data-model.md`, `pipeline.md`, `plugin-system.md`.
Affected compiler stages: Extract (emits Fact IR), Normalize (sole crossing point), Diff/Persist/Emit (Knowledge IR consumers).

## Alternatives Rejected

- **Facts only (A)** — storage would define semantics.
- **Knowledge only (B)** — the plugin contract stays unspecified.
- **Unified model (C)** — erases the identity boundary; destroys provenance granularity.

## Future Reconsideration

Revisit if a third representation genuinely emerges (e.g., a distinct agent-context format for MCP) that is neither facts nor entities, or if fact staging requirements make the fact layer durable in practice.

## References

- [ADR-004](ADR-004-entity-identity.md) — entity definition, identity-only-in-Normalize, anchor requirement
- [ADR-006](ADR-006-language-analyzers.md) — analyzers emit canonical facts only
- [ADR-003](ADR-003-current-state-delta-log.md) — the delta as a derived Knowledge IR artifact
- [ADR-007](ADR-007-plugin-architecture.md) — fail-loud plugin policy
- `docs/ir.md` — the specification implementing this decision

## Self-Review

- **Truly architectural?** Yes — it fixes the system's type boundaries and which contracts are public.
- **Already made?** Largely implied (ADR-004's fact/entity distinction, ADR-006's invariant, the Normalize stage); this ADR makes the implication a commitment and rejects the unified-model shortcut.
- **Reversible?** Layer *contents* are two-way (ir.md evolves); the boundary itself is one-way once plugins exist against it.
- **Dependent future documents:** ir.md (primary), data-model.md, pipeline.md, plugin-system.md.
- **Exposes unresolved decisions:** the vocabulary and schemas themselves — exactly ir.md's job.
