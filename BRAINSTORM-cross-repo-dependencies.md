# Brainstorm: Cross-repo dependency resolution
2026-07-19 · Mode: Refinement (starting from the sketch already recorded in decisions/index.md:143)

## Problem

Knowledge Compiler compiles each repo into an isolated `repo_id` partition of one shared Postgres DB (ADR-001). When a component in repo A imports a package that is actually repo B (also compiled into the same DB), the compiler currently has no way to know that — `repo-a`'s `component/backend-preprocessing-external-document-service` lists `repo_b` as a bare string in `external_dependencies`, with no link to `repo-b`'s `project` entity or its 123 components. This is flagged as a pre-milestone-3 design item because test generation (`vision.md` use case 4) needs to reason across a dependency boundary — e.g. "repo-a imports `repo_b.core.selector`; does the pinned version's test suite cover it?"

## Constraints & assumptions

**Hard constraints:**
- `repo_id` isolation is a stated invariant (ADR-001) — any design must not require ad-hoc cross-repo joins baked into every query.
- Deterministic-first extraction (ADR-006) — resolution logic must not require an LLM call.
- No graph database, no distributed architecture (V1 non-goals).
- `ir.md §3.3`: relationship additions are non-breaking; **changing** an existing relationship's semantics is breaking. A new relation type is safe to add.
- ADR-004's bias (over-split beats over-merge) should extend here: a *wrong* cross-repo link is worse than *no* link.
- Currently zero manifest/lockfile parsing exists — `external_dependencies` are bare import strings with no ecosystem tag and no version. Any design that needs "ecosystem + name" coordinates has to add that extraction, not assume it exists.

**Soft preferences:** minimal new schema surface; config over hardcoding (CLAUDE.md design principle); prefer additive to the existing pipeline over a new stage.

**Assumption made without asking:** "resolution" for V1 means *linking* a repo's external dependency to another compiled repo's `Project`/`Component` entities for query purposes — not merging identity, not creating a unified cross-repo graph model. The decisions/index.md sketch already implies this ("Repository depends_on Repository is a derived rollup, never the model").

**Success criterion:** given repo-a + repo_b both compiled, `kc serve`'s `get_entity` or a similar query on `component/backend-preprocessing-external-document-service` can answer "this depends on the `repo-b` repo" (and ideally which of repo_b's components), without a human manually cross-referencing two wikis.

## Context (codebase findings)

- `python_analyzer.py::_import_targets` / `typescript_analyzer.py` emit bare dependency strings only; `normalize.py::_resolve_internal` (just extended this session with a dotted-suffix fallback) only resolves *within* one repo's component set — never crosses `repo_id`.
- `component.payload.external_dependencies` is currently an unstructured list of raw import strings (`"fastapi"`, `"repo_b"`, `"@tanstack/react-query"`) — no ecosystem, no version.
- `RelationshipRow` schema has no structural barrier to a cross-repo edge (`from_entity_id`/`to_entity_id` are plain FKs to a global `entities.id`), but every relationship-building code path in `normalize.py::_p5_relationships` only ever looks up slugs within the current compile's own `components` dict.
- `data-model.md §7` already lists "Cross-repo queries" as an open item with "no schema blocker; `repo_id` scoping is already universal" — consistent with treating this as a resolution/query problem, not a schema migration.

## Options

### Option A: Do nothing (defer past V1)

**Sketch:** Leave `external_dependencies` as opaque strings. Cross-repo questions get answered manually (open both wikis) until real M3 test-generation work forces the issue.

**Pros:** Zero cost now; avoids designing against requirements that might change once M3's actual test-generation shape is known.

**Cons:** M3 (test generation) explicitly depends on this per `decisions/index.md:143` — deferring blocks that milestone entirely, not just delays a nice-to-have. Also: the two dogfood repos in front of us *are* a real cross-repo case right now, so deferring wastes the concrete grounding we have.

**Pre-mortem:** Six months in, someone starts M3, discovers cross-repo resolution was never designed, and has to retrofit it under deadline pressure with less clean grounding than today (no live dogfood pair to validate against).

**Reversibility:** Two-way door — trivially reversible, it's inaction.

**Effort:** None now; unknown-but-nonzero later.

---

### Option B: Query-time resolution via config map (simplest thing that works)

