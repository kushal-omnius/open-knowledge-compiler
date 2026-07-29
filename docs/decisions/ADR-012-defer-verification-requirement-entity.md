# ADR-012: Defer VerificationRequirement entity; mutation-kill rate is the V1 sub-component precision signal

**Date:** 2026-07-29
**Status:** Accepted
**Supersedes:** —
**Related:** ADR-009 (two-layer IR), ADR-008 (LLM semantic layer), ADR-011 (cross-repo dependency resolution)

---

## Context

BRAINSTORM-test-generation-eval.md established the scoring methodology for milestone 3 (test generation): declared-coverage (`kc-covers:` header, precision/recall vs `test_plan`) + mutation-kill rate. A subsequent spike arc (2026-07-29, documented in full in `BRAINSTORM-test-generation-mechanism.md`) confirmed the declared-coverage mechanism runs correctly across several independent runs — including a controlled comparison isolating information-access method (MCP-style tool calls vs. a raw `test_plan` JSON hand-off) from test-writing strategy, which found the former made no difference to achievable score once the latter was held constant. One early run in that arc was found on re-verification to have been misreported (see the mechanism doc's "retracted" section) and is not treated as evidence here.

The eval brainstorm's mutation-distribution spike surfaced a concrete precision gap: a test claiming `component/billing-rules` can score 100% declared-coverage while killing zero mutants on the boundary condition it purports to cover. The test imported the module and called `apply_discount(5)` without exercising the cap boundary. Current scoring at component/API granularity cannot distinguish "invoked the function" from "verified the invariant."

A `VerificationRequirement` entity was proposed as a structural fix: LLM-extracted sub-component obligations ("discount must not exceed 20%") minted as first-class IR entities, claimable in `kc-covers:` headers, surfaced by `test_plan`. This would make `validate-test` precision meaningful at finer granularity than component or API.

The question this ADR answers: does that structural fix justify a new entity kind + schema extension + extraction prompts in V1?

## Decision

**Do not add `VerificationRequirement` as a compiled entity in V1.**

The mutation-kill rate is the execution-based signal that closes the sub-component precision gap. It catches the cap-boundary miss directly (at execution time) without requiring agents to adopt a richer workflow or the compiler to extract and maintain a new entity kind.

## Rationale

**The gap is real but already closed by the existing execution-based tool.** Mutation testing catches "called the function but didn't hit the boundary" at CI time. `VerificationRequirement` would close the same gap at `validate-test` time (declaration time, seconds earlier), but with a weaker guarantee: a structurally-claimed condition is not the same as an executionally-verified one. Adding IR complexity for a weaker, earlier signal when the stronger signal is already in the loop is not justified.

**Agent workflow cost is real but lower than initially assumed.** The fuller spike record (`BRAINSTORM-test-generation-mechanism.md`) shows agents working correctly with `component/*` and `api/*` slugs from `test_plan` across multiple independent runs, including runs that required real judgment calls (declining to over-claim, correctly identifying dead-code targets, correctly recognizing already-claimed gaps). `VerificationRequirement` would add a step to that workflow — call `get_entity` on each recommendation to read the obligations, then structure tests per obligation — that hasn't been tested and could plausibly produce noisier headers without better coverage. This is a lower-confidence claim than the previous paragraph's execution-vs-declaration argument, and is offered as a secondary consideration, not the primary rationale.

**Architecture is frozen; the extension is speculative.** V1's entity model (ADR-009) covers the confirmed use cases. Extending it for a gap that mutation testing already closes, before observing whether the gap actually manifests in agent-generated test suites at scale, would add complexity against unconfirmed need.

**Known limitation in this argument, recorded rather than resolved:** mutation-kill rate is measured at component granularity, while the gap VerificationRequirement targets is sub-component (one specific condition within a component). A component can plausibly clear the trigger condition's ≤40%-mutation-kill bar in aggregate while the exact condition VerificationRequirement would have flagged is never hit by any test — the two signals aren't strictly substitutable, only correlated in the motivating example. The trigger condition below is stated at component granularity because that's what's measurable today; if evidence emerges that this granularity mismatch is hiding real misses, the trigger condition itself — not just the entity decision — should be revisited.

## Consequences

- `validate-test` precision remains meaningful at component/API granularity only. A test claiming `component/billing-rules` does not distinguish which invariant was exercised.
- The cap-boundary miss can score 100% declared-coverage and 0% mutation-kill; the feedback loop closes at CI, not at `validate-test` time.
- `VerificationRequirement` is deferred, not ruled out. The design is fully documented in BRAINSTORM-verification-requirement.md with Option A preserved.
- Separately, the spike record surfaced two other `test_plan`/`validate-test` limitations orthogonal to this decision — no representation of unreachable/dead-code targets, and no representation of the api/symbols reachability ceiling on achievable score. Both are noted in `BRAINSTORM-test-generation-mechanism.md`'s "Next steps" as candidate low-effort tool improvements, not entity questions.

## Trigger condition for revisiting

Reopen this ADR if, across deployed agent workflows, a consistent pattern emerges of **high declared-coverage scores + low mutation-kill rates on the same components** — specifically, if agents systematically score ≥80% declared-coverage but ≤40% mutation-kill on components they claim. That pattern would indicate the component/API granularity is systematically too coarse and the structural signal is needed.

Given the granularity mismatch noted above, also treat as a trigger: any concrete case where a component clears the mutation-kill bar in aggregate but a specific, known-important condition within it is demonstrably never exercised by any test. That case would mean the trigger condition needs restating at finer granularity, independent of whether the overall deferral decision still holds.

Without that evidence, the schema extension is speculative and deferred.
