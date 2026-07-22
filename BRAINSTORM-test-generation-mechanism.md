# Brainstorm: Test-generation mechanism
2026-07-22 · Mode: Exploration

## Problem

`BRAINSTORM-test-generation-eval.md` settled *how to score* a generated test (declared-coverage `kc-covers:` header + mutation-kill rate, no hard gate yet) and validated the mutation-testing half with a real spike across four Knowledge-Compiler modules (64.4%–81.1%). It deliberately scoped out *how a test actually gets generated* — `test_plan`'s own docstring says as much: "This decides *what* needs a test, not the test itself: writing it is a later, LLM step." This document is that later step: what triggers generation, what produces the test file, where it lands, and how the already-defined `kc-covers` header gets populated and checked.

## Constraints & assumptions

**Hard constraints:**
- Must build on `test_plan` (`mcp/queries.py:207`) as the input — it already names, per coverage gap, either concrete API entities or public symbols to target. Re-deriving this signal elsewhere would duplicate `impact_plan`/`test_plan` for no reason.
- The `kc-covers:` header format and its three-check scoring (existence, precision/recall vs. `test_plan`, mutation-kill) are frozen by the eval brainstorm — not open for renegotiation here.
- Deterministic-first philosophy (vision.md, ADR-006, ADR-008): LLM output must be validated before being trusted as fact, same standard applied to business rules/features/risks today.
- `kc serve` is explicitly read-only and **never compiles** (README, pipeline.md) — a strong existing precedent that MCP-surfaced capability and "the compiler does work" are kept separate.

**Assumption made without asking:** vision.md's own phrasing for milestone 4 is "**agents** generate implementation-aware regression tests from compiled business rules, API contracts, coverage gaps, and recent deltas" (use case list, priority-ordered) — not "the compiler generates tests." That word choice is treated here as a real signal, not incidental phrasing, since every other use case in that same list ("agents ... answer engineering questions," "AI agents answer engineering questions through MCP") consistently casts the compiler as the knowledge substrate and the agent (something consuming `kc serve`) as the actor. If this reading is wrong, Option C below becomes the natural pick instead of the recommendation.

## Context (codebase findings)

- `test_plan` (`mcp/queries.py:207-250`) already returns, per coverage gap: `target_kind` (`api` or `symbols`) and concrete targets (API slug/method/route or public symbol paths). This is a complete, structured "what to test" answer already exposed over MCP.
- ADR-008's LLM abstraction is `complete(prompt, schema) -> validated JSON` — a content-addressed cache keyed by `(template_id, template_version, model_id, content_hash)`, built for **structured facts** (business rules, features, risks), not free-form source code. Squeezing a full test file through this exact contract is possible (one JSON string field) but fights the shape the abstraction was designed for.
- `kc serve`'s MCP tools are all read-only queries over already-compiled state (`search_knowledge`, `get_entity`, `impact_plan`, `test_plan`, `resolve_dependency`, …) — there is no existing MCP tool or CLI command that writes files into the target repo.
- The CLI (`cli.py`) is a flat `click` group — `init`/`compile`/`reconcile`/`verify`/`inspect`/`serve` — each a self-contained command reading `kc.toml` + repo dir. A new command would fit this pattern easily if one were needed.
- Emit (pipeline.md §3.6) already does LLM-assisted prose generation for wiki pages from compiled entities — an existing, working precedent for "LLM turns compiled knowledge into human-facing prose," distinct from "LLM writes runnable code."

## Options

### Option A: Do nothing (defer past this brainstorm)

**Sketch:** Ship nothing further; `test_plan` stays the last shipped artifact for milestone 3.

**Pros:** Zero cost now; nothing here is blocking milestone 2's continued use.

**Cons:** Milestone 3 is vision.md's stated "long-term criterion" / "north star" — deferring doesn't reduce scope, it leaves the entire use case un-started after two brainstorms' worth of groundwork (eval methodology, `test_plan`) already paid for.

**Pre-mortem:** Six months on, `test_plan` is a nice MCP tool nobody built a workflow around, and "test generation" is still exactly as undesigned as it is today — the eval methodology brainstorm's cost was sunk for nothing.

**Reversibility:** Two-way — trivially reversible, it's inaction.

**Effort:** None now.

---

### Option B: External consumer; the compiler's responsibility ends at deterministic knowledge compilation and test-plan generation (recommended)

Test synthesis is delegated to external coding agents, while validation remains a downstream consumer of the compiled knowledge — not a pipeline stage.

