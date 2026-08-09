# ADR-020: Escaped-Defect Trust Score

## Status

Proposed — **not implemented** (explicitly out of scope for the 2026-08-08 QA-grounding-improvements PR, which covered backlog items 1–7 only; this is item 10)

## Date

2026-08-08

## Context

None of KC's existing or proposed test-quality signals — declared-coverage precision/recall (`kc validate-test`, ADR-012's methodology), mutation-kill rate (ADR-012), stale-test detection (ADR-018), or flakiness (ADR-019) — measure whether a test suite claimed as "covering" an entity has ever actually prevented a real regression. All four are proxies computed *before* or *independent of* production outcomes. `BRAINSTORM-test-generation-eval.md`'s Option E, historical-bug-replay (check out a pre-fix commit, run a generated test, require it to fail there and pass post-fix), was rated the most rigorous available signal but was explicitly not chosen for V1 because building a runnable-arbitrary-historical-revision replay harness is a real, weeks-scale operational cost (stale dependencies, broken migrations at old commits) with no guarantee of a large-enough historical-bug-fix corpus to be statistically useful.

This ADR proposes a materially cheaper, forward-looking alternative that doesn't require replaying old commits: when a bug-fix PR or Jira ticket lands against an entity going forward, check whether that entity had passing, `kc-covers`-claimed test coverage *at the time of the compile immediately preceding that fix*. Accumulated over time, this produces a per-entity trust score — "claimed covered five times, still shipped three regressions in that window" — that measures outcomes rather than inputs, without requiring the compiler to execute any historical revision.

## Decision Drivers

- Accuracy (this is explicitly about closing the "did any of this actually matter" gap the other four signals cannot close)
- Simplicity relative to the rejected full-replay alternative (Option E was rejected for cost; this must not reintroduce that cost)
- Incremental compilation (must fit the existing PR-triggered delta model, not require out-of-band historical analysis)
- Reversibility (a longitudinal signal with no data yet must degrade gracefully, not block anything, until enough history accumulates)

## Considered Options

### Option A: Forward-looking correlation via a new fact type + derived trust score (recommended)

Add `escaped_defect_observed` (deterministic fact type): when a merged PR or linked Jira ticket is identified as a bug fix (regex/label heuristic on PR title/body and Jira issue type, extending the existing `DCA-1234`-style issue-key extraction already used for PR↔Jira linkage, per pipeline.md §3.1) and it changes an entity that has prior compiled history, record a fact linking the fix to the entity plus a lookup of whether that entity had `kc-covers`-claimed, passing coverage as of the immediately preceding compile run (available via existing `provenance`/`delta_changes` history, data-model.md §2). Normalize aggregates these into a rolling trust-score attribute per entity, exposed via `test_plan`/`get_entity` and a new `escaped_defects_for(slug)`-style MCP tool.

Pros: uses only forward-flowing data the compiler already processes on every PR-triggered compile (ADR-002) — no replay, no historical-revision execution, no new runnability requirements; reuses the existing PR↔Jira issue-key extraction heuristic (pipeline.md §3.1) rather than inventing new bug-detection logic from scratch; degrades gracefully (zero signal, not a broken one, until enough fixes accumulate).
Cons: is inherently longitudinal — provides no signal on day one, and its statistical usefulness depends on the org's real bug-fix rate over subsequent months, same "measure first" caveat ADR-012 already applied to mutation-score-threshold-setting.

### Option B: Full historical-bug-replay harness (the eval brainstorm's Option E)

Build the replay harness as originally scoped: identify historical bug-fix PRs, check out pre-fix commits in a runnable environment, execute a generated test against both revisions.

Pros: the single most rigorous signal available — directly measures "would this have caught a real bug," including for past history, not just future incidents.
Cons: already assessed and not chosen in `BRAINSTORM-test-generation-eval.md` for concrete reasons that still hold — weeks of effort, a real operational cost to make arbitrary historical revisions runnable (stale dependencies, broken migrations), and no guarantee the historical bug-fix corpus is large enough to be statistically meaningful. Nothing new has changed that assessment.

### Option C: Do nothing; treat declared-coverage + mutation-kill + staleness + flakiness as sufficient

Rely on the four already-proposed/existing signals without adding an outcome-based one.

Pros: zero additional cost.
Cons: all four remaining signals are proxies for test quality computed independent of whether a regression actually occurred — none of them can ever answer "was this coverage claim ever validated by reality." This is the one gap none of the other backlog items close.

## Decision

**Option A.** Add a forward-looking, PR-triggered escaped-defect correlation as a new deterministic fact type and derived per-entity trust score, explicitly not attempting the full historical-replay harness rejected for cost reasons in the eval-methodology brainstorm.

