# ADR-017: UserJourney Entity for End-to-End Test Grounding

## Status

Accepted — **implemented 2026-08-08, scope reduced to Option B at implementation time**

Extraction sources 1 (E2E-header parsing) and 3 (LLM-candidate from Feature narratives) of Option A below are **deferred, not built**. What shipped is deterministic-only: journeys are declared explicitly via `kc.toml [[journeys]]` (an ordered list of already-compiled entity slugs), matching source 2's clustering intent without the Jira-epic-clustering machinery itself. This is Option B's tradeoff, accepted deliberately to keep the first PR's scope tractable and fully tested rather than half-implementing a new LLM candidate schema/cache/template pipeline; Option A's hybrid extraction remains the target for a follow-up ADR/PR once dogfood use of the deterministic-only path shows where the gaps actually are (per this ADR's own Future Reconsideration).

## Date

2026-08-08

## Context

The canonical Knowledge Model (vision.md, ir.md §3.2) compiles ten entity types: Project, Component, API, Feature, Business Rule, Risk, Test Coverage, Pull Request, Jira Story, Wiki Page. Business Rule already carries intent ("discount must not exceed 20%") and `governs` a Feature/Component/API (ir.md §3.3), but nothing represents a *sequence* of steps a real user takes across multiple APIs/components/rules.

This is a structural gap in `test_plan`'s ability to prevent coverage-gaming. An agent can satisfy every component- and API-level coverage recommendation individually — e.g. unit-testing `apply_discount()` in isolation — while never proving the full user-facing chain (add item → apply coupon → checkout → correct total) works together. `test_plan`'s two existing `target_kind`s (`api`, `symbols`, per `kc-cli-reference.md`) have no way to express "the steps are each tested, but the chain isn't."

Grounding evidence already exists that a human-authored convention for this is real, not hypothetical: the dogfood repo's own Playwright suite (`test_claims.py`) declares an "Endpoints covered:" header spanning 14 related routes — a hand-maintained journey declaration KC could formalize instead of inventing from nothing.

## Decision Drivers

- Extensibility (vision.md DP1: pluggable stages, additive entity vocabulary per ir.md §5)
- Determinism-first extraction philosophy (vision.md, ADR-006)
- Accuracy of test-quality signals (motivating: prevent coverage-percentage gaming)
- Incremental compilation (must fit the existing Fact IR → Knowledge IR boundary, ADR-009)

## Considered Options

### Option A: New LLM-derived entity, `UserJourney`, hybrid-extracted (recommended)

Add `UserJourney` as an eleventh entity type, same identity class as Feature/Business Rule/Risk (LLM-derived, match-then-mint cascade, mandatory anchors per ADR-004). Payload holds an **ordered** list of step references (API/Component/Business Rule slugs) plus a narrative. Extraction sources, in priority order:
1. Deterministic parsing of existing hand-written E2E test headers that already declare a multi-endpoint journey (the `test_claims.py` convention) — zero new invention, reuses a real human artifact.
2. Deterministic clustering of Jira epics/linked stories/PRs touching overlapping Features — LLM only for naming/narrative, not for step selection.
3. LLM-candidate extraction (`user_journey_candidate`, schema-validated, anchored per ADR-008) from Feature narratives that already describe multi-step intent, for journeys with no existing E2E test or epic grouping.

Pros: closes the coverage-gaming gap directly; reuses an existing human convention where available (highest-trust source first); fits the established LLM-candidate pattern (ADR-008) with no new architectural mechanism.
Cons: new entity kind = new extraction prompts, new fact type(s), new `test_plan` recommendation path — real, non-trivial scope, comparable to the `VerificationRequirement` proposal deferred in ADR-012.

### Option B: Deterministic-only (cluster from Jira epics/E2E headers, no LLM candidate path)

Skip source 3 above; only mint journeys where a deterministic source (E2E header or Jira epic cluster) already exists.

Pros: no LLM validation surface to build; strictly deterministic, cheapest to trust.
Cons: most repos and most features won't have a qualifying E2E header or Jira epic grouping yet — coverage of the *feature* would be sparse exactly where it's newest and least tested, which is the opposite of where this signal is most needed.

### Option C: Model journeys as a Feature attribute, not a new entity

Add an ordered step list directly onto the existing Feature payload instead of minting a new entity type.

Pros: no new entity kind, smaller IR change.
Cons: conflates two different identity questions — a Feature can have zero, one, or many distinct user journeys through it (e.g., "guest checkout" vs. "checkout with saved payment" are two journeys through one Feature), and forcing a 1:1 Feature↔journey relationship would either lose that distinction or push multiple orthogonal step-lists into one payload field, awkward for identity matching (ADR-004) and for `test_plan` to cite individually.

### Option D: Do nothing; leave journey-level reasoning to the consuming agent

`test_plan` stays at component/API/symbols granularity; an agent reasons about journeys itself using `get_entity`/`search_knowledge` calls across multiple Features.

Pros: zero compiler cost.
Cons: re-derives the same reasoning per agent per session with no shared, citable, stable identity for "this is the coupon-checkout journey" — the same argument that motivated Business Rule and Feature as compiled entities in the first place (vision.md: "every question re-derives the same understanding from scratch").

## Decision

**Option A.** Add `UserJourney` as a new LLM-derived entity type, extracted preferentially from existing deterministic sources (E2E test headers, Jira epic clusters) before falling back to LLM-candidate extraction from Feature narratives — in that order, so the most novel-per-repo case is also the most conservative (schema-validated, anchored) one.

## Consequences

### Positive

- `test_plan` gains a third `target_kind`, `journey`, distinguishing "every step tested individually" from "the chain is proven end-to-end" — the structural fix for the exact test-slip pattern that motivated this ADR.
- Reuses an already-observed human convention (`test_claims.py`-style headers) as the highest-trust extraction source, consistent with deterministic-first philosophy.
- Composes with the existing `kc-covers:` header and `validate-test` scoring mechanism (extend, don't replace — see companion ADR-018-equivalent follow-on work) rather than introducing a separate quality tool.

### Negative

- New entity kind touches Extract (new fact types), Normalize (new identity-cascade instance), and the `test_plan`/`kc-covers`/`validate-test` surfaces — the widest-reaching of the items in this backlog.
- LLM-candidate journeys (source 3) carry the same risk profile as Feature/Business Rule candidates: noisy or overlong journeys are possible and must go through the same schema validation and anchor requirements as existing candidates.

### Tradeoffs Accepted

- Journeys minted from Jira epic clustering will be as good as the org's actual epic-linking discipline — a repo with unlinked or informally-tracked epics will lean more heavily on the (more speculative) LLM-narrative path.
- Cross-repo journeys (a journey spanning Repo A's frontend and Repo B's backend) are explicitly out of scope for this ADR; see Open Questions.

## Failure Modes

- A malformed or overlong journey candidate (e.g., an LLM narrative describing an entire epic as "one journey") should split rather than silently merge, per vision.md DP8 — same over-split-over-merge bias already governing Feature/Business Rule/Risk identity.
- A journey whose steps span entities that no longer exist (post-removal) should be flagged, not silently retained with dangling references — mirrors the existing orphaned-wiki-page pruning precedent (CHANGELOG, wiki emitter fix).

## Assumptions

- At least one of the three extraction sources produces usable signal for any given repo; if none do, no journeys are minted for it and `test_plan` simply omits the `journey` target kind for that repo (graceful degradation, not a hard failure).
- Ordering within a journey's payload is sufficient to express the flow; branching/conditional journeys (if/else paths) are not modeled in V1.

## Open Questions

- Cross-repo journeys: deferred pending the same milestone-3 evaluation-methodology question ADR-011 already deferred cross-repo entity resolution behind.
- Relationship to Feature: should `UserJourney` gain an explicit `traverses` relationship to Feature (in addition to its step-list payload), or is the step list alone sufficient? Affects query ergonomics, not correctness.
- Identity-matching thresholds for journey candidates (name similarity, anchor overlap) need dogfood tuning, same as ADR-004's existing threshold-tuning precedent.

## Impact

Affected documents:
- ir.md (new fact types `journey_candidate`/journey-header-observed; new entity type + relationship considerations)
- data-model.md (new entity_type value; no new table required — reuses `entities`/`relationships`/`provenance`)
- pipeline.md (Extract: new deterministic + LLM extractors; Normalize: new identity-cascade instance)
- kc-cli-reference.md (`test_plan` new `target_kind`; `kc-covers:` header format)

Affected compiler stages: Collect (no change), Extract, Normalize, Diff, Persist, Emit (new wiki page type).

## Alternatives Rejected

Option B (deterministic-only) was rejected because it would leave newest/least-tracked features — exactly where journey-level grounding is most valuable — without any journey coverage. Option C (Feature attribute) was rejected because Feature↔journey is not 1:1. Option D (do nothing) was rejected for the same reason Business Rule and Feature exist at all: re-derived, non-shared, non-citable reasoning per agent per session.

## Future Reconsideration

Revisit scope (in particular, whether to add cross-repo journeys or branching/conditional journey support) once single-repo journeys have run in production for several dogfood cycles and real gaps in the model are observed — same "measure first, extend later" discipline as ADR-012's mutation-score threshold decision.

## References

- `BRAINSTORM-test-generation-mechanism.md` (dead-code and ceiling findings that motivate journey-level, not just component-level, gap detection)
- ADR-004 (entity identity — the cascade this reuses), ADR-008 (LLM candidate validation), ADR-009 (two-layer IR boundary), ADR-011 (cross-repo deferral precedent), ADR-012 (comparable-scope entity-addition precedent and its deferral reasoning)
