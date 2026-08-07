# Pipeline

> Status: Living specification. Defines the compile execution model and per-stage contracts over the IR ([ir.md](ir.md)) and schema ([data-model.md](data-model.md)).
> Last updated: 2026-08-05

---

## 1. Run modes

| Mode | Scope | Notes |
|---|---|---|
| `kc compile --pr <N>` | the PR's source diff | reconciles first (§4); the normal CI-triggered path (ADR-002) |
| `kc compile --full` | whole repository | bootstrap + correctness escape hatch (ADR-003); slug-preserving when run against an existing database (ADR-004) |
| `kc reconcile` | missed merged PRs | standalone catch-up; same machinery as §4 |
| `kc verify` | whole repository, read-only | shadow full compile + equivalence check (§7); never persists |
| `kc compile --emit-only` | Emit stage only, against already-compiled Knowledge IR | no Collect/Extract/Normalize, no new `compile_runs` row; the cheap OKF-spec-version rollout path (ADR-013, §3.6) |

Both `kc compile` and `kc reconcile` accept `--verbose / -v`: prints a per-slug breakdown of added/changed/removed/moved entities in the final summary (counts are always shown; the breakdown is opt-in to avoid noise on large repos).

All modes run the same stage sequence; only Collect's scope differs (architecture.md §4).

## 2. Execution model

- **Lock:** the compile acquires the per-repo Postgres advisory lock (key = `repositories.id`) before any work; a concurrent compile blocks or exits cleanly (ADR-002).
- **Idempotence:** if `compile_runs` has a `succeeded` row for `(repo_id, pr_number)`, the run is a no-op (ADR-002).
- **Two commit disciplines**, resolving what "checkpointing between stages" (architecture.md §4) means precisely:
  - **Staging writes** — `compile_runs` (status `running`), `artifacts`, `facts`, and `llm_cache` entries — commit incrementally as stages progress. Safe because all are disposable staging or content-addressed (see §6.2 for why eager cache commits are correct).
  - **The atomic commit** — `entities`, `relationships`, `provenance`, `delta_changes`, and the run's `succeeded` status — is **one transaction** in Persist (ADR-003 invariant). A crash at any point before it leaves compiled state untouched and the run re-runnable.

## 3. Stage contracts

Each stage: what it reads, what it produces, and the invariants it owns. All stage implementations are plugins resolved from `kc.toml` in configuration order (ADR-007).

### 3.1 Collect

- **Reads:** the repository at the compiled commit; forge/Jira APIs. Scope per run mode (§5).
- **Produces:** `artifacts` rows (typed, content-hashed).
- **Owns:** PR↔commit linkage via **forge API association, never commit parentage** — squash/rebase merges rewrite history, so the PR's file diff and metadata come from the forge, making merge strategy irrelevant to scope (resolves the ADR-002 open item). PR↔Jira linkage is **issue-key extraction from the PR title/body** (regex, e.g. `DCA-1234`) *(additive clarification)* — not a forge-API-reported link — since neither GitHub's REST PR payload nor a generic forge API reliably exposes structured issue links; the Jira collector then fetches exactly those keys (`collectors/jira.py`).
- **Failure:** a collector that cannot reach its source fails the compile loudly (ADR-007 — never silently compile with a subset of configured collectors).

### 3.2 Extract

- **Reads:** staged artifacts (never the repo directly).
- **Produces:** Fact IR rows (ir.md §2). Deterministic extractors (analyzers per ADR-006, parsers) run first and always; LLM extractors run second, consulting `llm_cache` before every call and writing validated outputs through it (ADR-008).
- **Owns:** grammar pinning, parse-failure file skips (recorded as warnings, never compile failures — ADR-006); the LLM validation gate and budget accounting (ADR-008); mandatory anchors on candidates (ADR-004).
- **Progress:** `[llm] i/n <file> --changed` is emitted to stderr for each file that required a real LLM call (cache miss). Cache hits are silent — the progress stream only reflects actual network work, so a mostly-cached run produces few or no lines.
- **Records:** which extractor families ran over which scope — required by removal evidence (§5).

### 3.3 Normalize

- **Reads:** this compile's facts + current-state entities.
- **Produces:** the compile's candidate Knowledge IR (entities incl. Wiki Page derivation, relationships), fully identified.
- **Owns:** the ADR-004 cascade incl. intra-compile candidate dedup in content-hash order (ir.md §4.2); rename mapping + anchor currency rewrites (ir.md §2.2); conflict surfacing (ir.md §4.3); slug minting with dedup suffixes.
- **Failure:** an unknown fact shape is a loud failure (ADR-009 — no entity-smuggling through the fact layer).

### 3.4 Diff

- **Reads:** normalized candidate state + current state.
- **Produces:** the knowledge delta (`entity_changes`, `relationship_changes` — ir.md §3.4), applying the **removal-evidence rule** (§5).
- **Owns:** `moved` detection (anchor relocation, ADR-004); the dirty-entity set for Emit (ir.md §3.4 dirty rule: entity changes ∪ relationship endpoints).

