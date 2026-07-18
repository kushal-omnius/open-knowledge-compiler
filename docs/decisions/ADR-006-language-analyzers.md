# ADR-006: tree-sitter as the Language-Analyzer Backbone

## Status

Accepted

## Date

2026-07-17

## Context

The vision commits to analyzing **Python and TypeScript** repositories, while the compiler itself is implemented in **Python only, deployed as a single executable**. Deterministic extraction (components, symbols, API surfaces, test structure) requires parsing target-language source. The question is which parsing technology anchors the `LanguageAnalyzer` plugin interface — a decision that determines whether the Python-only constraint survives contact with TypeScript, and how honest the "pluggable language support" promise is.

The vision's success criterion 5 makes this concrete: adding the second language must require no changes downstream of extraction.

## Decision Drivers

- Future language support — one mechanism must scale to language N without new runtimes
- Simplicity — single executable; no Node.js or per-language toolchain in the core
- Determinism — deterministic extraction is the compiler's skeleton (vision Design Principle 3)
- Performance — ~5M LOC must parse in minutes (architecture.md §12)
- Accuracy — enough structure for components/APIs/tests; semantic depth is explicitly secondary
- Maintainability — grammars maintained upstream, not by this project

## Considered Options

### Option A — tree-sitter backbone

One parsing runtime (Python bindings, prebuilt grammar wheels) for all target languages; analyzers query concrete syntax trees per language.

**Pros**

- Only option satisfying all constraints simultaneously: in-process Python, no external runtime, uniform across languages.
- Adding language N = adding a grammar wheel + an analyzer plugin; nothing else changes.
- Fast, incremental-parse-capable, error-tolerant (parses broken files partially).
- Grammars are maintained by a large upstream ecosystem.

**Cons**

- **Syntax-level only**: no type inference, no import resolution, no cross-file semantic analysis out of the box. Dynamic constructs (runtime-registered routes, decorator magic) are invisible.
- Query patterns are per-language and framework-aware extraction (e.g., FastAPI routes) is pattern engineering.

### Option B — Native per-language toolchains

Python via stdlib `ast`/`importlib`; TypeScript via the TypeScript compiler API.

**Pros**

- Richest semantic information per language (real type checking for TS, real import graphs for Python).

**Cons**

- **TS compiler API requires a Node.js runtime** — breaks Python-only and single-executable constraints outright.
- Every language brings a disjoint toolchain: N languages = N integration surfaces, N runtime dependencies, N failure modes.
- The plugin interface would be a fiction — each analyzer a bespoke subsystem.

### Option C — LLM-only code understanding

No AST parsing; LLM extractors read source directly for structure as well as semantics.

**Pros**

- Trivially language-agnostic; zero parsing infrastructure.

**Cons**

- Violates the deterministic-first principle at its root: the compiler's *skeleton* (components, APIs, test inventory) would hallucinate.
- Cost/scale collapse at 5M LOC (architecture.md §12 requires the deterministic pass to stand alone with zero LLM budget).
- Structure extraction becomes non-reproducible, poisoning identity (ADR-004) and deltas (ADR-003).

### Option D — LSP-based analysis

Drive language servers (pyright, tsserver) via the Language Server Protocol for semantic-grade analysis.

**Pros**

- Deep semantic info through a nominally uniform protocol.

**Cons**

- Language servers are external processes (tsserver again needs Node) — same constraint violations as B, plus process orchestration.
- LSP is designed for interactive editing, not batch extraction; servers are stateful, memory-heavy, and slow to warm on 5M LOC.
- Protocol uniformity is shallow: capabilities differ per server, so the analyzer interface leaks server specifics anyway.

## Decision

**Option A — tree-sitter as the mandatory backbone, with optional per-language enrichment plugins.**

The analyzer contract: given a file set, produce facts (components, symbols, API surfaces, test mappings) in the canonical model. The backbone guarantees this works for every supported language under the core constraints. Where richer analysis is worth having, an *enrichment plugin* may add it — the Python analyzer may use stdlib `ast` for import graphs (in-process, allowed); a TypeScript type-aware enricher may shell out to `tsc` — but **enrichment is always optional, never a core dependency**, and its absence only reduces richness, never correctness of the backbone facts.

Why A over the alternatives: B and D fail the Python-only/single-executable constraints (both need Node for TS); C fails deterministic-first and the 5M LOC scaling posture. A is not merely least-bad — its error tolerance and grammar ecosystem are genuine fits for a compiler that must parse real, sometimes-broken repositories.

## Architectural Invariants

- Language analyzers emit facts in the canonical model only; no language-specific types cross the analyzer boundary.
- The core pipeline never requires a non-Python runtime; anything that does is an optional enrichment plugin.
- The deterministic pass (backbone analyzers, zero LLM, zero enrichment) must alone produce a correct structural knowledge base.
- Analyzer output for unchanged input is byte-identical (grammar version is recorded; see Failure Modes).

