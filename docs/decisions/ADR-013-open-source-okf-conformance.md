# ADR-013: Open-Source Release as `open-knowledge-compiler`, OKF Spec-Version Conformance and Migration

## Status

Accepted — **the Option A mechanism (item 2: OKF spec-version conformance and migration) is implemented**, verified directly against code on 2026-08-26 while auditing the remaining ADR backlog: `OKF_SPEC_VERSION = "0.2"` (`knowledge_compiler/__init__.py`), `compile_runs.okf_spec_version` (a real column, `storage/schema.py`), bundle-root `index.md`'s `okf_version` frontmatter field (`wiki/emitter.py`'s `_render_index`), and the `kc compile --emit-only` re-render path (`cli.py`, wired to `compiler/run.py`'s `emit_only`) all exist and match this ADR's normative details exactly. The specific v0.1→v0.2 conformance bugs this ADR committed to fixing before release (`index.md` frontmatter restricted to `okf_version` only, `generated.at` replacing the old `timestamp` field, `log.md`'s date-grouped prose structure) are also already fixed in the current emitter — confirmed by direct reading, not assumed. **Item 1 (the open-source release itself — licensing, scrubbing internal references, community docs) remains untouched and was never this ADR's scope** — its own Context section explicitly tracks that outside this decision record. Status was flipped from Proposed without any code change; this is a documentation correction recording work that had already shipped, most likely alongside the OKF v0.2 conformance fixes bundled into earlier dogfooding passes, without the ADR's status line being updated at the time.

## Date

2026-08-05 (proposed) — 2026-08-26 (status corrected to Accepted; mechanism was already implemented)

## Context

Knowledge Compiler already emits an OKF-conformant wiki as its V1 wedge (ADR-010): YAML
frontmatter over Markdown, one concept per file, `index.md` entry points, published to a
dedicated branch. The Open Knowledge Format (OKF) is an external, independently evolving
specification (Google Cloud Data Analytics/Data Cloud, `github.com/GoogleCloudPlatform/
knowledge-catalog/tree/main/okf`) — not something KC controls the pace or shape of. Two things
surfaced together and need a single decision record:

1. **Open-sourcing KC** as `open-knowledge-compiler` positions it as a reference *producer* for
   the OKF ecosystem (the spec explicitly separates producers from consumers and welcomes
   independent implementations) — this requires release-readiness work (licensing, scrubbing
   internal references, community docs) that is a one-time project, not an architectural
   decision, and is tracked outside this ADR.
2. **OKF itself is versioned and evolving out from under KC.** During this planning pass we
   discovered the authoritative spec had already moved to **v0.2** with breaking changes from the
   v0.1 draft KC's emitter was originally written against (`timestamp` → `generated.at`,
   `# Citations` body section → `sources` frontmatter array, `index.md`/`log.md` structural
   rules stricter than initially assumed). This exposed real, pre-existing conformance bugs in
   `wiki/emitter.py` — not just a version-label mismatch. An external spec that can introduce
   breaking changes at any time, with KC as a producer against it, is a permanent architectural
   condition, not a one-time fix — hence this ADR.

## Decision Drivers

- Correctness — KC's emitted bundles must actually conform to the spec version they claim, not
  merely resemble an earlier draft of it
- Low migration cost — OKF spec bumps must not require re-collecting artifacts or re-running LLM
  extraction; the wiki is downstream of durable state, not the state itself
- Determinism / reproducibility — spec-version targeting must be explicit and recorded per
  compile, not inferred from code version at read time
- External-dependency discipline — the same rigor ADR-006/007/008 apply to language grammars,
  plugin interfaces, and LLM providers now applies to an external document-format spec
- Simplicity — no speculative infrastructure for spec versions that don't exist yet

## Considered Options

### Option A — Track `okf_spec_version` explicitly; migrate via wholesale re-emission, never data migration

Add an `OKF_SPEC_VERSION` constant in `wiki/emitter.py`, record it on `compile_runs` alongside
the existing `fact_vocabulary_version`/`knowledge_model_version` (ir.md §5's established
pattern), and stamp the spec-sanctioned `okf_version` field on the bundle-root `index.md`. A
breaking OKF spec bump is handled by updating the emitter's rendering functions and re-running
the **Emit stage only** against already-compiled Knowledge IR — never a data migration, since the
wiki is a disposable, wholesale-regenerated build artifact (ADR-010) and the database (ADR-001)
is the durable source of truth, never the rendered bundle.

**Pros**

- Directly reuses `ir.md` §5's proven versioning pattern — no new mechanism invented.
- Migration cost for a breaking spec bump is a code change + re-render, not a data migration;
  the same wholesale-regeneration property ADR-010 already established for wiki content changes
  applies unchanged to spec-version changes.
- `okf_version` on bundle-root `index.md` is not a KC invention — it's the spec's own sanctioned
  versioning field (v0.2 §12), so external OKF consumers get a version signal for free.
- Enables a cheap re-render primitive (`kc compile --emit-only`, tracked separately) that applies
  a spec bump across every previously-compiled repo without re-collecting or re-extracting.

**Cons**

- Requires a real (if narrow) `wiki/emitter.py` rewrite now to fix the v0.1-vs-v0.2 bugs already
  found (`index.md` frontmatter must not exist at all; `generated`/`sources` field collisions;
  `log.md`'s date-grouped-prose structure vs. KC's current compile-run-grouped bullet list).
- Every future breaking OKF bump is a required code change, not a passive pass-through — KC
  takes on ongoing maintenance tracking an externally-versioned spec, the same category of cost
  ADR-006 accepts for language grammars and ADR-008 accepts for LLM provider APIs.

### Option B — Freeze against OKF v0.1, treat later versions as out of scope

Ship the open-source release against the original v0.1 draft understanding and do not track
spec version at all.

**Pros**

- No emitter rework needed before release.

**Cons**

- **Ships already-known bugs** (index.md frontmatter violation, generated/sources key
  collisions) — a real OKF v0.2 consumer parsing KC's output today would misparse it. Releasing
  a "reference producer" tool that fails conformance against the current spec undermines the
  entire positioning rationale for open-sourcing it.
- No path forward when the spec bumps again — same problem resurfaces, next time in front of a
  public user base instead of during internal review.

### Option C — Database-less compile mode as the vehicle for spec-version agility

Speculatively design a stateless compile mode (no Postgres) on the theory that a simpler
rendering path would make spec-version changes easier to roll out.

**Pros**

- None specific to *this* problem — spec-version migration is already cheap under Option A
  because the wiki is disposable and separable from Collect/Extract/Normalize; a DB-less mode
  doesn't change that cost, it changes an unrelated infrastructure tradeoff (ADR-001).

**Cons**

- Conflates two independent questions. DB-less compilation is a real idea but cuts against
  ADR-001/ADR-003 foundationally (loses cross-run entity identity, the delta log, and the LLM
  cache) and needs its own design pass. Parked separately —
  see `BRAINSTORM-db-less-compile-mode.md` — explicitly **not** part of this decision.

## Decision

**Option A.** Track `OKF_SPEC_VERSION` as a first-class, versioned emitter property using the
existing `ir.md` §5 pattern; fix the concrete v0.1/v0.2 conformance bugs discovered during this
review before any public release; treat all future OKF spec migrations as **Emit-stage
re-renders against durable Knowledge IR**, never data migrations. The database-less compile mode
raised alongside this discussion is explicitly out of scope for this ADR (see
`BRAINSTORM-db-less-compile-mode.md`) — conflating it here would block a narrow, well-understood
fix behind an open architectural question.

Normative details:

- `wiki/emitter.py` declares `OKF_SPEC_VERSION` as a module-level constant (currently `"0.2"`).
- `compile_runs` gains an `okf_spec_version` column, populated at persist time alongside
  `fact_vocabulary_version`/`knowledge_model_version` (ir.md §5's existing mechanism, extended by
  one field — additive, non-breaking to that document's versioning scheme).
- Bundle-root `index.md` carries the spec's own `okf_version` field (its one permitted
  frontmatter-adjacent exception) — this is the spec's sanctioned mechanism, not a KC invention.
- Breaking OKF spec changes are handled by updating `wiki/emitter.py`'s rendering functions,
  bumping `OKF_SPEC_VERSION`, and re-running Emit — a full recompile is never required *because*
  of a spec bump (though one may coincidentally be due for unrelated reasons).
- Non-breaking (additive) spec changes require no action for already-emitted bundles under the
  spec's own consumer tolerance rules (v0.2 §11: consumers must not reject a bundle for missing
  optional fields introduced later).

