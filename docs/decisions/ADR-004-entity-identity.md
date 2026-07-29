# ADR-004: Stable Entity Identity

## Status

Accepted

## Date

2026-07-17

## Context

**Definition.** An *entity* is the canonical representation of a piece of engineering knowledge that persists across compilations. Facts are per-compile and disposable; entities are durable — they accumulate provenance, anchor wiki pages, and appear in deltas. This definition is the bridge to the canonical IR (`ir.md`): the IR describes entity *shape*; this ADR decides entity *identity*.

The Knowledge Compiler promises **knowledge deltas**: a merged PR produces "Business Rule X modified, API Y added" rather than a pile of re-extracted facts. That promise rests entirely on the compiler's ability to recognize that an entity produced by *this* compile is *the same entity* as one produced by a previous compile. Without stable identity:

- The **Diff stage** degenerates: every change becomes "delete + add," and "which business rules changed recently?" — a headline use case — becomes unanswerable.
- **Wiki cross-links** break: links are entity slugs; unstable slugs mean dead links on every recompile, violating the cross-link consistency requirement.
- **Provenance** is orphaned: an entity's accumulated history (which PRs shaped it, which artifacts it derives from) is only meaningful if the entity persists.
- **Incremental compilation** loses its meaning: if each compile mints a fresh universe of entities, "incremental" is just "smaller full compile."

Identity is trivial for entities with external identifiers and genuinely hard for entities whose *existence* is an LLM judgment. This ADR decides the identity mechanism for every entity type.

### Entity categorization

| Category | Entities | Identity source |
|---|---|---|
| **Deterministic** — an external or structural key exists in the source artifacts | Pull Request, Jira Story, Project, Component, API, Test Coverage | PR number; Jira issue key; repo config; module/package path; HTTP method + route (or OpenAPI `operationId`); test node id (e.g., pytest node id) |
| **Derived** — identity computed from other entities' identities | Wiki Page | owning entity slug + page type; a Wiki Page never has independent identity |
| **LLM-derived** — the entity is not named anywhere in the source; extraction itself is an LLM judgment | Feature, Business Rule, Risk | **undecided — the subject of this ADR** |

Deterministic and derived identity are settled by the table above (natural keys; no viable alternative was found that beats keys the source already provides). The remainder of this ADR concerns the LLM-derived category, with one cross-cutting rule for all categories at the end of the Decision section.

## Decision Drivers

- **Determinism / reproducibility** — the vision permits nondeterminism "in wording, never in provenance or structure." Identity is structure.
- **Incremental compilation** — identity must be assignable when only a PR's slice of the repo is recompiled.
- **Accuracy of deltas** — "modified" vs. "deleted + added" must be right often enough that engineers trust the delta.
- **Simplicity** — no identity infrastructure that requires its own operations manual.
- **Provenance & auditability** — every identity assignment must be explainable from recorded evidence.
- **Future language support** — the mechanism must not encode Python- or TypeScript-specific assumptions.
- **Maintainability** — failure modes must be visible and correctable, not silent.

## Considered Options

### Option A — Content-hash identity

Entity id = hash of the entity's normalized content (name + description + attributes).

**Pros**

- Perfectly deterministic and history-independent; identical content always yields identical identity, even in a from-scratch rebuild.
- Zero matching logic; trivially simple.

**Cons**

- **Destroys the "modified" delta.** Any change to an entity's content — including an LLM rewording a description with no semantic change — produces a new hash, hence delete + add. The knowledge delta, the system's core mechanic, becomes structurally impossible for exactly the entities it matters most for.
- Wiki links break on every content change.
- Provenance history fragments across hash generations.

### Option B — LLM-assigned identity

During extraction or normalization, the LLM is shown existing entities and asked to either match the new candidate to one or declare it new.

**Pros**

- Highest ceiling on matching intelligence: an LLM can recognize that a rewritten rule is "the same rule" through paraphrase, refactor, and relocation simultaneously.
- No threshold engineering.

**Cons**

