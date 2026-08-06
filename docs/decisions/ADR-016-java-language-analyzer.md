# ADR-016: Java Language Analyzer Support

## Status

Proposed — **not implemented**.

## Date

2026-08-06

## Context

KC's V1 target languages are Python and TypeScript, both parsed via tree-sitter in-process ([ADR-006](ADR-006-language-analyzers.md)). Java is a different language family entirely — JVM-targeted, a different build/package ecosystem (Maven `pom.xml` / Gradle `build.gradle`, not `package.json`/`pyproject.toml`), and critically, a different *style* of framework wiring: Spring MVC/Spring Boot routes are declared via annotations (`@RestController`, `@GetMapping`) and dependency injection, resolved partly through reflection and classpath scanning at runtime — not the explicit decorator-registration or direct call-site patterns (`@app.get(...)`, `app.get(...)`) that make Python/JS route detection a comparatively mechanical AST pattern-match today.

This matters architecturally, not just as an implementation detail: ADR-006's core invariant is that **the deterministic pass alone must produce a correct structural knowledge base**. For Java, that invariant is much easier to satisfy for *components and symbols* (classes, methods — straightforward AST facts) than for *API/route extraction*, where Spring's annotation-and-convention-driven wiring has a meaningfully lower deterministic-detection ceiling without deeper semantic analysis (classpath resolution, annotation-processor-level understanding) that a pure tree-sitter AST pass cannot provide.

## Decision Drivers

- `tree-sitter-java` is a mature, available grammar — no blocker there
- Preserve ADR-006's determinism invariant honestly — don't claim mechanical certainty for extraction that is inherently heuristic
- Avoid premature scope — don't design speculative Spring-specific route detection before a concrete demand or dogfood repo justifies the investment (the same discipline ADR-014 applied to `Attested Computation`)
- Build-file parsing parity — Java's `external_dependencies` extraction needs a Maven/Gradle equivalent of the dependency parsing Python/TS already have

## Considered Options

### Option A — Full-scope `JavaAnalyzer`: structure now, Spring-route detection now too

One analyzer, shipped complete: class/method/component extraction via `tree-sitter-java`, plus annotation-pattern-matching for Spring/JAX-RS route detection from day one.

**Pros**

- One release, feature-complete relative to Python/TypeScript's current API-detection capability.

**Cons**

- Annotation-based route detection without classpath/reflection awareness is genuinely less reliable than what Python/JS's call-site patterns already achieve — shipping it as parity-equivalent day one overstates its actual detection ceiling and risks a worse false-negative/false-positive rate than users would reasonably expect from "the same feature that works for Python."
- Speculative design for a heuristic whose real-world accuracy is unverified without a concrete Spring Boot dogfood repository.

### Option B — Structural-only `JavaAnalyzer` first; API/route detection explicitly deferred

Ship `component_observed` (class/interface-level), `symbol_observed` (method-level), and `test_case_observed` (JUnit `@Test`-annotated methods) extraction first. Explicitly do **not** attempt Spring/JAX-RS route detection in this increment — treat it as a separate, later ADR or implementation phase, informed by real dogfood evidence of how reliably annotation patterns can actually be matched.

**Pros**

- Delivers real, immediately useful value (structural knowledge base for Java repos, test coverage mapping) without overclaiming on the hardest, least-deterministic part of the problem.
- Matches ADR-014's established discipline: don't build for a need whose actual shape (accuracy, false-positive rate of annotation matching) is unverified.
- A smaller, more reviewable first increment — structural extraction is genuinely mechanical (tree-sitter AST facts), unlike route detection.

**Cons**

- A Java repo compiled under this scope would show `component`/`test_coverage` entities but no `api` entities at all initially — an incomplete picture compared to a Python/TS repo compiled today, and that gap needs to be documented plainly, not silently.

### Option C — Wait for a concrete Java dogfood repository before any design commitment

Do not commit to any analyzer design until a real Java codebase is available to validate assumptions against.

**Pros**

- Zero speculative-design risk.

**Cons**

- Leaves the gap (no Java support at all) fully unaddressed with no recorded direction, unlike Option B which at least commits to a scoped, honest first increment.

## Decision