## Consequences

### Positive

- The only signal in the full backlog (this ADR plus ADR-017/018/019 plus the earlier five items) that measures real-world outcomes rather than structural or execution-based proxies.
- Fits entirely within the existing PR-triggered incremental compile model (ADR-002) — no new operating mode, no historical-revision execution risk.
- Reuses the existing bug-fix-adjacent PR↔Jira linkage mechanism rather than inventing new heuristics.

### Negative

- Zero-value until real history accumulates — this is explicitly not a near-term deliverable in the way the query-only items (ADR-018, and backlog items 1/2/5/6) are.
- Bug-fix detection is a heuristic (regex/label-based), not a certainty — a PR titled "fix:" that isn't really a regression fix, or a real regression fix not labeled as such, will misclassify. This noise is bounded but not eliminable without human curation, which is explicitly out of scope for V1.

### Tradeoffs Accepted

- The trust score is a correlational signal, not causal proof that a specific test would or wouldn't have caught a specific bug (that stronger claim is exactly what the rejected Option B replay harness would have provided, at a cost this ADR declines to pay). This is an accepted, named tradeoff, not an oversight.

## Failure Modes

- Small-sample noise: an entity with only one or two observed fixes should not yet be treated as having a meaningful trust score; the aggregate must be reported with its underlying sample size, and likely withheld or clearly caveated below some minimum fix count — same shape as ADR-019's sparse-run-history caveat.
- Punitive misuse: if this score is surfaced in a way that reads as "blame" for whoever wrote the original test, it risks being gamed (e.g., avoiding `kc-covers` claims to avoid being on record) rather than acted on constructively. This must be framed and documented as a prioritization signal for future test-writing effort, not a retrospective scorecard on past authors.

## Assumptions

- The existing PR-title/Jira-issue-type bug-fix heuristic (extending pipeline.md §3.1's issue-key extraction) is precise enough to be useful without human labeling. If dogfood data shows this heuristic is too noisy, this ADR's fact type will need a stricter classification rule before the derived trust score is trustworthy — an implementation-time verification, not assumed proven here.

## Open Questions

- How far back should the rolling trust-score window extend, and should it decay older fixes rather than weighting all equally? Needs real longitudinal data before this can be answered with evidence rather than guesswork.
- Should a low trust score ever influence `test_plan`'s prioritization ordering (e.g., surfacing low-trust entities' remaining gaps first), or remain purely informational in V1? Recommend informational-only initially, consistent with ADR-012's and ADR-018's gate-later discipline — reopen once enough data exists to know whether the signal is reliable enough to prioritize on.

## Impact

Affected documents:
- ir.md (new fact type `escaped_defect_observed`, additive/non-breaking per §5)
- data-model.md (derived trust-score aggregate; no new durable table strictly required — could be computed at query time from existing `delta_changes`/`provenance` plus the new fact, similar in spirit to ADR-018's no-schema-change approach, or materialized if query cost warrants — an implementation-time choice, not fixed by this ADR)
- kc-cli-reference.md (`test_plan`/`get_entity` output; potential new MCP tool)

Affected compiler stages: Collect (bug-fix PR/Jira identification, extending existing linkage), Extract, Normalize (aggregation into the trust score).

## Alternatives Rejected

Option B (full historical-bug-replay) was rejected for the same cost and corpus-size reasons the eval-methodology brainstorm already identified — nothing in this ADR's context changes that assessment, so it is not re-litigated in full here. Option C (do nothing) was rejected because it is the one remaining gap no other proposed or existing signal closes: an outcome-based measure of whether declared coverage was ever validated by reality.

## Future Reconsideration

If, after sufficient longitudinal data accumulates, entities with high trust scores are still found to ship regressions at a rate indistinguishable from low-trust entities, that would indicate the correlation this ADR relies on is too weak to be useful — at that point, either the bug-fix classification heuristic needs tightening (Assumptions) or the full replay harness (Option B) should be revisited with the benefit of knowing exactly how much signal the cheaper approach was missing.

## References

- `BRAINSTORM-test-generation-eval.md` (Option E — historical-bug-replay — and the cost/corpus-size reasoning this ADR explicitly does not re-litigate)
- ADR-002 (PR-triggered compile model this fits within), ADR-012 (measure-first, informational-not-gating precedent), ADR-018 (sibling query-time signal; no-schema-change precedent), ADR-019 (sibling sparse-sample caveat precedent)
- pipeline.md §3.1 (existing PR↔Jira issue-key extraction this reuses)