- **Violates the reproducibility principle directly**: identity is structure, and this makes structure nondeterministic. Two runs over identical input can produce different entity graphs.
- Unauditable: "the model said so" is not recordable evidence.
- Deltas become non-reproducible, so `kc verify` (incremental ≡ full) can never pass reliably.
- Couples the knowledge base's *skeleton* to a specific model's behavior; a model upgrade can silently re-identify the corpus.

### Option C — Pure anchor-based deterministic keys

Identity = hash of the entity's **provenance anchors** (the set of files/functions/spans it was extracted from). No matching step; the anchor set *is* the key.

**Pros**

- Deterministic and history-independent, like Option A, but survives pure rewording (anchors unchanged ⇒ identity unchanged).
- No thresholds; simple to implement.

**Cons**

- **Refactors churn identity**: moving a function to a new module changes the anchor set, producing delete + add for an unchanged business rule — precisely when engineers most need "this rule survived the refactor."
- **Collisions**: one function embodying two business rules gives both rules the same anchor key.
- Anchor granularity becomes a load-bearing, language-analyzer-specific choice, leaking language details into identity.

### Option D — Match-then-mint with a deterministic cascade

At Normalize time, each LLM-extracted candidate is matched against **current-state entities of the same type** using an ordered, deterministic cascade; if no match clears the bar, a new stable slug is minted. The cascade, in strict priority order:

1. **External key match** — if the candidate carries an explicit identifier (e.g., a Jira-referenced rule id), use it.
2. **Provenance-anchor overlap** — compare the candidate's anchor set (files/symbols it derives from, with git rename detection applied first) against existing entities' anchors; overlap above threshold ⇒ match.
3. **Normalized-name similarity** — string-level similarity (case/punctuation-normalized, token-based) above a conservative threshold, scoped to candidates sharing at least one component. Last resort only.

The LLM proposes *content*; it never assigns or matches identity. Matching is a pure function of (candidate, current state, thresholds). Each match records which cascade rule fired and the evidence, in `provenance`.

**Pros**

- Survives both rewording (anchors match) and refactors (rename detection + name similarity), the two common evolution paths.
- Deterministic **given prior state**: same candidate + same current state ⇒ same outcome, satisfying reproducibility where it operationally matters (incremental compiles, `kc verify`).
- Every assignment is auditable: the fired rule and evidence are recorded.
- Language-agnostic: anchors are canonical-model references, not language constructs.

**Cons**

- **History-dependent**: a from-scratch rebuild into an empty database mints fresh slugs, so state is reproducible only *modulo slug renaming* (see Decision for how this is handled honestly).
- Threshold engineering is real work with real failure modes (over-merge / over-split; see Failure Modes).
- Matching cost at Normalize time (mitigated: candidates are scoped by type and component, so comparison sets are small).

### Option E — Human-curated identity registry

Features/rules/risks are declared with stable ids in a registry file in the repository (e.g., `knowledge.toml`); the LLM maps extracted content onto declared entities.

**Pros**

- Maximal stability; identity is source-of-truth by definition (the registry lives in the repo).
- Deltas and links are exact.

**Cons**

- **Shifts the compiler's core burden onto engineers** — the system's premise is extracting knowledge that is *implicit* in artifacts; requiring it to be made explicit first is a different (and much weaker) product.
- Rots silently: an unmaintained registry produces confidently wrong identity.
- Cold-start cost on the 5M LOC dogfood repo is prohibitive.

## Decision

**Option D — match-then-mint with a deterministic cascade — for LLM-derived entities; natural keys for deterministic entities; computed identity for derived entities.**

Refinements this ADR adds beyond `architecture.md` §6 (which it will inform, not contradict):

