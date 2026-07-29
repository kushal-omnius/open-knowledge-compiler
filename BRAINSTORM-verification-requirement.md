# Brainstorm: VerificationRequirement entity
2026-07-29 · Mode: Design decision + ADR call

## Context

BRAINSTORM-test-generation-eval.md established the scoring methodology (declared-coverage `kc-covers:` header + mutation-kill rate). BRAINSTORM-test-generation-mechanism.md confirmed Option B (external agents generate tests; KC provides `test_plan` + `validate-test`). Two spikes produced the concrete data that unblocks this question:

1. **Declared-coverage spike record:** `kc validate-test` is implemented and has scored real tests across a full spike arc (`BRAINSTORM-test-generation-mechanism.md`) — one early run in that arc was found on re-verification to be misreported and is excluded from this evidence base; the corrected record still confirms the declared-coverage convention "runs in practice," including a controlled comparison showing information-access method doesn't affect achievable score once test strategy is held constant.
2. **Mutation distribution (eval brainstorm spike):** Four-module mutation distribution across Knowledge-Compiler showed 64.4–81.1% kill rates with real execution-based signal — concrete evidence of what component-level granularity misses.

**The triggering observation (api/symbols asymmetry):** A test claiming `component/billing-rules` can score 100% on declared-coverage while killing zero mutants on the boundary condition it purports to cover — because it imported the module and called `apply_discount(5)` without ever exercising the cap boundary. The current scoring has no way to distinguish "test that invoked the function" from "test that verified the invariant." `VerificationRequirement` ("discount must not exceed 20%") would give the `kc-covers:` header a slot to claim that specific condition, making `validate-test` precision meaningful below the component level.

**What the data doesn't yet tell us:** Whether coding agents will actually use sub-component granularity in practice. Both spikes showed agents work fine with `component/*` and `api/*` slugs from `test_plan`. `VerificationRequirement` would require agents to call `get_entity` on each recommendation to read the verification obligations, then structure tests around them — a richer workflow but more friction.

## The decision

Does LLM-extracted verification obligations at sub-component granularity close a gap that justifies a new entity kind + schema extension + extraction prompts — or does the existing `component` + `api` granularity + mutation-kill rate together serve the same purpose without adding IR complexity?

## Options

### Option A: Add VerificationRequirement as a compiled entity (V1 extension)

**Sketch:** Extract `VerificationRequirement` from `BusinessRule` text via LLM (e.g. "discount must not exceed 20%", "claim must have at least one document before submission"). Mint as first-class entities in the IR, linked to their parent `BusinessRule` via `expresses` edges. Add to `test_plan` recommendations. Agents can then claim individual `verification_requirement/discount-cap` slugs in `kc-covers:` headers.

**Pros:**
- Closes the precision gap that the mutation-distribution data exposed — you can now distinguish "called the function" from "verified this specific condition"
- Consistent with the deterministic-first philosophy: LLM extracts the obligation, `validate-test` checks it deterministically
- Follows the established pattern for new entity kinds (ADR-009 two-layer IR is designed for this extension)

**Cons:**
- New entity kind = new extraction prompts, new schema column/table, new `test_plan` recommendation path, new `validate-test` check — non-trivial scope
- Partially duplicates the mutation-kill signal structurally: mutation testing already catches the cap-boundary miss at execution time; VerificationRequirement would try to catch it at declaration time, earlier in the loop but with different (weaker) guarantees
- **Agent friction increase:** agents currently receive `test_plan` output and write a correct header in 1–5 turns. VerificationRequirement requires `get_entity` → read obligations → structure test per obligation — unknown whether agents adopt this without explicit workflow guidance
- Architecture is frozen (V1 frozen 2026-07-18); this is additive but still a schema extension

**Pre-mortem:** Extraction prompts produce noisy `VerificationRequirement` entities ("the system should be reliable") that agents claim without actually testing; declared-coverage precision goes up on paper while execution coverage stays flat. The mutation-kill rate was the honest signal all along.

**Reversibility:** One-way for schema migration; two-way for extraction logic (can be turned off without data loss if the column is nullable).

**Effort:** Medium (days to a week): new entity kind, extraction prompts, `test_plan` update, `validate-test` update, tests.

---

### Option B: Do not add VerificationRequirement in V1 — mutation-kill rate is the execution-based signal (recommended)

**Sketch:** Leave the entity model as-is. The existing scoring duo — declared-coverage (precision/recall vs `test_plan` at component+API granularity) + mutation-kill rate — together cover the gap. `validate-test` confirms the test file named what it was supposed to test; the existing mutmut CI workflow confirms the test actually exercises the logic. `VerificationRequirement` is deferred to a post-V1 milestone, to be revisited once agent adoption of `test_plan` + `validate-test` at current granularity is observed at scale.

