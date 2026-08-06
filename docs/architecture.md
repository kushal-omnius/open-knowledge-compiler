# Architecture: Knowledge Compiler

> Status: **v1.0 — FROZEN (2026-07-18).** All decisions ratified in ADR-001…ADR-010 ([decisions/index.md](decisions/index.md)); changes require a superseding ADR. Post-freeze, new (non-superseding) decisions are still recorded as new ADRs per the process this freeze established — see [ADR-011](decisions/ADR-011-cross-repo-dependency-resolution.md) (cross-repo dependency resolution, 2026-07-20), [ADR-012](decisions/ADR-012-defer-verification-requirement-entity.md) (defer VerificationRequirement entity, 2026-07-29), and [ADR-013](decisions/ADR-013-open-source-okf-conformance.md) (open-source release + OKF spec-version conformance/migration, 2026-08-05, Proposed).
> Derived from `docs/vision.md` (committed direction) and `INITIAL-Brainstorm.md` (exploratory).
> This document selects an implementation architecture; it does not re-litigate the vision.
> Decisions marked **[ADR]** must be recorded in `docs/decisions/` before implementation begins.
> Last updated: 2026-08-05

---

## 1. Constraints (given, not chosen)

- Single executable deployment; no microservices
- Implementation language: Python only
- Storage: PostgreSQL only
- Plugin architecture for every pipeline stage
- Incremental compilation; trigger only on merged PRs
- Open source first
- Target repositories up to ~5M LOC
- Multiple repositories supported eventually (design for it now, ship single-repo first)

