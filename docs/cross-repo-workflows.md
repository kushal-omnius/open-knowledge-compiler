# Cross-Repo Workflows

How to set up and use Knowledge Compiler when a target repo depends on other compiled repos, and when tests live in a separate repo from the code they cover.

The architectural decision (query-time config map, no compile-time schema changes) is recorded in [ADR-011](decisions/ADR-011-cross-repo-dependency-resolution.md). This document covers operational setup and current limitations.

---

## Scenario

```
Repo A  (the service under test — depends on Repo B)
Repo B  (a library compiled into the same KC database)
Repo C  (a test repo — QA agent writes tests here)
```

A QA agent writing tests in Repo C needs to understand:
- what Repo A's components do
- which Repo B interfaces Repo A calls
- where the test coverage gaps are

---

## Setup

### 1. Compile both repos into the same KC database

Repo B first (it has no dependencies):

```bash
kc init --slug repo-b --forge-ref github.com/org/repo-b --dir /path/to/repoB
kc compile --full --dir /path/to/repoB
```

Then Repo A:

```bash
kc init --slug repo-a --forge-ref github.com/org/repo-a --dir /path/to/repoA
kc compile --full --dir /path/to/repoA
```

Both share the same `KC_DATABASE_URL` — the same Postgres instance is the cross-repo join point.

### 2. Declare the dependency in Repo A's `kc.toml`

```toml
[dependencies]
# Map import prefix → slug of the compiled repo in the same database (ADR-011).
# Exact or dotted-prefix match: "repo_b" matches "repo_b.module.Class".
repo_b = "repo-b"
```

This is query-time only — no recompile of Repo A is needed after editing this section.

### 3. Serve Repo A's knowledge to the QA agent

```bash
kc serve --dir /path/to/repoA
```

One `kc serve` process per repo. To expose both, run two processes on different ports and register both with the agent.

---

## What the QA agent can do

### Get cross-repo impact and test targets

```
test_plan("component/payment-processor")
```

Returns coverage gaps for Repo A components, including which Repo B interfaces they reach. The `resolve_dependency` coordinate in the output names the Repo B entity.

### Resolve a Repo B entity directly

```
resolve_dependency("repo_b.billing.DiscountEngine")
```

Returns the full Repo B entity (payload, relationships, source anchors) — the agent uses this to understand the interface contract before writing a test.

### Validate a test written in Repo C against Repo A's knowledge

```bash
kc validate-test /path/to/repoC/tests/test_payment.py \
  --for-entity component/payment-processor \
  --dir /path/to/repoA
```

`--dir` always points at the repo whose compiled knowledge is being tested, regardless of where the test file lives. The `kc-covers:` header travels with the test file.

---

## Known limitations

### Search is scoped to the served repo

`search_knowledge(...)` against `kc serve --dir repoA` searches Repo A's entities only. To search Repo B entities directly, register a second `kc serve --dir repoB` with the agent and route queries to it explicitly.

### `test_plan` does not recurse into Repo B's internal coverage gaps

If a Repo A component calls a Repo B function that has zero tests in Repo B, `test_plan` for the Repo A component will not surface "Repo B's function X is also uncovered." The cross-repo signal is: "Repo A reaches this Repo B interface" — Repo B's own coverage state is a separate `test_plan` call against Repo B's serve process.

### No compiled dependency edges

ADR-011 explicitly deferred compile-time `Project`-to-`Project` edges. The `[dependencies]` map is resolved at query time only — the database has no `depends_on` relationship between Repo A's entities and Repo B's entities. Cross-repo reachability is inferred at serve time from the config map, not stored as graph edges.

### Version constraints are not tracked

The dependency map links by import prefix, not by version. If Repo A pins Repo B at `v2.3` but the compiled KB reflects `v2.5`, the QA agent sees v2.5's entity shape. Version-aware snapshots are a deferred design item (see ADR index — "Releases as named checkpoints").

---

## Multi-repo MCP registration (Claude Code example)

```bash
# Register both repos as separate MCP servers
claude mcp add repo-a -- kc serve --dir /path/to/repoA
claude mcp add repo-b -- kc serve --dir /path/to/repoB
```

The agent can then call `search_knowledge` or `get_entity` on either server and `resolve_dependency` on the Repo A server to cross the boundary.

---

## References

- [ADR-011](decisions/ADR-011-cross-repo-dependency-resolution.md) — the decision: query-time config map, no compile-time schema changes
- [retrieval.md](retrieval.md) §5 — `resolve_dependency` MCP tool
- [kc-cli-reference.md](kc-cli-reference.md) — `kc validate-test` `--dir` flag, `kc.toml [dependencies]` section
- `BRAINSTORM-cross-repo-dependencies.md` — the design exploration that preceded ADR-011