1. **Anchor overlap outranks name similarity, strictly.** Names are LLM wording — the least stable evidence available. Provenance anchors are recorded, deterministic evidence. The cascade order is a normative part of this decision, not an implementation detail.
2. **The identity map is compiled state.** A *full recompile into an existing database* runs the same cascade against current entities, and therefore **preserves slugs**. Only a rebuild into an empty database mints fresh slugs. Consequently the reproducibility guarantee is stated precisely: *from-scratch rebuilds reproduce state equivalent modulo slug renaming; `kc verify` compares states by entity matching (the same cascade), never by slug equality.* This closes a gap in the vision's "reproducible" principle that would otherwise surface as mysterious `kc verify` failures.
3. **Prefer over-split to over-merge.** When matching is uncertain, mint a new entity. Over-split is visible (spurious delete+add in a human-reviewed delta) and correctable; over-merge silently corrupts an entity's history and provenance. All thresholds are therefore set conservatively.
4. **The LLM never assigns identity** — restated as an invariant because it is the boundary that keeps structure deterministic while wording remains free.
5. **Match evidence includes the numeric signals.** Every match records not just which cascade rule fired but the measured values (e.g., anchor overlap 0.92, name similarity 0.87) and the decision. Identity remains deterministic — scores never influence the outcome beyond the fixed thresholds — but the recorded evidence becomes rich enough to debug and retune matching from production data.

Why D over the alternatives: A and C are eliminated by the delta mechanic itself (A cannot say "modified" at all; C cannot say it across refactors). B is eliminated by the reproducibility principle — it is the only option that makes the entity *graph* nondeterministic. E inverts the product premise. D is the only option that keeps deltas meaningful, structure deterministic-given-state, and every assignment auditable.

## Architectural Invariants

This ADR establishes rules that every future component MUST obey — they are not implementation suggestions:

- Entity identities are immutable.
- LLMs never assign or mutate identity.
- Identity matching is deterministic (a pure function of candidate, current state, and configured thresholds).
- Every identity assignment is explainable.
- Every identity assignment records evidence, including the numeric signal values.
- Slugs are stable once minted.

> **Project philosophy — when uncertain, split; never silently merge.**
> Preferring over-split to over-merge is not a local tuning choice: it is the general stance that *visible, correctable noise beats silent corruption*. Future decisions in extraction, matching, and conflict resolution (see vision.md open question on conflict handling) should apply the same principle.

## Consequences

### Positive

- "Modified" deltas work across the two dominant change patterns: rewording (anchors carry identity) and refactoring (rename detection + name similarity carry it).
- Wiki cross-links (slugs) survive recompiles; link consistency needs no separate mechanism.
- Provenance accumulates on persistent entities, enabling "which PR introduced this rule?"
- Every identity decision is explainable from recorded evidence — auditable structure, per the vision.
- Identity logic lives entirely in Normalize; language analyzers and extractors stay identity-unaware, preserving the plugin boundary for future languages.

### Negative

- Identity is history-dependent: fresh-database rebuilds do not reproduce slugs bit-for-bit (equivalence is modulo renaming).
- Threshold tuning is an ongoing maintenance obligation, revisited as the dogfood repo exercises edge cases.
- Normalize gains a matching step with nontrivial logic — the most complex deterministic code in the compiler.

### Tradeoffs Accepted

- **Slug-level reproducibility is traded for delta continuity.** Perfect history-independence (Options A/C) is possible only by giving up "modified" deltas; the delta is the product.
- **Some churn is accepted by policy**: conservative thresholds mean genuine matches will occasionally be missed (over-split) to avoid silent over-merge.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| **Over-merge** (two distinct rules matched to one) | Silently corrupted history and provenance | Conservative thresholds (policy: prefer over-split); anchor evidence recorded so bad merges are diagnosable; manual split = mint via delta correction, recorded as a delta event |
| **Over-split** (same rule re-minted after heavy refactor + rewording) | Spurious delete+add in delta; broken link continuity | Visible in human-reviewed deltas; `kc verify` reports match-rate metrics; thresholds tuned on dogfood evidence |
| **Anchor decay** (all anchoring code deleted, concept persists elsewhere) | Entity marked removed while conceptually alive | Correct behavior by default (evidence is gone); if it recurs, the concept re-mints on next extraction — accepted V1 behavior |
| **Slug collision** (two new entities normalize to the same slug) | Ambiguous links | Deterministic dedup suffix (`-2`, `-3`) at mint time |
| **Extraction flapping** (LLM extracts an entity from unchanged input on one run, not the next) | Add/remove churn | Largely prevented by the LLM cache (ADR-008): unchanged input ⇒ cached output ⇒ identical candidates; flapping only possible where input actually changed |
| **Rename-detection miss** (git fails to track a moved file) | Anchor match fails; falls through to name similarity or over-split | Accepted; over-split policy makes the failure visible rather than silent |
| **Long incremental drift** (accumulated matching decisions diverge from what a full compile would produce) | Incremental state degrades | `kc verify` runs the cascade-based equivalence check; periodic full recompile into the existing database re-grounds state without slug loss |