## Consequences

### Positive

- Language N is a grammar + plugin, satisfying success criterion 5 by construction.
- 5M LOC parses in minutes; the deterministic skeleton stands alone (architecture.md §12 point 1).
- Broken files degrade gracefully (partial parse) instead of failing the compile.
- No Node.js anywhere in the required deployment.

### Negative

- V1 analysis is syntax-level: dynamically registered routes, metaprogramming, and type-level facts are invisible to deterministic extraction (LLM extractors may still surface some as semantic facts, with LLM-grade confidence).
- Framework-specific extraction (route patterns per web framework) is ongoing query-pattern maintenance.

### Tradeoffs Accepted

- **Semantic depth is traded for constraint compliance and uniformity.** Type-aware analysis is deliberately pushed to optional enrichment, accepting that V1's API inventory may miss dynamic constructs.
- Pattern-based framework detection will have per-framework gaps; the dogfood repo decides which patterns get built first.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Dynamic constructs invisible (runtime route registration, decorators) | Missing APIs/components in deterministic facts | Accepted V1 limit; LLM extractors may propose them as semantic facts (clearly provenance-marked as LLM-derived); enrichment plugins post-V1 |
| Grammar version drift (upstream grammar changes parse output) | Same source → different facts across compiler versions | Grammar versions pinned in the package and recorded with analyzer facts in provenance; version bumps are deliberate upgrades, re-grounded by full recompile |
| Unparseable file (syntax beyond grammar recovery) | Missing facts for that file | File-level skip, recorded as a compile warning artifact; never fails the compile |
| Framework pattern miss (unrecognized routing idiom) | APIs missing from inventory | Visible in dogfood wiki review; patterns are analyzer-plugin code, added incrementally |
| Monorepo language ambiguity (mixed-language file sets) | Wrong analyzer applied | File-extension + kc.toml path-scoping rules decide analyzer routing (configuration, not ADR) |

## Assumptions

- Prebuilt tree-sitter grammar wheels for Python and TypeScript remain maintained and pip-installable.
- Syntax-level facts suffice for the V1 wiki's structural content (validated on the dogfood repo).
- The canonical fact model can express everything analyzers emit (ir.md dependency).
- Analyzer routing by extension/path is adequate for target repos.

## Open Questions

- Exact fact vocabulary analyzers emit (symbols, spans, anchors) — deferred to `ir.md`; ADR-004 already requires anchors as a hard extractor obligation.
- Which framework route patterns ship in V1 analyzers — decided by the dogfood repo's stack, recorded as plugin scope, not ADR material.
- Whether `tsc`-based enrichment is ever worth its operational cost — post-V1, evidence-driven.

## Impact

Affected documents:

- `architecture.md` §8
- `ir.md` (planned) — fact vocabulary and anchor representation analyzers must emit
- `pipeline.md` (planned) — Extract stage contract
- `plugin-system.md` (planned) — analyzer and enrichment plugin registration

Affected compiler stages:

- **Extract** — the sole home of analyzers; owns grammar pinning and skip-on-parse-failure
- **Normalize** — consumes analyzer facts; unaffected by parsing technology (the boundary this ADR protects)

## Alternatives Rejected

- **Native toolchains (B)** — TS requires Node; N languages = N runtimes; the plugin interface becomes a fiction.
- **LLM-only (C)** — hallucinating skeleton; fails determinism, reproducibility, and 5M LOC economics.
- **LSP (D)** — external stateful processes (Node again for TS), editor-oriented protocol, shallow uniformity.

## Future Reconsideration

Revisit if dogfood evidence shows syntax-level extraction misses too much of the real API surface to sustain wiki trust (the signal to design the enrichment tier properly), or if a credible in-process, multi-language semantic-analysis library emerges for Python.

## References

- `docs/vision.md` — Language Scope; Design Principle 3
- `docs/architecture.md` — §8 (A5), §12 (scaling posture)
- [ADR-004](ADR-004-entity-identity.md) — Accepted; anchors emitted by analyzers feed the identity cascade
- ADR-007 — plugin architecture (analyzers and enrichers are plugins)

## Self-Review

- **Truly architectural?** Yes — it fixes the parsing runtime, the analyzer plugin boundary, and what "supported language" costs.
- **Already made?** Yes — architecture.md §8 chose tree-sitter; this ADR adds the LSP option, the backbone-vs-enrichment invariants, and grammar-pinning.
- **Reversible?** Largely two-way at the plugin level (an analyzer can be reimplemented behind the same contract); one-way in that core constraints permanently exclude required non-Python runtimes.
- **Dependent future documents:** ir.md (fact vocabulary), plugin-system.md, pipeline.md.
- **Exposes unresolved decisions:** the IR fact vocabulary (ir.md) — listed, not invented.