**Sketch:** Knowledge Compiler does **not** generate test code itself. `test_plan` is already the complete "what to test" hand-off; whatever coding agent is consuming `kc serve` (Claude Code, another MCP client) reads it and writes the test file directly into the target repo, using its own general code-generation ability plus the already-published `kc-covers:` header spec. KC's only new surface is a small, deterministic scoring capability — a `kc score-test <path>` CLI command (or a new read-only MCP tool, `score_test`) that runs the three checks the eval brainstorm already defined: existence (do the claimed slugs resolve via `get_entity`), precision/recall (claimed slugs vs. `test_plan`'s recommended targets), and a pointer to the existing mutmut CI workflow for the mutation-kill half.

**Pros:** Reuses 100% already-built work (`test_plan`, the `kc-covers` spec, the mutmut workflow) with no new LLM-output-validation surface inside the compiler; avoids forcing free-form source code through ADR-008's JSON-schema-validated contract, which was designed for structured facts, not files; consistent with `kc serve`'s existing "never compiles, read-only" invariant — the compiler stays a knowledge substrate, not a code-writing agent; matches vision.md's own wording ("agents generate ... tests").

**Cons:** No automatic trigger — nothing happens by itself after a compile; entirely dependent on some external agent/workflow actually being pointed at `test_plan` and told to write tests; the scoring tool alone can look like "not much was built" relative to the two brainstorms that preceded it.

**Pre-mortem:** Six months on, no concrete agent workflow ever gets built around `test_plan` + `score_test` — the pieces exist but nobody wires an actual "run this after every compile" habit, and milestone 3 stays true only in the most literal, technically-unblocked sense.

**Reversibility:** Two-way — `score_test` is a small additive tool; nothing here blocks building Option C later if this proves too passive.

**Effort:** Hours to a day (header parsing + existence/precision-recall check are already fully specified; wiring is the only new work).

---

### Option C: In-process generation stage, reusing the ADR-008 LLM abstraction

**Sketch:** Add a new command (`kc generate-tests --component <slug>`) that calls the existing LLM provider abstraction with a schema like `{test_code: str, kc_covers: list[str]}`, using `test_plan`'s recommendations as prompt input; validates the claimed `kc_covers` slugs deterministically against the KB immediately after generation (closing the loop in one step); writes the result into a dedicated `generated_tests/` directory, never auto-committed.

**Pros:** Single command, end-to-end; reuses the existing provider/cache/budget machinery instead of building new plumbing; the validation gate runs automatically right after generation rather than depending on a separate manual step.

**Cons:** Forces a full source file through a JSON-schema-validated contract built for structured facts — no real benefit from schema validation on a giant free-text code string, and it bloats the content-addressed cache with source blobs; more seriously, ADR-008's cache is built on "unchanged input → identical output, don't re-pay" — correct and load-bearing for reproducible facts, but actively wrong for generation, where a user re-running to get a *different* attempt at the same test would silently get the same cached file back unless they knew to bump `model_id` to force a miss. That's a real design smell, not a nitpick.

**Pre-mortem:** Six months on, someone burns an afternoon confused why "regenerating" a test does nothing, before finding out the cache — designed for a different problem — is silently serving the first attempt back every time.

**Reversibility:** Mostly two-way (additive stage), but the cache-semantics mismatch would need a real fix (e.g., a generation-specific cache bypass) before this is trustworthy, not an afterthought.

**Effort:** Low-days, plus the cache-semantics fix above if done properly.

---

### Option D: Buy/adopt — an off-the-shelf AI test-generation tool

**Sketch:** Skip building anything KC-specific; drive an existing tool (e.g., a generic LLM-based test generator, or an IDE/Copilot-style feature) directly against source, using `test_plan`'s output only as a hint list of files to point it at.

**Pros:** Fastest path to "some tests exist."

**Cons:** Throws away the entire differentiator vision.md is betting milestone 3 on — a generic tool has no notion of a compiled business rule or API entity, only raw source; it can't produce or honor a `kc-covers` header, so none of the eval methodology (existence check, precision/recall, mutation-kill scoping) built in the prior brainstorm would even apply to its output. The two pieces of work never connect.

**Pre-mortem:** Six months on, "tests exist" but none of them are traceable to a compiled entity, the eval methodology sits completely unused, and the two brainstorms invested in this milestone were for nothing.

**Reversibility:** Two-way, but represents abandoning the traceability bet entirely rather than extending it.

**Effort:** Hours to try, but disconnected from everything already built.

---

### Option E: Prose-only — "Suggested Tests" wiki pages, no code generation (contrarian)

**Sketch:** Extend Emit (the existing wiki-page derivation stage, which already does LLM-assisted prose for business rules/risks) to produce a "Suggested Tests" page per component: prose test *scenarios* derived from `test_plan`'s targets, not runnable code. Actual test authorship stays a human/agent task, informed by this page.

