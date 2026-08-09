# ADR-002: CI-Invoked CLI as the Compilation Trigger

## Status

Accepted

## Date

2026-07-17

## Context

The vision's freshness contract is PR-merge granularity: a merged PR updates the knowledge base automatically (success criterion 1). Something must therefore *invoke* compilation when a merge happens. The trigger mechanism determines the deployment's entire process model — whether the Knowledge Compiler is a stateless batch tool or an always-on service — which is why this is an architectural decision and not an integration detail.

Two boundary cases stress any trigger design: **bootstrap** (the first compile has no PR — "trigger only on merged PRs" cannot be literally true) and **missed events** (whatever the mechanism, merges will occasionally go unprocessed; the design must self-heal rather than assume perfect delivery).

## Decision Drivers

- Simplicity — no always-on compilation infrastructure (single executable; vision Design Principle 4)
- Determinism — merges processed exactly once, in order; concurrent triggers must be safe
- Maintainability — self-healing beats perfect-delivery assumptions
- Extensibility — open-source adopters must be able to wire the trigger with commodity tooling (a CI step), regardless of forge
- Performance — trigger latency is bounded by CI latency, which the PR-merge freshness contract already tolerates

## Considered Options

### Option A — CI-invoked CLI

The repository's CI runs `kc compile --pr <N>` on merge (e.g., a workflow on the default branch). The compiler ships a reference CI snippet/action.

**Pros**

- The deployment stays a **stateless batch executable**: no inbound network surface, no webhook auth/replay handling, no daemon lifecycle.
- Forge-agnostic in the way that matters: every forge has CI; a webhook receiver would need per-forge event schemas.
- Open-source adopters integrate with one workflow file — commodity knowledge.
- Failure handling inherits CI's own retry/visibility (failed compile = red run, re-runnable).

**Cons**

- Requires CI configuration in every compiled repository.
- CI outages/misconfigurations silently skip merges — *must* be compensated (see reconciliation).
- Compile cost runs on CI infrastructure unless the CI step delegates to a runner the team controls.

### Option B — Webhook server

A long-running process receives forge webhooks and runs compilations.

**Pros**

- No per-repo CI dependency; near-immediate trigger.

**Cons**

- Converts the deployment into an always-on service: inbound TLS/auth, webhook signature verification, replay and duplicate-delivery handling, queueing for concurrent events — a standing infrastructure bill the vision forbids.
- Still needs reconciliation (webhooks are at-least-once at best; outages drop events) — so it pays the service cost *and* needs Option A's safety net anyway.

### Option C — Polling daemon

A resident process polls the forge API for new merges.

**Pros**

- No repo-side setup at all.

**Cons**

- Always-on process with scheduling state; API rate-limit management; polling latency vs. cost tuning.
- Strictly dominated: it is reconciliation running continuously as a service — the design below gets the same guarantee without the daemon.

### Option D — Server-side git hooks (post-receive)

**Pros**

- Fires exactly on the git event, no CI or webhooks.

**Cons**

- Requires control of the git server — unavailable on hosted forges, which is where the open-source audience lives.
- Hooks see pushes, not PR semantics (linked Jira, PR metadata are collector inputs the pipeline needs).

## Decision

**Option A — CI-invoked CLI, made self-healing by mandatory reconciliation, with explicit bootstrap.**

Normative details:

- **Reconciliation is not optional:** every incremental compile *begins* by reconciling — listing merged PRs since the last recorded compile (`compile_runs`, ADR-003) and processing any missed ones in merge order, then the triggering PR. `kc reconcile` also exists standalone. Every trigger therefore heals prior gaps; a single working CI run catches up an arbitrary backlog.
- **Bootstrap:** `kc compile --full` is a first-class mode, not an escape hatch — the vision's "trigger only on merged PRs" is precisely scoped to *incremental* compilation (architecture.md §3 challenge).
- **Concurrency:** a per-repository Postgres advisory lock (ADR-001) serializes compiles; a concurrent trigger blocks or exits cleanly. Merge-order processing plus the lock yields exactly-once, in-order semantics without any queue.
- **Process split:** compilation is never triggered by the serve process; `kc serve` is read-only (invariant shared with ADR-001).

Why A over the alternatives: B pays for a standing service and still needs A's reconciliation; C *is* reconciliation, uneconomically packaged as a daemon; D is unavailable to the target audience. A is the only option whose failure story (CI skips a merge) is fully repaired by machinery every option needs anyway.

## Architectural Invariants

- Compilation runs only as an explicitly invoked batch process; no component of the system triggers compilation autonomously.
- Every incremental compile reconciles before processing its trigger; merged PRs are processed in merge order, exactly once (enforced via `compile_runs` + advisory lock).
- The serve process never writes and never compiles.
- A compile is idempotent to re-invocation: re-running a recorded PR is a no-op (bookkeeping check), making CI retries safe.

## Consequences

### Positive

- The deployment model of architecture.md §3 holds: stateless batch + one read-only server, nothing else.
- Missed events are a routine, self-repairing condition — not an incident.
- Adopters integrate with a copy-pasted workflow file; no secrets beyond what the collectors already need (forge/Jira tokens).

### Negative

- Knowledge freshness depends on the compiled repo's CI health; a broken workflow means staleness until the next successful trigger (bounded by reconciliation, but unbounded in time if CI stays broken).
- Per-repo CI setup is a real adoption step that a hosted webhook service would remove (post-V1 territory).

### Tradeoffs Accepted