**Sketch:** Add a `[dependencies]` table to `kc.toml` mapping package coordinate → repo slug (e.g. `repo_b = "repo-b"`). No Normalize/Persist/schema changes at all — `get_entity` (or a new `resolve_dependency` MCP tool) does the join at query time: look up the raw string in `external_dependencies`, check the config map, and if found, query the other repo's `Project` entity by slug (cross-`repo_id` read, read-only, in the query layer only).

**Pros:** No compile-time changes, no new relation type, no schema migration. Ships in an afternoon. Naturally handles the `repo_b` (import name, underscore) → `repo-b` (repo slug, hyphen) naming mismatch we found in the real data — that's exactly a per-package config entry.

**Cons:** The mapping is per-installation config, not compiled knowledge — it isn't in the delta log, doesn't show up in `recent_changes`, and every consuming query (wiki, MCP, future test-gen) has to independently apply it rather than reading one authoritative edge. Doesn't scale past a handful of manually-maintained coordinate→slug pairs.

**Pre-mortem:** Six months in, five repos are cross-linked via a `kc.toml` table that's grown to 40 entries, gets out of sync when a repo is renamed, and nobody remembers to update it — worse, the wiki (a build artifact per ADR-010) can't render the link because config lives in one repo's `kc.toml`, not the compiled state either side can see uniformly.

**Reversibility:** Two-way door — the config table can be deleted once (or if) a compiled version replaces it.

**Effort:** Hours.

---

### Option C: Coordinate-tagged facts + compile-time resolver → Project-level rollup edge (the recorded sketch, refined)

**Sketch:** Extend `dependency_observed` facts with `ecosystem` (pypi/npm/stdlib/unknown) — cheap, deterministic, derivable from the import syntax alone (no manifest parsing needed for ecosystem; version pins are a separate, explicitly deferred sub-item already noted at `decisions/index.md:144`). At Persist/Normalize time, a small per-ecosystem resolver (starting with one trivial rule: normalize `_`↔`-` and compare against known `repositories.slug` values, falling back to explicit `kc.toml [dependencies]` override for anything the naive rule misses — Option B's config table survives as the escape hatch, not a separate design) tries to match the coordinate to a registered `Repository`. On a match, mint exactly one new relationship type — `Project --external_depends_on--> Project` — a coarse rollup, never a fine-grained Component-to-Component cross-repo edge. Non-breaking per `ir.md §178`.

**Pros:** Matches the already-recorded direction, so it's not relitigating a decision. Deterministic, no LLM. The rollup edge is compiled state — it's in the delta log, in `recent_changes`, queryable via `kc serve` without every consumer re-implementing the join. The "entity-level edges are primary, Repository-to-Repository is a derived rollup, never the model" framing directly avoids the over-merge risk of Option D.

**Cons:** More moving parts than B — a new fact field, a resolver hook in Normalize, a new relation type, and the `Project` entity now needs to expose it. Still needs Option B's config table as the fallback for name mismatches the naive normalizer can't guess (there will always be some — e.g. a repo whose slug bears no resemblance to its package name).

**Pre-mortem:** The "coarse rollup only" decision holds up fine until someone actually needs to answer "which specific repo_b component does repo-a's OCR service exercise" for real test generation — and discovers the rollup edge doesn't carry that granularity, forcing either a fallback to manual cross-wiki lookup or a fast-follow to Option D.

**Reversibility:** Mostly two-way — the new relation type is additive and can be left unused if abandoned; the resolver hook is a small, isolable piece of Normalize.

**Effort:** Low-days (a fact-payload field, one resolver function, one relationship-building loop in `_p5_relationships`, a config schema addition).

---

### Option D: Full fine-grained cross-repo entity resolution (contrarian/full option)

**Sketch:** At Normalize time, when repo A's compile runs, it reads repo B's *already-persisted* entity state (a genuine cross-`repo_id` read during compile, not just at query time) and creates real `depends_on` relationship rows directly from repo A's importing Component to repo B's specific target Component — e.g. `repo-a:component/backend-preprocessing-external-document-service --depends_on--> repo-b:component/repo-b-core-selector`.

**Pros:** Maximum fidelity — this is the only option that actually answers "which specific component," which is what M3 test generation ultimately needs.

**Cons:** Breaks the clean per-repo compile boundary — Normalize for repo A now has an ordering dependency on repo B's last successful compile, which doesn't exist in the pipeline model (`compile_runs` has no notion of "depends on this other repo's run"). Requires resolving *which specific symbol* inside the target package the import maps to — genuinely hard without either parsing the installed package's actual source (a second analyzer pass over `venv/site-packages`, which the current pipeline explicitly excludes as generated/vendored, not project source) or guessing from the import path alone (fragile: `from repo_b import X` doesn't tell you which of 123 components implements `X` without resolving `repo_b`'s own `__init__.py` re-exports). Much higher over-merge risk — ADR-004's explicit bias is against exactly this kind of confident-but-wrong fine linking.

**Pre-mortem:** Six months in, the fine-grained cross-repo edges are frequently wrong (mis-resolved re-exports, stale if repo B recompiles after repo A), erode trust in the whole KB's edges the same way the pre-fix `external_dependencies` misclassification did, and someone has to add a "confidence" or "unverified" flag that Option C never needed.

**Reversibility:** One-way-ish — once consumers (wiki pages, MCP answers) start depending on fine-grained cross-repo edges being present, walking back to a coarser model is a behavior change for every downstream consumer.

**Effort:** Weeks (new compile ordering/dependency tracking between repos, a resolver that can look inside installed packages, confidence/staleness handling).

## Comparison

| | A: Defer | B: Query-time config | C: Coordinate + rollup edge | D: Fine-grained cross-repo |
|---|---|---|---|---|
| Unblocks M3 | ✗ | ~ (manual assembly still needed) | ✓ | ✓✓ |
| Fits `repo_id` isolation invariant | ✓ | ~ (query-time cross-read only) | ✓ | ✗ (compile-time cross-read) |
| Deterministic / no LLM | ✓ | ✓ | ✓ | ✓ (but fragile resolution logic) |
| Avoids over-merge risk (ADR-004 spirit) | ✓ | ✓ | ✓ | ✗ |
| In the delta log / queryable uniformly | — | ✗ | ✓ | ✓ |
| Effort | none | hours | low-days | weeks |
| Reversibility | two-way | two-way | mostly two-way | one-way-ish |

## Recommendation

**Option C** (coordinate-tagged facts + compile-time resolver → `Project`-level rollup edge), with **Option B's config table folded in as its fallback mechanism** rather than treated as competing.

**Confidence: medium-high.** It's the smallest design that produces genuinely compiled, queryable state (not a per-installation config side-channel) while staying inside every hard constraint (`repo_id` isolation, determinism, non-breaking vocabulary, ADR-004's under-linking bias). It also directly reuses the design already recorded at `decisions/index.md:143`, so it's refinement, not relitigation.