## Assumptions

- Provenance anchors (entity → source files/symbols) are reliably captured at extraction time for every LLM-derived entity — this is a hard requirement on extractor implementations.
- Git rename detection is good enough to carry anchors through typical refactors.
- Candidate sets at match time are small after type + component scoping (performance assumption at 5M LOC).
- The LLM cache (ADR-008) exists and is keyed to make unchanged inputs produce byte-identical extraction output.
- Deltas are human-reviewed often enough that over-split churn gets noticed (dogfood-phase assumption).

## Open Questions

- **Exact thresholds** for anchor overlap and name similarity — deliberately unnumbered here; to be tuned on the dogfood repo and recorded as configuration defaults, not ADR amendments.
- **Whether embeddings may participate in matching.** Recommended **no** for V1: it would couple identity (structure) to an embedding model, importing Option B's central flaw in diluted form. Revisit only with evidence that string + anchor signals are insufficient.
- **Manual identity corrections** — should a maintainer be able to assert "these two entities are the same"? If added, corrections must live in the repo (e.g., `kc.toml`), not the database, to preserve the disposable-artifacts principle.
- **Cross-repo identity** (the same conceptual feature spanning repositories) — out of scope for V1; multi-repo currently means `repo_id`-scoped identity.

## Impact

Affected documents:

- `architecture.md` §6 — this ADR refines the matching cascade order and adds the reproducibility-modulo-renaming statement (architecture.md to be updated after acceptance; not modified by this ADR)
- `ir.md` — the canonical IR relies on the entity definition and invariants established here: every IR entity carries a stable slug, and identity fields are outside the LLM-writable surface
- `data-model.md` — `entities.natural_key`, slug format, match-evidence columns (fired rule + numeric signals) in `provenance`
- `pipeline.md` — Normalize stage contract gains the matching step; Diff consumes matched identities

Affected compiler stages:

- **Extract** — must emit provenance anchors for every LLM-derived candidate (hard requirement)
- **Normalize** — owns the cascade; the primary implementation site
- **Diff** — consumes identity; emits `entity_moved` events when anchors relocate via rename detection
- **Persist** — slug uniqueness enforcement, dedup suffixing
- **Emit** — wiki filenames/anchors derive from slugs; unaffected by matching internals

## Alternatives Rejected

- **Content-hash identity (A)** — structurally cannot express "modified"; kills the knowledge delta.
- **LLM-assigned identity (B)** — makes the entity graph nondeterministic; violates "nondeterminism in wording, never in structure"; unauditable.
- **Pure anchor keys (C)** — identity churns on refactors and collides on shared anchors; the delta breaks exactly when it matters most.
- **Human-curated registry (E)** — inverts the product premise (compiler extracts implicit knowledge; registry demands it be made explicit); rots silently; prohibitive cold start at 5M LOC. May return post-V1 as an *optional* correction overlay (see Open Questions), never as the primary mechanism.

## Future Reconsideration

Revisit this ADR if:

- Dogfood evidence shows over-split churn above tolerable levels despite tuning — the signal that string + anchor evidence is insufficient and a stronger (possibly embedding-assisted) matcher needs evaluation against its reproducibility cost.
- `kc verify` equivalence-modulo-renaming proves too weak in practice (e.g., matching-based comparison itself becomes a source of disputes).
- Multi-repo support graduates from `repo_id` scoping to genuine cross-repo entities.
- A future model/tooling shift makes deterministic semantic matching (not generation) practical and auditable.

## References

- `docs/vision.md` — Design Principles 3 (deterministic whenever possible) and 5 (reproducible ≙ semantically equivalent; nondeterminism in wording, never in provenance or structure)
- `docs/architecture.md` — §6 (entity identity), §4 (pipeline: Normalize/Diff), §14 (challenge #2)
- ADR-003 — current-state + delta log: the state this cascade matches against
- ADR-008 — LLM cache: prevents extraction flapping, making match inputs stable