Why A over the alternatives: B ships known-bad output under the exact banner ("conformant OKF
producer") the open-source release is built on — self-defeating. C solves a different problem
than the one this ADR exists to close, and forcing them together would delay a narrow, ready fix
behind a genuinely unresolved architectural question.

## Architectural Invariants

- The wiki bundle is never the durable record of OKF-spec-version state — `compile_runs` is.
- An OKF spec-version change is never expressed as a database migration against `entities`/
  `relationships` — only as an Emit-stage code change plus re-render.
- `OKF_SPEC_VERSION` is a single source of truth in code; the persisted `okf_spec_version` per
  compile run is a historical record of what was targeted at that compile, not a live pointer.
- Non-breaking spec additions never require touching already-emitted bundles; breaking changes
  are handled by wholesale regeneration (ADR-010's existing invariant), never partial patching.

## Consequences

### Positive

- KC's open-source positioning as a reference OKF producer is backed by a real, checkable
  conformance mechanism, not an assumption frozen at whatever draft the emitter happened to be
  written against.
- Spec-version migration cost is bounded and cheap by construction (re-render, not recompile,
  not data migration) — directly reusing ADR-009's Fact IR / Knowledge IR boundary and ADR-010's
  disposable-wiki invariant.
- `kc inspect`/`compile_runs` become the audit trail for "what OKF version does this repo's wiki
  currently target," answerable without inspecting file contents.

### Negative

- KC now carries an ongoing maintenance obligation to track an external, independently-versioned
  spec — the same category of cost already accepted for tree-sitter grammars (ADR-006) and LLM
  provider APIs (ADR-008), but a new instance of it.
- The fixes required before release (index.md frontmatter removal, `generated`/`sources` field
  collision fixes, `log.md` restructure) are real emitter code changes, not documentation-only —
  this ADR commits to that work before public release, not just to the tracking mechanism.

### Tradeoffs Accepted

- **Correctness over shipping speed**: the open-source release is gated on fixing the discovered
  bugs rather than shipping against a known-stale spec understanding.
- **Ongoing spec-tracking cost accepted** as the price of the "reference producer" positioning —
  if OKF is not worth tracking, this ADR's premise (open-sourcing under that positioning) would
  need to be revisited, not just this ADR's mechanism.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| OKF spec bumps breaking-ly and KC's emitter is not updated in time | Public bundles silently drift out of conformance | `OKF_SPEC_VERSION` is a visible, single-source-of-truth constant; `docs/okf-conformance.md` (tracked separately) is the audit surface reviewed on each upstream spec release |
| A breaking spec change requires new data KC doesn't currently capture (e.g. a future required field with no analog in Knowledge IR) | Emit-stage-only migration insufficient | Escalates to a normal Knowledge IR change under ir.md §5's existing breaking-change process (full recompile, slug-preserving) — not a new failure mode, falls back to an already-solved one |
| `--emit-only` (tracked separately) is run against a DB whose Knowledge IR predates fields the new emitter version expects | Partial/degraded bundle for that repo until a full recompile | Emit stage must tolerate absent optional Knowledge IR fields gracefully (mirrors OKF's own "consumers must not reject for missing optional fields" rule, applied internally) |

## Assumptions

- OKF will continue to evolve via additive-preferred, occasionally-breaking version bumps (per
  its own §12 versioning policy: minor = additive, major = breaking) — the same shape KC already
  assumes for its own Fact/Knowledge IR layers.
- The upstream spec repository (`GoogleCloudPlatform/knowledge-catalog`) remains the source of
  truth to track against; if OKF governance moves elsewhere, this ADR's mechanism is unaffected,
  only the tracked URL changes.
- Re-emission (Emit-stage-only rerun) remains sufficient for the overwhelming majority of future
  spec changes, because OKF is a rendering-layer concern for KC, not a Knowledge IR concern.

## Open Questions

- Whether `okf_spec_version` warrants its own `kc verify`-style drift check (e.g. "this repo's
  last-emitted bundle predates the emitter's current `OKF_SPEC_VERSION` — re-emit recommended")
  — plausible follow-on CLI ergonomics, not required for this ADR's core mechanism.
- Whether future OKF concept types (e.g. v0.2's `Attested Computation`) map onto existing KC
  entity types (e.g. mutation-kill rate as an attested computation) — a genuine future design
  question, explicitly not resolved here.
- Final scope of the database-less compile mode remains fully open — see
  `BRAINSTORM-db-less-compile-mode.md`; this ADR takes no position on it.

## Impact

Affected documents:

- `docs/okf-conformance.md` (new, tracked outside this ADR) — the living spec-to-emitter mapping,
  reviewed whenever `OKF_SPEC_VERSION` changes
- `docs/data-model.md` — `compile_runs.okf_spec_version` column
- `docs/ir.md` §5 — extended (additively) to mention the third tracked version alongside
  `fact_vocabulary_version`/`knowledge_model_version`
- `docs/pipeline.md` §3.6/§8 — Emit-stage re-render path (`--emit-only`, tracked separately)

Affected compiler stages:

- **Emit** — sole owner of OKF-spec-version targeting and the fixes this ADR commits to
- **Persist** — writes `okf_spec_version` alongside the other two version fields, same
  transaction, no new invariant

## Alternatives Rejected

- **Freeze against v0.1 (B)** — ships already-identified conformance bugs under the exact banner
  the release depends on; no forward path.
- **Database-less mode as the vehicle (C)** — solves an unrelated infrastructure question;
  conflating it here would block a ready, narrow fix behind an open architectural debate. Parked
  in `BRAINSTORM-db-less-compile-mode.md`.

## Future Reconsideration

Revisit if OKF's versioning policy itself changes shape (e.g. moves to a scheme incompatible with
minor/major additive-vs-breaking), if spec bumps become frequent enough that per-bump emitter
rework becomes the dominant maintenance cost, or if the parked database-less mode is picked up
and turns out to change how Emit-stage re-rendering is invoked.

## References

- `docs/vision.md` — Design Principle 5 restated by ADR-010 (wiki as disposable build artifact)
- [ADR-001](ADR-001-postgresql.md) — Accepted; the durable-store invariant this ADR's migration
  story relies on
- [ADR-003](ADR-003-current-state-delta-log.md) — Accepted; current-state + delta log, the
  mechanism `log.md`'s date-grouped history is derived from
- [ADR-004](ADR-004-entity-identity.md) — Accepted; slug stability assumed by "re-render, not
  recompile"
- [ADR-009](ADR-009-two-layer-ir.md) — Accepted; the Fact IR / Knowledge IR boundary that makes
  Emit-stage-only reruns possible
- [ADR-010](ADR-010-wiki-destination.md) — Accepted; wiki as disposable, wholesale-regenerated
  build artifact — the property this ADR's whole migration story depends on
- `ir.md` §5 — the `fact_vocabulary_version`/`knowledge_model_version` pattern this ADR extends
- `BRAINSTORM-db-less-compile-mode.md` — the explicitly-parked, out-of-scope alternative
- External: `https://okf.md/spec/` (v0.1 draft) and
  `https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md` (v0.2,
  authoritative) — the spec versions this ADR was written against

## Self-Review

- **Truly architectural?** Yes — it fixes how KC relates to an external, independently-versioned
  format spec going forward, and commits to a migration mechanism, not just a point-in-time fix.
- **Already made?** Yes, for this ADR's actual scope (item 2, the spec-version mechanism) — verified implemented 2026-08-26. ADR-010 already established the wiki-as-disposable-artifact invariant this ADR's migration story depends on; this ADR was the first to name OKF-spec-version tracking explicitly and to record the v0.1→v0.2 discovery, and its mechanism has since been built. Item 1 (the open-source release itself) remains separately unmade and out of this ADR's scope.
- **Reversible?** The tracking mechanism is low-cost and reversible (just a constant + a column);
  the underlying assumption (spec changes are re-renderable, not migratory) would need
  revisiting only if OKF started requiring genuinely new Knowledge IR data on a breaking bump.
- **Dependent future documents:** `docs/okf-conformance.md`, `docs/data-model.md`, `docs/ir.md`
  §5, `docs/pipeline.md`.
- **Exposes unresolved decisions:** database-less compile mode (parked separately), possible
  `kc verify`-style OKF drift check, future OKF concept-type mappings (`Attested Computation`).
