# Data Model

> Status: Living specification. Realizes [ir.md](ir.md) in PostgreSQL per [ADR-001](decisions/ADR-001-postgresql.md).
> Logical schema level: columns and keys are normative; exact SQL types, index parameters, and Alembic migrations are implementation.
> Last updated: 2026-08-05

---

## 1. Principles (inherited, not re-decided)

- Single PostgreSQL database; the only persistent store (ADR-001). Extensions: `pgvector`, built-in FTS.
- Every table carries `repo_id` (ADR-001 invariant).
- State mutation + delta append in one transaction per compile (ADR-003).
- All compiled state is disposable and rebuildable from source (vision DP 5; slug caveat per ADR-004).
- The delta log and match evidence are the durable history; everything else is a rebuildable projection.

## 2. Table catalog

### `repositories`

| Column | Notes |
|---|---|
| `id` PK | also the advisory-lock key (ADR-002) |
| `slug`, `forge_ref`, `default_branch` | |
| `config_ref` | where `kc.toml` was read from at registration |

### `compile_runs`

| Column | Notes |
|---|---|
| `id` PK, `repo_id` | |
| `scope` | `full` \| `pr` \| `commit` |
| `pr_number`, `commit_sha`, `merged_at` | `merged_at` is the reconciliation watermark for `scope='pr'` runs; see `commit_timestamp` |
| `commit_timestamp` | Committer timestamp for `scope='commit'` (direct-push) runs (migration 0006); NULL for `pr` and `full` runs. Unified reconciliation watermark: `max(COALESCE(commit_timestamp, merged_at))` across succeeded non-full runs (ADR-002 addendum). |
| `status` | `running` \| `succeeded` \| `failed` |
| `fact_vocabulary_version`, `knowledge_model_version`, `okf_spec_version` | per ir.md §5; `okf_spec_version` records the OKF spec version this run's wiki emission targeted — a historical record of what was targeted at that compile, not a live pointer (ADR-013). Nullable: rows predating ADR-013 have none. |
| `started_at`, `finished_at`, `error` | |
| `degraded` | boolean; `true` when compiled with `--no-llm` (pipeline.md §6.1) |
| `files_seen`, `files_parsed`, `files_failed`, `failed_files` | Compile completeness signal (migration 0007, dogfood-review finding): a parser failure skips the file, never the compile (ADR-006) — these make that skip visible instead of a clean-looking compile silently losing coverage. Populated by the Extract stage on every full/PR/commit run; NULL on rows predating this migration and on `kc compile --emit-only` reruns (no Extract stage). Surfaced via `knowledge_stats()`'s `knowledge_completeness` and `kc inspect` |

Idempotence check (ADR-002): a `succeeded` run for `(repo_id, pr_number)` makes a re-trigger a no-op.

### `artifacts`

| Column | Notes |
|---|---|
| `id` PK, `repo_id` | |
| `artifact_type` | git file, PR, Jira issue, OpenAPI doc, README, … |
| `source_ref` | forge/Jira/path reference at the compiled commit |
| `content_hash` | provenance anchor; also LLM-cache input correlation (ADR-008) |
| `content` | staged content; **prunable** — see §4 lifecycle |
| `compile_run_id`, `collected_at` | |

### `facts` *(staging, prunable)*

Fact IR rows (ir.md §2): `id`, `repo_id`, `compile_run_id`, `fact_type`, `payload` JSONB, `content_hash`, `extraction` JSONB (method, extractor+version, grammar/model/template versions), `artifact_ids`.

Facts are **per-compile staging**, not durable knowledge (ADR-009). See §4 for the pruning rule and why provenance survives it.

### `entities`

