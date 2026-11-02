# Normalize: Identity Resolution & Normalization Algorithm

> Status: Implementation specification (the final pre-implementation document per the v1.0 freeze).
> Composes the rules fixed by [ADR-004](decisions/ADR-004-entity-identity.md), [ir.md](ir.md) §4, and [pipeline.md](pipeline.md) §3.3 into one executable algorithm. Gaps found while implementing this go back into ir.md as additive clarifications — never into new documents.
> Pseudocode is normative for *behavior*; names and factoring are not.
> Last updated: 2026-07-19

---

## 1. Contract

```
normalize(facts: list[Fact], current: CurrentState, config: Thresholds)
    -> CandidateState { entities, relationships, conflicts }
```

- `current` is the persisted entity/relationship state (ADR-003) including stored anchors and slugs.
- Output is *fully identified* candidate state handed to Diff. Normalize never writes to the database and never concludes removals (Diff owns removal evidence — pipeline.md §5).
- Determinism requirement (ADR-004): `normalize` is a pure function of its three inputs. No randomness, no wall clock, no iteration over unordered containers without an explicit sort.

## 2. Phase order

```
P1  build rename map              (from git facts)
P2  deterministic entities        (natural keys)
P3  LLM candidates: order + dedup + cascade   (the core)
P4  merge co-matched candidates
P5  relationships                 (paths → slugs)
P6  wiki page derivation
P7  anchor currency rewrite
P8  conflict surfacing
```

Phases run strictly in this order; P3 depends on P2's entities existing (component scoping) and P1's map.

## 3. P1 — Rename map

From `source_change_observed` facts (git rename detection, already computed by the collector at the compiled commit):

```
rename_map: dict[old_path -> new_path]   # single-compile gap only
```

The anchor-currency invariant (ir.md §2.2) guarantees stored anchors are at most one compile behind, so `rename_map` never needs transitive composition. `map_anchor(a)` replaces `file_path` via the map **and rewrites the symbol_path's module prefix** derived from the old→new file paths — a file rename changes the module prefix of every symbol inside it, and without the prefix rewrite, symbol-granularity overlap scores 0 across renames and identity churns. *(Additive clarification, discovered by the rename test during implementation.)*

## 4. P2 — Deterministic entities

Group facts by natural key; construct/refresh one entity per key:

```
for fact_group in group_by_natural_key(deterministic_facts):     # sorted by key
    slug = slugify(entity_type, natural_key)                     # ir.md §3.2 rules
    entity = aggregate(fact_group)                               # ir.md §4.1 table
    entity.provenance = envelope_snapshots(fact_group)
```

Natural-key normalization applied here: API routes get positional parameters (`GET /users/{}`); Component hierarchy produces `contains` edges (package → module). Project is constructed from repo config.

### 4.1 Rename lineage *(additive clarification, frida dogfood finding)*

Component, Test Coverage and State Model natural keys contain a path (module path; test node id; owning component path). A file rename therefore changes the natural key, and ADR-004's deterministic identity holds: the renamed entity is a **new entity with a new slug**, and the old one is removed. The cascade's rename mapping (§3) applies only to LLM-derived candidates. Replaying frida PR #468, which only renamed 43 migration files, gave `+87 -86 moved:0`: 43 components and their 43 wiki pages removed and re-added. This broke every `kc-covers:` citation and journey step that named the old slugs.

Identity is unchanged. The rename is recorded as **lineage** so that a stale slug can be followed:

- **Normalize** pairs, after P3/P4, each current entity of those types that this compile no longer observes with the newly observed entity at its renamed key, using `rename_map`. For components the key is the `files` set mapped through the map; for test coverage it is the node id with its file prefix remapped; for a state model it is the owner path of a component paired in the same pass, plus the field. A pair forms only on a unique match in both directions (DP 8). Pairs are returned as `CandidateState.renames` (old slug → new slug).
- **Diff** keeps the ops as `removed` + `added`, not `moved`: the slug changed, so identity was not preserved. When the old slug is really removed and the new one really added this compile, it stamps the removed row's evidence with `renamed_to` and the added row's with `renamed_from`. Wiki pages pair when their owners pair. The append-only delta log is therefore the durable lineage; no schema change is needed.
- **Consumers follow it.** `load_current_state` rebuilds `CurrentState.renames` from the delta log. A slug's *latest* removal decides, so a slug re-added and later removed without a rename stops redirecting. Journey steps follow lineage: a pair from this compile always applies; persisted lineage applies only once the step resolves nowhere. Each followed step is resolved to the live slug, warned about, and recorded in the journey's `renamed_steps` payload field, which is present only when non-empty. MCP `get_entity` (and through it `impact_plan`/`test_plan`) returns the successor with `renamed_from` for a retired slug. `kc validate-test` scores a renamed claim as its successor and lists it under `renamed_claims`, instead of failing it as nonexistent.

Scope: lineage needs the forge's rename report, so only PR compiles produce it. Direct-commit compiles carry no `source_change_observed` facts, and full compiles have no previous tree to diff against. In both, a rename still shows as unpaired `removed` + `added`.

## 5. P3 — Candidate identity (the cascade)

