# ADR-005: Embedding Storage in pgvector

## Status

Accepted

## Date

2026-07-17

## Context

Semantic retrieval (architecture.md §7) requires vector embeddings of compiled entities. Given ADR-001 (single Postgres store), the storage question looks forced — but recording it as an ADR matters for two reasons. First, "no embeddings in V1" is a genuine architectural alternative that deserves explicit rejection or adoption. Second, pgvector has real capacity and coupling implications that the project must accept knowingly, not discover later.

A scale fact shapes everything: the embedded corpus is **compiled entities, not source lines**. At the 5M LOC target this means thousands to low hundreds of thousands of rows per repository — three to four orders of magnitude below where pgvector's limits bite.

## Decision Drivers

- Simplicity — no second storage system (ADR-001 invariant)
- Incremental compilation — re-embedding must be delta-driven, not corpus-wide
- Performance — retrieval latency under `kc serve`; index build within compile budgets
- Extensibility — embedding models will change; storage must survive model churn
- Determinism — embeddings must never affect identity or deltas (ADR-004 boundary)

## Considered Options

### Option A — pgvector in the primary database

Vectors stored per entity with HNSW indexing; embedding provider (model) is a plugin.

**Pros**

- Zero additional infrastructure; embeddings ride ADR-003's compile transaction and ADR-001's backup/disposability story.
- Metadata filtering (entity_type, component, recency, repo_id) is a SQL predicate *in the same query* as vector search — the hybrid retrieval design (architecture.md §7) needs exactly this.
- At compiled-entity corpus size, HNSW recall/latency are comfortably within range.

**Cons**

- Embedding writes and index maintenance share resources with the primary store.
- Very large multi-repo growth eventually pressures a single instance (far beyond V1 scale).

### Option B — External vector database (Qdrant/Weaviate-class)

**Pros**

- Purpose-built vector performance and features (quantization, advanced filtering).

**Cons**

- Violates the single-store constraint (ADR-001 invariant) and boring-infrastructure principle.
- Dual-write consistency between knowledge base and vector store — machinery without a V1 customer, at a corpus size that doesn't need it.

### Option C — No embeddings in V1 (FTS only)

Ship keyword retrieval only; add semantic search later.

**Pros**

- Genuinely simpler; FTS alone serves many wiki/Q&A lookups.
- Defers model choice and embedding cost entirely.

**Cons**

- Semantic retrieval is in the vision's committed workflow ("Generate embeddings" is a pipeline stage) and MCP Q&A (milestone 2) leans on it for paraphrased questions — "which rules govern login?" must match entities that never contain the word "login."
- Retrofitting embeddings later is cheap *only* if the schema anticipates them now; deciding C would still require designing most of this ADR.

### Option D — File-based index (FAISS-class) alongside Postgres

**Pros**

- Fast, no DB extension dependency.

**Cons**

- A second persistence artifact with its own lifecycle, breaking the disposability story (rebuildable, but now two things to rebuild and keep consistent).
- No transactional tie to compiles; no SQL metadata filtering — hybrid retrieval reimplements joins in application code.

## Decision

**Option A — pgvector, with embeddings as derived, disposable, model-tagged artifacts.**

Normative details:

- Embeddings are computed from **compiled entities** (their normalized content), never from raw source lines.
- Every vector records its **embedding model identifier**; a model change is a new generation of vectors, re-embedded incrementally (content unchanged ⇒ old vector remains valid *for its model*; retrieval uses the active model's generation).
- Re-embedding is **delta-driven**: only entities in the compile's delta (ADR-003) are re-embedded, mirroring wiki emission.
- Embedding providers are plugins (ADR-007), called from the Emit stage.

Why A over the alternatives: B and D reintroduce the multi-store consistency problem ADR-001 exists to prevent, at a corpus size that cannot justify it; C deletes a committed pipeline stage and milestone-2 capability while saving little (the schema design work remains). A is the constraint-compliant option that also happens to be sufficient by the numbers.

## Architectural Invariants

- Embeddings are derived artifacts: disposable, rebuildable, never load-bearing for correctness.
- **Embeddings never participate in identity matching or delta computation** (ADR-004's boundary, restated at the storage layer).
- Every vector carries its model identifier; no query mixes vector generations across models.
- Embedding input is compiled-entity content, never raw source.

## Consequences

### Positive

- Hybrid retrieval (FTS + vector + metadata filters) is single-system SQL; no cross-store joins.
- Model upgrades are incremental re-embedding operations, not migrations.
- Corpus-size analysis gives comfortable headroom through V1 and multi-repo growth.

### Negative

- Embedding throughput couples to the primary database (accepted per ADR-001; bounded by corpus size).
- Per-model vector generations add schema/query complexity that a single-model assumption would avoid (data-model.md carries it).

### Tradeoffs Accepted

- **Best-of-breed vector features are traded for zero consistency machinery** — the same trade as ADR-001, applied to vectors specifically.
- Model-tagged generations accept storage duplication during model transitions in exchange for safe, incremental migration.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Model change forces corpus re-embedding | Cost/time spike | Incremental by generation: new model's vectors are built delta-first, backfilled by a bounded batch job; retrieval falls back to FTS-only for entities not yet re-embedded |
| Dimension mismatch across models | Index/query errors | Per-model generations isolate dimensions; enforced by schema (data-model.md) |
| HNSW recall degradation as corpus grows | Retrieval quality drop | Corpus monitored against pgvector guidance; parameters are configuration; far from limits at V1 scale |
| Embedding provider outage | Emit stage can't complete vectors | Vectors are non-load-bearing: compile completes with embeddings marked pending; FTS covers retrieval until backfill |
| pgvector extension unavailable (adopter environment) | No semantic retrieval | Retrieval provider is a plugin (ADR-007); FTS-only provider is a supported degraded configuration |

## Assumptions

- Embedded corpus stays in the thousands-to-low-hundreds-of-thousands of entities per repo (architecture.md §12 analysis).
- pgvector remains maintained and packaged for mainstream Postgres.
- One active embedding model per deployment at a time (generations exist for *transitions*, not parallel ensembles).
- Entity normalized content is a sufficient embedding input (no need to embed raw source) — dogfood-validated.

## Open Questions

- Embedding schema detail (generation representation, HNSW parameters as config) — `data-model.md`.
- Hybrid ranking (RRF constant, filter interaction) — `retrieval.md`; explicitly a two-way door (architecture.md §7).
- Chunking policy for long entity content — `retrieval.md`.

## Impact

Affected documents:

- `architecture.md` §7
- `data-model.md` (planned) — `embeddings` schema, model generations
- `retrieval.md` (planned) — hybrid ranking over these vectors

Affected compiler stages:

- **Emit** — delta-driven embedding writes
- **Serve (MCP)** — vector search consumer

## Alternatives Rejected

- **External vector DB (B)** — constraint violation; consistency machinery without a customer at this corpus size.
- **No embeddings (C)** — deletes a committed pipeline stage and weakens milestone 2; savings are smaller than they look.
- **File-based index (D)** — second persistence lifecycle; hybrid filtering reimplemented in application code.

## Future Reconsideration

Revisit if measured recall/latency degrades against corpus growth, if multi-repo scale pushes vector volume orders of magnitude past the analysis here, or if retrieval evidence (retrieval.md evaluations) shows pgvector's feature set is the quality bottleneck. The retrieval-provider plugin boundary (ADR-007) is the designed escape path — an external engine would arrive as a provider, with this ADR superseded knowingly.

## References

- `docs/vision.md` — workflow ("Generate embeddings"); Retrieval Philosophy
- `docs/architecture.md` — §7 (A4/A10), §12
- ADR-001 — single-store invariant this ADR operates under
- [ADR-004](ADR-004-entity-identity.md) — Accepted; embeddings excluded from identity (boundary restated here)
- ADR-003 — delta-driven re-embedding; ADR-007 — embedding/retrieval providers as plugins

## Self-Review

- **Truly architectural?** Yes — it fixes where a whole artifact class lives, its lifecycle, and its exclusion from identity; borderline cases (HNSW parameters, RRF constants) are explicitly pushed to configuration/retrieval.md.
- **Already made?** Yes — architecture.md §7/A4; this ADR adds the no-embeddings and file-index options and the model-generation lifecycle.
- **Reversible?** Two-way by design — the retrieval-provider boundary is the escape path; vectors are disposable artifacts.
- **Dependent future documents:** data-model.md, retrieval.md.
- **Exposes unresolved decisions:** hybrid ranking and chunking (retrieval.md) — listed, not decided.
