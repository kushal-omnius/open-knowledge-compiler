# ADR-001: PostgreSQL as the Single Knowledge Store

## Status

Accepted

## Date

2026-07-17

## Context

The compiler needs one persistent store for compiled entities, relationships, provenance, deltas, embeddings, the LLM cache, and compile bookkeeping. The vision's boring-infrastructure principle and the architecture's constraints (single executable, no microservices, single storage backend) frame the question: **which single storage system can carry every persistence need the pipeline has?**

This decision was pre-committed in the project constraints ("PostgreSQL only"); this ADR records the rationale so the commitment is auditable rather than folklore — and honestly evaluates the alternative that the constraints do *not* automatically exclude: SQLite.

The needs the store must cover: relational integrity (entities/relationships), flexible per-type payloads (JSONB), full-text search, vector similarity (ADR-005), transactional persist (ADR-003's atomicity invariant), concurrency control across compile runs and a read-only serve process (ADR-002), and multi-repository scoping.

## Decision Drivers

- Simplicity — one storage system, operable by any open-source adopter
- Determinism — transactional persist (state + delta in one transaction, per ADR-003)
- Performance — current-state queries at 5M LOC-scale entity counts; concurrent serve + compile
- Extensibility — multi-repo from day one (`repo_id` everywhere); future growth without re-platforming
- Maintainability — mainstream operational knowledge; boring by design

## Considered Options

### Option A — PostgreSQL, single database

All persistence in one Postgres database: relational tables + JSONB payloads + built-in FTS + pgvector + advisory locks.

**Pros**

- **One system covers all six needs**: relations, JSONB, FTS, vectors (pgvector), ACID transactions, advisory locks for per-repo compile serialization. No second store, ever, for V1.
- True concurrent access: `kc serve` (long-running, read-only) and compile runs (batch writers) coexist under MVCC without coordination beyond the advisory lock.
- Multi-repo is a column, not a deployment question — one database serves many repositories.
- Ubiquitous operational knowledge; first-class Docker story for the reference deployment.

**Cons**

- A server process to run — the heaviest piece of the otherwise single-executable deployment (`docker compose` mitigates; still real friction for a laptop-only trial).
- Embedding/index throughput shares resources with the primary store (accepted; see ADR-005).

### Option B — SQLite (embedded)

A file-per-deployment embedded database; FTS5 for text, sqlite-vec for vectors.

**Pros**

- Zero operational footprint — genuinely single-executable; ideal trial UX for open source.
- FTS5 is capable; transactions are solid.

**Cons**

- **Single-writer concurrency**: `kc serve` plus a compile run plus `kc reconcile` contend on one writer lock; WAL mode softens but does not solve it — and the serve process is long-lived by design (ADR-002).
- Vector search (sqlite-vec) is young relative to pgvector; corpus growth headroom is unclear at the 5M LOC target.
- Multi-repo "eventually" means either one file per repo (cross-repo queries die) or one shared file (writer contention worsens).
- Advisory-lock-equivalent coordination across processes is DIY.

### Option C — PostgreSQL + dedicated engines (search/vector services)

Postgres for state; Elasticsearch/Qdrant-class services for search and vectors.

**Pros**

- Best-of-breed retrieval performance and features.

**Cons**

- Violates the stated constraints outright (single storage backend, no microservices) and the boring-infrastructure principle.
- Cross-store consistency machinery (dual writes, sync jobs) — precisely the complexity the vision forbids before it is earned; architecture.md §12 shows the corpus (compiled entities, not source lines) doesn't need it.

### Option D — Document store (MongoDB-class)

Documents for entity payloads; everything else adapted around it.

**Pros**

- Payload flexibility without schema migrations.

**Cons**

- The knowledge model is *explicitly relational* — typed entities with first-class relationships (vision: "relationships are explicit… not inferred at query time"). A document store makes the core structure the awkward part.
- Transactional multi-collection persist, FTS, and vector support are all weaker fits; JSONB already provides the flexible-payload benefit inside Postgres.

## Decision

**Option A — a single PostgreSQL database.**

Why A over the alternatives: C and D fail constraints or the data model's relational nature. B is the serious contender — and loses on exactly two grounds that matter architecturally: concurrent serve-plus-compile access (ADR-002's process model) and multi-repo growth. Postgres is the only option satisfying all six persistence needs *today* with headroom for the stated future.

SQLite's trial-UX advantage is real and is deliberately conceded; the reference `docker compose` (app + Postgres) is the mitigation, not a storage compromise.

