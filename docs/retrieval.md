# Retrieval & Serve

> Status: Living specification, written FROM the milestone-2 implementation (the
> deferral was deliberate: architecture.md §7 fixed the shape, code fixed the details).
> Realizes ADR-005 (embeddings) and the serve half of ADR-002.
> Last updated: 2026-07-19

---

## 1. Shape (inherited from architecture.md §7)

Two branches over one store, fused:

```
query ──┬─ keyword:  websearch_to_tsquery over entities.search_vector (GIN)
        └─ semantic: pgvector cosine KNN over embeddings (current generation)
              │
        reciprocal-rank fusion (k=60) ── provenance-carrying results
```

- Both branches share SQL predicates (`repo_id`, `entity_type`); wiki pages are
  excluded from results (they document entities; the entity is the answer).
- RRF rather than score mixing: FTS ranks and cosine distances are incomparable.
- Results always carry slug, payload, and anchors — an answer that can't say
  where it came from doesn't ship (vision DP 4).

## 2. Degraded modes (ADR-005)

| Condition | Behavior |
|---|---|
| `[embeddings]` disabled (default) | FTS-only; fully functional, not semantic |
| Provider outage during compile | affected rows marked `pending` (null vector); warning; next compile backfills |
| No `current` generation for the query embedder | hybrid facade silently degrades to FTS |

## 3. Embeddings lifecycle

- Emitter is a **post-Persist stage** (same never-roll-back contract as the wiki):
  embeds dirty entities + pending rows, skips unchanged embedded text by content
  hash — recompiles cost nothing.
- Embedded text = entity type + name + string payload fields, deterministically
  rendered (`llm/embeddings.py::embedding_text`).
- One generation per `model_id`; switching models creates a parallel generation
  (data-model.md §4 retention). Vectors are dimensionless in-schema; exact-scan
  KNN suffices at dogfood scale — HNSW partial indexes are an activation-time
  optimization, not schema.
- Providers: `openai` / `azure-openai` behind the ADR-008-style factory
  (`[embeddings]` in kc.toml, credentials from env only); `FakeEmbedder` for tests.

## 4. FTS notes (implementation findings)

- `entities.search_vector` is an ORM-declared generated column (`Computed`),
  GIN-indexed. It indexes the name **plus a de-dotted rendering of the name**:
  Postgres tokenizes `pkg.mod` as a single host-token, so without the de-dotted
  form, searching "mod" (or "normalize" against `…compiler.normalize`) matches
  nothing — dogfood finding, migration 0004.

## 5. `kc serve` (milestone 2, ADR-002's read-only half)

Stdio MCP server over the compiled knowledge base. **Never compiles** — state
updates come exclusively from CI-triggered `kc compile`; the server is a pure
reader and can be restarted/scaled freely.

Tools map 1:1 to the headline queries the schema was indexed for
(data-model.md §5); query logic lives in `mcp/queries.py`, directly tested,
reusable by a future `kc query` CLI:

| MCP tool | Answers |
|---|---|
| `search_knowledge(query, entity_type?, limit?)` | hybrid/FTS search with provenance |
| `get_entity(slug)` | payload, anchors, relationships, provenance + match evidence; components also get `cross_repo_dependencies` when an `external_dependencies` entry resolves via `[dependencies]` |
| `resolve_dependency(coordinate)` | cross-repo lookup (query-time only, `kc.toml` `[dependencies]` map — no compiled edge): does this coordinate name another repo compiled into this database? |
| `impact_plan(slug)` | composed planning query: one-hop `affected` entities (`depends_on`/`governs`/`implemented_by`/`affects`), which of those have `coverage_gaps`, and outbound `cross_repo_dependencies` — pure composition over `get_entity`/`coverage_for`, no new schema. Cross-repo *inbound* impact is out of scope (per-consumer `[dependencies]` config has no cross-repo registry to search) |
| `list_entities(entity_type)` | enumeration |
| `recent_changes(runs?)` | delta log per compile, old→new values |
| `which_pr_introduced(slug)` | PR/commit attribution from the delta log |
| `coverage_for(component_slug)` | `covers` edges |
| `knowledge_stats()` | counts + last compile (incl. degraded flag) |

Install: `pip install 'knowledge-compiler[serve]'`. Register with an agent, e.g.
Claude Code: `claude mcp add kc -- kc serve --dir <repo>`.

## 6. Open items

- HNSW index activation policy (needs dogfood-scale data).
- Cross-repo *search* UX (post-V1; `repo_id` scoping is already universal). Cross-repo *dependency resolution* has a first cut — `resolve_dependency`/`get_entity`'s `cross_repo_dependencies`, config-mapped, query-time only (BRAINSTORM-cross-repo-dependencies.md Option B). A compiled rollup edge (Option C in that doc) remains open, gated on the milestone-3 eval-criteria question.
- Result snippeting/highlighting for keyword hits.