### 5.1 Ordering and the match pool

```
candidates = sort(llm_candidates, key=content_hash)        # deterministic order (ir.md §4.2)
pool       = current.entities_of_type(t)                   # per candidate type t
minted     = []                                            # this compile's minted entities
```

Each candidate is matched against `pool ∪ minted` — the union is what makes intra-compile dedup and bootstrap compiles (empty `pool`) correct.

### 5.2 The cascade (normative behavior)

```
def match_or_mint(c, pool, minted, cfg):
    targets = [e for e in pool + minted if e.type == c.type]

    # Rule 1 — external key (ADR-004 cascade step 1)
    if c.external_key:
        hit = [e for e in targets if e.external_key == c.external_key]
        if hit: return matched(hit[0], rule="external_key", evidence={})

    # Rule 2 — anchor overlap (step 2; primary evidence)
    scored = []
    for e in targets:
        mapped = {map_anchor(a) for a in e.stored_anchors}      # P1 map, one-gap only
        score  = overlap(c.anchors, mapped)                     # §5.3
        if score >= cfg.T_anchor: scored.append((score, e.slug, e))
    if scored:
        score, _, e = max(scored)                               # ties: lexicographically
        return matched(e, rule="anchor_overlap",                #   smallest slug wins —
                       evidence={anchor_overlap: score})        #   deterministic

    # Rule 3 — name similarity (step 3; last resort, component-scoped)
    shared = [e for e in targets if components(e) & components(c)]
    scored = [(name_sim(c.name, e.name), e.slug, e)
              for e in shared if name_sim(c.name, e.name) >= cfg.T_name]
    if scored:
        sim, _, e = max(scored)
        return matched(e, rule="name_similarity", evidence={name_similarity: sim})

    # Mint (over-split beats over-merge — vision DP 8)
    slug = mint_slug(c.type, c.name, taken=slugs(pool + minted))  # -2/-3 dedup
    return minted_new(slug, rule="minted", evidence={})
```

Every outcome records `(rule, numeric evidence, decision)` into `match_evidence` (ADR-004; data-model.md `provenance`).

### 5.3 Scoring functions (deterministic, pure-Python)

- **Anchor overlap:** compare at symbol granularity where both sides have `symbol_path`, else file granularity. `overlap(A, B) = |A ∩ B| / |A|` (candidate-relative: "how much of the candidate's evidence points at this entity"). Rationale: entity anchor sets grow over time; normalizing by the candidate keeps a small focused candidate from failing to match a large entity.
- **Name similarity:** token-set Jaccard over case/punctuation-normalized names. Chosen over edit distance: robust to word order ("discount validation rule" ≈ "rule for validating discounts"), trivially deterministic, no dependency.
- Both thresholds (`T_anchor`, `T_name`) are configuration (ADR-004: dogfood-tuned; conservative defaults, since over-split is policy).

## 6. P4 — Merging co-matched candidates

Two candidates in one compile may match the *same* entity (the same rule extracted from two files). Deterministic merge:

```
anchors     = union of both candidates' anchors
provenance  = both envelopes
content     = from the candidate with more anchors;
              tie → smaller content_hash                 # deterministic
conflict    = if contents disagree materially, record a conflict (P8) —
              never silently blend prose
```

## 7. P5–P7 — Relationships, wiki pages, anchor currency