| Column | Notes |
|---|---|
| `id` PK (surrogate), `repo_id` | |
| `slug` | unique per `(repo_id)`; ADR-004 dedup suffixes applied at insert |
| `entity_type` | the ten canonical types plus `user_journey` (ADR-017, additive) and `state_model` (ADR-023, additive) |
| `name` | display name; may change without identity change |
| `payload` JSONB | per-type schema (ir.md §3.2) |
| `content_hash` | payload-only (dirty rule additionally consults relationship changes — ir.md §3.4) |
| `anchors` JSONB | LLM-derived entities only; **rewritten to current paths on every matching compile** (anchor currency, ir.md §2.2) |
| `search_vector` tsvector | generated from name + payload text; GIN-indexed (FTS) |
| `first_compile_run_id`, `last_compile_run_id` | |

### `relationships`

`(repo_id, from_entity_id, relation_type, to_entity_id)` unique; `relation_type` from ir.md §3.3, now including `traverses` (User Journey → step entity, ADR-017, additive). No payload in V1 (attributes on relationships are a breaking vocabulary change per ir.md §5).

**Stale-test detection (ADR-018, additive, no schema change):** `coverage_for`/`test_plan` compare a Test Coverage row's own `last_compile_run_id` against the `last_compile_run_id` of the component(s) it `covers`; if the component changed more recently than the test was last touched, the test is flagged `stale`. Pure read-time computation over the existing entity envelope — no new table or column.

