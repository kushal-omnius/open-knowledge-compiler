# Brainstorm: Test-generation evaluation methodology
2026-07-20 · Mode: Exploration

## Problem

`vision.md:161` gates milestone 3 (test generation) on an unanswered question: *"what does 'better test cases' mean measurably?"* `decisions/index.md:152` lists it as a pre-milestone-3 design doc, and a related unresolved idea — a "Verification Requirement" entity (business rules decomposing into durable, testable obligations) — sits at `decisions/index.md:159`, explicitly deferred to this same design. This document defines the evaluation methodology itself — not the test-generation mechanism — so that milestone 3 has a target to build toward instead of an open-ended "generate good tests" brief.

## Constraints & assumptions

**Hard constraints:**
- Real dogfood repos (frida, omnius_llmlib) are the evaluation target, not a synthetic benchmark.
- Both automated and human-review evaluation capacity exist; automated should carry most of the load (per the answered questions — safety-net + coverage-gap are the ranked priorities, both automatable in principle).
- Provenance is non-negotiable (vision DP 4) — a generated test's claims about what it covers must be checkable against the compiled KB, not just asserted.

**Grounding from `frida-testing/playwright`** (scoped narrowly, per correction — not the parent `frida-testing` workspace, which covers unrelated LLM-evaluation tooling):
- Test files declare a structured docstring header enumerating the **exact endpoints they cover** (method + path) and cite the **specific backend source file/permission model** behind assertions (e.g. `test_claims.py`'s header lists all 14 `/api/claims/*` endpoints and notes "see `backend/claims/routes.py`" for permission behavior).
- Timeouts are always explicit, sized and commented to the endpoint's actual behavior (DB-backed vs. LLM-backed) — not guessed.
- The `api`/`ui`/`e2e` tier split exists, but `api` being more built-out so far is a **pilot-stage fact, not a stated team preference** (corrected during this session — do not treat it as a design principle).
- This suite tests a **live app instance** via Keycloak auth against a configured environment (`localhost`/`pr`/`qa`/`stg`) — real or locally-running claim data, not something Knowledge Compiler's compiler process itself has access to (no browser automation, no Keycloak credentials, no live claim IDs in the compile pipeline).

**What this grounding actually tells us:** the team's own bar for "this test is well-formed" is **declared, structured traceability to the exact real thing being verified** — not vibes, not raw prose. `test_claims.py`'s "Endpoints covered:" header is doing, by hand, almost exactly what Knowledge Compiler's `api` entities (`method`, `route`, `handler`, `defined_in`) already represent as compiled, machine-readable state. That's the concrete anchor point for a measurable definition of "better."

**Assumption made without asking:** milestone-3 test generation, if built, most plausibly targets source-level tests colocated with frida's/omnius_llmlib's own repos (the same tier as `backend/tests/test_claims.py`, which Knowledge Compiler already compiles as `test_coverage` entities) — not the separate live-environment Playwright suite, which needs infrastructure (deployed app, Keycloak, real claim data) outside anything the compiler touches. If this assumption is wrong, several options below (especially the mutation-testing ones) need rethinking, since mutation testing requires running the target code directly.

**Success criterion:** a methodology exists such that, given a set of Knowledge-Compiler-generated tests, a single measurable score (or small vector of scores) can be computed largely without manual review, and that score should correlate with whether the tests would actually catch a real regression.

## Options

### Option A: Do nothing (defer past this brainstorm)

**Sketch:** Leave the eval-methodology question open; keep milestone 3 blocked until someone tackles it under deadline pressure later.

**Pros:** Zero cost now.

**Cons:** This is the literal, explicit gate on milestone 3 (`vision.md:161`) — deferring doesn't reduce scope, it just delays the one prerequisite already identified as necessary.

**Pre-mortem:** Six months on, someone starts building a test generator with no defined success metric, ships something that "looks like tests," and there's no way to say whether it's actually good — the same failure mode a rushed cross-repo design would have hit before ADR-011.

**Reversibility:** Two-way — trivially reversible, it's inaction.

**Effort:** None now.

---

### Option B: Declared-coverage accuracy only (simplest thing that works)

**Sketch:** Require every generated test to carry a structured header (docstring or comment block) naming the exact compiled entity slugs it targets — `api/post-claims`, `component/backend-claims-routes`, `business_rule/...` — mirroring `test_claims.py`'s "Endpoints covered:" convention but machine-parseable. The entire evaluation is: parse that header, check every cited slug actually exists in the compiled KB and is actually exercised by the code the test calls (e.g. the test's HTTP calls hit routes matching the cited `api` entities' `method`+`route`). No execution-based signal, no mutation testing — purely structural.

**Pros:** Cheap, fully automatable, directly reuses the exact convention already observed in real code (`test_claims.py`), which is unusually strong precedent to build from. Fast to ship.

**Cons:** Says nothing about whether the test *actually verifies* the cited behavior correctly — a test could declare accurate coverage and still assert something trivial or wrong (`assert response.status_code in (200, 201, 204, 400, ...)`-style laziness would pass this check).

**Pre-mortem:** Six months on, generated tests all pass this metric with perfect declared-coverage accuracy, dashboards look great, and then a real regression ships anyway because none of the "covering" tests actually asserted anything meaningful about the behavior — the metric measured bookkeeping, not quality.

**Reversibility:** Two-way — it's a structural check that can be extended or replaced without touching generated-test content already produced.

**Effort:** Hours to define the header format and write the KB-crosscheck script.

---

### Option C: Declared-coverage + mutation-kill rate + periodic human spot-check (recommended)

**Sketch:** Extend Option B with an execution-based signal: for each generated test, run mutation testing (e.g. `mutmut` for Python, since frida's backend and omnius_llmlib are both Python — flagged as an assumption pending a real tooling check, not verified fresh this session) scoped to *only* the source file(s) the test's declared header cites. A test "passes" quality-wise if (a) its declared coverage is accurate (Option B) and (b) it kills a meaningful fraction of mutants introduced specifically in the cited entity's implementation. A small, periodic human-review sample (mirroring the "spot-check" capacity you said exists) checks for the failure mode Option B alone can't catch — a test that's structurally honest and kills mutants, but is still testing the wrong thing in spirit (e.g. asserting an implementation detail rather than the actual business rule).

**Pros:** Directly answers both ranked priorities (safety-net via mutation-kill rate, coverage-gap via the declared-coverage cross-check against `coverage_for`'s existing uncovered-component list) with a composite score, not just one axis. Mutation-scoping to only the cited entity keeps the expensive part (running mutation testing) bounded instead of running it over entire files/repos.

**Cons:** More moving parts than B — needs a mutation-testing tool wired into an eval harness, plus the human-sampling process needs a lightweight rubric so different reviewers don't drift.

**Pre-mortem:** Six months on, mutation-kill rate becomes the dashboard number everyone chases, and generated tests start over-fitting to kill mutants narrowly (assert exact internal values) rather than testing the business rule robustly — the same "metric becomes the target" risk any composite score carries; the human spot-check is exactly the check meant to catch this, so its cadence matters as much as its existence.

**Reversibility:** Mostly two-way — the mutation-testing harness and header format are both additive/replaceable.

**Effort:** Low-days (mutation-tool wiring, scoping logic, header-to-file mapping, a short human-review rubric).

---

### Option D: Buy/adopt — generic mutation-testing score only, no KC-specific traceability convention

**Sketch:** Skip inventing a Knowledge-Compiler-specific declared-coverage header entirely. Just run an off-the-shelf mutation-testing tool (`mutmut`/similar for Python) against the whole repo before and after adding generated tests, and use the delta in overall mutation-kill rate as the single quality metric — the same measure generic test-suite-quality tooling already uses elsewhere.

**Pros:** Fastest to stand up if a mutation-testing tool is already usable as-is; no new convention to design or maintain; "don't build what you can adopt."

**Cons:** Loses the traceability signal entirely — can't answer "does this specific generated test verify the specific business rule it was generated for," only "did overall mutation-kill rate go up." Doesn't reuse or extend the real convention already found in `test_claims.py`, which is a missed opportunity given how directly it maps to KC's own compiled `api`/`component` entities.

**Pre-mortem:** Six months on, overall mutation-kill rate ticks up nicely, but there's no way to audit *which* generated test is responsible for *which* rule being covered — exactly the kind of untraceable "it works, don't ask why" state vision DP 4 (provenance on every answer) explicitly tries to avoid.

**Reversibility:** Two-way, but represents a genuinely different direction from B/C rather than a superset — adopting D first and adding C's traceability later is redoing work, not extending it.

**Effort:** Hours to wire the tool; ongoing tuning of what "meaningful delta" means at repo scale.

---

### Option E: Historical-bug-replay (contrarian option)

**Sketch:** Knowledge Compiler's own delta log (ADR-003) already tracks PR-level history with old→new values and `which_pr_introduced`. Identify past bug-fixing PRs in frida/omnius_llmlib's real history, check out the pre-fix commit, run a Knowledge-Compiler-generated test (written from the post-fix understanding of the relevant business rule) against the pre-fix code, and require it to **fail** there and **pass** post-fix. Success is measured as: what fraction of known historical bugs would this generated test suite have caught, had it existed at the time?

**Pros:** Uses real, already-compiled project history as ground truth instead of synthetic mutants or human judgment — arguably the most rigorous signal available, and uniquely enabled by Knowledge Compiler's own delta log (no other option in this document could reuse compiled state this directly). Directly validates the "regression safety net" priority in the most literal sense: would this actually have caught a real bug.

**Cons:** Requires identifying which historical PRs were genuine bug fixes (not every PR is) — likely needs either commit-message heuristics or a human-tagged sample, an upfront cost this document doesn't scope. Also requires being able to check out and run old commits of frida/omnius_llmlib in isolation, which needs a runnable environment for arbitrary historical revisions (dependencies, migrations, etc. as they were then) — a real operational cost, not just a query.

**Pre-mortem:** Six months on, the historical-bug corpus turns out tiny (most PRs in frida's history are features, not bug fixes) or the old-revision runnability problem (stale dependencies, broken migrations at old commits) makes replay too flaky to trust, and the option quietly degrades into "run it against the 3 bugs we could get working" — directionally right but statistically thin.

**Reversibility:** Two-way as an evaluation technique (nothing about generated tests themselves depends on it), but building the historical-replay harness is a real one-time investment that doesn't transfer if abandoned.

**Effort:** Weeks (bug-fix-PR identification, historical-revision runnability, replay harness) — the highest-effort option here.

## Comparison

Weighted by the stated priorities: safety-net + coverage-gap (both ranked highest), automated-first with human as spot-check, real dogfood repos.

| | Answers safety-net priority | Answers coverage-gap priority | Reuses real team convention | Automation-first | Effort | Reversibility |
|---|---|---|---|---|---|---|
| A. Defer | ✗ | ✗ | — | — | None | Two-way |
| B. Declared-coverage only | ✗ (no execution signal) | ✓ | ✓✓ (direct match) | ✓ | Hours | Two-way |
| **C. Declared-coverage + mutation + spot-check** | ✓ | ✓ | ✓✓ | ✓ (human as sample only) | Low-days | Mostly two-way |
| D. Generic mutation score only | ✓ | ~ (no per-rule granularity) | ✗ | ✓ | Hours | Two-way (different direction) |
| E. Historical-bug-replay | ✓✓ (most rigorous) | ✗ (not its purpose) | ~ (reuses delta log, not the test convention) | ✓ (once built) | Weeks | Two-way (technique), costly to abandon (harness) |

## Recommendation

**Option C** — declared-coverage accuracy (structural, automated) + mutation-kill rate scoped to the cited entity (execution-based, automated) + a periodic, lightly-rubric'd human spot-check.

**Confidence: medium-high.** It's the smallest methodology that answers both ranked priorities with mostly-automated signals, and it directly extends a convention already proven in real code (`test_claims.py`'s declared-coverage header) rather than inventing one from nothing. The "Verification Requirement" entity idea already flagged at `decisions/index.md:159` is effectively what Option C's declared-coverage header would formalize into compiled state, if this methodology later graduates from "test metadata" to "compiled entity" — a natural, non-forced next step, not a new invention.

**Decisive uncertainty — RESOLVED (2026-07-20):** whether `mutmut` (or an equivalent) is actually the right mutation-testing tool for this codebase. Ran the spike via a `workflow_dispatch` GitHub Actions job on `ubuntu-latest` (`.github/workflows/mutation-test.yaml`) against `knowledge_compiler/compiler/normalize.py` using `tests/test_normalize.py`'s real 232-line suite: **1080 mutants generated, 695 killed, 383 survived, 2 timeout — a 64.4% mutation score, completed cleanly with no crashes or interruption.** Two real environment findings along the way: `mutmut` 3.x has no native Windows support at all (confirmed via source, not docs — `configuration.py`/`__main__.py`), and its CLI changed completely between 2.x and 3.x (config moves to `[tool.mutmut]` in `pyproject.toml`; `mutmut html` was replaced by `mutmut export-cicd-stats`). Neither blocks Option C — CI-only execution was always the plan for the real harness, not local runs.

64.4% is a genuinely informative number (not ~100%, which would mean the tool isn't finding anything real; not ~0%, which would mean total breakage) — and it's a real finding in its own right: **35% of behavioral mutations in the identity cascade — a module the project's own docs call "the determinism checklist §9 review gate" — would slip past the current test suite undetected.** Option C's execution-based half is confirmed practical, not just theoretically sound.

**Steelman of the runner-up (Option E):** A reasonable person optimizing purely for rigor would pick historical-bug-replay — it's the only option that measures "would this have caught a real bug" directly, rather than a proxy (mutants, declared coverage). If Option C's mutation-kill signal turns out to correlate poorly with real bug-catching (discoverable only after C has been running a while), E becomes the natural escalation — not a replacement for C's traceability half, but an additional, more expensive validation layer once the cheaper option's limits are known.

## Declared-coverage header format (resolves next-step #2, decided 2026-07-20)

A generated test file carries a **module-level docstring** with a `kc-covers:` block naming the exact compiled entity slugs it claims to verify — directly extending `test_claims.py`'s real "Endpoints covered:" convention (`BRAINSTORM-test-generation-eval.md`'s own grounding), made machine-parseable instead of free prose:

```python
"""
<optional human-readable description of what this test file covers>

kc-covers:
  - api/post-claims
  - api/get-claims
  - component/backend-claims-routes
"""
```

**Placement — module-level, not per-function.** Two reasons, not one: it matches the real convention already found in `test_claims.py` (declared once per file), and it matches `test_plan`'s own output granularity — recommendations are per gap-*component*, a list of targets, not per individual test assertion. Per-function declaration is a natural finer-grained extension once this coarser version is validated in practice; it is not a prerequisite for shipping the check.

**Parsing — deterministic, no LLM involved in checking** (only in generating): extract the module docstring via `ast.get_docstring()`, locate the `kc-covers:` line, then every following line matching `^\s*-\s*(\S+)\s*$` until a blank line or the docstring's end. Each captured group is one claimed slug. This keeps the *checking* side consistent with ADR-006's deterministic-first spirit even though the artifact being checked (the test) was LLM-written.

**Scoring — three checks, composable into the eval methodology's declared-coverage axis:**
1. **Existence** — every claimed slug must resolve to a real compiled entity (`get_entity` returns non-null). A claimed slug that doesn't exist is an automatic failure for that test — structurally dishonest, not just imprecise.
2. **Relevance** — cross-reference claimed slugs against what `test_plan` actually recommended for the gap this test was generated to close. Precision = (claimed slugs that were in `test_plan`'s recommended targets) / (total claimed). Recall = (`test_plan`'s recommended targets that got claimed) / (total recommended).
3. **No header at all = automatic 0%**, not a skip. A generated test without a `kc-covers:` block doesn't get excused from this scoring axis — it fails it, keeping the metric honest rather than letting silence read as "not applicable."

## Mutation-score threshold decision (resolves next-step #4, decided 2026-07-20)

**Decision: track scores as a reported metric, no hard gate, for now.** 64.4% on `normalize.py` is exactly one data point — not enough to know whether 64% is good, bad, or typical for this codebase's actual testing style. Setting a hard threshold on one data point risks two real failure modes: too strict (blocking legitimate work on modules that simply have less test infrastructure built up yet) or too lax (manufacturing false confidence from a number nobody calibrated). Standard practice for introducing a new quality metric is measure-first, gate-later — the same instinct that made the M3 brainstorm itself insist on running a spike before committing to Option C's mechanism.

**Revisit condition:** once mutation scores exist for several more modules across both dogfood repos, review the actual distribution and set a threshold informed by real data — likely per-repo or per-language rather than one global number, since frida (product code, real business logic) and omnius_llmlib (an LLM-tooling library) plausibly have different natural ceilings that a single global gate would flatten incorrectly.

## Next steps

1. ~~Run the mutation-testing spike~~ **Done 2026-07-20** — 64.4% baseline on `normalize.py`, tool practical, see above.
2. ~~Define the declared-coverage header format~~ **Done 2026-07-20** — see above.
3. ~~Decide the mutation-score threshold question~~ **Done 2026-07-20** — track-only for now, see above.
4. ~~Revisit `decisions/index.md:159`'s "Verification Requirement" entity question once the declared-coverage convention has run long enough in practice to know whether it deserves to graduate from test metadata into compiled Knowledge IR state (which would need its own ADR, per that item's note).~~ **Done 2026-07-29** — resolved by [ADR-012](decisions/ADR-012-defer-verification-requirement-entity.md): deferred; mutation-kill rate is the V1 sub-component precision signal. See `BRAINSTORM-verification-requirement.md` for the full options analysis.
5. ~~Run mutation testing against a few more modules~~ **Partially done 2026-07-22** — three more Knowledge-Compiler modules run (see distribution below); frida/omnius_llmlib still pending on cross-org CI access (see note).
6. Begin the actual milestone-3 test-generation mechanism design, now with a defined success metric (declared-coverage + mutation-kill) to build toward.

### Mutation-score distribution (2026-07-22)

Ran via the same `mutation-test.yaml` GitHub Actions workflow (`workflow_dispatch`, `ubuntu-latest`), one module at a time. Mid-way through this batch, scoping `source_paths` to a single file was found to break cross-module imports at test time (`diff.py` importing `normalize.py` failed — `normalize.py` was never copied into the mutmut sandbox); fixed by copying the whole `source_paths = ["knowledge_compiler"]` tree and scoping the actual mutation count via the separate `only_mutate` glob config. `normalize.py`'s original 64.4% predates the fix but is unaffected by it (that module doesn't import a sibling that needed copying).

| Module | Killed | Survived | No-tests | Total | Score (killed/total) |
|---|---|---|---|---|---|
| `compiler/normalize.py` | 695 | 383 | — | 1080 | 64.4% |
| `compiler/diff.py` | 196 | 45 | 12 | 253 | 77.5% |
| `extractors/python_analyzer.py` | 374 | 87 | 0 | 461 | 81.1% |
| `extractors/typescript_analyzer.py` | 428 | 228 | 43 | 699 | 61.2% |

**Reading this, not over-reading it (still 4 data points, not a calibrated baseline):** `normalize.py` and `typescript_analyzer.py` — the two modules with the most branching, edge-case logic (the identity cascade; tsconfig alias resolution + JSONC parsing) — score lowest, while `diff.py` and `python_analyzer.py` score highest. Directionally consistent with "more intricate control flow is harder to fully exercise," but this is four modules in one repo, still short of the "several more modules across both dogfood repos" the threshold decision above asked for.

**Decided against, 2026-07-22: frida/omnius_llmlib mutation runs via this workflow.** The cross-org checkout path needs a `CROSS_REPO_TOKEN` repo secret scoped to `omni-us-ea` — not something that will be provisioned, so this path is closed, not just pending. The `target_repo` input stays in `mutation-test.yaml` (harmless, no cost if unused) but the distribution above will not be extended to the other dogfood repos through it. If frida/omnius_llmlib mutation scores are wanted later, the workflow would need to live in each of those repos' own CI instead — a real re-scoping, not a config tweak, and out of scope for now.
