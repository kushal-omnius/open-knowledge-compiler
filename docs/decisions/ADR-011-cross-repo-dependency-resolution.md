# ADR-011: Cross-Repo Dependency Resolution — Query-Time Config Map, Not Compiled State

## Status

Accepted

## Date

2026-07-20

## Context

Each repository compiles into an isolated `repo_id` partition of one shared Postgres database (ADR-001). When a component in one repo imports a package that is itself another repo compiled into the same database, the compiler has no way to recognize that — the import shows up as an opaque string in `external_dependencies`, indistinguishable from a genuine third-party library. `decisions/index.md` (line 143) flagged this as a pre-milestone-3 design item, needed before test generation can reason across a dependency boundary (vision.md use case 4).

The dogfood repos gave this real grounding: `frida/backend/requirements.txt` pins `omnius-llmlib[bm25,phoenix]==2.10.2`, and multiple frida files `import omnius_llmlib` — a live cross-repo dependency between two repos already compiled into this database, not a hypothetical.

`BRAINSTORM-cross-repo-dependencies.md` worked through the option space in detail (refinement mode, starting from the coordinate-resolution sketch already recorded at `decisions/index.md:143`). This ADR records the outcome.

## Decision Drivers

- `repo_id` isolation (ADR-001 invariant) — must not require Normalize to read another repo's state during compile
- Determinism (ADR-006) — no LLM in the resolution path
- Avoid over-merge (ADR-004's spirit, extended here) — a wrong cross-repo link is worse than no link
- Simplicity — proportionate to the two-repo case actually in front of us; no schema migration for a design still gated on an open question (milestone-3 eval criteria)
- Reversibility — the decision should not foreclose a richer compiled model later

## Considered Options

Full option analysis, pre-mortems, and a comparison matrix are in `BRAINSTORM-cross-repo-dependencies.md`. Summary:

### Option A — Do nothing (defer past V1)

Leave `external_dependencies` opaque; answer cross-repo questions by manually opening both wikis.

**Pros:** zero cost now.
**Cons:** blocks milestone 3 outright, which explicitly needs this; wastes the concrete dogfood grounding available right now.

### Option B — Query-time resolution via `kc.toml` config map

A `[dependencies]` table (coordinate → repo slug) resolved entirely in the `kc serve` query layer. No Normalize, Persist, or schema changes.

**Pros:** ships in hours; handles real naming mismatches (PyPI package `omnius_llmlib`, repo slug `omnius-llmlib`) trivially as one config entry; two-way door.
**Cons:** the mapping is per-installation config, not compiled knowledge — invisible to the delta log; every consumer (wiki, MCP, future test-gen) must independently apply it.

### Option C — Coordinate-tagged facts + compile-time resolver → `Project`-to-`Project` rollup edge

Extend `dependency_observed` facts with an `ecosystem` field; a per-ecosystem resolver at Normalize/Persist time mints one coarse `external_depends_on` relationship between `Project` entities (never fine-grained Component-to-Component cross-repo edges — matches ADR-004's over-split bias). Non-breaking per `ir.md §178`.

**Pros:** produces real compiled state — in the delta log, queryable uniformly by every consumer without reimplementing the join.
**Cons:** more moving parts (new fact field, resolver hook in Normalize, new relation type); still needs Option B's config table as the fallback for name mismatches a naive normalizer can't guess.

### Option D — Full fine-grained cross-repo entity resolution (rejected for now)

Normalize reads another repo's already-persisted state during compile and creates real Component-to-Component `depends_on` edges across repos.

**Pros:** maximum fidelity — the only option that answers "which specific component," which is what test generation ultimately wants.
**Cons:** breaks the clean per-repo compile boundary (an ordering dependency between repos' compiles that doesn't exist in the pipeline model); requires resolving which symbol inside an *installed* package an import maps to, which the pipeline currently has no mechanism for (it doesn't parse vendored/installed sources, only project source); high over-merge risk, directly against ADR-004's explicit bias.

## Decision

**Option B — query-time resolution via a `kc.toml` `[dependencies]` config map.** No Normalize, Persist, or schema changes. Implemented as:

- `kc.toml`'s `[dependencies]` section (added to the `kc init` template): keys are the dependency coordinate as observed by the analyzer, values are the target's registered `repository.slug`.
- `mcp/queries.py::resolve_dependency(session, coordinate, dep_map)` — looks up a coordinate by **exact match or dotted prefix** (the same submodule-import pattern `normalize.py`'s internal resolver handles: `omnius_llmlib.core.predict` must match a `omnius_llmlib` config key), and if it names a registered repo, returns that repo's slug, project entity, and entity counts.
- `mcp/queries.py::get_entity()` annotates `component`-type entities with a `cross_repo_dependencies` list, resolving every `external_dependencies` entry against the map.
- A standalone `resolve_dependency` MCP tool for direct lookups without an entity slug.

Validated against the real dogfood pair: `frida`'s `component/backend-app` correctly resolves both `omnius_llmlib.core.output` and `omnius_llmlib.core.predict` to the registered `omnius-llmlib` repo (123 components, 256 test_coverage entities).

**Option C is not rejected — it is deferred**, exactly as ADR-010 deferred its static-site publisher option. The brainstorm's decisive uncertainty — whether milestone 3's test-generation use case needs component-level granularity from day one, or whether "depends on repo X, go read its wiki" is sufficient for a first cut — is unresolved, and resolving it is cheaper after the milestone-3 evaluation-methodology question (`decisions/index.md:142`) is answered than before. Building Option C's schema/resolver machinery now, against an unvalidated granularity assumption, would risk the same "confident but wrong" failure mode this ADR is designed to avoid (see Option D's rejection).

Option D remains rejected for the reasons above; revisiting it requires either a project-source-only resolution strategy (no reliance on installed-package introspection) or accepting materially higher over-merge risk than ADR-004 tolerates elsewhere.

## Architectural Invariants

- Cross-repo dependency resolution is **query-time only** for V1 — it produces no relationship rows, no delta-log entries, and requires no cross-`repo_id` read during compile. Normalize's per-repo compile boundary (implicit in ADR-001/ADR-003) is unbroken.
- The `[dependencies]` config map is per-consuming-repo `kc.toml` state, not compiled knowledge — it is deliberately excluded from identity, provenance, and the delta log (contrast ADR-005's embeddings, which *are* derived-but-compiled; this is neither).
- Matching is exact-or-dotted-prefix, mirroring `normalize.py::_resolve_internal`'s suffix-fallback pattern (added this same dogfood cycle) — bare-name and root-mismatch import styles are both first-class, not edge cases bolted on later.
- Should a future ADR adopt Option C (or D), this ADR's query-time mechanism does not need to be removed — it degrades gracefully to a fallback for coordinates the compiled resolver misses, the same relationship Option C's design already anticipates for the config table.

## Consequences

### Positive

- Unblocks the concrete dogfood use case (frida ↔ omnius_llmlib) with an afternoon of work, not a schema migration.
- Reversible: Option C or D can be layered on later without deprecating this mechanism.
- Naming mismatches (PyPI/npm package name vs. registered repo slug) are handled by explicit config, not guessed.

### Negative

- Cross-repo links are invisible to `recent_changes` and any future audit of "when did this cross-repo dependency appear" — because they aren't compiled state.
- The `[dependencies]` map must be maintained by hand per consuming repo; it does not discover new cross-repo dependencies automatically.
- Every consumer of cross-repo information (currently: `kc serve`'s `get_entity`/`resolve_dependency`) must independently read `[dependencies]` — a future wiki-rendering consumer would need the same wiring, not get it for free from compiled state.

### Tradeoffs Accepted

- Compiled-state fidelity traded for shipping something before the milestone-3 eval question is answered — the same "value before completeness" tradeoff ADR-010 made for the wiki's reading experience.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Coordinate not in `[dependencies]` | `resolve_dependency` returns "no configured mapping"; `get_entity` simply omits `cross_repo_dependencies` | Explicit, non-silent (returns an `error` field, not an empty success) |
| Target slug in `[dependencies]` not a registered repository | Same as above — resolution returns `None` | No compile failure; this is a read-time miss, not a correctness bug |
| Config map entry stale after a repo rename | Cross-repo links silently stop resolving | Same class of drift as any hand-maintained config; caught by dogfood use, not automated |

## Assumptions

- The two-repo dogfood pair (frida, omnius_llmlib) is representative enough of near-term cross-repo needs to validate this mechanism before generalizing.
- Milestone 3's actual granularity requirement is still unknown — this ADR deliberately does not guess it.

## Open Questions

- Whether Option C (compiled `Project`-to-`Project` rollup edge) becomes necessary — gated on the milestone-3 evaluation-methodology question (`decisions/index.md:142`); the brainstorm doc proposes a half-day spike to resolve it.
- Whether the `[dependencies]` map should eventually be partially auto-populated (e.g. from a manifest/lockfile parse against known registered repo slugs) rather than fully hand-maintained — not designed here.

## Impact

Affected documents: `docs/retrieval.md` §5/§6 (new MCP tool, `cross_repo_dependencies` field, open-items note), `docs/decisions/index.md` (entry resolved).
Affected compiler stages: none — this is entirely in **Serve** (`kc serve`), which per ADR-002 never compiles.

## Alternatives Rejected

- **Do nothing (A)** — blocks milestone 3 with no offsetting benefit given the real dogfood case in hand.
- **Full fine-grained cross-repo entity resolution (D)** — breaks the per-repo compile boundary and requires resolving symbols inside installed (non-project) source, which the pipeline has no mechanism for; over-merge risk exceeds ADR-004's tolerance.

Option C is explicitly **deferred, not rejected** (see Decision).

## Future Reconsideration

Revisit once the milestone-3 evaluation-methodology question is answered and the brainstorm's proposed spike (one concrete test-generation prompt using only the coarse signal this ADR provides) shows whether repo-level granularity is sufficient or component-level linking (Option C, or D) is required.

## References

- `BRAINSTORM-cross-repo-dependencies.md` — full option analysis, pre-mortems, comparison matrix
- `docs/decisions/index.md` (line 143) — the original open item this ADR resolves
- [ADR-001](ADR-001-postgresql.md) — `repo_id` isolation invariant
- [ADR-004](ADR-004-entity-identity.md) — over-split-over-merge bias, extended here to cross-repo linking
- [ADR-010](ADR-010-wiki-destination.md) — precedent for "decide the V1 reference behavior, defer the richer option" structure
- `docs/retrieval.md` §5 — MCP tool surface this ADR adds to

## Self-Review

- **Truly architectural?** Yes — it fixes where cross-repo knowledge lives (query-time config, not compiled state) and sets an invariant (no cross-`repo_id` reads during compile) that constrains every future design in this area, including Option C/D if adopted later.
- **Already made?** Partially — `decisions/index.md:143` sketched a direction (which this ADR's deferred Option C matches), but the actual V1 decision (ship B now, defer C) had not been made until this dogfood cycle produced a real example to validate against.
- **Reversible?** Two-way door — Option B's mechanism can coexist with a future Option C/D rather than being removed.
- **Dependent future documents:** none new; updates `retrieval.md` additively.
- **Exposes unresolved decisions:** the milestone-3 evaluation-methodology question (already tracked, not newly introduced by this ADR).