## Architectural Invariants

- The single PostgreSQL database is the only persistent store of compiled knowledge. No second datastore may be introduced without superseding this ADR.
- All compiled state is disposable: the database can be dropped and rebuilt from the repository (vision Design Principle 5; slug caveat per ADR-004).
- Every table carries `repo_id`; no schema decision may assume single-repo.
- The serve process opens the database read-only; only compile processes write (shared with ADR-002).

## Consequences

### Positive

- One backup, one connection string, one operational surface for adopters.
- ADR-003's single-transaction persist, ADR-008's cache-rides-the-transaction, and per-repo advisory locking all come from one system's native features.
- Multi-repo needs no future re-platforming.

### Negative

- A database server accompanies the "single executable" — the deployment's one honest piece of infrastructure.
- All workloads (state, search, vectors, cache) share one resource pool; a heavy embedding pass can degrade serve latency (bounded by corpus size analysis in ADR-005).

### Tradeoffs Accepted

- **Trial friction is traded for concurrency and growth headroom** (vs. SQLite).
- **Best-of-breed retrieval is traded for zero cross-store consistency machinery** (vs. dedicated engines) — revisitable with evidence, per ADR-005/retrieval.md.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Postgres unavailable | No compile, no serve | Fail loudly; compiles are re-runnable batch jobs (ADR-002); no queuing or degraded-write modes |
| Resource contention (embedding pass vs. serve queries) | Serve latency degradation | Corpus is compiled entities, not source lines (small); if evidenced, standard Postgres tuning before any architectural change |
| Schema migration failure mid-upgrade | Broken deployment | Alembic migrations run transactionally where possible; disposability is the ultimate escape hatch (rebuild from source) |
| Adopter can't run Postgres (constrained environments) | Lost adoption at the margin | Accepted; storage interface (architecture.md §9) keeps a future embedded backend *conceivable* without promising one |

## Assumptions

- Compiled-knowledge volume (entities, not source lines) fits comfortably in a single mainstream Postgres instance at the 5M LOC / multi-repo target — thousands to low hundreds of thousands of entities per repo (architecture.md §12).
- Docker-based Postgres is acceptable friction for the open-source audience.
- pgvector remains a maintained extension (detailed in ADR-005).

## Open Questions

- Column-level schema — `data-model.md`.
- Connection management between the CLI's short-lived compile processes and the long-lived serve process — `pipeline.md`; operational, not architectural.
- Managed-Postgres guidance for adopters — docs concern, not ADR material.

## Impact

Affected documents:

- `architecture.md` §5
- `data-model.md` — the entire schema
- `storage.md` (planned) — operational guidance, migrations, backup

Affected compiler stages:

- **Persist** — transactional writes; **all stages** checkpoint through it
- **Serve (MCP)** — read-only consumer
- **Emit** — embeddings written here (ADR-005)

## Alternatives Rejected

- **SQLite (B)** — loses on serve/compile concurrency and multi-repo growth; its trial-UX win is conceded and mitigated, not ignored.
- **Dedicated engines (C)** — violates constraints; consistency machinery before evidence of need.
- **Document store (D)** — fights the explicitly relational knowledge model.

## Future Reconsideration

Revisit if retrieval quality/scale outgrows pgvector+FTS with evidence (ADR-005's reconsideration path), if a credible embedded deployment demand emerges (SQLite-backed storage plugin as an *addition* behind the storage interface), or if multi-repo scale forces partitioning beyond a single instance.

## References

- `docs/vision.md` — Design Principles 4 (boring infrastructure), 5 (disposable artifacts)
- `docs/architecture.md` — §5, §12
- [ADR-004](ADR-004-entity-identity.md) — Accepted; rebuild-modulo-slugs caveat on disposability
- ADR-003 — transactional persist; ADR-005 — pgvector; ADR-008 — cache in Postgres; ADR-002 — process model driving the concurrency requirement

## Self-Review

- **Truly architectural?** Yes — the persistence foundation every other decision writes through.
- **Already made?** Yes — pre-committed in the constraints; this ADR records rationale and honestly runs the SQLite comparison the constraint skipped.
- **Reversible?** Effectively one-way once schemas, FTS queries, advisory locks, and pgvector usage accumulate; that is precisely why the rationale is recorded now.
- **Dependent future documents:** data-model.md, storage.md.
- **Exposes unresolved decisions:** schema detail (data-model.md); none architectural beyond ADR-005's embedding specifics.
