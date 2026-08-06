# ADR-015: JavaScript Language Analyzer Support

## Status

Accepted — **implemented** (2026-08-06).

`knowledge_compiler/extractors/javascript_analyzer.py` — Option A: dedicated
`JavaScriptAnalyzer` via `tree-sitter-javascript`, parallel to `TypeScriptAnalyzer`.
Wired into `compiler/run.py`'s `_extract()`. 16 tests in
`tests/test_javascript_analyzer.py`. Open questions from the ADR resolved at
implementation time: no shared helper code with TS (kept fully independent per Option
A's rationale); ESM and CJS both extracted; JSX supported natively by the grammar.

## Date

2026-08-06

## Context

KC's V1 target languages are Python and TypeScript ([ADR-006](ADR-006-language-analyzers.md)): tree-sitter backbone, in-process, no Node.js runtime. `extractors/python_analyzer.py` and `extractors/typescript_analyzer.py` are separate, per-language modules, each claiming a fixed set of file extensions and each independently producing the same Fact IR shapes (`component_observed`, `symbol_observed`, `api_observed`, `test_case_observed`, …) so Normalize treats every language uniformly downstream.

Plain JavaScript is currently **invisible** to the compiler. No analyzer claims `.js`/`.jsx`/`.mjs`/`.cjs` files. Any repository containing plain-JS modules — a JS-only codebase, or a partially-migrated TypeScript repo with legacy `.js` files still present — gets zero `component`/`api`/`test_coverage` extraction for that portion of the code, silently. This is a real, concrete gap, not a hypothetical one: mixed JS/TS repositories are common.

## Decision Drivers

- Reuse ADR-006's existing tree-sitter infrastructure — a new grammar dependency (`tree-sitter-javascript`), not a new parsing strategy
- Preserve ADR-006's "no Node.js runtime" invariant — parsing must stay in-process
- Fact IR parity — a `JavaScriptAnalyzer` must emit the same fact shapes `PythonAnalyzer`/`TypeScriptAnalyzer` already emit, so Normalize needs no per-language special-casing
- Determinism — the deterministic pass alone must produce a correct structural knowledge base for JS, per ADR-006's core invariant, before any LLM enrichment

## Considered Options

### Option A — Dedicated `JavaScriptAnalyzer` via `tree-sitter-javascript`

A new analyzer module, claiming `.js`/`.jsx`/`.mjs`/`.cjs`, structurally parallel to `TypeScriptAnalyzer` but using the distinct `tree-sitter-javascript` grammar rather than reusing the TypeScript one.

**Pros**

- Matches the established one-analyzer-per-language-file pattern exactly — no special-casing, no shared-analyzer coupling between TS-specific extraction (interfaces, type aliases) and plain-JS parsing.
- `tree-sitter-javascript` is a mature, independently-maintained grammar — no risk of TypeScript-grammar edge cases misparsing modern JS-only syntax.
- Framework-pattern heuristics for HTTP routes (Express, Fastify — `code_pattern`-sourced `api_observed` facts, the same heuristic category already used for Python) can be authored directly against JS's actual AST shapes rather than adapted from TS's.

**Cons**

- A second grammar dependency alongside `tree-sitter-typescript`, even though the two languages overlap heavily.
- Duplicated boilerplate between `TypeScriptAnalyzer` and `JavaScriptAnalyzer` for the parts of the language that are identical (most expression/statement syntax) — some shared-helper extraction may be worth doing at implementation time, not decided here.

### Option B — Extend `TypeScriptAnalyzer` to also claim `.js`/`.jsx`

Since valid JavaScript is very close to valid TypeScript syntactically, parse `.js`/`.jsx` files with the existing TS grammar and reuse `TypeScriptAnalyzer` wholesale.

**Pros**

- Zero new grammar dependency; smallest possible change.

**Cons**

- Silently couples two conceptually distinct languages behind one analyzer — a bug fix or extraction improvement aimed at TypeScript-specific syntax (generics, type-only imports) risks unintended effects on JS extraction, and vice versa.
- Newer JS-only syntax (certain proposal-stage or JS-specific idioms not yet in TS's grammar support) could misparse silently, with no dedicated JS grammar to fall back on. This risk is unquantified without dogfood evidence either way.
- Breaks the established "one file per language" module convention for no strong technical reason beyond initial implementation speed.

### Option C — No JavaScript support; document as an explicit non-goal

Leave `.js`/`.jsx` unclaimed, as today, and state plainly that plain JavaScript is out of scope until real demand justifies it.

**Pros**

- Zero cost; avoids speculative design for a need not yet validated by a concrete dogfood repo.

**Cons**

- Leaves the concrete, already-identified gap (mixed JS/TS repos silently losing coverage) unaddressed.

## Decision

**Implemented as Option A.** A dedicated `JavaScriptAnalyzer` via `tree-sitter-javascript`, structurally parallel to the existing per-language analyzers, was preferred over Option B (coupling risk without a strong offsetting benefit) and over Option C (which would have left a real, already-identified gap unaddressed).

## Architectural Invariants

- `JavaScriptAnalyzer` emits only Fact IR shapes the existing schema already defines (ir.md §2) — no new fact types were needed.
- No Node.js runtime dependency is introduced — parsing stays in-process via tree-sitter, per ADR-006.
- API/route detection for JS remains a heuristic, `code_pattern`-sourced signal (as it already is for Python) — never claimed as more deterministic than the underlying pattern-matching actually is.

## Consequences

### Positive

- Closes a real, concrete extraction gap for any repository containing plain JavaScript.
- No architectural surprises — this is squarely inside ADR-006's already-established "optional per-language enrichment plugin" pattern, not a new category of decision.

### Negative

- One more grammar dependency to track for upstream tree-sitter grammar updates (the same maintenance category ADR-006 already accepts for Python and TypeScript).
- Framework-heuristic quality (Express/Fastify route detection) remains regex-on-raw-text, the same design already accepted for Python/TypeScript — false positives from comments/string literals resembling route calls are a known, un-fixed limitation shared across all three analyzers (surfaced by post-implementation adversarial review, not fixed as out-of-scope for this ADR).

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| `tree-sitter-javascript` grammar fails to parse a file (syntax error, unsupported proposal-stage syntax) | That file's facts are skipped | Recorded as a compile warning, never a compile failure — same policy as existing analyzers (ADR-006) |
| Express/Fastify route heuristic misses a real route or misidentifies a non-route call as one | Incomplete or noisy `api_observed` facts | No mechanical prevention; `kc verify` and dogfood review are the correction mechanism, same as existing heuristic-sourced facts |
| Mixed JS/TS repo produces overlapping component slugs from two different analyzers touching related files | Possible identity-cascade collision | ADR-004's cascade (anchor overlap, name similarity) is the existing mechanism for this — no special JS-specific handling anticipated, but unverified without real mixed-repo dogfood evidence |

## Assumptions

- `tree-sitter-javascript` remains a maintained, available package for the Python bindings KC already uses.
- Fact IR's existing shapes are sufficient for JS extraction — confirmed at implementation time; no new fact type was needed.

## Open Questions (resolved at implementation)

- Shared helper code between `TypeScriptAnalyzer` and `JavaScriptAnalyzer`: kept fully independent, per Option A's stated rationale — no shared module.
- Module-system ambiguity (CommonJS `require()` vs. ESM `import`): both are extracted as `dependency_observed` facts. `require()` detection is a full-tree scan (not limited to variable-declarator RHS), so bare calls (`require('./x');`), chained calls (`require('dotenv').config()`), and assignment RHS (`module.exports = require('./y')`) are all found — this full-tree-scan approach was itself a fix applied after an adversarial-review pass found the original declarator-only implementation silently dropped these common CJS shapes.
- JSX handling: no extra work needed — `tree-sitter-javascript` parses JSX natively.

## Impact

Affected code:

- New `extractors/javascript_analyzer.py`
- `compiler/run.py`'s `_extract()` — one more analyzer in the fixed list run every Extract stage
- `pyproject.toml` — new `tree-sitter-javascript` dependency

Affected documents:

- `docs/architecture.md` §1 (language split), §8 (language analyzers) — post-freeze additive notes
- `docs/decisions/index.md`, `docs/decisions/README.md` — status updated to Accepted

## Alternatives Rejected

Options B (extend `TypeScriptAnalyzer` to also claim `.js`/`.jsx`) and C (no JavaScript support) were rejected in favor of Option A, for the reasons given in Considered Options above.

## Future Reconsideration

Revisit if dogfood evidence on a real JS-heavy repository shows the Express/Fastify route heuristic's false-positive/negative rate (regex-on-raw-text, shared design with the TS/Python analyzers) is unacceptable, or if a genuinely new JS-specific fact-extraction need surfaces.

## References

- [ADR-006](ADR-006-language-analyzers.md) — the language-analyzer backbone and per-language plugin pattern this ADR extends
- `knowledge_compiler/extractors/python_analyzer.py`, `typescript_analyzer.py` — the existing analyzer pattern this mirrors
- `knowledge_compiler/extractors/javascript_analyzer.py` — the implementation
- `tests/test_javascript_analyzer.py` — 23 tests, including regression coverage for the describe()-nesting, `.skip`/`.only`, bare-`require()`, and CJS-`exports`-assignment gaps found by post-implementation adversarial review
- `docs/ir.md` §2 — Fact IR shapes `JavaScriptAnalyzer` emits without deviation

## Self-Review

- **Truly architectural?** Marginally — it's mostly an application of an already-decided pattern (ADR-006) to a new language, recorded as its own ADR because the user requested it be tracked explicitly rather than folded silently into ADR-006's existing scope.
- **Already made?** Yes — implemented 2026-08-06.
- **Reversible?** Fully — a single new file (`javascript_analyzer.py`) plus one line in `compiler/run.py`'s `_extract()`; removing both fully reverts.
- **Dependent future documents:** `docs/architecture.md` (updated with post-freeze additive notes), `docs/ir.md` (no change needed — no new fact types).
- **Exposes unresolved decisions:** none remaining — all open questions were resolved at implementation time (see above).