**Pros:**
- Mutation testing is an execution-based signal — it catches the "called the function but didn't hit the boundary" failure mode directly, not structurally
- No IR complexity added to a frozen architecture
- The agent workflow is simpler: `test_plan` → write test → `validate-test` exit 0 → mutation CI. No `get_entity` loop required
- Deferred, not abandoned: the VerificationRequirement design is now fully documented; if mutation scores cluster high while agents demonstrably miss specific conditions, revisiting is low-friction

**Cons:**
- `validate-test` precision is meaningful only at component/API granularity — a claimed `component/billing-rules` slug doesn't distinguish which invariant was tested
- The cap-boundary miss is real: an agent CAN score 100% declared-coverage and 0% mutation-kill on the same condition, and the feedback loop only closes at CI time (slower)

**Pre-mortem:** Six months on, agents consistently generate tests that score 100% declared-coverage but kill <50% mutants on the components they claim. The component-level granularity was always too coarse; by deferring VerificationRequirement we built a habit of trusting the declared-coverage score when the mutation score is the honest one.

**Reversibility:** Two-way — Option A remains fully available.

**Effort:** None.

---

### Option C: Prose-only — emit VerificationRequirement as wiki-page section, not compiled entity

**Sketch:** Extend Emit to include a "Verification obligations" section in each `BusinessRule` wiki page (LLM-derived prose conditions). Agents can read the wiki page to understand what to test; no new slug type, no `kc-covers:` header change.

**Pros:** Zero schema impact; reuses existing Emit LLM prose pattern; gives agents readable context.

**Cons:** Can't be claimed in a `kc-covers:` header — the precision gap goes unaddressed; wiki pages aren't surfaced by `test_plan`; same pre-mortem as Option E in the mechanism brainstorm (nice reading material, the scoring loop never closes).

**Reversibility:** Two-way.

**Effort:** Low (days).

---

## Comparison

| | Closes precision gap | Mutation-kill redundancy | Agent friction | IR complexity | Effort | Reversibility |
|---|---|---|---|---|---|---|
| **A. New entity kind** | ✓ at declaration time (before a test runs) | ~ (partial duplicate) | High | Medium | Days–week | Mostly one-way |
| **B. Defer (mutation is the signal)** | ✓ at execution time (once a test runs) — not at declaration time | ✓ (execution signal) | None | None | None | Two-way |
| C. Prose wiki section | ✗ (not claimable in a header at all) | ✗ | Low | None | Low | Two-way |

(Both A and B close the gap, at different points in the loop — A earlier and weaker, B later and stronger. Only C fails to close it at all. See Recommendation below for why B's later-but-stronger signal is preferred.)

## Recommendation

**Option B — defer VerificationRequirement; mutation-kill rate is the execution-based signal that closes the cap-boundary gap.**

**Confidence: medium.** The mutation-distribution spike showed 64–81% kill rates with real signal, and the fuller declared-coverage spike record now supports the workflow-friction argument better than the original write-up did (see the note above on the retracted early run). The gap that VerificationRequirement would close structurally — "did the test claim the right condition?" — is already closed executionally by mutation testing, which doesn't require agents to adopt a richer workflow. Adding a new entity kind to close a gap that an existing execution-based tool already closes is adding IR complexity for a weaker guarantee (declaration) when the stronger one (execution) is already in the loop. One unresolved caveat, recorded in ADR-012 rather than solved here: mutation-kill is measured at component granularity, while the gap VerificationRequirement targets is sub-component — the two aren't strictly substitutable, only correlated in the motivating example.

**The case to make explicitly in the ADR:** VerificationRequirement is not ruled out — it is deferred pending evidence that agents at scale produce systematically low mutation scores on components they claim at 100% declared-coverage. If that pattern emerges post-V1, Option A has a clear justification. Without that evidence, the schema extension is speculative.

**Steelman of Option A:** A reasonable person optimizing for "the scoring loop closes before CI" would pick A. Mutation testing runs in CI and gives feedback minutes or hours after a test is written; VerificationRequirement would give feedback at `validate-test` time (seconds). If the workflow target is an agent that iterates on test quality before committing, earlier feedback matters. The counter-argument is that agents in the two-spike sample did not iterate on mutation scores at all — they wrote a test, scored it, and moved on.

## ADR call

This decision warrants a new ADR (post-freeze additive decision, same precedent as ADR-011). Draft: **ADR-012: Defer VerificationRequirement entity; mutation-kill rate is the V1 signal for sub-component test precision.**

Record: the triggering evidence, the option considered (Option A above), the deferral rationale, and the explicit trigger condition for revisiting (pattern of high declared-coverage + low mutation-kill at scale).

**Done 2026-07-29** — created as [ADR-012](docs/decisions/ADR-012-defer-verification-requirement-entity.md) (Accepted).
