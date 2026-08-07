# Plan: QA-agent substrate — from coverage tests to meaningful tests
2026-08-07 · Mode: Build plan · Status: Proposed, unstarted

## Problem

vision.md's north star is "agents generate implementation-aware regression tests
from compiled business rules, API contracts, coverage gaps, and recent deltas."
Today the compiler ships `test_plan`/`impact_plan`/`kc validate-test` and an LLM
semantic layer producing business rules — but a review of the actual data path
finds the two are **structurally disconnected**, and separately that the
compiled graph **cannot represent a user journey at all**.

Two root causes, both structural rather than tuning problems:

**RC-1 — Business rules never reach the test path.**
`impact_plan` builds `candidate_targets` from `s.startswith("component/")` only
(`mcp/queries.py`). A `business_rule` participates solely as an inbound
`governs` edge marking a component "affected"; the rule itself can never be a
target. `test_plan` emits only `target_kind: "api" | "symbols"`.
`citable_targets_for` maps api→API slugs and everything else→the component
slug, so a `kc-covers:` header **can never legally cite a business rule**.
`coverage_for` counts `covers` edges into components. Net: rules are compiled,
rendered to the wiki for humans, and discarded by the QA path. The planner's
only vocabulary is "this component has no test attached" — which is precisely
coverage theater.

