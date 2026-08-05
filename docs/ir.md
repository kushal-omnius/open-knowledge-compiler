# Canonical Intermediate Representation (IR)

> Status: Living specification. Structure fixed by [ADR-009](decisions/ADR-009-two-layer-ir.md) (Accepted).
> Field-level storage detail (column types, indexes) belongs to `data-model.md`; this document defines the *logical* contracts.
> Last updated: 2026-08-05

---

## 1. Where the IR sits: the canonical pipeline

```
        Artifacts            (git, PRs, Jira, docs, OpenAPI, tests)
            │
            ▼
        Collectors
            │
            ▼
   ╔════ Fact IR ════╗       extraction output — per-compile, disposable,
   ║  (Extraction)   ║       identity-free (§2)
   ╚═════════════════╝
            │
            ▼
        Normalize            the ONLY crossing point: identity cascade
            │                (ADR-004) + aggregation
            ▼
   ╔══ Knowledge IR ══╗      canonical entities + relationships —
   ║                  ║      durable, slug-bearing (§3)
   ╚══════════════════╝
            │
            ▼
          Diff               → knowledge delta (§3.4)
            │
            ▼
         Persist
            │
            ▼
        Emitters
            │
    ┌───────┼─────────┬──────────────┐
    ▼       ▼         ▼              ▼
  Wiki  Embeddings   MCP    Test generation (milestone 3)
```

Per [ADR-009](decisions/ADR-009-two-layer-ir.md), the IR consists of the two boxed layers — two distinct, versioned models with a directional boundary. Everything upstream of Normalize speaks Fact IR; everything downstream speaks Knowledge IR.

The **knowledge delta** ([ADR-003](decisions/ADR-003-current-state-delta-log.md)) is a derived artifact expressed in Knowledge IR vocabulary — not a third layer.

### Boundary invariants (normative, from ADR-009)

- Extractors and analyzers emit **Fact IR only**; fact types carry no identity fields.
- Only Normalize produces Knowledge IR; slugs exist only there ([ADR-004](decisions/ADR-004-entity-identity.md)).
- Consumers read **Knowledge IR only**, never facts.
- Every fact carries provenance (artifact refs); LLM-derived candidates additionally carry anchors (ADR-004 hard requirement).
- Fact-vocabulary additions are non-breaking; Knowledge IR changes are breaking (§5).

---

## 2. Fact IR

### 2.1 Common fact envelope

Every fact, regardless of type, carries:

| Field | Meaning |
|---|---|
| `fact_type` | One of the vocabulary below |
| `payload` | Type-specific content (schemas per fact type; LLM payloads schema-validated per [ADR-008](decisions/ADR-008-llm-abstraction-caching.md)) |
| `provenance` | Artifact refs this fact was derived from |
| `extraction` | `method` (`deterministic` \| `llm`), extractor name + version; for analyzers additionally the grammar version ([ADR-006](decisions/ADR-006-language-analyzers.md)); for LLM facts additionally model + prompt template version (ADR-008) |
| `content_hash` | Hash of the normalized payload — used for change detection and cache correlation |

Facts are scoped to a compile run. They have no slugs, no cross-compile identity, and no ordering guarantees beyond their compile.

### 2.2 Anchors

An **anchor** is a stable-enough reference to a source location, used by the ADR-004 identity cascade for overlap matching:

```
anchor = { file_path, symbol_path?, span? }
```

- `file_path` — repo-relative, as observed at the compiled commit. Anchors are recorded **raw**; git rename mapping is applied by Normalize at match time, never baked into facts.
- `symbol_path` — dotted path within the file (e.g., `billing.invoice.apply_discount`), language-analyzer-normalized.
- `span` — optional start/end lines; the least stable component, used to disambiguate, never as primary match evidence.

**Anchor currency invariant:** on every compile where an entity is matched, its *stored* anchors are rewritten to the compiled commit's paths. Matching therefore only ever bridges one compile gap; rename mapping never has to compose chains across multiple historical compiles.