- **Trigger latency and delivery guarantees are delegated to CI** in exchange for zero standing infrastructure — consistent with the PR-merge freshness contract, which never promised real-time.
- Compile compute location (CI runner vs. self-hosted runner) is the adopter's choice and cost.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| CI fails/skips a merge | Knowledge temporarily stale | Next successful trigger reconciles the gap; standalone `kc reconcile` for manual/cron repair |
| CI retries a completed compile | Duplicate trigger | Idempotence invariant: recorded PR ⇒ no-op |
| Concurrent triggers (rapid merges) | Contention | Advisory lock serializes; reconciliation preserves merge order regardless of trigger arrival order |
| Out-of-order trigger arrival | Would corrupt delta ordering if processed naively | Reconciliation processes by merge order from the forge API, not trigger arrival |
| Long CI outage | Large backlog | Reconciliation processes the backlog serially on first recovery; LLM cache (ADR-008) keeps cost proportional to actual change |
| Forge API rate limits during reconcile | Slow catch-up | Reconciliation lists PRs (cheap calls); collector fetches are the same work any trigger design pays |
| Squash/rebase merges obscure PR linkage | Wrong compile scope | Collector concern (PR association via forge API, not commit parentage); flagged for pipeline.md, not a trigger problem |

## Assumptions

- Compiled repositories have CI capable of running a Python CLI (or a container) on merge to the default branch.
- The forge API can enumerate merged PRs since a marker, in merge order (true of major forges).
- Compile duration on CI runners is acceptable for incremental scope (deterministic pass is minutes-scale; LLM scope is delta-sized, ADR-008).
- `compile_runs` reliably records the reconciliation watermark (ADR-003).

## Open Questions

- Reference CI implementations to ship (which forges' snippets/actions in V1) — product/docs decision, not ADR material.
- Whether `kc reconcile` should be recommended as a scheduled belt-and-braces job — operational guidance for `storage.md`/docs.
- Compile-scope semantics for merge strategies that rewrite history — `pipeline.md` (Collect stage).

## Impact

Affected documents:

- `architecture.md` §3
- `pipeline.md` — Collect scope per trigger type; reconciliation algorithm detail
- `mcp.md` (planned) — serve process boundaries (read-only invariant)

Affected compiler stages:

- **Collect** — scope selection per trigger (PR slice vs. full)
- **Persist** — `compile_runs` watermarking, idempotence check
- **Serve** — explicitly *not* a trigger path

## Alternatives Rejected

- **Webhook server (B)** — standing service costs, and reconciliation is still required; pays twice.
- **Polling daemon (C)** — reconciliation packaged as an always-on process; strictly dominated.
- **Server-side hooks (D)** — unavailable on hosted forges; wrong event granularity (pushes, not PRs).

## Addendum — Commit-fill reconcile (2026-08-09)

**Problem:** reconcile was PR-centric; direct pushes to the default branch, squash commits with no PR, and repos without a PR workflow were silently skipped.

**Resolution (Option C from BRAINSTORM-commit-reconcile.md):** reconcile now runs two passes per invocation, merged by timestamp:
1. PR-based pass (unchanged): `ForgeGateway.list_merged_prs` — produces `scope='pr'` runs with full `pr_observed`/Jira metadata.
2. Commit-fill pass: `ForgeGateway.list_commits` — any commit whose SHA is not already covered by Pass 1 produces a `scope='commit'` run; Jira keys extracted from commit messages.

**Watermark change:** unified to `max(COALESCE(commit_timestamp, merged_at))` across both `pr` and `commit` scoped succeeded runs. The `commit_timestamp` column (migration 0006) stores the committer timestamp for `scope='commit'` runs; `merged_at` remains the watermark field for `scope='pr'` runs.

**Idempotence:** PR runs checked via `(repo_id, pr_number)`; commit runs checked via `(repo_id, commit_sha, scope='commit')`.

**Unchanged invariants:** merge-order processing, advisory lock, atomic Persist commit, exactly-once semantics, and degraded-mode behaviour all hold without modification. PR metadata (`pr_observed`, Jira linkage) is preserved exactly where it exists.

## Future Reconsideration

Revisit if a hosted/multi-tenant offering emerges (webhooks become the natural front door for a service that already runs 24/7 — B's costs are then sunk), or if dogfood evidence shows CI-dependency staleness is a recurring pain that scheduled reconciliation doesn't adequately bound.

## References

- `docs/vision.md` — success criterion 1; freshness contract (Non-Goals: real-time indexing)
- `docs/architecture.md` — §3 (A1), bootstrap/reconciliation challenge
- ADR-003 — `compile_runs` watermark and delta ordering this trigger model preserves
- ADR-001 — advisory locks; read-only serve invariant
- ADR-008 — cache bounding backlog catch-up cost
- [ADR-004](ADR-004-entity-identity.md) — Accepted; in-order processing keeps identity matching against correct prior state

## Self-Review

- **Truly architectural?** Yes — it fixes the process model (batch vs. service), exactly-once semantics, and the deployment's operational shape.
- **Already made?** Yes — architecture.md §3/A1; this ADR adds Options C/D, promotes reconciliation from mitigation to invariant, and adds idempotence.
- **Reversible?** Two-way at the mechanism level (a webhook front door could invoke the same CLI); the invariants (batch-only compilation, read-only serve, reconcile-first) are the durable commitments.
- **Dependent future documents:** pipeline.md (reconciliation detail, collect scope), mcp.md.
- **Exposes unresolved decisions:** merge-strategy scope semantics (pipeline.md); shipped CI integrations (docs) — listed, not invented.
