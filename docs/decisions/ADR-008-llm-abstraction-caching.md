# ADR-008: LLM Provider Abstraction and Content-Addressed Caching

## Status

Accepted

## Date

2026-07-17

## Context

LLM extraction produces the compiler's semantic layer: feature narratives, business rules, risks, wiki prose. Two architectural questions arise. First, **how extractors talk to models** — the abstraction determines provider portability and how model output enters the knowledge base. Second, **whether and how LLM output is cached** — which architecture.md §12 shows is not an optimization question at all: at ~5M LOC, the vision's promise that "full recompilation remains cheap enough to run" is *false* without caching. The cache is load-bearing architecture.

There is also a reproducibility dimension: ADR-004's identity cascade and ADR-003's deltas assume that unchanged input produces stable extraction output. Only a cache can make that literally true across compiles.

## Decision Drivers

- Determinism / reproducibility — unchanged input must yield identical extraction output across compiles
- Performance & cost — full recompiles and large incremental compiles must not re-pay for unchanged content
- Accuracy — malformed model output must never enter the knowledge base
- Extensibility — providers must be swappable (open-source adopters will not share one vendor)
- Simplicity — no heavyweight dependency for what V1 needs
- Maintainability — prompt evolution must be possible without corrupting or orphaning cached knowledge

## Considered Options

### Option A — Thin custom provider interface + content-addressed cache in Postgres

A single interface — `complete(prompt, schema) -> validated JSON` — implemented per provider as a plugin (ADR-007). An `llm_cache` table keyed by `hash(prompt template version + model + input content)` stores validated outputs. Extractors consult the cache before every call.

**Pros**

- The interface is exactly as large as the compiler's need: structured, schema-validated completion. Nothing leaks into extractor code.
- Cache in Postgres (ADR-001) rides existing transactions, backup, and the disposability story — no second storage system.
- Content-addressing makes cache correctness trivial: the key *is* the input; staleness is impossible by construction, invalidation is a non-concept.
- Providers are ordinary plugins; adding one is implementing one method.

**Cons**

- Provider-specific capabilities (native structured-output modes, batching, streaming) need per-provider adapter work behind the single method.
- The project owns retry/rate-limit handling per provider rather than inheriting it from a router library.

### Option B — Router library (litellm-style)

Adopt a multi-provider routing dependency as the LLM layer.

**Pros**

- Many providers supported immediately; retry/fallback machinery included.

**Cons**

- A heavy, fast-moving dependency for what V1 needs (one or two providers); its release cadence becomes the compiler's problem.
- Its abstractions (model strings, provider quirks, response shapes) leak into extractor code — the opposite of a boundary.
- Caching would still have to be built (its notion of caching is response-level, not content-addressed against template versions).

### Option C — Direct SDK usage per extractor, no abstraction

Each LLM extractor imports a vendor SDK directly.

**Pros**

- Zero indirection; full access to provider features.

**Cons**

- Provider lock-in smeared across every extractor; swapping providers is a rewrite.
- Schema validation, caching, and provenance recording would be reimplemented (or forgotten) per extractor.
- Untestable without network; fixture-driven stage tests (ADR-007) become impossible.

### Option D — No cache (recompute LLM extraction every compile)

Any interface option, but LLM calls always execute.

**Pros**

- No cache table, no template-versioning discipline.

**Cons**

- **Falsifies the vision**: full recompilation at 5M LOC costs thousands of prompts per run — "cheap enough to run" becomes marketing.
- Extraction flapping: the same unchanged file can yield differently-worded entities per compile, churning identities (ADR-004 explicitly assumes the cache prevents this).
- Every compile's cost scales with corpus size instead of change size, breaking the incremental economics (ADR-003).

## Decision

**Option A — thin custom interface, providers as plugins, content-addressed cache in Postgres.**

Normative details:

- **Validation gate:** output failing JSON-schema validation is rejected, logged, retried once, and never persisted malformed. Only validated output enters the cache or the knowledge base.
- **Cache key:** `hash(prompt template version, model identifier, input content)`. Prompt templates are versioned artifacts; improving a prompt bumps its version, producing cache misses exactly where the change matters and nowhere else.
- **Provenance:** every LLM-derived fact records model, prompt template version, and source artifacts (extending ADR-004's evidence discipline to content).
- **Budget controls:** `kc.toml` LLM scoping (include/exclude paths, per-run budget caps) governs first-compile economics (architecture.md §12).

Why A over the alternatives: B imports a dependency treadmill and still doesn't solve the actual hard part (content-addressed caching); C destroys testability and portability; D breaks the vision's economics and ADR-004's stability assumption. A is small enough to own and exactly shaped to the need.

## Architectural Invariants

- No LLM output is persisted anywhere without passing schema validation.
- Every LLM-derived fact records model, prompt template version, and source artifacts.
- The cache is content-addressed; entries are immutable and never expire by time. Unchanged (template, model, input) ⇒ byte-identical output across compiles.
- Extractor code depends only on the provider interface — never on a vendor SDK.
- LLMs never assign or mutate identity (restating ADR-004's invariant at the layer that enforces it).

## Consequences

### Positive

- "Full recompilation cheap enough to run" becomes true by construction after first compile — the load-bearing property architecture.md §12 demands.
- Unchanged inputs produce literally identical extraction output, eliminating flapping and stabilizing ADR-004's matching inputs.
- Provider swaps and model upgrades are configuration + selective recompilation (bump versions → targeted cache misses), not migrations.
- Extractors are testable with a fake provider and fixture cache.

### Negative

- The cache table grows with distinct (template, model, input) tuples — unbounded in principle (bounded in practice by content change rate; retention is a data-model.md concern alongside ADR-003's delta archival).
- Prompt-template versioning is a discipline the team must actually follow; an unversioned prompt edit silently serves stale cache.
- Per-provider adapter maintenance (retries, rate limits) is owned in-house.

### Tradeoffs Accepted

- **Breadth for depth:** few providers, properly adapted, over many providers shallowly routed (vs. B).
- **First-compile cost is accepted** as the one genuinely expensive operation, managed by scoping/budget caps rather than architecture.
- Cached wording is frozen until inputs or versions change — "the model got better" alone does not refresh prose (deliberate: stability over freshness; refresh = version bump).

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Unversioned prompt edit | Stale cache served for changed template | Template version is part of the template artifact itself (single file, version field); CI lint can enforce bump-on-change; residual risk accepted |
| Schema validation failure persists after retry | Missing semantic fact | Fact skipped and recorded as a compile warning with provenance; never persisted malformed; visible in delta review |
| Provider API drift / outage | Compile cannot complete LLM pass | Deterministic pass is unaffected (ADR-006 invariant); compile fails loudly or runs with `--no-llm` degradation flagged in the run record |
| Cost runaway on first compile of a large repo | Budget shock | kc.toml scoping + per-run caps; compile halts at cap with resumable state (cache makes resumption cheap) |
| Cache poisoning (bad-but-valid output cached) | Wrong fact persists across compiles | Correctable by template or model version bump (targeted invalidation-by-rekey); provenance identifies affected facts |
| Model deprecation by vendor | Cached entries keyed to a retired model | Entries remain valid (they are history); new compiles use the new model id ⇒ natural re-extraction |

## Assumptions

- A JSON-schema-validated completion call is expressible against every provider worth supporting (native structured output or constrained retry).
- Postgres comfortably holds cache volume for target repos (same class of assumption as ADR-001 generally).
- Prompt templates live in the repo as versioned artifacts (`llm/` module, per architecture.md §13).
- Team discipline (plus lint) suffices for version-bump-on-edit.

## Open Questions

- Cache retention policy for superseded (template, model) generations — data-model.md, alongside delta archival.
- Batch/concurrent request handling within a compile (throughput engineering) — pipeline.md; not architectural.
- Whether `--no-llm` degraded compiles write to the shared knowledge base or a scratch state — pipeline.md.
- Default provider(s) shipped active in `kc init` profiles — product decision, not ADR material.

## Impact

Affected documents:

- `architecture.md` §10, §12
- `data-model.md` — `llm_cache` schema, retention
- `pipeline.md` — Extract stage cache consultation, budget-cap halt semantics
- `plugin-system.md` (planned) — `LLMProvider` interface contract

Affected compiler stages:

- **Extract** — sole caller of the provider interface; owns cache consultation and validation gate
- **Persist** — cache writes ride the compile transaction
- **Normalize / Diff** — indirect: stable extraction output stabilizes identity matching and deltas

## Alternatives Rejected

- **Router library (B)** — dependency treadmill, leaky abstractions, and the hard part (content-addressed caching) still unbuilt.
- **Direct SDKs (C)** — lock-in in every extractor; untestable; validation/provenance become per-extractor afterthoughts.
- **No cache (D)** — falsifies the vision's recompilation economics and ADR-004's stability assumption.

## Future Reconsideration

Revisit if provider count grows beyond what thin adapters sustain (router as an *implementation* behind the same interface), if a provider's semantics can't be expressed as schema-validated completion, or if cache growth forces a retention architecture rather than a policy.

## References

- `docs/vision.md` — Extraction Philosophy (deterministic-first, LLM for semantics); Design Principle 5 (reproducibility)
- `docs/architecture.md` — §10 (A7), §12 (the cache as load-bearing)
- [ADR-004](ADR-004-entity-identity.md) — Accepted; assumes this cache prevents extraction flapping; identity invariant restated here
- ADR-001 — cache lives in the single Postgres store; ADR-003 — incremental economics; ADR-007 — providers as plugins

## Self-Review

- **Truly architectural?** Yes — it fixes the semantic layer's boundary, its reproducibility mechanism, and the economics of recompilation.
- **Already made?** Yes — architecture.md §10/§12; this ADR adds the no-cache option (to show it fails), validation-gate normativity, and failure handling.
- **Reversible?** Interface: two-way (a router could implement it). Cache-as-load-bearing: effectively one-way — removing it re-falsifies the vision.
- **Dependent future documents:** data-model.md, pipeline.md, plugin-system.md.
- **Exposes unresolved decisions:** cache retention, degraded-compile semantics, batching — listed for data-model.md/pipeline.md.
