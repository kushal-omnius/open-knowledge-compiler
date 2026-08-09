# ADR-019: Test Flakiness Signal from CI Run History

## Status

Proposed — **not implemented** (explicitly out of scope for the 2026-08-08 QA-grounding-improvements PR, which covered backlog items 1–7 only; this is item 9)

## Date

2026-08-08

## Context

`test_coverage` entities record that a test exists and what it targets (ir.md §2.3, `test_case_observed`/`test_target_observed`). Nothing records whether that test is *trustworthy* — a test that intermittently fails in CI for reasons unrelated to real regressions (timing, shared state, environment) is worse than no test at all, because it teaches engineers and agents to ignore red builds. A QA agent consuming `coverage_for`/`test_plan` today has no way to distinguish a solid passing test from a coin-flip one; both simply appear as "covers this component."

KC already collects PR and commit metadata via the forge collector (`collectors/forge.py`) and Jira issues via `collectors/jira.py` (ir.md §2.3). CI run results (pass/fail per test invocation over time) are a comparable, forge/CI-API-sourced, deterministic data source that isn't currently ingested.

## Decision Drivers

- Determinism (this must be a fact derived from real CI history, never an LLM guess)
- Accuracy (a "covered" signal that includes untrustworthy tests is misleading)
- Maintainability (must not require the compiler to execute the target repo's tests itself)
- Extensibility (fits the existing collector/fact-type/plugin pattern, ADR-006/ADR-007)

## Considered Options

### Option A: New deterministic fact type ingested from CI provider APIs, aggregated into a flakiness attribute (recommended)

Add `test_run_observed` (deterministic fact type, ir.md §2.3-style: test node id, run outcome, run timestamp, CI job ref) collected from the CI provider's API (e.g. GitHub Actions' check-run/workflow-run APIs, mirroring how `pr_observed` is already sourced from the forge API). Normalize aggregates a rolling window of these into a flakiness attribute on the corresponding `Test Coverage` entity's payload (e.g., fraction of runs with a non-deterministic outcome over the last N runs on unchanged code). Surfaced in `coverage_for`/`test_plan` output.

Pros: fully deterministic, no LLM involved; reuses the existing collector/plugin pattern (ADR-007) rather than inventing a new stage; the aggregate is an ordinary payload field, no new relationship semantics.
Cons: requires a new collector (or an extension to `collectors/forge.py`) per CI provider — GitHub Actions first, others as additive plugins, same posture as language analyzers (ADR-006).

### Option B: External tool integration, no compiled entity

Point users at an existing flaky-test-detection tool (e.g., a CI dashboard plugin) instead of compiling the signal into KC's own knowledge base.

Pros: zero KC-side engineering.
Cons: the signal would have no provenance, no slug, and wouldn't be queryable via `kc serve`'s MCP tools — an agent would have to separately integrate with whatever external tool was chosen, defeating the "one compiled surface" value proposition this whole knowledge base exists to provide.

### Option C: Derive flakiness by having the compiler re-run the target repo's tests during compile

Detect flakiness directly by re-executing tests multiple times as part of compilation.

Pros: doesn't depend on CI provider API availability or history retention.
Cons: KC's compiler has never executed target-repo code at compile time — Collect/Extract/Normalize/Diff/Persist all operate on already-produced artifacts (git history, PR/Jira metadata, source text for parsing), never by running the target repo (mutation testing itself runs in a *separate* CI workflow, not inside `kc compile`, per the eval-methodology brainstorm's explicit "CI-only execution was always the plan" note). Adding test execution inside the compile pipeline is a materially different and riskier operating model (arbitrary target-repo code execution, environment/dependency requirements) than anything the pipeline currently does, and duplicates work CI already performs.

## Decision

**Option A.** Ingest CI run history as a new deterministic fact type via a CI-provider collector plugin, aggregated into a flakiness attribute on Test Coverage entities. GitHub Actions is the first supported provider, consistent with the project's existing GitHub-first posture (`GITHUB_TOKEN`/`reconcile` already assume GitHub as the reference forge).

