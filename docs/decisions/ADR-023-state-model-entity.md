# ADR-023: State Model Entity for Behavioral Contract Knowledge

## Status

Accepted — **implemented 2026-08-26, Python analyzer only (V1 scope, TypeScript/JavaScript deferred)**

## Date

2026-08-26

## Context

CLAUDE.md's north star names two capabilities as this project's actual differentiation from generic repository-intelligence tools: a **Behavioral Contract** model (state/transition/failure-mode knowledge, not just structural facts) for "what must be verified," and a **Test Trust Score** for "is the test trustworthy." Neither existed as a compiled entity before this ADR. An external review of the compiled Knowledge Model (Project, Component, API, Feature, Business Rule, Risk, Test Coverage, Pull Request, Jira Story, Wiki Page, User Journey) surfaced the concrete gap directly: nothing represents that a resource is a state machine. `BusinessRule` captures a static invariant ("discount ≤ 20%"); `UserJourney` (ADR-017) captures an ordered, human-declared traversal *across* components/APIs. Neither captures "this resource has states `PENDING/AUTHORIZED/SETTLED/FAILED/EXPIRED/REFUNDED`, and `PENDING → AUTHORIZED` is legal while `SETTLED → PENDING` is not." Without that, `test_plan` recommendations bottom out at component/API granularity and can only suggest "test `POST /payment` returns 201" — never "test the illegal transition is rejected."

`BRAINSTORM-state-model.md` (2026-08-26) explored this gap in full: five options, a spike verifying the deterministic-extraction approach against real dogfood code (frida), and a recommendation to ship the deterministic-only slice (Option B) first, mirroring the discipline ADR-012 (deferred `VerificationRequirement`) and ADR-017 (`UserJourney` shipped deterministic-only, LLM path deferred) both established: ship less than the ideal design, extend once dogfood evidence shows where the real gaps are.

Two findings during implementation (not anticipated in the brainstorm) materially shaped the final design:

1. **Owner granularity is Component, not class.** Real code (`admin/services.py` in frida) mutates a `.status` field on objects whose class is only known dynamically (`db.query(model_cls)...` — `model_cls` is itself a parameter). Static analysis cannot resolve "which class does this state machine belong to" in general. Attributing the state model to the *module* (Component) that contains the mutating code — the same granularity `Business Rule.governs`, `mutation_kill_rate`, and every other cross-cutting signal in this IR already use — sidesteps the unresolvable type-inference problem entirely.
2. **Naive sequential-assignment scanning fabricates false edges.** A first-draft design (chain every same-field assignment in source order within a function) would have claimed `succeeded → failed` as a transition in a `try`/`except` block — two mutually exclusive branch outcomes that can never actually follow each other. The shipped design tracks state per branch independently and resets to unknown (`from_state: null`) after any branch construct where a field was touched, rather than merging or guessing. This was verified against real code (`claim_save_conversion/jobs.py`'s `ConversionJob`, a genuine `queued → running → {succeeded, failed}` state machine) before being trusted.

## Decision Drivers

- Serve the north star directly (Behavioral Contract), not general repository-understanding — CLAUDE.md's explicit prioritization rule
- Deterministic-first extraction (ADR-006): the V1 slice must stand alone, correct, with no LLM involvement
- Never fabricate a transition that cannot happen — representing *less* than the true graph is acceptable; representing a *false* edge is not (vision.md DP8, over-split beats over-merge, extended here to "under-claim beats over-claim")
- Fit the two-layer IR (ADR-009) and the existing entity/relationship/query extension patterns exactly — no new architectural mechanism

## Considered Options

Full exploration lives in `BRAINSTORM-state-model.md`. Summary of the five options considered there:

- **A — Do nothing.** Rejected: the motivating gap (shallow `expect 201` tests) persists unchanged; re-derives the same "every question re-derives understanding from scratch" problem `BusinessRule`/`Feature`/`UserJourney` already exist to solve.
- **B — Deterministic-only `state_model` entity (recommended, this ADR).** States + structurally-inferred transitions, Python analyzer only, no LLM. See Decision below.
- **C — Full hybrid: deterministic base + LLM-assisted graph completion.** Rejected for V1: the state-of-the-art search backing the brainstorm found LLM-based FSM inference proven for protocol implementations, not arbitrary business resources — repeating this unproven-application risk now would repeat the exact overreach ADR-017 deliberately avoided for its own LLM-candidate path. Real value (the payment example's cross-process webhook/retry cases) is left for a follow-on once B's dogfood results are in.
- **D — Fold transitions into `UserJourney` payload.** Rejected: conflates "one resource's own lifecycle" with "an ordered path through many resources" — the same identity-conflation objection ADR-017 raised against its own rejected Option C.
- **E — Annotation-only convention (`# kc:state:`), no static extraction.** Not built in this pass; remains a reasonable companion (mirrors ADR-022's `kc:external-key:` precedent) for authors to correct or extend what static extraction misses, noted as future work rather than blocking this ADR.

## Decision

**Option B, refined during implementation as described in Context.** `state_model` is a new, **deterministic-identity** Knowledge IR entity type (natural key: owning Component path + field name — same identity class as `Component`/`API`, never the LLM match-then-mint cascade).

**Extraction (Python analyzer only, `python_analyzer.py`):** scans every function/method body for `<anything>.<field> = "literal"` assignments where `field ∈ {status, state}` (configurable set). Same-level sequential assignments within one function chain `from_state → to_state` in source order. `if`/`elif`/`else` and `try`/`except` are treated as branch constructs: each branch runs from an independent copy of the pre-branch state (branches never see each other's assignments), and after the construct, any field touched in *any* branch resets to unknown (`from_state: null`) rather than merging — this is what prevents the false `succeeded → failed` edge. Loops and `with` bodies are walked sequentially without a branch reset (a known, accepted imprecision — general control-flow analysis is out of scope). Every emitted fact carries `confidence: "structural"`, labeling the extraction method explicitly rather than presenting an inferred edge as proven fact (ir.md §4.3's conflict-surfacing philosophy — represent uncertainty, never hide it).

**Fact type:** `state_transition_observed` — `{field, from_state (nullable), to_state, confidence, file}`, anchored to the owning module (`symbol_path` = module path, not a specific function — the entity is Component-scoped).

**Normalize aggregation:** groups facts by `(owning module, field)`, resolved by exact match against already-minted Component paths (a fact whose module doesn't resolve is silently skipped — same class as `mutation_score_observed`'s unresolved-module precedent). Payload: `states` (union of all from/to literals), `transitions` (deduplicated edges with `confidence`), `terminal_states` (states that never appear as a `from` — computed, not extracted). New relationship `models`: State Model → Component.

**Consumption:** `test_plan` gains a fifth `target_kind`, `transition_gap` — for a requested component with an associated state model, lists its known transitions unconditionally (with `confidence`). This is a deliberately weaker claim than `journey`'s or `api`'s recommendations: per-edge covered/uncovered detection would require semantic test-body analysis, out of scope for V1, so the tool surfaces the transition *shape* for review rather than a precise verdict — consistent with how `symbols`-kind recommendations already don't claim per-symbol coverage either.

**Wiki:** `state_model` joins `PAGE_OWNER_TYPES`; each page renders states, terminal states, and a transitions table explicitly labeled "structural inference," with a standing caveat that the list is not proven exhaustive.

## Consequences

### Positive

- Closes a real, previously-unrepresented gap: `test_plan` can now say "test `PENDING → EXPIRED`, not just `POST /payment` returns 201" for any component with an extractable lifecycle.
- Verified against real dogfood code, not synthetic examples: frida's `ConversionJob` (`queued/running/succeeded/failed`) and `admin/services.py`'s bundle/workflow/task lifecycle (`draft/released/active/deprecated/archived`, already covered by hand-written guard-condition tests `test_deprecation_fallback_blocked_when_no_other_version_exists` etc.) both produced clean, non-noisy extraction.
- Zero LLM involvement, zero validation surface beyond what deterministic extraction already requires — no new architectural mechanism, same posture as every other deterministic fact type (ADR-006).
- The branch-reset design is a real correctness property, not a convenience: it was specifically checked against the try/except case that would otherwise fabricate a `succeeded → failed` edge, and a unit test (`test_if_elif_else_branches_do_not_leak_into_each_other`) pins it.

### Negative

- **Coverage is real but narrow.** Only Python; only `.status`/`.state` field names; only intra-function structural patterns. `omnius_llmlib` (a library, not a stateful service) legitimately produces zero state models — correct behavior, not a bug, but it means this feature's value is concentrated in service-shaped repos.
- **Cross-process transitions are invisible by construction.** The motivating payment example's hardest cases (duplicate/late/out-of-order webhook, retry-after-timeout) span processes and cannot be seen by single-function static analysis. This is the same limitation Option C would have addressed and explicitly did not — a named, accepted gap, not a hidden one.
- **Module-granularity can conflate genuinely distinct lifecycles** if a module manages more than one type of stateful object under the same field name. Observed as *not* a problem in the one real example checked (`admin/services.py`'s shared draft/released/active/deprecated/archived pattern is genuinely one lifecycle applied generically across model classes) — but this is a coincidence of that codebase's design, not a property this ADR can guarantee holds elsewhere.
- **Loops/`with` bodies get no branch-reset treatment** — a state assignment inside a loop that runs conditionally could still produce a transition claim that isn't universally true. Accepted as V1 scope; flagged for the same reason as the two limitations above: explicit is better than silently wrong.

### Tradeoffs Accepted

- TypeScript/JavaScript analyzer support is deferred, not built — same "ship the highest-signal language first, extend once demand is real" discipline ADR-016 applies to Java.
- The annotation-convention companion (Option E) is not built in this pass — noted as a natural follow-on, not required for this ADR to stand alone.

## Failure Modes

- A transition list that looks exhaustive but isn't (the "no other edge exists" property is never claimed — the wiki page and `test_plan` recommendation both state this explicitly) must not be read by a consuming agent as a completeness guarantee.
- A module whose `.status`/`.state` field is used for an unrelated, non-lifecycle purpose (e.g., a boolean-like flag reusing the name) would still mint a `state_model` entity — no semantic validation that the field is actually a lifecycle. Not observed in the two dogfood repos checked, but not structurally prevented either.

## Assumptions

- `status`/`state` as the configured field-name set is representative of real Python code's naming conventions — validated against frida (both examples used `status`), not validated against a broader corpus.
- The branch-reset-to-unknown heuristic is conservative enough to never fabricate a false edge, at the cost of sometimes reporting `from_state: null` where a human could determine the true predecessor from context. This trade (under-claim over over-claim) is the explicit design choice, not an oversight.

## Open Questions

- Should the field-name set (`status`, `state`) be configurable per-repo via `kc.toml`, for codebases using a different convention (`phase`, `lifecycle_state`, etc.)? Not built in V1; revisit once a real repo needs it.
- Should `transition_gap` recommendations eventually attempt per-edge coverage detection (was this specific transition exercised by an existing test)? Explicitly out of scope here — would require semantic test-body analysis, a materially different and larger capability than structural source extraction.
- Relationship to a future guarded-transition extension (preconditions on a specific edge, e.g. `AUTHORIZED → REFUNDED` requires `refund_amount ≤ original_amount`): if ADR-012's `VerificationRequirement` deferral trigger ever fires, the right move is likely extending `state_model`'s transition payload with guard conditions, not resurrecting `VerificationRequirement` as a separate entity kind — noted here so the connection is on record rather than rediscovered later.

## Impact

Affected documents:
- ir.md §2.3 (new fact type `state_transition_observed`), §3.2 (new entity type `state_model`, deterministic identity class), §3.3 (new relationship `models`), §4.1 (aggregation mapping)
- data-model.md (`entity_type` value list; no new table required — reuses `entities`/`relationships`/`provenance`)
- pipeline.md (Extract: Python analyzer gains a new deterministic pass; Normalize: new aggregation rule)

Affected compiler stages: Extract (`python_analyzer.py`), Normalize (`compiler/normalize.py`, new `_state_models()` pass + `models` relationship + wiki-page ownership), Emit (`wiki/emitter.py`, new `_body_state_model` renderer), MCP (`mcp/queries.py`, `test_plan`'s new `transition_gap` target_kind).

## Alternatives Rejected

See Considered Options above; full reasoning in `BRAINSTORM-state-model.md`.

## Future Reconsideration

Extend to TypeScript/JavaScript once a dogfood repo demonstrates real signal there (same trigger discipline as ADR-016). Revisit the annotation-convention companion (Option E) if static extraction's false-negative rate (modules with real lifecycles that this pass misses — dynamic field names, cross-file state, non-literal assignments) proves high enough in practice to be worth the added authoring convention. Revisit cross-process transition modeling (Option C) only after this deterministic slice has run in production for several dogfood cycles, per the same "measure first, extend later" discipline as ADR-012 and ADR-017.

## References

- `BRAINSTORM-state-model.md` (full option exploration, spike results, decisive-uncertainty reasoning)
- ADR-012 (VerificationRequirement deferral — the sub-component-precision precedent this ADR's Open Questions connects to for guarded transitions)
- ADR-017 (UserJourney — the closest structural precedent: deterministic-only V1 slice, LLM path deferred, same discipline applied here)
- ADR-006 (deterministic-first extraction), ADR-009 (two-layer IR boundary), ADR-004 (deterministic identity class, same as Component/API)
- `knowledge_compiler/extractors/python_analyzer.py` (`_walk_state_block`, `_branch_state`, `_maybe_state_assignment`), `knowledge_compiler/compiler/normalize.py` (`_state_models`), `knowledge_compiler/mcp/queries.py` (`test_plan`'s `transition_gap` branch)
