# Normalize: Identity Resolution & Normalization Algorithm

> Status: Implementation specification (the final pre-implementation document per the v1.0 freeze).
> Composes the rules fixed by [ADR-004](decisions/ADR-004-entity-identity.md), [ir.md](ir.md) §4, and [pipeline.md](pipeline.md) §3.3 into one executable algorithm. Gaps found while implementing this go back into ir.md as additive clarifications — never into new documents.
> Pseudocode is normative for *behavior*; names and factoring are not.
> Last updated: 2026-07-18

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

- **P5:** candidate `related component paths` resolve to Component slugs via P2's entities (+ rename map for stale paths); unresolvable paths are recorded as warnings, never guessed (DP 8). Relationship rows materialize per ir.md §3.3/§4.1.
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