To be explicit about the language split: **the compiler is implemented in Python only; the repositories it analyzes are Python and TypeScript** (the vision's committed V1 target languages). These are compatible — target-language parsing uses tree-sitter (§8), which runs in-process in Python for any grammar. No Node.js runtime is required.

*Post-freeze addition:* a third target language, plain JavaScript (`.js`/`.jsx`/`.mjs`/`.cjs`), was added 2026-08-06 via [ADR-015](decisions/ADR-015-javascript-language-analyzer.md) — same tree-sitter backbone, same in-process/no-Node.js invariant, no change to this section's frozen V1 commitment.

---

## 2. Undecided assumptions

The vision intentionally leaves these open. Each is listed with the reasonable options, a recommendation, and the reason. None of these were silently chosen.

| # | Assumption | Options | Recommendation | ADR? |
|---|---|---|---|---|
| A1 | Trigger mechanism | CI-invoked CLI · webhook server · polling daemon | **CI-invoked CLI** | **ADR-002** |
| A2 | Versioning model | Immutable snapshot per merge · mutable current state + append-only delta log | **Current state + delta log** | **ADR-003** |
| A3 | Entity identity across compiles | Natural keys · LLM-assigned stable IDs · similarity matching | **Natural keys where deterministic; match-then-mint for LLM entities** | **ADR-004** |
| A4 | Embedding storage | pgvector · external vector DB | **pgvector** (forced by Postgres-only, but record it) | **ADR-005** |
| A5 | Target-language parsing | tree-sitter · per-language native tooling (ast, TS compiler API) | **tree-sitter backbone, optional per-language enrichment** | **ADR-006** |
| A6 | Plugin discovery | entry points · config-file registry · both | **Entry points for discovery, config for activation** | **ADR-007** |
| A7 | LLM provider & caching | direct SDK · litellm-style router · thin custom interface | **Thin custom interface + content-addressed cache in Postgres** | **ADR-008** |
| A8 | Wiki destination | directory in target repo · separate git branch · standalone output dir | **Dedicated `knowledge/wiki` branch in the compiled repo; publisher pluggable** | **ADR-010** |
| A9 | DB access layer | raw SQL · SQLAlchemy + Alembic | SQLAlchemy Core + Alembic | no (reversible) |
| A10 | Retrieval ranking | FTS only · vector only · hybrid RRF | Hybrid with Reciprocal Rank Fusion | no (tunable later) |

Details and tradeoffs in the sections below. All ADRs referenced in this table (A1–A8) have since been written and **Accepted** (see §15 and [decisions/index.md](decisions/index.md)); the recommendations below are now decisions.

---

## 3. Runtime shape

**One Python package, one CLI (`kc`), two run modes.**

```
kc init                      # register a repo, create schema, write config
kc compile --full            # bootstrap / escape-hatch full compilation
kc compile --pr <N>          # incremental compilation of one merged PR
kc compile --emit-only       # re-render the wiki from already-compiled Knowledge IR only (ADR-013)
kc reconcile                 # catch up on merged PRs missed since last compile
kc verify                    # recompile-and-diff: check incremental state ≡ full compile
kc inspect                   # entity/relationship counts and last delta — first debugging surface
kc validate-test <f> --for-entity <slug>  # score a generated test's kc-covers: header
kc validate-okf              # check the emitted wiki bundle against OKF conformance rules (ADR-013)
kc serve                     # long-running: read-only MCP server over the knowledge base
```

- **Compile runs are batch processes**: start, run the pipeline, exit. No daemon, no queue, no scheduler. Concurrency control is a Postgres advisory lock per repository — a second compile of the same repo blocks or exits.
- **`kc serve` is the only long-running process**, and only for consumption (MCP), never for compilation. It is read-only against the knowledge base.
- Everything ships as a single pip-installable package; `docker compose` (app + Postgres) is the reference deployment.

### A1 — Trigger mechanism [ADR-002]

| Option | Tradeoffs |
|---|---|
| **CI-invoked CLI** (recommended) | CI workflow runs `kc compile --pr N` on merge. Pros: stateless, no inbound network surface, no webhook auth/replay handling, trivially open-source-friendly (a GitHub Action anyone can add). Cons: requires CI config per repo; missed events if CI fails. |
| Webhook server | Pros: no CI dependency. Cons: turns the deployment into an always-on service with inbound auth, retries, and replay handling — infrastructure the vision says to avoid. |
| Polling daemon | Pros: no repo-side setup. Cons: always-on process, API rate limits, latency, wasted work. |

**Why:** CI-invocation is the only option that keeps the deployment a stateless executable, and it matches the freshness contract (PR-merge granularity, not real-time). The missed-event weakness is covered by `kc reconcile`, which lists merged PRs since the last recorded compile and processes them in order — run it at the start of every compile, making every trigger self-healing.

**Challenge to the vision (bootstrap):** "trigger only on merged PRs" cannot be literally true — the first compile has no PR. `kc compile --full` is a required mode, not an escape hatch only. The vision's framing should be read as "*incremental* triggers are PR-merge only."

---

## 4. Pipeline

The compiler is a sequential, in-process pipeline. No workflow engine, no task queue — a compile is a function call chain with checkpointing to Postgres between stages.

```
                ┌────────────────────────── kc compile ───────────────────────────┐
Artifacts  ──▶  Collect  ──▶  Extract  ──▶  Normalize  ──▶  Diff  ──▶  Persist  ──┐
(git, PR, Jira,   raw           facts        canonical      knowledge    entities  │
 OpenAPI, tests)  artifacts                  entities       delta        + delta   │
                                                                                   ▼
                                                              ┌─────────  Emit  ─────────┐
                                                              ▼                          ▼
                                                        Wiki pages                 Embeddings
                                                        (dirty only)               (dirty only)
```

Stage contracts (each a plugin interface, §9):

1. **Collect** — fetch raw artifacts for the compile scope (full repo, or the PR's commits/diff/linked Jira). Output: `Artifact` records (type, source ref, content, content_hash) staged in Postgres.
2. **Extract** — turn artifacts into typed **facts**. Two extractor families behind one interface: deterministic (tree-sitter, git, OpenAPI parser, test mapper) and LLM-backed (business rules, feature narratives, risks). Every fact carries provenance (artifact ids) and extraction method.
3. **Normalize** — resolve facts into canonical entities (Feature, Component, API, Business Rule, Test Coverage, Risk, PR, Jira Story), assign identities (§6), and materialize explicit relationships.
4. **Diff** — compare normalized entities against current state → the **knowledge delta** (added/changed/removed entities and relationships).
5. **Persist** — apply the delta transactionally: update current-state tables, append to the delta log.
6. **Emit** — regenerate wiki pages and embeddings **only for dirty entities** (those in the delta). Emitters read compiled knowledge only, never raw artifacts.

Incremental vs. full compile run the *same* pipeline; only the Collect scope differs. This is what keeps `kc verify` meaningful: full compile is the reference semantics, incremental must converge to it.

---

## 5. Data & storage

Single PostgreSQL database. Extensions: `pgvector` (embeddings), built-in FTS (`tsvector`). Schema managed with Alembic; access via SQLAlchemy Core (A9 — typed, migration-friendly, and reversible if it becomes friction).

**Hybrid relational/JSONB schema** — normalized where the compiler needs to query, JSONB where entity payloads vary by type:

- `repositories` — multi-repo from day one; **every table below carries `repo_id`**.
- `artifacts` — raw collected inputs: type, source ref, `content_hash`, collected_at. The provenance anchor.
- `entities` — id, repo_id, entity_type, `natural_key`, `content_hash`, payload JSONB, `search_vector` tsvector, timestamps.
- `relationships` — (from_entity, to_entity, relation_type), explicit, compiled — never inferred at query time.
- `provenance` — entity/fact → artifact links, plus extraction method (deterministic vs. LLM, extractor name/version).
- `deltas` — append-only log: one row per compile, JSONB delta document, PR/commit refs.
- `embeddings` — entity_id, model, vector (pgvector), `content_hash` of the embedded text.
- `llm_cache` — (prompt_hash, model) → output. See §10; this table is load-bearing.
- `compile_runs` — bookkeeping: scope, status, timings, last processed PR (used by `kc reconcile`).

The exact column-level schema belongs in `docs/data-model.md`; this document commits only to the shape above.

### A2 — Versioning model [ADR-003]

| Option | Tradeoffs |
|---|---|
| **Current state + append-only delta log** (recommended) | Queries hit small current-state tables; history lives in `deltas`. Cons: point-in-time reconstruction requires replaying deltas or re-compiling at a commit — slow, but rare. |
| Immutable snapshot per merge (`valid_from`/`valid_to` on every row) | Pros: time-travel queries are trivial. Cons: every query needs temporal predicates; tables grow with merge count × entity count; wiki/MCP only ever want "now". |

**Why:** every V1 consumer (wiki, MCP Q&A, test generation) reads the *latest* state; the delta log already satisfies "which business rules changed recently" and per-PR provenance. Paying a permanent per-query temporal tax for a rare need is the wrong trade. If time-travel becomes a real requirement, snapshots can be layered on without breaking the delta log.

---

## 6. Entity identity — the hardest problem [ADR-004: Accepted]

Incremental compilation and knowledge deltas both assume the compiler can say "this is the *same* Business Rule as last compile, modified" rather than "one deleted, one added." **Identity is the mechanism that makes deltas possible, and it is genuinely hard for LLM-extracted entities.** The full analysis, architectural invariants, and failure modes are in [ADR-004](decisions/ADR-004-entity-identity.md); this section summarizes the accepted decision.

Policy, per entity class:

- **Deterministic entities get natural keys.** Component = module path; API = method + route (or OpenAPI operationId); Test Coverage = test node id; PR/Jira = their external ids. Wiki Pages have *derived* identity (owning entity slug + page type). Stable, cheap, correct.
- **LLM-extracted entities (Feature, Business Rule, Risk) use match-then-mint** with a strictly ordered deterministic cascade at Normalize time: (1) external key match → (2) provenance-anchor overlap (after git rename detection) → (3) normalized-name similarity as last resort, scoped to shared components → otherwise mint a new stable slug. Anchor evidence outranks name similarity because names are LLM wording — the least stable signal. The LLM never assigns identity; every match records the fired cascade rule and its numeric signal values as evidence.
- Renames/moves that break natural keys are recorded as delta events (`entity_moved`), not delete+add, when the anchor evidence (same file history via git rename detection) supports it.
- **When matching is uncertain, mint a new entity** — over-split beats over-merge: visible, correctable churn is preferred to silent history corruption (vision.md Design Principle 8). Thresholds are conservative accordingly.

**Reproducibility, stated precisely:** match-then-mint is history-dependent, so the identity map is part of compiled state. A full recompile into an *existing* database matches against current entities and preserves slugs; only a rebuild into an empty database mints fresh slugs, producing state equivalent *modulo slug renaming*. `kc verify` therefore compares states by entity matching, never by slug equality.

**Why:** letting the LLM assign or match identities makes deltas nondeterministic and unauditable — it violates the vision's own principle that nondeterminism is allowed "in wording, never in provenance or structure." Exact thresholds are configuration, tuned on the dogfood repo (ADR-004 Open Questions).

---

## 7. Retrieval (A10)

- **Keyword:** Postgres FTS over entity payloads (`search_vector`).
- **Semantic:** pgvector cosine search over entity embeddings.
- **Hybrid (default):** run both, merge with Reciprocal Rank Fusion; metadata filters (entity_type, component, recency, repo_id) are plain SQL predicates applied to both branches.

RRF is chosen because it is rank-based (no score-calibration between FTS and cosine), has one tunable constant, and is well understood. Ranking quality is explicitly *not* a V1 commitment — the retrieval provider is a plugin, and the vision leaves this open. Not an ADR; it is a two-way door.

### A4 — Embeddings [ADR-005]

pgvector is effectively forced by the Postgres-only constraint, but it should still be an ADR because it caps corpus size per index and couples embedding throughput to the primary database. At ~5M LOC the embedded corpus is *compiled entities, not source lines* — thousands to low hundreds of thousands of rows, comfortably within pgvector's HNSW range. Embedding provider (model) is a plugin; vectors carry the model name so re-embedding on model change is an incremental, cache-aware operation.

---

## 8. Language analyzers (A5) [ADR-006]

| Option | Tradeoffs |
|---|---|
| **tree-sitter backbone** (recommended) | One parsing runtime (Python bindings, prebuilt grammar wheels) for Python *and* TypeScript; uniform plugin interface; no Node.js. Cons: syntax-level only — no type inference, no import resolution out of the box. |
| Native tooling per language (`ast` for Python, TS compiler API for TypeScript) | Pros: richer semantic info. Cons: TS compiler API requires a Node runtime — breaks the Python-only, single-executable constraints; two completely different toolchains. |

*Post-freeze:* JavaScript (`tree-sitter-javascript`) was added as a third analyzer under this same backbone via [ADR-015](decisions/ADR-015-javascript-language-analyzer.md) — the tree-sitter choice's "adding language N = adding a grammar wheel + an analyzer plugin, nothing else changes" claim (ADR-006) held in practice.

**Why:** tree-sitter is the only option satisfying the constraints, and syntax-level extraction (symbols, signatures, routes-by-pattern, test structure) covers the deterministic column of the vision's extraction table. The interface allows **per-language enrichment**: the Python analyzer may additionally use stdlib `ast`/`importlib` for import graphs; a TS analyzer may later shell out to `tsc` *as an optional enrichment plugin* — never as a core dependency.

Analyzer plugin contract: given a file set, produce facts (components, symbols, API surfaces, test mappings) in the canonical model. Everything downstream is language-agnostic, which is precisely the vision's "second language stress test" (success criterion 5).

---

## 9. Plugin system (A6) [ADR-007]

- **Interfaces:** one Python `Protocol` per stage — `Collector`, `Extractor`, `Normalizer`, `Emitter` (wiki generators and embedding writers are Emitters), `RetrievalProvider`, `LanguageAnalyzer`, `LLMProvider`. Each independently testable with fixture artifacts.
- **Discovery:** Python entry points (`importlib.metadata`) under groups like `knowledge_compiler.collectors`. Third-party plugins are ordinary pip packages — the open-source-first distribution model.
- **Activation:** discovery ≠ activation. A repo-level config file (`kc.toml`) explicitly lists enabled plugins and their settings. Installing a package must never silently change compilation output — that would violate reproducibility.
- Built-in stages (git collector, tree-sitter analyzers, OpenAPI extractor, Markdown wiki emitter, pgvector embedder) are themselves plugins registered via the same mechanism. The core defines interfaces and the pipeline; it ships batteries included but not welded in.

Rejected alternative: config-file-only registry (import paths in `kc.toml` without entry points). Simpler, but hostile to a plugin ecosystem — no discoverability, and every plugin install requires manual path wiring. Entry points + explicit activation gets both.

---

## 10. LLM layer (A7) [ADR-008]

- **Thin provider interface** (`complete(prompt, schema) -> validated JSON`), implemented per provider. Structured output validated against a JSON schema per extractor; a fact that fails validation is rejected, logged, and retried once — never persisted malformed. Rejected alternative: a router dependency (litellm-style) — a heavy dependency for what V1 needs (one or two providers), and its abstractions leak into extractor code.
- **Content-addressed cache, in Postgres** (`llm_cache`): key = hash(prompt template version + model + input content). This is **not an optimization — it is load-bearing** (see §12): it is what makes "full recompilation cheap enough to run" true after the first compile, and what makes recompiles semantically stable (identical inputs → identical cached outputs, strengthening the reproducibility principle).
- Every LLM-derived fact records model, prompt template version, and source artifacts in `provenance` — auditable and selectively recompilable when prompts or models change (bump the template version → cache misses only where it matters).

---

## 11. Wiki emission (A8)

- Wiki pages are **Markdown files generated into a standalone output directory**, one page per major entity (feature, component, API group) plus index and recent-changes pages built from the delta log.
- **Incremental regeneration:** Normalize maintains an entity→page mapping; only pages whose backing entities appear in the delta are regenerated. Full regeneration is `kc compile --full`'s job and the consistency escape hatch. This answers the vision's open "regenerate vs. patch" question at the *page* granularity: pages are always regenerated wholesale, but only dirty pages — no intra-page patching, which is fragile.
- **Cross-links** use stable entity slugs (from §6 identity) as filenames/anchors — link consistency is a direct consequence of identity stability, not a separate mechanism.
- **Publishing is a generalized plugin concept**: Emit produces *publications* (the wiki as OKF-conformant Markdown — [okf.md](https://okf.md/)); a **Publisher** ships a publication to a destination. Reference destination: a dedicated `knowledge/wiki` branch in the compiled repository, forge-rendered, publisher-owned, loop-safe by construction ([ADR-010](decisions/ADR-010-wiki-destination.md)). Other destinations (GitHub Pages, separate knowledge repo, Confluence, OKF bundle export) are additive publisher plugins. The canonical home of compiled knowledge is the database (ADR-001) — publications are renders, never stores.

---

## 12. Scaling posture (~5M LOC) — and a challenged assumption

**Challenge:** the vision asserts full recompilation must remain "cheap enough to run." At 5M LOC this is **false for the LLM pass without caching** — a naive full compile would push thousands of source-derived prompts through an LLM at real cost and hours of wall time. The architecture makes the vision's claim true by construction, not by hope:

1. **The deterministic pass is the skeleton and must stand alone.** tree-sitter parsing of 5M LOC is minutes, not hours. A repo compiled with *zero* LLM budget still yields components, APIs, tests, structure — a useful (if prose-poor) wiki.
2. **LLM extraction is scoped and cached.** Extractors run on *changed* content (incremental) or *cache-missed* content (full). First-ever compile of a large repo is the only expensive one, and `kc.toml` supports LLM scoping (include/exclude paths, per-run budget caps) so teams control the bill.
3. **Emission is delta-driven** — wiki pages and embeddings regenerate only for dirty entities regardless of repo size.
4. **Multi-repo** is a `repo_id` column and per-repo advisory locks today; nothing else. No shared-nothing partitioning, no per-repo databases — that would be complexity the vision forbids before it is earned.

---

## 13. Module layout

Matches the structure committed in the brainstorm, mapped to the stages above:

```
knowledge_compiler/
├── compiler/      # pipeline orchestration, compile_runs, verify, reconcile
├── collectors/    # git, GitHub PR, Jira, OpenAPI, README (Collector plugins)
├── extractors/    # deterministic + LLM extractors, language analyzers (tree-sitter)
├── storage/       # schema, migrations, repositories (SQLAlchemy + Alembic)
├── retrieval/     # FTS, vector, hybrid RRF (RetrievalProvider plugins)
├── wiki/          # Markdown emitters, page mapping, publishers, OKF conformance checker (okf_conformance.py, ADR-013)
├── mcp/           # MCP server: search_knowledge, get_entity, impact_plan, test_plan, recent_changes, …
├── llm/           # provider interface, cache, prompt templates (versioned)
└── cli.py         # kc entry point
tests/             # per-stage fixture-driven tests; golden-file wiki tests
```

MCP tools are read-only views over compiled knowledge (retrieval + entity/delta lookup). The MCP server never triggers compilation — compilation is CI's job (A1).

---

## 14. Summary of challenges raised against the source documents

1. **"Full recompilation cheap" is only true with a load-bearing LLM cache** (§10, §12). The cache is core architecture, not an optimization.
2. **Entity identity is the hardest unsolved problem** and the vision assumes it implicitly by promising deltas (§6). It gets its own ADR and explicit failure-mode handling.
3. **"Trigger only on merged PRs" needs two exceptions**: bootstrap full compile and reconciliation after missed events (§3).
4. **Wiki publishing location was undecided but success-critical** — resolved by [ADR-010](decisions/ADR-010-wiki-destination.md): a dedicated `knowledge/wiki` branch in the compiled repo, publisher-pluggable (§11).
5. **Python+TypeScript analysis under a Python-only implementation constraint** required an explicit resolution (tree-sitter, §8), with the note that TypeScript analysis is syntax-level in V1 (type-aware enrichment via `tsc` is an optional post-V1 plugin, never a core dependency).

## 15. Architecture Decision Records

ADR-001 through ADR-012 and ADR-015 are **Accepted**; ADR-013, ADR-014, and ADR-016 are **Proposed** — see [decisions/index.md](decisions/index.md) for summaries, dependencies, and the dependency graph:

- [ADR-001](decisions/ADR-001-postgresql.md) — PostgreSQL as the single knowledge store
- [ADR-002](decisions/ADR-002-ci-trigger.md) — CI-invoked CLI trigger, reconcile-first
- [ADR-003](decisions/ADR-003-current-state-delta-log.md) — Current-state + append-only delta log
- [ADR-004](decisions/ADR-004-entity-identity.md) — Entity identity: natural keys + match-then-mint
- [ADR-005](decisions/ADR-005-embeddings-pgvector.md) — Embedding storage in pgvector
- [ADR-006](decisions/ADR-006-language-analyzers.md) — tree-sitter as the language-analyzer backbone
- [ADR-007](decisions/ADR-007-plugin-architecture.md) — Plugin discovery via entry points, activation via config
- [ADR-008](decisions/ADR-008-llm-abstraction-caching.md) — LLM provider abstraction + content-addressed cache
- [ADR-009](decisions/ADR-009-two-layer-ir.md) — Two-layer IR: Fact IR (plugin contract) + Knowledge IR (consumer contract)
- [ADR-010](decisions/ADR-010-wiki-destination.md) — Wiki destination: dedicated branch in the compiled repo
- [ADR-011](decisions/ADR-011-cross-repo-dependency-resolution.md) — Cross-repo dependency resolution: query-time config map (added 2026-07-20 during dogfood)
- [ADR-012](decisions/ADR-012-defer-verification-requirement-entity.md) — Defer VerificationRequirement entity; mutation-kill rate is the V1 sub-component precision signal (added 2026-07-29)
- [ADR-013](decisions/ADR-013-open-source-okf-conformance.md) — Open-source release as `open-knowledge-compiler` + OKF spec-version conformance and migration (Proposed 2026-08-05)
- [ADR-014](decisions/ADR-014-shared-okf-rules-file.md) — Shared declarative OKF rules file unifying the emitter and `kc validate-okf` (Proposed 2026-08-06, not implemented)
- [ADR-015](decisions/ADR-015-javascript-language-analyzer.md) — JavaScript language analyzer (`.js`/`.jsx`/`.mjs`/`.cjs` via `tree-sitter-javascript`) (Accepted, implemented 2026-08-06)
- [ADR-016](decisions/ADR-016-java-language-analyzer.md) — Java language analyzer, structural extraction only; Spring/JAX-RS route detection explicitly deferred (Proposed 2026-08-06, not implemented)

Decisions still unresolved are listed in [decisions/index.md](decisions/index.md) with the future design document responsible for each.