Anchor overlap semantics (what fraction of two anchor *sets* coincide, at what granularity) are threshold configuration per ADR-004, not IR.

### 2.3 Deterministic fact vocabulary

Emitted by language analyzers and deterministic extractors:

| Fact type | Payload (essentials) | Emitted by |
|---|---|---|
| `component_observed` | path, kind (package/module), language | analyzers |
| `symbol_observed` | symbol_path, kind (class/function/method), signature, anchor | analyzers |
| `dependency_observed` | from-component path → to-component path or external package | analyzers |
| `api_endpoint_observed` | HTTP method, route, handler anchor, source (`code_pattern` \| `openapi` — kept distinct for conflict surfacing, §4.3) | analyzers, OpenAPI extractor |
| `test_case_observed` | test node id, framework, **module** (the analyzer's own module-path convention — Normalize never derives it with language assumptions; boundary leak found by the TypeScript stress test), anchor | analyzers |
| `test_target_observed` | test node id → targeted symbol/component, mechanism (import/call/marker) | analyzers |
| `source_change_observed` | file, change kind, spans (the PR's *source diff* — drives incremental scoping; deliberately named to avoid confusion with the knowledge delta) | git collector |
| `pr_observed` | number, title, body, merged_at, commit shas, linked issue keys | PR collector |
| `jira_observed` | issue key, summary, status, acceptance criteria, links | Jira collector |
| `doc_section_observed` | doc path, section heading, content ref — consumed as context input by LLM extractors and as provenance for candidates; produces no entity directly (§4.1) | README/doc collector |

### 2.4 LLM candidate facts

Emitted by LLM extractors; all payloads schema-validated before entering the compile (ADR-008), all carry **mandatory anchor sets**:

| Fact type | Payload (essentials) |
|---|---|
| `feature_candidate` | name, narrative, anchors[], related component paths |
| `business_rule_candidate` | name, rule statement, intent, anchors[] |
| `risk_candidate` | name, description, category, anchors[] |

Candidates propose *content only*. Whether a candidate becomes a new entity or an update to an existing one is exclusively Normalize's decision (ADR-004).

> Wiki prose is **not** a fact type: page text is generated at Emit time from Knowledge IR entities, so prose regeneration never re-runs extraction.

---

## 3. Knowledge IR

### 3.1 Common entity envelope

| Field | Meaning |
|---|---|
| `slug` | Stable identity (ADR-004); also the wiki filename/anchor |
| `entity_type` | One of the ten canonical types |
| `repo_id` | Multi-repo scoping ([ADR-001](decisions/ADR-001-postgresql.md) invariant) |
| `name` | Human-readable display name (may change without identity change) |
| `payload` | Type-specific content |
| `content_hash` | Change detection for Diff and delta-driven emission |
| `provenance` | Links to the facts (and through them, artifacts) this entity was compiled from, including identity-match evidence (fired cascade rule + numeric signals, per ADR-004) |

### 3.2 Entity types and identity classes

Identity classes per ADR-004's categorization:

| Entity | Identity class | Natural key / identity source |
|---|---|---|
| Project | deterministic | repo config |
| Component | deterministic | package/module path (see granularity note below) |
| API | deterministic | HTTP method + normalized route (see key note below) |
| Test Coverage | deterministic | test node id |
| Pull Request | deterministic | repo + PR number |
| Jira Story | deterministic | issue key |
| Feature | LLM-derived | match-then-mint cascade |
| Business Rule | LLM-derived | match-then-mint cascade |
| Risk | LLM-derived | match-then-mint cascade |
| Wiki Page | derived | owning entity slug + page type |

**API key note:** the natural key is *always* method + normalized route — route parameters are positional, with names elided (`GET /users/{}`), so `/users/{id}` and `/users/{user_id}` are the same API. OpenAPI `operationId` is an **alias attribute**, never identity: adding operationIds to an existing spec must not churn API identity. API kinds beyond HTTP (GraphQL, gRPC, CLI) are a future vocabulary extension, not an abstraction to build now.

**Component granularity note:** both packages and modules are Components; the hierarchy is explicit via the `contains` relationship (§3.3), not encoded in identity. A package Component contains its module Components.

**Slug format:** `{entity_type}/{key}` — for deterministic entities the key is the slugified natural key (e.g., `component/billing-invoice`, `api/post-v1-invoices`); for LLM-derived entities the key is minted from the normalized name at first sight, with deterministic `-2`/`-3` dedup suffixes (ADR-004).

### 3.3 Relationship vocabulary

Relationships are explicit, typed, and compiled — never inferred at query time (vision, Canonical Knowledge Model):

| Relationship | From → To |
|---|---|
| `implemented_by` | Feature → Component |
| `exposes` | Feature → API |
| `governs` | Business Rule → Feature \| Component \| API |
| `verified_by` | Business Rule → Test Coverage |
| `covers` | Test Coverage → Component \| API |
| `defined_in` | API → Component |
| `depends_on` | Component → Component |
| `contains` | Project → Component; Component → Component (package → module hierarchy) |
| `affects` | Risk → Component \| Feature |
| `motivates` | Jira Story → Feature \| Pull Request |
| `documents` | Wiki Page → any entity |

Relationship additions are non-breaking (they extend the vocabulary like fact types); changing the semantics of an existing relationship is breaking (§5).

### 3.4 Delta document vocabulary

The delta (one per compile, ADR-003) is expressed entirely over Knowledge IR:

```
delta = {
  compile_run, refs { pr, commits, jira_keys },
  entity_changes:       [ { op: added|changed|removed|moved, slug, entity_type, change_summary, evidence } ],
  relationship_changes: [ { op: added|removed, relationship, from_slug, to_slug } ]
}
```

`moved` is the `entity_moved` event from ADR-004 (anchor relocation via rename detection) — identity preserved, location changed. `change_summary` granularity is a `data-model.md` decision (ADR-003 fixes entity-level ops as the floor).

**Dirty-entity rule (normative):** for delta-driven emission (wiki pages, embeddings), an entity is dirty if it appears in `entity_changes` **or on either end of a `relationship_changes` row**. Entity `content_hash` covers payload only; without this rule, a relationship-only change (e.g., a Feature gaining a Component) would silently leave the Feature's wiki page stale.

---

## 4. Normalize mapping rules

### 4.1 Fact → entity aggregation

| Fact types | Entity produced |
|---|---|
| — (repo config, at Collect) | Project (+ `contains` to top-level Components) |
| `component_observed` + `dependency_observed` + `symbol_observed` | Component (+ `depends_on`, `contains`) |
| `api_endpoint_observed` (all sources) | API (+ `defined_in`) |
| `test_case_observed` + `test_target_observed` | Test Coverage (+ `covers`) |
| `pr_observed` | Pull Request |
| `jira_observed` | Jira Story (+ `motivates` where links resolve) |
| `feature_candidate` | Feature via identity cascade (+ `implemented_by`, `exposes`) |
| `business_rule_candidate` | Business Rule via cascade (+ `governs`, `verified_by`) |
| `risk_candidate` | Risk via cascade (+ `affects`) |
| — (derived) | Wiki Page entities are derived **by Normalize** deterministically from the entity set (identity = owning entity slug + page type); Emit only renders them. Producing them anywhere else would violate the §1 invariant that only Normalize produces Knowledge IR |

Aggregation is many-facts-to-one-entity; the entity's provenance records every contributing fact (ADR-009: granularity preserved).

### 4.2 Identity

Deterministic entities take their natural key directly from fact payloads. LLM candidates enter the ADR-004 cascade: external key → anchor overlap (after applying git rename mapping to raw fact anchors) → normalized-name similarity → mint. Evidence recorded per match.

**Intra-compile candidate deduplication (normative clarification, consistent with ADR-004):** the cascade also runs candidate-vs-candidate *within* a compile — two extractions of the same rule from different files must converge on one entity, including on bootstrap compiles where current state is empty. To keep minting deterministic, candidates are processed in a **stable order (sorted by content hash)**: the first mints, later ones match against it. Without a fixed order, identical inputs could produce different entity sets — violating ADR-004's determinism guarantee.

### 4.2.1 Removal semantics

**Absence is evidence only within the compile's collection scope.** An incremental compile collects only the PR's slice; an entity whose sources lie outside that scope is *unobserved*, never *removed*. Normalize may emit `op: removed` only when:

- *deterministic entity:* its natural key's source location was **in scope** and the key is no longer observed there;
- *LLM-derived entity:* **all** of its stored anchors were in scope and its anchoring evidence is gone (ADR-004 anchor decay).

A full compile's scope is the whole repository, so full compiles may remove anything. The naive rule — "not observed this compile ⇒ removed" — would mass-delete every out-of-scope entity on the first incremental run; this invariant exists to make that implementation impossible to write accidentally.

### 4.3 Conflict surfacing

When facts disagree — e.g., `api_endpoint_observed(source=openapi)` with no corresponding `code_pattern` observation, or vice versa — Normalize applies vision Design Principle 8 (**when uncertain, split; never silently merge**): the disagreement is represented explicitly on the entity (e.g., an API carrying `sources: [openapi]` but `sources: [code]` absent is a visible discrepancy attribute), never resolved by silently preferring one source. A full conflict-resolution *policy* (which source outranks which, when) is deliberately **not** defined here — it is an open vision question requiring its own future ADR; the IR's obligation is only that conflicts are representable and visible.

---

## 5. Versioning

- Each layer declares an independent version: `fact_vocabulary_version` and `knowledge_model_version`. Both are recorded in every compile run.
- **Fact layer:** *additive* changes (new fact types, new optional payload fields) are non-breaking and bump minor. Changing or removing an existing fact type's semantics is breaking and bumps major. Plugins declare the fact-vocabulary versions they emit against ([ADR-007](decisions/ADR-007-plugin-architecture.md) versioned-interface invariant); mismatches fail loudly at activation.
- **Knowledge layer:** entity schema and relationship *semantics* changes are breaking by default (consumers — wiki, MCP, deltas — read this layer). New relationship types and new optional entity payload fields are the additive exception.
- **Migration story:** because compiled artifacts are disposable (vision Design Principle 5), a breaking knowledge-model change is handled by **full recompilation into the existing database** (slug-preserving, per ADR-004), not by data migration. The delta log is the one durable history: breaking changes to the *delta vocabulary* therefore need an explicit compatibility note in `data-model.md`.
- **A third tracked version, orthogonal to the two above:** `okf_spec_version` records which version of the external [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) spec the Emit stage's wiki bundle targeted for that compile ([ADR-013](decisions/ADR-013-open-source-okf-conformance.md)). It versions a *rendering* of Knowledge IR, not Knowledge IR itself — a breaking OKF spec bump is handled by an Emit-stage code change plus a re-render (`kc compile --emit-only`), never a Knowledge IR migration, since the wiki is disposable and wholesale-regenerated (ADR-010) rather than durable state.

---

## References

- [ADR-009](decisions/ADR-009-two-layer-ir.md) — the structural decision this document implements
- [ADR-003](decisions/ADR-003-current-state-delta-log.md) — delta as derived artifact; [ADR-004](decisions/ADR-004-entity-identity.md) — identity classes, anchors, evidence; [ADR-006](decisions/ADR-006-language-analyzers.md) — analyzer fact obligations; [ADR-007](decisions/ADR-007-plugin-architecture.md) — versioned plugin interfaces; [ADR-008](decisions/ADR-008-llm-abstraction-caching.md) — validated LLM payloads; [ADR-013](decisions/ADR-013-open-source-okf-conformance.md) — the third tracked version (`okf_spec_version`) and its re-render-not-migrate story
- `docs/architecture.md` §4–6
- Open items originating here: conflict-resolution policy ADR (future); delta `change_summary` granularity and schema detail → `data-model.md`
