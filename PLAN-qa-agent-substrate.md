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

**Exit criteria:** the declared-coverage / mutation-kill pair is computed for
every existing `kc-covers`-tagged test in the repoC Playwright suite (~30
headers, 76 slugs per the spike record). If the pattern is ≥80%/≤40%, ADR-012's
revisit trigger has fired *with evidence*, and Phase 2 is justified rather than
assumed.

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

## First three concrete steps

1. Instrument Phase 1's two-tier report and run it over the existing repoC
   suite — get the declared-coverage / mutation-kill pair on real tests.
2. Hand-label ~30 obligations on repoA and measure correlation (risk 1).
3. Only then write ADR-018 and start template v2 — with the baseline in hand,
   so the A/B in Phase 2's exit criteria has something to compare against.
