# ADR-014: Shared Declarative OKF Rules File (Emitter + Validator)

## Status

Proposed — **not implemented**. Recorded to hold the design and its rationale; no code exists for it yet.

## Date

2026-08-06

## Context

`kc validate-okf` ([ADR-013](ADR-013-open-source-okf-conformance.md)) checks an emitted wiki bundle against OKF v0.2's conformance rules — reserved-filename frontmatter restrictions (`index.md`, `log.md`), and the required-`type` rule for concept files. Those rules are currently hard-coded as Python `if`/`elif` branches in `knowledge_compiler/wiki/okf_conformance.py`.

Separately, `knowledge_compiler/wiki/emitter.py` independently encodes the *same* structural facts as rendering decisions — `_render_index()` happens to emit only an `okf_version` frontmatter key because that's how the function was written; `_render_log()` happens to emit no frontmatter at all for the same reason. Nothing ties these two encodings together. The rules the validator checks and the shape the emitter produces are two independent expressions of the same underlying spec facts, kept in sync only by whoever edits both files remembering to do so.

This is a real, if currently latent, risk: a future edit to either file — fixing a bug in one, adding a feature to the other — could silently desync them. The emitter could start producing output the validator wouldn't catch as wrong, or the validator could start rejecting output the emitter correctly produces. Nothing in the codebase today prevents that drift.

## Decision Drivers

- Single source of truth — the same structural fact (e.g. "log.md carries no frontmatter") should exist in exactly one place, not two independently-maintained encodings
- Cheap, safe spec-version upgrades — editing declarative data should be lower-risk and more reviewable than editing imperative control flow in two files
- Earlier failure detection — a conformance violation should ideally be caught at emission time (loud failure), not only downstream by a separate `kc validate-okf` run
- Simplicity — no premature generalization to spec facts KC doesn't need yet (ADR-007's boring-infra principle: no heavyweight abstraction ahead of real demand)

## Considered Options

### Option A — Shared declarative rules file, generic interpreter in both emitter and validator (structural rules only)

A small JSON (or TOML) file — e.g. `docs/okf-rules/okf-rules-0.2.json` — declares exactly the *structural* facts both sides need: which filenames are reserved and what frontmatter they may carry, and which top-level fields are required on concept pages. `okf_conformance.py`'s `check_bundle()` becomes a generic interpreter over this file instead of hard-coded per-filename branches. `wiki/emitter.py` imports the same file: `_render_index()`/`_render_log()` consult it directly rather than hard-coding the constraint, and `_frontmatter()` self-checks that all required fields are non-empty before writing a page, raising instead of silently emitting a violation.

Explicitly scoped to **structural** rules only — reserved-filename shape and required-field presence. Content mapping (how `type`, `title`, `files`, `generated` get their actual values from an `Entity`) stays exactly what it is today: KC's own domain logic in `emitter.py`, untouched by this file. Future OKF concept-type-specific schemas (e.g. `Attested Computation`'s `runtime`/`executor`/`attester` sub-fields) are explicitly out of scope until KC actually implements support for that concept type.

**Pros**

- Closes the actual, present drift risk between emitter output and validator rules — the two things that most concretely need to agree.
- Upgrading to a new OKF spec version becomes: read the new spec, write a new rules file, bump which one is active — a reviewable data diff, not a code change in two places.
- Conformance failures move earlier — the emitter can fail loudly at emission time for a missing required field, rather than only being caught by a separate, easy-to-forget `kc validate-okf` invocation.
- Enables validating older/retired bundles against a pinned prior version (`kc validate-okf --spec-version 0.1`), which the current hard-coded-to-one-version validator cannot do at all.

**Cons**

- A real refactor of working, tested code (`okf_conformance.py`'s interpreter, `emitter.py`'s two render functions and `_frontmatter()`) — not a pure addition; regression risk in code that currently passes its own conformance tests.
- The rules file is still hand-authored by a human reading OKF's prose spec — this does not make spec upgrades automatic, only cheaper and safer once a human has done that reading.
- A new "rules file schema" is itself a small contract to design correctly up front (what shape does a rule take, how are reserved filenames vs. concept-file rules distinguished) — getting this wrong now means a second migration later.

### Option B — Leave the two encodings independent, rely on tests to catch drift

Keep `okf_conformance.py` and `emitter.py` exactly as they are; add or strengthen tests that fail if the emitter's output would fail the validator's checks (already partially true — `tests/test_okf_conformance.py` and `tests/test_wiki_emitter.py` both exercise real emitted output today).

**Pros**

- Zero refactor risk; ships nothing new.
- Tests already provide *some* of this guarantee today, informally.

**Cons**

- Tests catch drift only for cases someone thought to write a test for — they are a safety net over two independent implementations, not a structural guarantee that the two can't diverge.
- Every future spec-version bump still requires editing imperative logic in two separate files by hand, with no data-diff-only path.
- Does nothing for the "self-check at emission time" benefit — violations are still only caught downstream, if at all.

### Option C — Full schema-driven template engine covering all current and future OKF concept types (including type-specific schemas like Attested Computation)

Generalize far enough that adding a new OKF concept type requires zero emitter code — purely a schema/template addition.

**Pros**

- Maximally future-proof against OKF's evolution.

**Cons**