## Consequences

### Positive

- Gives `coverage_for`/`test_plan` a trustworthiness dimension, not just an existence dimension — a QA agent can deprioritize writing new tests around a component whose existing coverage is already known-flaky, and instead flag the flaky test itself for stabilization.
- Fits the existing collector/plugin/fact-type extension pattern exactly (ADR-006, ADR-007) — no new architectural mechanism, just a new plugin and a new (additive, non-breaking per ir.md §5) fact type.
- Fully deterministic; no LLM cost or validation surface.

### Negative

- Requires CI-provider API access and sufficient run-history retention on the provider side; repos on CI systems with short retention windows or private/inaccessible CI APIs get a degraded (or absent) signal, similar in spirit to the existing "no GITHUB_TOKEN → reconcile unavailable" degradation.
- A new collector is new surface area to maintain per CI provider, same maintenance cost class as adding a new language analyzer.

### Tradeoffs Accepted

- V1 scope is GitHub Actions only; other CI providers (CircleCI, Jenkins, GitLab CI) are explicitly deferred as additive plugins, not built speculatively — same "don't build for unverified need" discipline as ADR-016's Java-analyzer scoping.

## Failure Modes

- Sparse run history (a new test with only 1–2 runs) should not be scored as flaky or stable — the aggregate must be reported with a confidence/sample-size qualifier, or withheld below a minimum run count, to avoid a misleadingly confident signal from too little data.
- A test that fails consistently (not flakily) is a different condition than flakiness (intermittent) and must not be conflated with it — consistent failure means the test (or the code) is broken, which is a build failure the team already sees; this ADR's signal is specifically about *inconsistent* outcomes on unchanged code.

## Assumptions

- The CI provider's API exposes enough per-test (not just per-job) granularity to attribute pass/fail to individual test node ids, not just whole workflow runs. If GitHub Actions' check-run granularity turns out to be coarser than assumed, this ADR's fact shape may need revision before implementation — flagged as an implementation-time verification, not assumed proven here.

## Open Questions

- What rolling window size (number of runs, or a time window) best balances signal freshness against sample-size confidence? Needs dogfood tuning, same as ADR-004's threshold-tuning precedent.
- Should a flakiness score above some threshold ever gate `kc validate-test`'s exit code, or remain purely advisory in `coverage_for`/`test_plan`? Recommend advisory-only for V1, consistent with ADR-012's and ADR-018's measure-first-gate-later stance.

## Impact

Affected documents:
- ir.md §2.3 (new fact type `test_run_observed`, additive/non-breaking per §5)
- data-model.md (new payload field on `entities` for Test Coverage rows; no new table required)
- pipeline.md (Collect: new CI-provider collector plugin; Extract: aggregation into the flakiness attribute)

Affected compiler stages: Collect, Extract, Normalize (aggregation), Emit (surfaced on Test Coverage wiki pages and MCP tools).

## Alternatives Rejected

Option B was rejected because it would leave the signal outside KC's compiled, provenance-bearing, queryable surface — the same reasoning that keeps KC from just pointing users at generic RAG tools (vision.md's "not another RAG system" framing). Option C was rejected because it would require the compiler to execute target-repo code, a materially different and riskier operating model than anything the pipeline does today, and duplicates what CI already does.

## Future Reconsideration

Add CI providers beyond GitHub Actions only once real demand appears (dogfood or adopter request), per the same discipline ADR-016 applied to Java language support — do not build multi-provider abstraction speculatively.

## References

- ADR-006 (language-analyzer plugin pattern this collector extension mirrors), ADR-007 (plugin architecture, activation discipline), `collectors/forge.py` (existing GitHub-sourced collector this extends)
- `BRAINSTORM-test-generation-eval.md` (the CI-only-execution precedent this ADR's Option C rejection cites)
