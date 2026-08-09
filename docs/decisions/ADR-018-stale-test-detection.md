# ADR-018: Stale-Test Detection via Delta-Log Cross-Reference

## Status

Accepted — **implemented 2026-08-08, as Option A below with no changes to the recommended design**

## Date

2026-08-08

## Context

`test_coverage`'s `covers` relationship (ir.md §3.3) proves a test currently touches a component or API — it says nothing about whether the test was updated the last time that entity's *behavior* changed. A business rule's cap can move from 10% to 20% in PR #200 while the test claiming to cover it was last touched at compile run 47, months earlier, and `coverage_for`/`test_plan` will still report it as covered. `kc validate-test`'s declared-coverage scoring (ADR-012's methodology) checks whether a test's claimed slugs exist and match `test_plan`'s recommendations — it does not check currency against the entity's own change history.

This is a real, cheap-to-close gap: the data needed already exists and is already durable. `delta_changes` (data-model.md §2) records every entity change with `compile_run_id`; `test_coverage` entities carry `last_compile_run_id` (the compile run that last touched that entity, per the common entity envelope, ir.md §3.1). Nothing currently joins the two.

## Decision Drivers

- Simplicity (vision.md DP4: boring infrastructure — prefer a query over a new mechanism)
- Determinism (the signal must be a fact, not an LLM judgment)
- Accuracy (closes a real trust gap in what "covered" means)
- No schema changes if avoidable (data-model.md's existing tables already carry what's needed)

## Considered Options

### Option A: Query-time cross-reference, no schema change (recommended)

For a given Test Coverage row and the entity(ies) it `covers`, compare the entity's most recent `delta_changes` row (by `compile_run_id`) against the test's own `last_compile_run_id`. If the entity changed more recently than the test was last touched, surface a `stale: true` flag with the causing PR/compile reference. Exposed via `coverage_for` and folded into `test_plan`'s existing gap reporting as a distinct condition from "no coverage."

Pros: zero schema change — pure read-time computation over already-durable data (`delta_changes`, `data-model.md` §2, kept indefinitely in V1); consistent with ADR-011's precedent of solving a real gap at query time rather than extending Persist; fully deterministic.
Cons: requires a join across `delta_changes` and `test_coverage.last_compile_run_id` on every `coverage_for`/`test_plan` call — a performance cost to verify at dogfood scale, though bounded by the same indexes already serving `data-model.md` §5's "which tests cover this component" headline query.

### Option B: New relationship type with a "verified as of" attribute

Add a `verified_as_of` attribute to the `covers` relationship, updated at Persist time whenever a test's coverage is (re-)confirmed current.

Pros: pre-computed, no query-time join needed.
Cons: ir.md §3.3 is explicit that V1 relationships carry no payload — "attributes on relationships are a breaking vocabulary change." This option would require reopening that boundary for a signal Option A can compute without touching it at all.

### Option C: Trigger a mutation-testing re-run whenever a covered entity changes

Instead of a currency check, automatically re-run mutation testing on any component whose covering tests weren't touched in the same PR.

Pros: would catch not just staleness but whether the untouched test still actually verifies the new behavior.
Cons: conflates two different signals — mutation-kill rate answers "does this test verify the invariant," staleness answers "was this test looked at when the behavior changed." Automatically triggering execution-based testing on every relevant change is also a real cost (mutation testing is comparatively expensive, per the eval-methodology brainstorm's own CI-only-execution decision) for a question a much cheaper read-time check already answers directly.

## Decision

**Option A.** Compute staleness at query time as a join over already-durable data, with no schema or Persist-stage change.

## Consequences

### Positive

- Closes a real trust gap using data that already exists and is never pruned (`data-model.md` §4) — no new durable storage.
- Consistent with the project's own precedent (ADR-011) for solving cross-cutting questions at query time rather than extending compiled state.
- Immediately available to both the wiki (`coverage_for` page rendering) and MCP (`test_plan`/`coverage_for` tools) with one shared query.

### Negative

- A test can be "stale" by this definition even when the underlying change was cosmetic (e.g., a docstring or type-hint change) rather than behavioral — see Failure Modes.
- Adds a join to two of the more frequently called MCP tools; needs index support verified at dogfood scale before being treated as free.

### Tradeoffs Accepted

- This signal reports *possible* staleness, not confirmed test inadequacy — it is a prioritization hint for a QA agent or engineer, not a hard gate, mirroring ADR-012's own "track as a reported metric, no hard gate, for now" stance on mutation-score thresholds.

## Failure Modes

- False positives: an entity's `content_hash` can change for a non-behavioral edit (e.g., a comment) that doesn't warrant retesting. Mitigate by only flagging staleness when the specific changed payload path (`change_summary`, data-model.md §2) is a behaviorally meaningful field (e.g., a Business Rule's `rule_statement`, an API's route/method) — not blanket entity-changed detection. This needs a small allowlist of "meaningful" payload paths per entity type, defined alongside implementation.
- A test intentionally left unchanged because a reviewer confirmed it still covers new behavior has no way to acknowledge/suppress the flag in V1 — see Open Questions.

## Assumptions

- `delta_changes.change_summary`'s payload-path granularity (data-model.md §2) is fine-grained enough to distinguish behaviorally meaningful changes from cosmetic ones. If not, this ADR's false-positive rate will be higher than acceptable and the allowlist approach in Failure Modes will need revisiting.

## Open Questions

- Should there be a way to acknowledge/suppress a stale-test flag once a human or agent has confirmed the existing test still holds? Currently no such mechanism exists in the entity model; adding one may require its own small design (a suppression note is arguably wiki/agent-facing metadata, not compiled knowledge, and may not need a schema change at all).
- Should staleness ever gate CI (fail a build), or remain purely advisory in `test_plan`/`coverage_for`? Recommend advisory-only for V1, consistent with ADR-012's measure-first-gate-later discipline.

## Impact

Affected documents:
- data-model.md (documents the new query pattern in §5's headline-queries table; no schema change)
- kc-cli-reference.md (`test_plan`/`coverage_for` output shape gains a `stale` field)

Affected compiler stages: none at compile time — this is a `kc serve`/query-path addition only, same posture as ADR-011.

## Alternatives Rejected

Option B was rejected because it reopens the "relationships carry no payload" invariant (ir.md §3.3) for a signal that doesn't need it. Option C was rejected for conflating two independently useful, differently-costed signals (currency vs. verification depth) into one expensive mechanism.

## Future Reconsideration

If the meaningful-payload-path allowlist (Failure Modes) proves too coarse or too noisy in dogfood use, revisit whether a lighter LLM-assisted classification of "was this a behavioral change" is warranted — but only after the deterministic allowlist approach has been tried and shown insufficient, per the project's own deterministic-first bias.

## References

- ADR-011 (query-time-only precedent for a cross-cutting signal with no schema change)
- ADR-012 (measure-first, advisory-not-gating precedent for a new quality signal)
- `data-model.md` §2 (`delta_changes`), §5 (headline queries), §4 (retention — why this data is guaranteed to still be there)