**Pros:** The closest possible reuse of an existing, already-working pattern (LLM prose from compiled entities, same as feature/risk narratives) — zero new output-validation contract needed, since prose isn't "code" and the `kc-covers`/mutation-kill scoring doesn't apply to it at all; ships in days.

**Cons:** Doesn't produce a runnable test — a strict reading of vision.md's "generate implementation-aware regression tests" would say this doesn't satisfy milestone 3 on its own, only a precursor to it.

**Pre-mortem:** Six months on, "Suggested Tests" pages are genuinely useful reading material, but nobody ever takes the next step to actual generated-and-scored code, and the eval methodology built for that next step sits unused — the same half-finished-pipeline risk as Option A, just one stage further along.

**Reversibility:** Two-way, fully additive; composes fine with Option B or C as a complementary output, not a competing one.

**Effort:** Days.

## Comparison

Weighted by: fit with the deterministic-first/validated-before-fact philosophy, reuse of already-built work (`test_plan`, `kc-covers` spec, mutmut CI), and consistency with `kc serve`'s read-only invariant — all explicitly established precedents in this codebase.

| | Reuses existing work | Fits deterministic-first / kc serve read-only | Produces runnable code | Automatic trigger | Effort | Reversibility |
|---|---|---|---|---|---|---|
| A. Defer | — | — | ✗ | ✗ | None | Two-way |
| **B. External consumer + score_test** | ✓✓ | ✓✓ | ~ (external, not KC's job) | ✗ | Hours–day | Two-way |
| C. In-process generation | ✓ | ~ (cache-semantics mismatch) | ✓ | ✓ | Low-days | Mostly two-way |
| D. Adopt generic tool | ✗ | ✗ | ✓ | ~ | Hours (wasted) | Two-way, disconnected |
| E. Prose-only wiki pages | ✓✓ | ✓✓ | ✗ | ✓ (part of compile) | Days | Two-way |

## Recommendation

**Option B** — Knowledge Compiler does not generate test code; it ships `test_plan` (already done) plus a small deterministic `score_test` tool, and treats actual generation as the job of whatever agent is consuming `kc serve`.

**Confidence: medium.** This is the option most consistent with everything already established in this codebase — `kc serve`'s "never compiles" invariant, ADR-008's schema-validated-JSON contract (a poor fit for free-form source files), and vision.md's own "agents generate tests" wording. It also avoids a real, not-yet-solved problem (Option C's cache semantics fighting regeneration) rather than building past it.

**Decisive uncertainty:** whether "milestone 3 done" is expected to mean an automatic, no-agent-required pipeline capability (in which case Option C, cache-semantics fix included, becomes necessary) or an enabled workflow that an agent performs using KC's compiled knowledge (in which case B is sufficient as designed). This is a product-expectation question, not a technical one, and it isn't resolved by anything already written down.

**Proposed spike (half a day):** Take `test_plan`'s real output for one actual coverage gap on frida, hand it to a coding agent in a normal working session, have it write a `kc-covers`-tagged test by hand using the published header spec, then run it through a manually-built `score_test` check plus the existing mutmut workflow. If that loop feels adequate, B is confirmed as sufficient; if the friction of "someone has to remember to do this" feels like the real blocker, that's the concrete signal to build C instead.

**Steelman of the runner-up (Option C):** A reasonable person optimizing for "milestone 3 actually ships something automatic" would pick C — it's the only option where running one command produces a test file without a human/agent workflow decision in the loop, and reusing ADR-008's existing provider/cache machinery is genuinely less new plumbing than it sounds. The cache-semantics mismatch (unchanged input → same output, which is wrong for "give me a different attempt") is real but fixable — a generation-specific cache bypass or a nonce in the content hash would resolve it without abandoning the rest of ADR-008's machinery. If Option B's spike above shows the manual workflow never actually happens in practice, C is the correct escalation, not a fallback.

## Next steps

1. Run the proposed spike: hand `test_plan`'s real frida output to a coding agent, write one real `kc-covers`-tagged test by hand, score it manually — resolves the decisive uncertainty above.
2. If confirmed: build `kc score-test <path>` (or an MCP `score_test` tool) implementing the three checks from `BRAINSTORM-test-generation-eval.md` (existence, precision/recall vs. `test_plan`, note on mutation-kill via the existing CI workflow).
3. If the spike shows the manual workflow doesn't stick: revisit Option C with the cache-semantics fix scoped in from the start, not bolted on after.
4. Once `score_test` exists and has scored a handful of real generated tests, revisit `decisions/index.md:159`'s "Verification Requirement" entity question (M3 step 4, deferred until real `kc-covers` usage exists to observe).