- **P5:** candidate `related component paths` resolve to Component slugs via P2's entities (+ rename map for stale paths); unresolvable *internal* paths are recorded as warnings, never guessed (DP 8) — unresolved *external* imports (stdlib, third-party) are silently classified as external dependency coordinates, not warned (dogfood finding: warning on known-externals is noise). Relationship rows materialize per ir.md §3.3/§4.1. **Containment edges are generated over ALL known components (observed ∪ current state)** *(additive clarification, verify-chain finding)*: containment derives from paths alone, so restricting it to observed components would make the always-observed Project (or a touched package) silently drop `contains` edges to out-of-scope children on every PR compile. Resolution of internal-vs-external imports likewise consults the combined map. **Journey `traverses` edges go to every resolved step, observed or current-state-only** *(additive clarification, same class, found adding rename lineage)*: a journey is re-observed on every compile (kc.toml facts), so Diff treats its outgoing edges as authoritative. P5 used to emit `traverses` only to steps in the compile's own slice, so a PR that touched one step's file deleted the edges to every other step, and the next `kc verify` diverged. Diff still never adds an edge to an entity it removes in the same compile.
- **Internal-import resolution beyond simple prefix matching** *(additive clarification, dogfood finding on repoA)*: `_resolve_internal` first tries the longest dotted-prefix match of the raw import string against known component paths (the case where a project's import root equals the repo root used to name components). Two real-world layouts break that assumption and needed a fallback, both discovered dogfooding on a repo outside the synthetic test fixtures:
  - **Python — import root ≠ repo root.** When a subdirectory (e.g. `backend/`) is on `PYTHONPATH` rather than the repo root, bare imports like `from db import Claim` share no prefix with the component `backend.db`. Fallback: match the import string as a **unique dotted suffix** of a known component path (`db` matches `backend.db` iff no other component also ends in `.db`). Ambiguous suffixes stay unresolved — the same over-split-over-merge bias as the P3 cascade (vision DP 8), applied here to avoid wrongly linking unrelated modules that happen to share a basename.
    *(Additive clarification, frida dogfood finding.)* The suffix fallback only considers components of the importer's **language family** (Python; TypeScript+JavaScript), taken from the components' analyzer-reported `language` — never from file extensions (ADR-006 boundary). Before this, Python's stdlib `import types` resolved to a lone TypeScript `widgets/types.ts`, minting a bogus `depends_on` edge and an import-`covers` edge from every Python test importing `types`. Test-coverage targets use the family of the test file's component. Known limit, not fixed here: the "exactly one candidate" rule is order-sensitive within a family — adding a second same-named module re-resolves only the files compiled in that change, so an incremental and a full compile can still disagree until those importers next change or a full compile runs.
  - **TypeScript — tsconfig path aliases.** Bare specifiers matching a `tsconfig.json` `compilerOptions.paths` alias (e.g. `@/*` -> `./src/*`) are expanded to their real internal module path *before* internal/external classification, at extraction time in the TypeScript analyzer (not in Normalize, since alias config is TS-specific and file-local to resolve). Without this, alias-based imports — the dominant internal-import style in a typical Vite/CRA-convention frontend — were uniformly misclassified as external, hiding most of the real dependency graph.
    *(Additive clarification, frida dogfood finding — incremental ≠ full.)* The alias config must be visible to every compile, not just full ones: a PR/commit compile collects only the change's files, which rarely include `tsconfig*.json`, so its touched TS files resolved aliased imports as external while a full compile resolved them internally (`kc verify` diverged on `internal_dependencies`/`external_dependencies`, and `depends_on` edges went missing). PR/commit compiles that include any `.ts`/`.tsx` file therefore also collect every `tsconfig*.json` at the compile's commit as alias input. They are not added to the compile scope — they mint no entities, so they carry no removal evidence. Known limit: a change to a tsconfig's `paths` only re-resolves the TS files in that same compile; other files pick it up when they next change or on a full compile.
- **Jira Story → Pull Request (`motivates`)** *(additive clarification)*: minted from `jira_observed.linked_pr` (set at Collect time from the originating PR's title/body issue-key match — architecture.md §6's forge-linkage rule, extended to Jira) — a direct field lookup, not a path-resolution cascade like the component relationships above. **Jira Story → Feature (`motivates`, LLM-derived):** `_jira_stories()` reads `jira_feature_link_observed` facts — produced by `_jira_feature_enrichment_facts` in the Extract stage — and resolves each `feature_names` entry to a Feature slug. `_p5_relationships()` then emits the `motivates → Feature` edges. Skipped when LLM or Jira is disabled; enrichment failures on individual issues are silently skipped (best-effort).
- **P6:** Wiki Page entities derive deterministically from the candidate entity set (identity = owning slug + page type) — in Normalize, per the ADR-009 boundary (ir.md §4.1).
- **P7:** every matched entity's stored anchors are rewritten to the candidate's current-commit anchors (anchor currency, ir.md §2.2). Unmatched entities keep their anchors (they were out of scope or unobserved — Diff decides what that means).

## 8. P8 — Conflict surfacing

Per ir.md §4.3: disagreeing facts (e.g., `api_endpoint_observed(openapi)` without a `code_pattern` counterpart) become explicit discrepancy attributes on the entity plus a conflict record in the output — visible, never auto-resolved (the ranking policy is a deferred future ADR).

## 9. Determinism checklist (review gate for the implementation)

- [ ] Candidates processed in content-hash order; all other iterations over sorted keys.
- [ ] Tie-breaks everywhere are total orders (score, then slug / content_hash) — no dict-order dependence.
- [ ] No wall-clock, randomness, or environment reads inside `normalize`.
- [ ] LLM is never consulted (identity is Normalize's, content is Extract's — ADR-004).
- [ ] Same `(facts, current, config)` ⇒ byte-identical output (property test in the suite).
- [ ] Every match/mint outcome carries recorded evidence.

## 10. Failure handling

| Condition | Behavior |
|---|---|
| Unknown fact shape | Loud failure (ADR-009 anti-smuggling; pipeline.md §3.3) |
| Candidate missing anchors | Reject the candidate, record a warning — it cannot participate in identity (ADR-004 hard requirement) |
| Slug collision at mint | Deterministic `-2`/`-3` suffix (ADR-004) |
| Unresolvable component path | Warning; relationship dropped, entity kept |

## 11. Open items (config, not spec)

- `T_anchor`, `T_name` defaults — dogfood-tuned (ADR-004 open question); start conservative.
- Whether Rule-3 component scoping should also consult `contains` ancestors — decide from dogfood false-split evidence; additive if so.

## References

[ADR-004](decisions/ADR-004-entity-identity.md) · [ADR-009](decisions/ADR-009-two-layer-ir.md) · [ir.md](ir.md) §2.2, §4 · [pipeline.md](pipeline.md) §3.3–3.4, §5 · [data-model.md](data-model.md) (`provenance.match_evidence`)