**Mutation-kill rate (ADR-012's named trigger, additive):** `mutation_kill_rate`/`mutation_sample` are opt-in payload fields on Component rows, populated from a `[mutation]`-configured scores file (`_mutation_scores`, pipeline.md §3.3); no new table.

### `provenance`

One row per entity-fact derivation, **denormalized to survive fact pruning** (§4):

| Column | Notes |
|---|---|
| `entity_id`, `repo_id` | |
| `compile_run_id` | when this derivation happened |
| `fact_type`, `extraction` JSONB, `artifact_refs` JSONB | snapshot of the contributing fact's envelope (not a FK into prunable `facts`) |
| `anchors` JSONB | as recorded at that compile (raw, per ir.md §2.2) |
| `match_evidence` JSONB | ADR-004: fired cascade rule + numeric signals + decision; null for freshly minted / deterministic-key rows |

### `delta_changes` / `delta_relationship_changes` *(append-only, the delta log)*

Normalized rows rather than one JSONB blob per compile — the headline query "which business rules changed recently?" must be SQL, not JSON archaeology:

| `delta_changes` column | Notes |
|---|---|
| `compile_run_id`, `repo_id` | delta header (refs to PR/commits/Jira) lives on `compile_runs` |
| `op` | `added` \| `changed` \| `removed` \| `moved` |
| `entity_id`, `slug`, `entity_type` | slug + type denormalized so the log stays readable even if an entity is later removed |
| `change_summary` JSONB | **granularity decision (ADR-003 left this open):** list of changed payload paths **with old→new values per path** + a one-line human-readable summary per change; for `moved`, old→new anchor file paths. Old values make the delta log *replayable backward*: point-in-time field values for version-skew queries (pinned-dependency test generation) reconstruct from the log instead of requiring recompile-at-tag |
| `evidence` JSONB | supporting refs (facts' artifact refs) |

`delta_relationship_changes`: `compile_run_id`, `op` (`added`\|`removed`), `relation_type`, `from_slug`, `to_slug`.

Append-only is enforced by policy and privileges (no UPDATE/DELETE grants on these tables to the application role).

### `embeddings`

| Column | Notes |
|---|---|
| `entity_id` (PK, FK `ON DELETE CASCADE`), `model_id` (PK) | composite PK — one row per (entity, model generation) |
| `repo_id` | |
| `vector` | pgvector, dimensionless in-schema (dimensionality varies per model); exact-scan KNN suffices at dogfood scale — **one HNSW partial index per active model generation** (`WHERE model_id = …`) is an activation-time optimization, not required now |
| `content_hash` | of the embedded text (`llm/embeddings.py::embedding_text`) — skip re-embedding when unchanged |
| `status` | `current` \| `pending` (ADR-005: embedding outage degrades to FTS, backfilled next compile) |
| `updated_at` | |

### `llm_cache`

> **The one documented exemption to ADR-001's `repo_id` invariant** *(additive clarification)*: the cache is content-addressed and repo-agnostic by design — identical (template, model, input content) is the same answer in any repository, so sharing entries across repos is correct and saves cost. The schema test encodes this exemption explicitly.

| Column | Notes |
|---|---|
| `cache_key` PK | `hash(template_id + template_version + model_id + input content)` (ADR-008) |
| `template_id`, `template_version`, `model_id` | denormalized for retention queries |
| `output` JSONB | schema-validated only (ADR-008 invariant) |
| `created_at` | |

## 3. Delta representation decisions

- `entity_moved` (ADR-004) is `op: moved` in `delta_changes`, with old→new paths in `change_summary` — an op, not an attribute, so "what moved?" is a WHERE clause.
- Change granularity floor is entity-level (ADR-003); `change_summary`'s payload-path list is the finer detail, and its *shape* may evolve additively without a delta-vocabulary version bump (consumers must tolerate unknown summary fields).

## 4. Lifecycle & retention

The durable/disposable line, made explicit:

| Data | Class | Retention |
|---|---|---|
| `entities`, `relationships` | current state | live; rebuildable |
| `delta_changes` (+rel), `compile_runs`, `provenance` | **durable history** | kept indefinitely in V1; archival-by-export flagged for post-dogfood evidence (ADR-003) |
| `facts`, `artifacts.content` | staging | prunable after compile success; default: keep last N successful runs per repo (config) for debugging. **Provenance is denormalized precisely so this pruning breaks nothing** |
| `embeddings` | derived | superseded model generations dropped after backfill of the active generation completes |
| `llm_cache` | derived, load-bearing (ADR-008) | keep the **latest (template, model) generation** indefinitely; superseded generations prunable after a configurable window (default: 90 days) — never prune the active generation |

## 5. Indexes & headline queries

| Query (vision) | Served by |
|---|---|
| "Which business rules changed recently?" | `delta_changes (repo_id, entity_type, compile_run_id)` joined to `compile_runs.merged_at` |
| "What changed since last release?" | `delta_changes` filtered by `compile_runs.merged_at > release date` |
| "Which PR introduced this rule?" | `delta_changes` WHERE `slug = … AND op = 'added'` → `compile_runs.pr_number` |
| "Which tests cover this component?" | `relationships` (`covers`) |
| Keyword search | GIN on `entities.search_vector` |
| Semantic search | HNSW partial index per active model (ADR-005) |
| Hybrid + metadata filters | both branches share SQL predicates on `entity_type`/`repo_id`/recency (architecture.md §7) |

## 6. Migrations & versioning

- Alembic manages schema (architecture.md A9). Schema migrations are for *this catalog's shape*.
- Breaking **knowledge-model** changes are handled by slug-preserving full recompilation, not data migration (ir.md §5) — the exception is the delta log, which cannot be recompiled: delta-vocabulary breaking changes require an explicit log-compatibility migration note, per ir.md §5.
- `compile_runs` records both IR versions per run, so mixed-version history is always interpretable.

## 7. Open items

- Exact SQL types, HNSW/GIN parameters, partitioning of `delta_changes` at scale — implementation, revisited with dogfood data.
- Artifact `content` size policy for large files (store vs. hash-only + refetch) — decide at collector implementation.
- Cross-repo queries (post-V1 multi-repo UX) — no schema blocker; `repo_id` scoping is already universal. V1 reference behavior (query-time config map, no schema changes) resolved by [ADR-011](decisions/ADR-011-cross-repo-dependency-resolution.md).

## References

[ir.md](ir.md) · [ADR-001](decisions/ADR-001-postgresql.md) · [ADR-003](decisions/ADR-003-current-state-delta-log.md) · [ADR-004](decisions/ADR-004-entity-identity.md) · [ADR-005](decisions/ADR-005-embeddings-pgvector.md) · [ADR-008](decisions/ADR-008-llm-abstraction-caching.md) · [ADR-011](decisions/ADR-011-cross-repo-dependency-resolution.md) · [ADR-013](decisions/ADR-013-open-source-okf-conformance.md) · architecture.md §5