**Decisive uncertainty:** whether M3's actual test-generation use case needs component-level granularity from day one, or whether "repo-a depends on repo-b; go check its wiki" is sufficient for a first cut. If M3 turns out to need fine-grained links immediately, Option C under-delivers and Option D's cost becomes justified sooner than this document assumes.

**Cheap spike to resolve it (≤ half a day):** Once the M3 eval-criteria question (the *other* open pre-milestone-3 item) is answered, write one concrete example test-generation prompt using only the coarse rollup edge — e.g. "generate a regression test for repo-a's OCR preprocessing path, aware that it depends on repo-b" — and see whether an LLM extractor can do useful work with just "depends on repo X" plus X's wiki as context, or whether it stalls without a specific component pointer. That answer settles C vs. D without building either first.

**Steelman of the runner-up (Option D):** A reasonable person building toward "generate implementation-aware regression tests" (vision.md's literal M3 definition) would argue that a rollup edge saying "depends on some repo" is barely more useful than no edge at all — real test generation needs to know *which* function is called, not just which repository. If the spike above shows LLM extraction genuinely needs that specificity to produce non-generic tests, Option D's cost is worth paying, and the "resolve inside installed packages" sub-problem should be scoped as its own follow-up design rather than folded into this one.

## Next steps

1. Add `ecosystem` to the `dependency_observed` fact payload (deterministic from import syntax; no manifest parsing needed for V1).
2. Add a naive per-ecosystem name-normalizer (`_`↔`-`, case-fold) in Normalize, checked against registered `Repository.slug` values, with `kc.toml [dependencies]` as an explicit override table for anything it misses.
3. Add `external_depends_on` as a new `Project → Project` relation type (non-breaking per `ir.md §178`); wire it into `_p5_relationships`.
4. Validate against the real pair: recompile repo-a, confirm `repo_b` (bare string) resolves to the registered `repo-b` repo via the naive normalizer alone (no config override needed — good sign the automatic path covers the common case).
5. Revisit after the M3 eval-criteria brainstorm resolves the decisive uncertainty above.
