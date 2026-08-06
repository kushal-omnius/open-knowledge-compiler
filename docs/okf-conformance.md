# OKF Conformance

How Knowledge Compiler's emitted wiki bundle maps onto the [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) (OKF) specification, section by section. The architectural decision to track spec version explicitly and treat migrations as re-renders (never data migrations) is recorded in [ADR-013](decisions/ADR-013-open-source-okf-conformance.md).

**Target spec version:** OKF v0.2 (`knowledge_compiler.OKF_SPEC_VERSION`). This document is reviewed whenever that constant changes.

**Implementation:** `knowledge_compiler/wiki/emitter.py` (emission) and `knowledge_compiler/wiki/okf_conformance.py` (the `kc validate-okf` checker).

---

## Bundle structure

KC emits a flat OKF bundle rooted at `kc.toml [wiki] output_dir` (default `kc-wiki`), one concept file per page-owning entity, plus the two reserved files:

```
kc-wiki/
├── index.md              # reserved — bundle-root navigation
├── log.md                # reserved — chronological changelog
├── recent-changes.md     # KC-specific, not reserved — last-compile-only view
├── component/
│   └── billing-rules.md
├── api/
│   └── get-discount.md
├── feature/
├── business_rule/
└── risk/
```

Entity types with their own concept page (`normalize.md` P6 "page owners"): `component`, `api`, `feature`, `business_rule`, `risk`.

## Frontmatter fields (concept pages)

| OKF v0.2 field | KC emits it? | Notes |
|---|---|---|
| `type` | **Yes, always** | The entity's `entity_type` (`component`, `api`, `feature`, `business_rule`, `risk`). Required by spec §11 rule 2 — `kc validate-okf` checks this on every non-reserved file. |
| `title` | Yes | Entity's display `name`. |
| `resource` | Not yet | Would map naturally to a forge URL for the source location — not currently emitted; a plausible additive enhancement. |
| `tags` | Not yet | No KC concept currently maps to free-form tags. |
| `sources` (provenance objects) | Not yet | KC's own extraction provenance (`provenance` table: model, template version, artifact refs) is richer than this field's shape but isn't currently projected into frontmatter. Historically KC used the `sources` key name for something else entirely (see below) — fixed. |
| `generated.by` / `generated.at` | Yes | `by` is `process:knowledge-compiler/<version>` (spec §7 actor convention); `at` is the compile run's `finished_at`, ISO 8601. Only emitted when a `finished_at` timestamp is available in the emission context. |
| `verified` | No | KC has no human-review workflow over compiled entities; not applicable yet. |
| `status` / `stale_after` | No | Plausible future mapping: a `removed`-but-not-yet-repersisted entity, or a risk/business_rule the diff engine flagged as stale. Not implemented. |

**KC-specific extension fields** (not part of the OKF vocabulary, but explicitly permitted — the spec tolerates arbitrary extra frontmatter keys): `slug`, `repo`, `compile_run`, `commit`, `files`.

### Fixed: the `sources`/`generated` key collisions

An earlier version of the emitter used `sources:` for a flat list of source file paths and `generated: true` as a bare boolean — both collide with OKF v0.2's actual shapes for those keys (`sources` is a list of provenance *objects*; `generated` is an object with required `by`/`at`). These were renamed/fixed:

- File paths now emit under `files:` (a KC extension key, no spec collision).
- `generated:` now emits the spec-shaped object when a timestamp is available, or is omitted entirely rather than emitting a boolean.

## Reserved filenames

### `index.md` (§8)

**Rule:** no general frontmatter; the sole permitted exception is a bundle-root `okf_version` field.

KC's `index.md` frontmatter contains only `okf_version`. Repo/commit/compile-run context that previously lived in frontmatter now renders as body prose instead (`Repo: ... · Compile run: ... · Commit: ...`), keeping the information visible without violating the reserved-file rule. Body content is a bulleted list per entity type (`## Components (N)`, etc.) — matches the spec's `* [Title](url) - description` convention (KC uses `-` bullets; both are equivalent CommonMark list markers).

### `log.md` (§9)

**Rule:** no frontmatter at all; date-grouped (`YYYY-MM-DD` headings, newest first); prose entries.

KC's `log.md` is built from the delta log (`delta_changes`, via the last 10 succeeded `compile_runs`), grouped by each run's `finished_at` date, with entries using the spec's convention words:

| Delta op | Log convention word |
|---|---|
| `added` | `**Creation**` |
| `changed` | `**Update**` |
| `removed` | `**Deprecation**` |
| `moved` | `**Update**` |

This is KC's full chronological history view (bounded to the last 10 compiles per repo — a practical cap, not a spec requirement).

### `recent-changes.md` — not reserved, KC-specific

This is **not** an OKF reserved filename — it's a KC-specific convenience page scoped to only the *latest* compile's delta, kept alongside `log.md` because the two serve different purposes: `log.md` is the spec-conformant historical record; `recent-changes.md` answers "what did the last compile change" without scanning history. Frontmatter is unrestricted here since the filename carries no spec obligations.

## Conformance checking

```bash
kc validate-okf --dir /path/to/repo
```

Checks every `.md` file in the wiki output directory against the three rules above (spec §11): non-reserved files need parseable frontmatter with a non-empty `type`; `index.md` may carry only `okf_version`; `log.md` may carry none at all. Never compiles — reads whatever the last `kc compile` wrote to disk, same posture as `kc validate-test`.

## Spec-version migration

Per ADR-013: a breaking OKF spec bump is handled as an emitter code change plus a re-render, never a database migration. `kc compile --emit-only` re-renders the wiki bundle from already-compiled Knowledge IR without re-running Collect/Extract/Normalize — the mechanism for rolling out a spec-version bump across every previously-compiled repo cheaply.

`compile_runs.okf_spec_version` records which spec version each compile's wiki emission targeted — a historical record of what was targeted at that compile, not a live pointer to the current emitter's version.

## Known gaps (not yet implemented)

- `resource` and `tags` frontmatter fields — plausible additive enhancements, no current KC concept maps to them.
- `sources[]` provenance objects (the real OKF shape) — KC's richer `provenance` table isn't currently projected into concept-page frontmatter at all; this is additive future work, not a conformance bug (the field is optional).
- `status` / `stale_after` lifecycle fields — no current mapping from KC's diff/removal semantics.
- The `Attested Computation` concept type (v0.2 §"Attested Computation") — a plausible future mapping for KC's mutation-kill-rate signal, entirely unexplored.

None of these are conformance violations — OKF v0.2 requires only `type`; everything else is optional, and consumers must not reject a bundle for their absence.

**Architectural note, not a gap:** `wiki/emitter.py` (what shape output takes) and `wiki/okf_conformance.py` (what shape is required) currently encode the same structural facts — reserved-filename frontmatter restrictions, required concept-page fields — independently, with nothing structurally preventing the two from drifting apart over time. [ADR-014](decisions/ADR-014-shared-okf-rules-file.md) (Proposed, **not implemented**) records a design to unify both behind one shared declarative rules file, so a future spec-version bump is a data diff reviewed once, not two independent code edits kept in sync by hand.
