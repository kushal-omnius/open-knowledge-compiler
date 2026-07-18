# ADR-007: Plugin Discovery via Entry Points, Activation via Configuration

## Status

Accepted

## Date

2026-07-17

## Context

The vision makes pluggability a design principle: collectors, extractors, normalizers, emitters (wiki generators, embedding writers), retrieval providers, language analyzers, and LLM providers must each sit behind a stable, independently testable interface. Open-source-first means third parties must be able to ship plugins as ordinary packages. Two questions must be answered coherently: **how are plugins discovered**, and **what makes a plugin active in a given compilation**?

The stakes are reproducibility and trust. A compilation whose output depends on which packages happen to be installed in the environment is not reproducible in any meaningful sense — so the discovery/activation split is not packaging trivia; it is a reproducibility boundary.

## Decision Drivers

- Extensibility — third-party plugins as ordinary pip packages, discoverable without manual wiring
- Determinism / reproducibility — compilation output must be a function of repo + configuration, never of environment accidents
- Simplicity — standard Python mechanisms; no bespoke plugin framework
- Maintainability — interfaces must be able to evolve without silently breaking the ecosystem
- Testability — each stage interface independently testable with fixture artifacts

## Considered Options

### Option A — Entry points for discovery, explicit configuration for activation

Plugins register under entry-point groups (`knowledge_compiler.collectors`, …) via standard packaging metadata. A repo-level `kc.toml` explicitly lists which discovered plugins are *active*, with their settings. Built-ins register through the same mechanism and are active by default profile.

**Pros**

- Discovery is the packaging ecosystem's native mechanism — `pip install kc-plugin-x` makes it *visible*, and `kc plugins list` can enumerate candidates.
- Activation is explicit and versioned with the repo: compilation output is a function of (repo, kc.toml), restoring reproducibility.
- Standard tooling (`importlib.metadata`); no custom registry code.

**Cons**

- Two-step UX: install, then activate — a (deliberate) friction.
- Entry-point metadata bugs surface at discovery time, slightly obscure to debug.

### Option B — Configuration-only registry (import paths in kc.toml)

No entry points; `kc.toml` names plugin classes by dotted import path.

**Pros**

- Maximally explicit; zero discovery machinery.

**Cons**

- Hostile to an ecosystem: no enumeration of installed plugins, every activation requires knowing internal module paths, refactors of plugin internals break user configs.
- Third-party docs devolve to "paste this import path" — the failure mode of every ad-hoc plugin system.

### Option C — Auto-activation on install

Installed plugins are active by default; configuration only disables.

**Pros**

- Zero-step UX; plugins "just work."

**Cons**

- **Breaks reproducibility fundamentally**: two machines with different installed packages silently compile different knowledge. In CI (ADR-002) this means an unrelated dependency change alters the knowledge base.
- Security posture: installing a package (possibly as a transitive dependency) grants it a place in the pipeline.

### Option D — Out-of-process plugins (subprocess protocol)

Plugins are executables speaking a JSON protocol over stdio, à la Terraform providers.

**Pros**

- Language-agnostic plugins; crash isolation.

**Cons**

- A protocol to version, serialize, and debug — heavy infrastructure the vision forbids before it is earned.
- Per-invocation process overhead across thousands of extraction calls.
- V1 has no demand for non-Python plugins (the compiler is Python; ADR-006 keeps even TS analysis in-process).

## Decision

**Option A — entry-point discovery, explicit `kc.toml` activation.**

Interfaces are one Python `Protocol` per stage: `Collector`, `Extractor`, `Normalizer`, `Emitter`, `RetrievalProvider`, `LanguageAnalyzer`, `LLMProvider`. Built-ins (git collector, tree-sitter analyzers, OpenAPI extractor, Markdown wiki emitter, pgvector embedder) are plugins registered through the same entry-point mechanism — the core defines interfaces and the pipeline, batteries included but not welded in.

Why A over the alternatives: B sacrifices the ecosystem for explicitness A already provides at the activation layer; C sacrifices reproducibility for a UX convenience; D buys isolation V1 doesn't need at a protocol cost the boring-infrastructure principle forbids.

## Architectural Invariants

- **Installing a package never changes compilation output.** Activation is always an explicit configuration act, versioned with the repository.
- Discovery and activation are distinct: discovery enumerates candidates; only `kc.toml` activates.
- Built-in and third-party plugins pass through the same discovery, activation, and interface mechanisms — no privileged path.
- Plugin execution order within a stage is deterministic (configuration order), never installation or enumeration order.
- Every stage interface is versioned; a plugin declares the interface version it implements, and mismatches fail loudly at activation, not silently at runtime.