**Not yet decided for implementation — this ADR records Option B as the recommended design, Proposed status, explicitly unbuilt.** Structural-only extraction first (components, symbols, test coverage via `tree-sitter-java`), with Spring/JAX-RS route detection explicitly deferred to a later phase gated on real dogfood evidence of annotation-pattern detection reliability — preferred over Option A (overclaims determinism the underlying heuristic can't support) and Option C (commits to nothing, leaving the gap fully unaddressed). No code exists; implementation is gated on someone picking this up.

## Architectural Invariants (if implemented)

- Java's structural facts (`component_observed`, `symbol_observed`, `test_case_observed`) are produced with the same determinism guarantee ADR-006 requires for Python/TypeScript — no reliance on LLM extraction to make the deterministic pass "work."
- API/route extraction, if and when built, is explicitly labeled a heuristic, `code_pattern`-sourced signal — never presented as more mechanically certain than annotation-pattern matching actually achieves, consistent with how existing heuristic-sourced API facts are already flagged (`api_observed`'s `sources` field, the "Observed via" wiki rendering).
- Maven/Gradle dependency parsing feeds `external_dependencies`, matching the existing parity Python (`requirements.txt`/`pyproject.toml`) and TypeScript (`package.json`) already have — no new relationship types invented without a demonstrated need.

## Consequences (if implemented)

### Positive

- Real, useful structural knowledge (classes, methods, test coverage) for Java repositories — a genuine gap closed without overclaiming what's actually deterministic.
- Sets an honest precedent: a language whose framework-wiring conventions are less mechanically detectable gets a scoped-down first release rather than a feature-parity-on-paper release that quietly underperforms in practice.

### Negative

- Java repositories compiled under this scope initially show no `api` entities at all — a real, visible gap versus Python/TypeScript repos, which needs to be documented in `kc inspect` output expectations and README language-support claims, not left implicit.
- Route detection, if built later, carries real risk of being less reliable than Python/JS's equivalent — that risk should be communicated to users, not hidden behind an apparently-equivalent feature list.

## Failure Modes (if implemented)

| Failure | Effect | Handling |
|---|---|---|
| `tree-sitter-java` fails to parse a file | That file's facts skipped | Recorded as compile warning, never a compile failure — same policy as existing analyzers |
| Maven/Gradle build-file parsing encounters a build-file shape not anticipated (multi-module projects, custom Gradle DSL) | Incomplete or missing `external_dependencies` | Documented as a known limitation; not a blocker for structural extraction, which doesn't depend on it |
| A later Spring-route-detection increment produces a high false-positive/negative rate | Misleading `api` entities, or missing ones | Explicitly why Option B defers this work until real dogfood evidence exists — building it blind risks exactly this outcome |

## Assumptions

- `tree-sitter-java` remains available and maintained for the Python bindings KC uses.
- Structural extraction (classes/methods/tests) is genuinely reliable via AST alone for Java, the same way it already is for Python/TypeScript — a reasonable assumption given Java's explicit, non-dynamic class/method declaration syntax, but unverified until implementation is attempted.

## Open Questions

- Exact scope of "structural-only" — does it include package-level component grouping (Java's package structure vs. Python/TS's file-level component granularity), and how that maps onto KC's existing `component` slug scheme.
- Whether Maven and Gradle both need first-class support in the initial increment, or whether one can come first with the other explicitly deferred.
- What a concrete, real dogfood Spring Boot repository would reveal about annotation-pattern-matching reliability — the actual gating question for ever attempting Option A's deferred scope.

## Impact (if implemented)

Affected code:

- New `extractors/java_analyzer.py`
- `compiler/run.py`'s `_extract()` — one more analyzer in the fixed list
- `pyproject.toml` — new `tree-sitter-java` dependency
- Possibly a new build-file collector/parser for Maven/Gradle dependency facts, parity with existing `external_dependencies` handling

Affected documents:

- `docs/architecture.md` §8 (language analyzers), §13 (module layout)
- README.md language-support claims — must be explicit that Java support (if shipped under this ADR's scope) excludes API/route detection initially, not silently incomplete

## Alternatives Rejected

Not rejected — **not yet decided**. Options A and C are recorded with honest tradeoffs; this ADR recommends Option B without committing the project to building it.

## Future Reconsideration

Revisit when someone picks this up for implementation (the open questions above need answers first), when a concrete Java/Spring Boot dogfood repository becomes available to validate route-detection reliability, or when real demand for Java support materializes.

## References

- [ADR-006](ADR-006-language-analyzers.md) — the language-analyzer backbone and determinism invariant this ADR must honor honestly, not just nominally
- [ADR-015](ADR-015-javascript-language-analyzer.md) — a sibling language-support ADR recorded the same day, same discipline (structural-first, avoid overclaiming heuristic reliability); implemented 2026-08-06, unlike this ADR
- [ADR-014](ADR-014-shared-okf-rules-file.md) — the "don't build for unverified need" discipline this ADR's Option B follows

## Self-Review

- **Truly architectural?** Yes, more so than ADR-015 — Java's annotation-driven framework conventions genuinely stress-test ADR-006's determinism invariant in a way Python/TypeScript's more explicit registration patterns don't, making the scoping decision (structural-only first) a real architectural call, not just a new-language checkbox.
- **Already made?** No — explicitly unimplemented; no code exists.
- **Reversible?** Fully — nothing has been built.
- **Dependent future documents:** `docs/architecture.md`, README.md language-support claims (conditionally, if implemented).
- **Exposes unresolved decisions:** package-vs-file component granularity, Maven-vs-Gradle initial scope, and the deferred route-detection question — all listed as open questions above.