**RC-2 — The graph is two disconnected islands.**
Frontend components and backend APIs are both entities, but the only linking
fact is `dependency_observed` (dotted **import** paths, per `normalize.py`).
A React component never *imports* a Flask route — it calls it over HTTP, which
nothing observes. Verified absent in `extractors/`: no `data-testid`/selector
extraction, no `fetch`/`axios` call-site extraction, no frontend routing.
Route detection is server-side only. There is therefore no path
`screen → API → business rule` in the graph — and that path *is* a user
journey. Compounding it, the extraction prompt is explicitly per-file ("ONE
source file"), so a `Feature` is file-shaped; 517 features on repoA are file
summaries, not journeys, and no aggregation pass exists.

**RC-3 — The objective function rewards the wrong thing.**
`validation.score_test` computes precision/recall over *slug citations*. It
measures obedience to the target list, not whether the test asserts anything.
A file containing `assert True` with a correct header scores 100%. The Redo-2
spike scored 100.0% for a UI test asserting that a component mounts.

Notably, **ADR-012 already predicted this**: its recorded revisit trigger is
"pattern of ≥80% declared-coverage + ≤40% mutation-kill at scale." Nothing has
ever measured that pair. Phase 1 below is the instrument that does.

## Constraints & assumptions

- **Option B holds** (BRAINSTORM-test-generation-mechanism.md): KC does not
  generate test code. It plans, records, and scores; authorship stays with the
  consuming agent. Nothing in this plan changes that, and the plan is *more*
  dependent on it — the deliverable is a better substrate, not a generator.
- **Architecture v1.0 is frozen.** All entity/relationship/fact additions here
  are additive per ir.md §5; each phase that introduces a real decision lands a
  new ADR. ADR-012 is *superseded*, not amended (see Phase 2).
- **Deterministic-first (vision DP 3).** Structure is derived deterministically;
  the LLM names and narrates. Where the LLM asserts fact (obligations), a
  deterministic cross-check against anchored source gates persistence — the
  ADR-008 validation invariant extended from *shape* to *substance*.
- **normalize.md §9 determinism checklist is the review gate** for every
  Normalize change: pure function, sorted iteration, no clock, no randomness.
- **Low migration friction (confirmed):** `EntityRow.entity_type` and
  `RelationshipRow.relation_type` are plain strings with JSON payloads, so new
  entity and relation types need **no schema migration** — only new columns
  would. This materially lowers the cost of Phases 2–5.
- **Testing conventions:** no mocks; real git/Postgres/tree-sitter;
  `FakeLLMProvider`/`FakeEmbedder`; unique `model_id` per cache-asserting test.

## Phase sequence

Ordered so each phase is independently shippable and **measurable before the
next one starts**. Phase 1 is first because without it there is no way to tell
whether Phases 2–5 improved anything.

| # | Phase | Delivers | ADR | Effort |
|---|---|---|---|---|
| 1 | Honest objective function | Anchor-scoped mutation-kill in the loop; baseline measurement | ADR-017 | 3–5 d |
| 2 | Obligations | Rule-level testable semantics + rule-shaped coverage | ADR-018 (supersedes ADR-012) | 2–3 wk |
| 3 | The `calls` edge | Joins the two islands; journeys become representable | ADR-019 | 1–2 wk |
| 4 | Screens + selectors | Entry points and locators for e2e agents | ADR-020 | 1–2 wk |
| 5 | Journey entities | Journey coverage metric | ADR-021 | 2–3 wk |

Realistic solo-maintainer total: **~1 quarter.** Phases 1–2 alone deliver the
"meaningful unit/integration tests from business rules" outcome; Phases 3–5 are
what unlock user-journey testing.

---

### Phase 1 — Make the objective function honest (do this first)

**Why first:** it requires no new extraction, and it produces the baseline
number that justifies or kills every later phase. It also directly measures
ADR-012's own revisit trigger.

**Build:**
- Extend `kc validate-test` with a behavioral tier alongside the existing
  citation tier:
  - **Anchor-scoped mutation-kill.** Reuse the existing mutmut workflow, but
    scope `only_mutate` to the anchor spans of the entity under test rather
    than a whole module. Anchors already carry `file_path`/`symbol_path`/`span`.
  - **Assertion-presence check (deterministic).** Parse the test AST; a test
    claiming a slug but containing no assertion against that target's
    observable surface is flagged.
- Report both tiers separately. **Do not merge them into one score** — citation
  precision/recall answers "did it aim correctly," mutation-kill answers "did it
  verify anything." Collapsing them hides exactly the pathology being hunted.
  The full before/after model is Appendix A.

**Granularity correction (ADR-012's second trigger).** ADR-012 records a second,
easier-to-fire revisit trigger alongside the ≥80%/≤40% statistical pattern:

> any concrete case where a component clears the mutation-kill bar in aggregate
> but a specific, known-important condition within it is demonstrably never
> exercised by any test

and states the underlying mismatch directly — mutation-kill is measured at
*component* granularity while the gap is *sub-component*. A single documented
case fires this trigger, so it is likely to fire before the statistical pattern
does. **Phase 1 must therefore scope mutants to the obligation/condition anchor
span from the start, not to the component.** Building Phase 1 at component
granularity would reproduce the exact blind spot ADR-012 warned about and would
make the Phase 2 A/B unmeasurable.

**Exit criteria:** the declared-coverage / mutation-kill pair is computed for
every existing `kc-covers`-tagged test in the repoC Playwright suite (~30
headers, 76 slugs per the spike record). If the pattern is ≥80%/≤40% — or if a
single concrete sub-component miss is documented — ADR-012's revisit trigger has
fired *with evidence*, and Phase 2 is justified rather than assumed.

**Risk:** mutation testing on a Playwright/e2e suite is slow and may be
impractical per-run; if so, scope it to unit/integration tests and treat e2e
adequacy as a Phase 5 problem. Record the limit rather than silently narrowing.

---

### Phase 2 — Obligations: make rules testable, and make coverage rule-shaped

**Build, in order:**

1. **Template v2 (`llm/templates.py`, bump `TEMPLATE_VERSION` → `"2"`).**
   Replace the prose-only `BusinessRuleOut.statement` with structured
   obligations (statement retained for wiki prose):

   ```python
   class Obligation(BaseModel):
       given: str; when: str; then: str        # observable outcome, not implementation
       kind: Literal["boundary", "invariant", "error",
                     "permission", "calculation", "state_transition"]
       concrete_values: list[str]               # literals the code actually uses
       observable_via: Literal["api", "symbol", "ui", "unspecified"]
   ```

   A rule that cannot be phrased as given/when/then usually is not a rule —
   this is a free quality filter on the weakest layer. Version bump produces
   cache misses exactly where intended (ADR-008).

2. **Deterministic literal cross-check (`extractors/llm_extractor.py`).**
   Every `concrete_values` entry must appear as a literal in the rule's
   anchored source span, else the obligation is rejected or flagged
   `unverified_values`. This is the load-bearing piece: it extends ADR-008's
   "validated before persisted" gate from schema shape to factual substance,
   and it is what keeps deterministic-first intact as semantics move onto the
   critical path.

3. **`test_verifies_observed` (new deterministic fact type, `ir.py`).**
   Parse `kc-covers:` headers *at Collect/Extract time* in test files.
   Today the header is write-only — consumed by `validate-test`, never
   compiled. Making it an input turns declared coverage into a queryable
   compiled fact and yields the `verifies` edge below.

4. **`verifies` relationship** (`test_coverage → business_rule`), alongside
   `covers`. Register in ir.md §3.3. No migration needed.
   **Cross-repo caveat (real, must be designed not discovered):** tests live in
   repoC, rules in repoA, and `RelationshipRow` is repo-scoped. In-repo
   `verifies` is a compiled edge; cross-repo must be a query-time join, as
   ADR-011 does for dependencies. See Phase 3 for why the join key cannot be
   the import prefix.

5. **Obligation coverage (`mcp/queries.py`).** Extend `coverage_for` to report
   obligation-level coverage. Headline metric: **a component at 100%
   component-coverage can sit at 0% obligation-coverage.** This is the
   anti-theater number, and it also fixes today's gaming surface where one
   trivial smoke test flips `covered: true`.

6. **`target_kind: "business_rule"` in `test_plan`,** ranked first; api/symbols
   become the fallback for components with no compiled rules.
   `citable_targets_for` gains the rule branch so headers can legally cite
   rules.

7. **Ranking from signals already present** — pure composition, no new
   infrastructure, the same move `impact_plan` made:
   `affects` edges from Risk · `DeltaChangeRow` churn on the rule's anchored
   symbols · `depends_on` fan-in · `observable_via`.

8. **Supersede ADR-012.** The structured Obligation *is* the
   `VerificationRequirement` entity ADR-012 deferred; it was deferred for want
   of a consumer, and item 6 is that consumer. Write ADR-018 as superseding,
   per the immutability rule — do not amend ADR-012.

**Exit criteria:** re-run the spike arc as a controlled A/B —
**rule-targeted vs. component-targeted tests, anchor-scoped mutation-kill as
the dependent variable.** Phase 2 is justified only if rule-targeted tests kill
measurably more mutants. Also report: obligations extracted, % passing the
literal cross-check (the semantic layer's first real quality number).

**Risk:** this deepens dependence on the least-validated layer. Item 2 is the
mitigation and is not optional.

---

### Phase 3 — `http_call_observed`: join the two islands

The smallest change that makes journeys representable. Everything in Phases 4–5
is unreachable until this lands.

**Build:**
- **`http_call_observed`** (new deterministic fact, TS/JS analyzers first):
  extract `fetch('/api/claims')`, `axios.post(...)`, and generated-client calls
  via tree-sitter. Payload: `{method, route, raw_route, from_symbol, file,
  client_kind, confidence}`. Normalize the path with the existing
  `normalize_route()` so `/users/{id}` and `/users/<uid>` collapse identically
  on both sides of the wire — the join works because both halves already share
  one canonicalizer.
- **`calls` relationship**: `component → api`, resolved by normalized route +
  method. Follow ADR-004's bias: ambiguous or dynamically-constructed URLs stay
  unresolved and visible rather than being guessed.
- **Cross-repo join key.** ADR-011's `[dependencies]` map resolves by *import
  prefix*; a Playwright repo imports nothing from the app repo, so
  `resolve_dependency` structurally cannot bridge repoC→repoA — a genuine gap
  in ADR-011 for the primary QA use case. The e2e join key must be
  **URL/route**, not import path. ADR-019 records this.
- Extend `impact_plan` to traverse `calls`, enabling "which screens does this
  backend change break?"

**Exit criteria:** ≥X% of repoA's frontend API usage resolves to a compiled API
entity (set X from a manual count on one module first, so the target is
grounded); `impact_plan` answers one real screen-level question end to end.

**Risk:** dynamic URL construction (template literals, base-URL vars) will
resist static resolution. Report resolution rate as a first-class number —
silent partial coverage here would poison every downstream journey claim.

---

### Phase 4 — Screens and selectors

- **`screen_observed`**: React Router / Next.js file-based routing → entities
  carrying a real URL path. Gives entry points and navigation targets.
- **`selector_observed`**: `data-testid` / role attributes from JSX via
  tree-sitter, anchored with provenance. Highest practical value per unit of
  effort — it removes the most tedious, most drift-prone part of Playwright
  authoring. repoA already maintains `internal-docs/data-testid-conventions.md`
  **by hand**, which is the signal that this is wanted and currently uncompiled.

**Scope warning (deliberate, needs ADR-020 to own it):** this is the first time
KC needs **framework** analyzers rather than **language** analyzers (React
Router vs. Next.js; fetch vs. axios vs. generated clients). That is a real tax
on the "two languages to keep the plugin interface honest" minimalism from
vision.md. Decide it explicitly; do not let it arrive as drive-by scope creep.

---

### Phase 5 — Journey entities and the metric that matters

- **`journey` entity, assembled deterministically** — a new Normalize pass
  (same shape as P6 wiki-page derivation): enumerate paths from a `Screen`
  through `calls → api → governs → business_rule`. Structure is deterministic;
  the **LLM only names and narrates** the path. Never let the LLM invent
  journeys.
- **Seed and validate from two sources already collected:** existing Playwright
  specs (reverse-compiled into journeys) and Jira stories (the collector
  already mints these with `motivates` edges — a story usually *is* a journey).
  Agreement between the two is the correctness check.
- **The metric nobody else reports:** *obligations reachable in a journey but
  never verified end-to-end.* A rule may be unit-covered in isolation while no
  user-reachable path exercises it. This is the strongest single output of the
  whole plan.

---

## Explicitly not building

Recorded to prevent re-litigation:

- **In-process test generation** — Option B stands (ADR-008's content-addressed
  cache is actively wrong for regeneration: same input silently returns the
  same "new" attempt).
- **Auth/permission and test-data/fixture entities** — real journey needs, but
  defer until Phase 5 produces evidence of exactly what shape is required.
- **Transitive impact** — `impact_plan` stays one-hop until Phase 3 shows the
  `calls`-extended graph is trustworthy; a wrong blast radius is worse than a
  narrow one.
- **Auto-suppressing dead-code gaps before Phase 3** — the reachability signal
  (zero inbound `depends_on`, no route, no entrypoint) is only sound once
  `calls` edges exist; suppressing on the import graph alone would hide live
  UI-reachable code.

## Cross-cutting risks

1. **Evidence base is n≈1.** The spike record is one repo, few runs, private
   code, and already contains one fabricated result caught on re-verification.
   Before Phase 2 commits, build a labeled corpus on both dogfood repos: ~30
   obligations, human-judged "does this test meaningfully verify it," and check
   whether anchor-scoped mutation-kill correlates with that judgment. If it
   does, that is the first real measurement of "meaningful test" in this
   category — and the most defensible thing this project would own.
2. **Solo maintainer, ~1 quarter of work.** Phases 1–2 are the value floor;
   if bandwidth collapses, stopping after Phase 2 still leaves a coherent,
   shipped improvement rather than a half-built journey model.
3. **Framework sprawl** (Phase 4) is the most likely source of unbounded scope.
4. **Determinism regressions**: every Normalize change re-runs the §9
   checklist and `kc verify` must stay clean across all dogfood repos.

## Priority build list

Ordered by dependency, then by leverage. `B5` and `B4` are **kill gates** — do
not start tier 2 before both have answered. Effort is solo-maintainer days.

### Tier 0 — Instrument before building (no new extraction required)

| ID | Item | Files | Dep | Eff |
|---|---|---|---|---|
| B1 | Anchor-scoped mutmut harness: scope `only_mutate` to an anchor span, not a module | `.github/workflows/mutation-test.yaml`, new `validation/mutation.py` | — | 2 d |
| B2 | Two-tier report shape: split targeting/verification, add `score_version`, product composite | `validation.py`, `cli.py` | — | 1 d |
| B3 | Assertion-presence check (test-body AST; no obligations needed) | `validation.py` | B2 | 1 d |
| B4 | **GATE —** baseline run over repoC's ~30 `kc-covers` tests; record the declared-coverage / mutation-kill pair | — | B1–B3 | 1 d |
| B5 | **GATE —** hand-label ~30 existing repoA rules: "does this test meaningfully verify it?"; correlate with B1's kill rate | — | B1 | 2–3 d |

`B5` is the highest-value item in the entire plan and needs **no new code** —
repoA's 50 business rules already exist as prose. It answers the question every
later phase assumes: *is anchor-scoped mutation-kill actually a proxy for
"meaningful"?* If correlation is weak, the objective function is wrong and
tiers 2–5 are built on sand. Stop and rethink rather than proceeding.

### Tier 1 — Cheap wins unlocked by the new report

| ID | Item | Files | Dep | Eff |
|---|---|---|---|---|
| B6 | Tag existing targets `observable_via` (api-kind→`api`, symbols-kind→`symbol`); recall denominator → reachable subset | `queries.py`, `validation.py` | B2 | 1 d |
| B7 | Dead-code/reachability flag on gaps (import-graph only; provisional until B16) | `queries.py` | — | 1 d |

`B6` retires the known 86.1%-ceiling artefact immediately — the number stops
needing a footnote. Both items ship value before any ADR is written.

### Tier 2 — Obligations (ADR-018)

| ID | Item | Files | Dep | Eff |
|---|---|---|---|---|
| B8 | ADR-018, superseding ADR-012 (do not amend ADR-012) | `docs/decisions/` | B4, B5 | 1 d |
| B9 | Template v2: `Obligation` schema + prompt; bump `TEMPLATE_VERSION` | `llm/templates.py` | B8 | 2 d |
| B10 | Deterministic literal cross-check against anchored source | `extractors/llm_extractor.py` | B9 | 2–3 d |
| B11 | Obligations into `business_rule` payload; re-run §9 determinism checklist | `compiler/normalize.py` | B9 | 2 d |
| B12 | `test_verifies_observed` — parse `kc-covers:` at Extract time | `ir.py`, `extractors/*` | — | 2 d |
| B13 | `verifies` edge (`test_coverage → business_rule`); register in ir.md §3.3 | `normalize.py`, `docs/ir.md` | B12 | 1 d |
| B14 | Obligation coverage in `coverage_for` | `queries.py` | B13 | 1 d |
| B15 | `target_kind: "business_rule"` in `test_plan`; rule branch in `citable_targets_for` | `queries.py`, `validation.py` | B14 | 2 d |
| B16 | Ranking: risk `affects` · `DeltaChangeRow` churn · fan-in · `observable_via` | `queries.py` | B15 | 2 d |
| B17 | Boundary-literal check (`concrete_values` present in test body) | `validation.py` | B9, B3 | 1 d |
| B18 | **A/B —** rule-targeted vs component-targeted tests, mutation-kill as dependent variable | — | B15, B17 | 2 d |

### Tier 3 — The `calls` edge (ADR-019)

| ID | Item | Files | Dep | Eff |
|---|---|---|---|---|
| B19 | ADR-019: `calls` edge + URL-keyed cross-repo join (records the ADR-011 gap) | `docs/decisions/` | — | 1 d |
| B20 | `http_call_observed` fact: `fetch`/`axios`/generated clients via tree-sitter | `ir.py`, `extractors/typescript_analyzer.py`, `javascript_analyzer.py` | B19 | 4–5 d |
| B21 | `calls` resolution through `normalize_route()`; ambiguous URLs stay unresolved | `normalize.py` | B20 | 2 d |
| B22 | Resolution-rate reporting (first-class honesty number) | `cli.py`, `queries.py` | B21 | 1 d |
| B23 | `impact_plan` traverses `calls` | `queries.py` | B21 | 1 d |
| B24 | Cross-repo URL join (repoC↔repoA); supersedes B7's provisional reachability | `queries.py` | B21 | 2 d |

### Tier 4 — Screens and selectors (ADR-020)

| ID | Item | Dep | Eff |
|---|---|---|---|
| B25 | ADR-020 — owns the language-analyzer → **framework**-analyzer scope decision | B19 | 1 d |
| B26 | `selector_observed` (`data-testid`/roles from JSX) — highest value per unit effort | B25 | 3 d |
| B27 | `screen_observed` (React Router / Next.js routing) | B25 | 3–4 d |

### Tier 5 — Journeys (ADR-021)

| ID | Item | Dep | Eff |
|---|---|---|---|
| B28 | ADR-021 + deterministic journey-derivation pass (LLM names only) | B23, B27 | 5 d |
| B29 | Seed/validate from Playwright specs + Jira stories; agreement = correctness check | B28 | 4 d |
| B30 | Metric: obligations reachable in a journey but never verified end-to-end | B29 | 2 d |

### Ordering rationale

- **Tier 0 is not optional and not deferrable.** Every later claim is measured
  against B4's baseline; without it there is no way to tell whether any of this
  worked.
- **B5 before B8.** Validate the metric before designing a schema around it.
- **B6/B7 early** — they cost 2 days total and retire two known artefacts that
  currently make the tool's output confusing to read.
- **Tier 2 is the value floor.** If bandwidth collapses after it, the result is
  a coherent shipped improvement, not a half-built journey model.
- **B20 is the single riskiest item** (dynamic URL construction). Its
  resolution rate gates whether tiers 4–5 are worth starting at all.

## Appendix A — Scoring model, before and after

**Today** (`validation.py`): the file is read, but the AST is parsed *only* to
extract the module docstring (`:105-107`). The test body is never inspected and
nothing is executed.

```python
precision = |claimed ∩ citable| / |claimed|
recall    = |claimed ∩ citable| / |citable|
score_pct = ((precision + recall) / 2) * existence_penalty * 100
```

This answers exactly one question — *did the agent cite what `test_plan`
recommended?* It is structurally incapable of detecting whether the test
asserts anything.

**After** — three independent signals, deliberately not collapsed:

| Tier | Question | Cost | Source |
|---|---|---|---|
| Targeting | Did it aim at the right things? | cheap, static | today's P/R, denominator fixed (B6) |
| Verification | Did it actually verify them? | expensive | boundary literals (B17) + anchor-scoped mutants (B1) |
| Obligation coverage | What fraction of rules are verified at all? | cheap, query | `verifies` edges (B13) |

**Worked example — ADR-012's own motivating case** (`ADR-012:14`): a test
claiming `component/billing-rules` that calls `apply_discount(5)` and never
exercises the 20% cap.

| | Today | After |
|---|---|---|
| Targeting | **100%** | 100% (correctly aimed) |
| Boundary literals | not checked | **FAIL** — no `20`/`0.2` in test |
| Mutation-kill (cap branch) | not run at validate-test time | **0%** |
| Obligation coverage | not representable | **0/1** |
| Verdict | *passes* | *flagged* |

ADR-012 accepted this as a consequence of deferral: "the cap-boundary miss can
score 100% declared-coverage and 0% mutation-kill; the feedback loop closes at
CI, not at `validate-test` time." The change in one line: **that failure moves
from undetectable-until-CI to detectable at `validate-test` time, at the
granularity of the condition itself.**

**Composite must be a product, not an average.** Today P and R are averaged.
Averaging *targeting* with *verification* would reintroduce the pathology — a
perfectly-cited test asserting nothing would score ~50% and read as mediocre
rather than broken:

```
score = targeting × verification      # verification = 0  ⟹  score = 0
```

Average hides; product punishes. That choice is what makes the score
un-gameable by citation alone.

**Two operational decisions:**

1. **Do not gate on the behavioral tier initially.** Keep exit-1 for the two
   objective conditions it has today (missing header, nonexistent slug) and
   ship mutation-kill as reporting-only until B4's baselines exist. Gating on
   an unbaselined metric blocks every test in the repo on day one, and matches
   the eval brainstorm's existing "no hard gate yet" stance.
2. **Stamp `score_version`.** The spike record's numbers (100.0%, 86.1%, 27.8%)
   are computed under the current formula and become non-comparable once the
   denominator and composite change — the same class of error the retracted
   spike-2 entry is kept in the record to prevent.