## Consequences

### Positive

- Reproducible compilation as a function of (repo, kc.toml, compiler version) — the property ADR-003's deltas and ADR-004's identity both silently require.
- A real third-party story from day one: publish a package, document one `kc.toml` line.
- The pipeline core stays small: it orchestrates interfaces and owns no domain logic.

### Negative

- Interface versioning is a permanent maintenance contract; every stage `Protocol` change is a compatibility event.
- Activation friction: new users must edit `kc.toml` (mitigated by `kc init` writing a default profile with built-ins active).

### Tradeoffs Accepted

- **Convenience is traded for reproducibility** (vs. Option C) — deliberately, and the invariant is stated so the trade is never quietly reversed.
- In-process plugins mean a misbehaving plugin can take down a compile (vs. Option D's isolation); accepted because compiles are re-runnable batch jobs, not serving processes.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Interface evolution breaks third-party plugins | Ecosystem breakage on upgrade | Versioned interfaces; loud activation-time mismatch errors; deprecation window policy (to be set in plugin-system.md) |
| Misbehaving plugin (exception mid-stage) | Compile fails | Fail the compile loudly — never silently skip an activated plugin (a partial compile would corrupt deltas); the compile is re-runnable after fix/deactivation |
| Plugin name collision across packages | Ambiguous activation | Activation names are fully qualified (`package:name`); collisions fail at activation |
| kc.toml activates an uninstalled plugin | Compile cannot honor config | Hard error at startup — never proceed with a subset of configured plugins |
| Nondeterministic plugin (violates stage contract) | Non-reproducible output | Contract stated in interface docs; `kc verify` surfaces drift; cannot be mechanically prevented — accepted residual risk |

## Assumptions

- Standard packaging entry points remain the stable Python ecosystem mechanism they have been.
- Plugin authors keep plugins deterministic per the stage contracts (enforceable by convention and verification, not by the type system).
- `kc init` default profiles make the activation step negligible for the common case.
- All V1 plugin demand is Python (revisit trigger for Option D).

## Open Questions

- Interface deprecation window and compatibility policy — deferred to `plugin-system.md`.
- Per-plugin configuration schema validation — plugin-system.md.
- Whether MCP retrieval providers need capability negotiation (e.g., a provider that cannot filter by metadata) — retrieval.md.

## Impact

Affected documents:

- `architecture.md` §9
- `plugin-system.md` (planned) — the detailed contract per interface, versioning and deprecation policy
- `pipeline.md` (planned) — how the orchestrator resolves and orders activated plugins

Affected compiler stages:

- **All stages** — every stage boundary is a plugin interface defined under this ADR; the pipeline core is the only non-plugin code

## Alternatives Rejected

- **Config-only registry (B)** — explicit but ecosystem-hostile; import paths as public API.
- **Auto-activation (C)** — breaks reproducibility; environment accidents become knowledge changes.
- **Out-of-process protocol (D)** — infrastructure without a V1 customer; revisit only with real non-Python plugin demand.

## Future Reconsideration

Revisit if genuine demand emerges for non-Python plugins or for isolation of untrusted third-party plugins (both point to Option D as an *additional* transport behind the same interfaces), or if entry-point tooling shifts underneath the packaging ecosystem.

## References

- `docs/vision.md` — Design Principle 1 (pluggable stages); open-source-first posture
- `docs/architecture.md` — §9 (A6), §13 (module layout)
- [ADR-004](ADR-004-entity-identity.md) — Accepted; reproducibility invariants this ADR's activation model protects
- ADR-003 — deltas assume reproducible stage output
- ADR-006 — language analyzers as plugins; ADR-008 — LLM providers as plugins

## Self-Review

- **Truly architectural?** Yes — it defines every extension boundary and a reproducibility invariant that constrains all future components.
- **Already made?** Yes — architecture.md §9; this ADR adds Options C/D, ordering determinism, versioned interfaces, and the fail-loud policy.
- **Reversible?** Mostly two-way (discovery mechanism swappable behind `kc plugins`); the *invariants* (install ≠ activate, no privileged built-ins) are one-way commitments.
- **Dependent future documents:** plugin-system.md (primary), pipeline.md, retrieval.md.
- **Exposes unresolved decisions:** deprecation policy, config schema validation, retrieval capability negotiation — all listed for plugin-system.md/retrieval.md.