### 3.5 Persist

- **Reads:** the delta + candidate state.
- **Produces:** the atomic commit (§2): current-state mutation + append-only delta rows + provenance snapshots + run `succeeded`.
- **Owns:** ADR-003's single-transaction invariant; slug uniqueness enforcement; provenance denormalization (data-model.md §4).

### 3.6 Emit

- **Reads:** Knowledge IR only (never facts — ADR-009). Input: the dirty-entity set.
- **Produces:** **publications** — file-shaped renders of compiled knowledge — plus embeddings for dirty entities (per active model generation, ADR-005). The reference publication is the wiki: regenerated Markdown pages for dirty pages (wholesale per page, ir.md), **OKF v0.2-conformant** ([SPEC.md](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — Open Knowledge Format; see [docs/okf-conformance.md](okf-conformance.md) for the full spec-to-emitter mapping): each page carries YAML frontmatter (entity slug, type, compile run, provenance refs) over standard Markdown, so the page set is simultaneously a human wiki and an agent-readable OKF bundle with zero extra tooling. Two reserved OKF filenames get exact-spec treatment — `index.md` (no general frontmatter beyond an optional bundle-root `okf_version`) and `log.md` (no frontmatter, date-grouped, prose changelog entries) — alongside a third, KC-specific `recent-changes.md` (not spec-reserved, frontmatter unrestricted) scoped to only the latest compile's delta, distinct from `log.md`'s full chronological history (ADR-013).
- **Spec-version tracking:** every compile run records `okf_spec_version` (alongside `fact_vocabulary_version`/`knowledge_model_version`, ir.md §5) — the OKF spec version its wiki emission targeted. `kc compile --emit-only` re-renders the wiki from already-compiled Knowledge IR with no new compile run — the migration path for rolling out a spec-version bump across previously-compiled repos without re-running Collect/Extract/Normalize (ADR-013). `kc validate-okf` checks an emitted bundle against the conformance rules directly.
- **Dirty-set contract (clarification):** the Emit-stage `dirty` parameter is `set[str] | None`, not merely `set[str]` — `None` means *no filter* (render every page; `--emit-only`'s spec-version rollout path above), while an empty-but-not-`None` set means *nothing is dirty* (render nothing). Conflating the two — treating an empty set as "no filter" — silently forces a full wiki rerender, and thus a full-tree publish commit, on every genuinely no-op compile. Caught via self-compile dogfooding; fixed in `wiki/emitter.py`/`compiler/run.py`.
- **Orphaned-page pruning (clarification):** emission is otherwise dirty-only/additive — a page is rewritten only when its owner is dirty, and is never touched at all once its owner is removed (the owner-is-None skip, §3.4/§3.6). Without an explicit prune step, a removed entity's page survives on disk (and in the published `knowledge/wiki` branch) forever, frozen at whatever content it had when last written — including a stale, pre-migration frontmatter shape from a much earlier emitter version. `WikiEmitter.emit` now compares every on-disk page against the current live owner-slug set on every run (regardless of `--full`/`--pr`/`--emit-only` or the dirty scope) and deletes anything with no current owner, so it is self-healing for orphans already stranded by prior compiles, not just future removals.
- **Publisher (generalized concept):** a Publisher is a plugin that ships a publication to a destination. One publication, many possible destinations:

  ```
  Emit ──▶ publication (wiki / OKF bundle) ──▶ Publisher ──▶ GitHub branch   (reference, ADR-010)
                                                            GitHub Pages
                                                            separate knowledge repo
                                                            Confluence
                                                            OKF bundle export (directory/archive)
  ```

  The reference destination is the `knowledge/wiki` branch (ADR-010); every Publisher inherits ADR-010's invariants (loop-safe, destination is publisher-owned and fully regenerable). The canonical home of compiled knowledge remains the database (ADR-001) — publications are renders, never stores.
- **Progress:** `[embed] n/total <N> entities --changed` is emitted to stderr once per batch of 64 entities. All entities in the batch are by definition dirty or pending (unchanged-hash rows are filtered before batching), so `--changed` is accurate for every line.
- **Owns:** entity→page mapping; embedding `pending` status on provider outage (degrade to FTS, backfill later — ADR-005).
- **Failure:** emit failures never roll back Persist — compiled state is already committed and correct; emission is re-runnable from the delta (idempotent by content hash).

## 4. Reconciliation algorithm (ADR-002, normative)

At the start of every incremental compile (and as `kc reconcile`):

1. Under the advisory lock, read the watermark: `max(merged_at)` over `succeeded` runs for this repo.
2. List PRs merged after the watermark from the forge API, ordered by `merged_at` (ties broken by PR number).
3. For each listed PR not already recorded `succeeded` (idempotence), run the full stage sequence in order — **including the PR that triggered this run**, in its merge-order position.
4. Each PR is its own compile run with its own atomic commit; a failure stops the sequence there (later PRs remain for the next reconcile — order is never violated by skipping ahead).

Consequence: any single successful trigger heals an arbitrary backlog, in order, exactly once.

## 5. Compile scope & removal evidence

- **Collection scope:** `--full` = whole repo; `--pr` = the PR's forge-reported file diff + its PR/Jira metadata artifacts.
- **Removal evidence** (ir.md §4.2.1, extended with the extractor condition): Diff may emit `op: removed` only if the entity's evidence location was **in collection scope** *and* **the extractor family that produced it actually ran** over that scope this compile. The second condition matters for degraded runs: in a `--no-llm` compile, LLM-derived entities are never removed — the extractor that could re-observe them didn't run, so absence is not evidence (§6.1).
- **PR and Jira entities are never removable** *(additive clarification, dogfood finding)*: they are records of events, not statements about current source — absence from any compile is never evidence against a merge that happened. Without this rule, a later PR touching the same files would delete an earlier PR's record via the file-scope check.
- **Edges of removed entities are recorded in the delta** *(additive clarification)*: Diff synthesizes `relationship removed` rows for every edge touching a removed entity, matching what the database cascade deletes — otherwise the append-only delta log silently under-records history.

## 6. Degraded modes

### 6.1 `--no-llm` (and LLM-provider outage)

Resolves the ADR-008 open item: **degraded compiles write to the shared knowledge base** — not a scratch state. Rationale: the deterministic pass is correct on its own (ADR-006 invariant), and freezing all knowledge because prose extraction is down would fail the freshness contract. Semantics:

- Deterministic entities update normally.
- LLM-derived entities are untouched: not updated, and per §5 never removed.
- The run is recorded `succeeded` with a `degraded: no-llm` marker in `compile_runs`; the wiki renders deterministic updates, keeping existing LLM prose.
- The next non-degraded compile reconciles the semantic layer naturally (cache makes re-extraction of unchanged content free).

### 6.2 Budget-cap halt

When the per-run LLM budget cap (`kc.toml`, ADR-008) is reached mid-Extract, the run **fails resumably**: status `failed (budget)`, nothing persisted to compiled state (the atomic commit never ran). Resumption is a plain re-run: completed LLM work survives in `llm_cache`, so the re-run pays only for what wasn't done.

**Clarification to ADR-008's Impact wording** ("cache writes ride the compile transaction"): cache entries commit **eagerly, outside the atomic commit** — otherwise a halted run would lose its cache and resumability would be fiction. This is consistency-safe precisely because the cache is content-addressed and immutable (ADR-008 invariants): an orphaned cache entry from a failed run is simply a prepaid answer. The ADR's invariants are untouched; only the Impact section's transactional phrasing is refined here.

### 6.3 Failure summary

| Failure point | Compiled state | Recovery |
|---|---|---|
| Collect/Extract/Normalize/Diff crash | untouched | re-run (staging + cache survive) |
| Persist crash mid-transaction | untouched (rollback) | re-run |
| Emit failure after Persist | committed and correct | re-emit from delta; no rollback |
| Budget halt | untouched | re-run; cache-funded resumption |
| Reconcile: PR *k* of backlog fails | PRs 1..k-1 committed | next reconcile resumes at *k* — order preserved |

## 7. `kc verify`

Runs Collect + Extract + Normalize as a **shadow full compile** (no Persist, no Emit), then matches the shadow entity set against current state using the ADR-004 cascade itself — never slug equality (ADR-004's reproducibility statement). Reports: entities the incremental history missed / fabricated / mismatched, with match-rate metrics (feeding ADR-004's threshold tuning). Nonzero divergence → nonzero exit; the remedy is a real `kc compile --full` (slug-preserving) into the database.

## 8. Open items

- Concurrent LLM request batching within Extract — throughput engineering, dogfood-tuned.
- Whether `kc verify` should sample (scoped shadow compiles) for cheap scheduled runs — post-dogfood.
- Publisher plugin contract: unblocked by [ADR-010](decisions/ADR-010-wiki-destination.md) — input = a publication (dirty pages + delta), output = delivery to one destination (reference: one commit to the `knowledge/wiki` branch); detail lands with the reference GitHub publisher implementation. Additional publishers (Pages, separate knowledge repo, Confluence, OKF bundle export) are additive per ADR-010's invariants.

## References

[ir.md](ir.md) · [data-model.md](data-model.md) · [ADR-002](decisions/ADR-002-ci-trigger.md) · [ADR-003](decisions/ADR-003-current-state-delta-log.md) · [ADR-004](decisions/ADR-004-entity-identity.md) · [ADR-006](decisions/ADR-006-language-analyzers.md) · [ADR-007](decisions/ADR-007-plugin-architecture.md) · [ADR-008](decisions/ADR-008-llm-abstraction-caching.md) · [ADR-009](decisions/ADR-009-two-layer-ir.md) · [ADR-010](decisions/ADR-010-wiki-destination.md) · [ADR-013](decisions/ADR-013-open-source-okf-conformance.md) · [okf-conformance.md](okf-conformance.md) · architecture.md §3–4