- Premature generalization for concept types KC does not emit and has no concrete plan to emit (`Attested Computation` is explicitly unexplored per `docs/okf-conformance.md`'s known-gaps list). Building a general schema-to-render engine ahead of real demand is exactly the abstraction this project's own design principles warn against.
- Content-value computation (how a `type` or `files` field's *value* is derived from an `Entity`) is inherently KC domain logic; forcing it into a generic template system doesn't remove that logic, it just relocates it somewhere harder to read and test.

## Decision

**Not yet decided for implementation — this ADR records Option A as the recommended design, Proposed status, explicitly unbuilt.** Option A is preferred over B (B leaves the real drift risk unaddressed, relying on test coverage as the only guard) and over C (C generalizes past any concrete, current need). If and when this is implemented, the scope is Option A exactly as described: structural reserved-filename and required-field rules shared between emitter and validator; concept-type-specific schemas (Option C's scope) remain future work, gated on KC actually needing them.

## Architectural Invariants (if implemented)

- Reserved-filename frontmatter constraints exist in exactly one place — the shared rules file — never independently re-encoded in emitter rendering logic or validator checks.
- The emitter self-checks required fields against the same rules file it would share with the validator; a missing required field is a loud failure at emission time, not merely a downstream conformance-check finding.
- Content-value computation (mapping an `Entity` to actual field values) remains outside the rules file — the file describes structure and presence, never how to derive a value.
- Type-specific concept schemas (e.g. `Attested Computation`) are added to the rules file only alongside actual emitter support for that concept type — never speculatively ahead of it.

## Consequences (if implemented)

### Positive

- Eliminates the specific, present drift risk between `emitter.py` and `okf_conformance.py`.
- Spec-version upgrades become a reviewable data diff instead of a code change spread across two files.
- Conformance violations are caught earlier (emission time) rather than only via a separate validate step someone has to remember to run.

### Negative

- A real refactor of currently-working, currently-tested code — genuine regression risk that has to be managed with the existing test suite (`test_okf_conformance.py`, `test_wiki_emitter.py`) as the safety net during the change.
- One more small file-format contract (the rules file's own shape) to get right and maintain.

### Tradeoffs Accepted (if implemented)

- Scope discipline: structural rules only, explicitly not type-specific schemas — deliberately leaves `Attested Computation` and any other future concept-type work fully out of scope, to avoid building for a need that doesn't exist yet.

## Failure Modes (if implemented)

| Failure | Effect | Handling |
|---|---|---|
| Rules file itself is malformed | Both emitter and validator fail identically | Fail loudly at load time — a malformed shared rules file breaking both consumers identically is preferable to either silently ignoring it |
| A new OKF version needs a rule shape the interpreter doesn't support (e.g. a genuinely new kind of constraint, not just new field names) | Rules file alone can't express the new rule | Requires an interpreter code change alongside the data change — same category of work as today, just narrower in scope and less frequent |
| Emitter's self-check and validator's check drift anyway (e.g. one side caches a stale copy of the rules file) | Silent conformance drift returns | Both must load the rules file from the same single location at runtime — no per-consumer copies |

## Assumptions

- OKF's future versions continue to be expressible as structural constraints (required/allowed frontmatter keys, reserved-filename restrictions) rather than requiring a fundamentally different validation mechanism.
- The volume of structural rules stays small enough that a simple declarative file (not a full schema language) remains adequate — reconsider if the rule set grows substantially more complex.

## Open Questions

- Exact rules-file format (JSON vs. TOML vs. something else) — leaning JSON to avoid introducing a YAML/TOML-parsing dependency beyond what's already used, but not decided.
- Whether `kc.toml` should let a repo pin a specific OKF spec version to validate/emit against, or whether the compiler's own `OKF_SPEC_VERSION` constant should always be the sole source of truth.
- Whether this warrants versioning the rules-file format itself, separately from the OKF spec version it encodes.

## Impact (if implemented)

Affected code:

- `knowledge_compiler/wiki/okf_conformance.py` — `check_bundle()` becomes a generic rules-file interpreter
- `knowledge_compiler/wiki/emitter.py` — `_render_index()`, `_render_log()`, `_frontmatter()` consult the same rules file
- New: a rules-file location (e.g. `docs/okf-rules/`) and its loader

Affected documents:

- `docs/okf-conformance.md` — "Known gaps" section, until implemented
- `docs/decisions/index.md` — this ADR's entry

## Alternatives Rejected

Not rejected — **not yet decided**. Options B and C are recorded above with honest tradeoffs; this ADR does not commit to Option A over them, only recommends it as the best-understood path if/when this work is picked up.

## Future Reconsideration

Revisit when either: (a) someone picks this up for implementation, at which point the open questions above need answers first, or (b) OKF's evolution reveals the structural-rules-only scope is insufficient (e.g. type-specific schemas become unavoidable because KC starts emitting a typed concept like `Attested Computation`), at which point Option C's tradeoffs should be re-evaluated against real, not speculative, need.

## References

- [ADR-013](ADR-013-open-source-okf-conformance.md) — OKF spec-version tracking and the emitter/validator split this ADR proposes unifying
- [ADR-007](ADR-007-plugin-architecture.md) — boring-infra principle motivating the structural-rules-only scope
- `knowledge_compiler/wiki/okf_conformance.py`, `knowledge_compiler/wiki/emitter.py` — the two currently-independent encodings this ADR concerns
- `docs/okf-conformance.md` — "Known gaps" section

## Self-Review

- **Truly architectural?** Yes — it concerns whether two subsystems' shared assumptions live in one place or two, which is exactly the kind of boundary decision this project records as an ADR.
- **Already made?** No — explicitly not decided for implementation; this ADR exists to hold the design, not announce a shipped change.
- **Reversible?** Fully — nothing has been built; adopting or discarding this design later costs nothing beyond the analysis already captured here.
- **Dependent future documents:** `docs/okf-conformance.md` (once implemented), this ADR's own status line (Proposed → Accepted or Rejected, once someone actually decides to build or explicitly declines to).
- **Exposes unresolved decisions:** rules-file format, `kc.toml` version-pinning, rules-file-format versioning — all listed as open questions above.
