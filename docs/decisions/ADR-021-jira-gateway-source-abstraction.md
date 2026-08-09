# ADR-021: Jira Gateway Source Abstraction — `[jira] source = "rest" | "file"`

## Status

Accepted

## Date

2026-08-09

## Context

The Jira collector (`collectors/jira.py`, pipeline.md §3.1) has had exactly one
gateway since it existed: `AtlassianJiraGateway`, a live REST call authenticated
via `JIRA_BASE_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN` from the environment. That is
the only backend that can run from `kc compile --pr N`'s unattended,
CI-triggered path (ADR-002) — it needs no interactive session.

A separate, real need surfaced: an agent driving a compile interactively (e.g.
via Claude Code) may already have its own Jira access through an Atlassian MCP
connector, without the repo ever having a Jira API token configured. That
connector access is a claude.ai-connector-style, user-delegated OAuth grant —
the agent (the MCP *client*) can call it, but the `kc` process it spawns is a
bare Python subprocess with no path to the same tool connection; MCP tool
access belongs to the interactive session's harness, not to child processes it
launches. So "run `kc compile` from inside an agent session with Jira MCP
access" does not, by itself, give the Jira collector that access.

## Decision Drivers

- ADR-002's invariant: the only Jira backend usable from the CI-triggered path
  must need no interactive credential
- ADR-007: activation and configuration are explicit, plugins fail loud on
  misconfiguration
- Backward compatibility: every existing `kc.toml` has `[jira] enabled = true`
  with no `source` key at all — that shape must keep working unchanged
- Reuse the existing `JiraGateway` Protocol seam rather than inventing a new
  extension mechanism for one more backend

## Considered Options

### Option A — Give `kc compile` an MCP client, call the connector directly

Embed an MCP client inside the Jira collector so it can call an Atlassian MCP
server's tools (e.g. `getJiraIssue`) itself.

**Pros:** no intermediate file; one less moving part conceptually.
**Cons:** doesn't solve the actual problem — the connector's OAuth grant is
held by the interactive session, not by `kc`'s subprocess; embedding an MCP
client wouldn't grant `kc` a credential it was never issued. Also pulls a
heavier runtime dependency (an MCP client stack) into a CLI tool built to run
unattended in CI, for a backend that structurally can never run unattended.

### Option B — Agent pre-fetches, writes a JSON cache; a new `FileJiraGateway` reads it

The agent (already holding whatever Jira access it has) fetches the PR-linked
issue keys itself and writes them to a JSON file shaped like `JiraIssue`; a
new `FileJiraGateway` implementing the existing `JiraGateway` Protocol reads
that file instead of calling any API.

**Pros:** requires zero new runtime dependency; reuses the Protocol seam
exactly as designed (ADR-007) — `_jira_facts`, `normalize.py`, and `ir.py`
need no changes; works with *any* source of Jira access the agent has (an MCP
connector, a browser session, manual copy-paste), not just one connector
implementation.
**Cons:** only covers compiles the agent runs interactively with an
already-fresh cache — not a substitute for a real, unattended integration.

### Option C — Do nothing; require a real API token for any agent-driven use too

Leave the collector as-is; an agent without a configured Jira API token simply
gets no Jira facts.

**Pros:** zero implementation cost.
**Cons:** wastes real, already-available Jira access (via MCP) that has no
way to reach KC today, for no correctness reason.

## Decision

**Option B.** Add `FileJiraGateway` (`collectors/jira.py`), and give
`build_jira_gateway` an explicit `source` selector:

```toml
[jira]
enabled = true
source = "rest"              # default; live API via JIRA_BASE_URL/EMAIL/TOKEN
# source = "file"             # alternate: read cache_file instead
# cache_file = "jira-cache.json"
```

- `source` **defaults to `"rest"`** when absent — every `kc.toml` written
  before this ADR keeps working unchanged.
- `source = "file"` reads `cache_file` (default `jira-cache.json`), a flat
  JSON object keyed by issue key, each value shaped like `JiraIssue` (all
  fields but `key` optional). A key missing from the cache is silently
  omitted — the same "absence is data, not an outage" contract
  `AtlassianJiraGateway` already has for a 404, not a new gap.
- **Any other value fails loud** (`JiraError`) at gateway-construction time —
  a typo (`"flie"`) must never silently fall back to REST (a confusing
  missing-env-var failure later) or silently read a stale file, matching
  ADR-007's fail-loud posture.

Option A is rejected outright, not deferred — it doesn't solve the stated
problem regardless of implementation effort, since the credential it would
need was never issued to `kc`'s process in the first place.

## Architectural Invariants

- `source = "file"` is valid only for compiles a human/agent runs
  interactively with a cache they just produced. It must never be wired into
  the CI-triggered path (ADR-002) — there is no mechanism by which an
  unattended job could produce a fresh cache, and a stale one would silently
  under-report Jira facts with no signal that anything's wrong.
- The `JiraGateway` Protocol gains no new method — `FileJiraGateway` and
  `AtlassianJiraGateway` are interchangeable from every caller's perspective
  (`_jira_facts`, tests), exactly as ADR-007 intends for a plugin seam.
- An unrecognized `source` value is a construction-time `JiraError`, never a
  silent default.

## Consequences

### Positive

- Unblocks agent-driven compiles with real Jira data using whatever Jira
  access the agent already has, with no new Jira API token required.
- Zero changes to `_jira_facts`, `normalize.py`, `ir.py`, or any consumer of
  compiled `jira_story` entities — the seam absorbed the new backend cleanly.
- Every existing `kc.toml` keeps working with no edit required.

### Negative

- `source = "file"` compiles are only as fresh as the last time someone
  refreshed the cache — a new PR merging later, citing a key not yet in the
  cache, is silently invisible to that compile (same shape as a genuinely
  nonexistent key; not a new failure mode, but worth knowing).
- Two backends to reason about instead of one, for anyone reading the
  collector for the first time.

### Tradeoffs Accepted

- Freshness/automation traded for unblocking a real, immediate use case
  (agent-driven compiles) without waiting on a non-interactive Jira
  credential to be provisioned.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| `source = "file"` but `cache_file` doesn't exist | `get_issues` raises `JiraError` | Fails loud at fetch time, not silently returning zero issues |
| Cache file exists but isn't valid JSON | `JiraError` | Fails loud, distinct message from the missing-file case |
| A key isn't in the cache | Silently omitted from the result | Matches `AtlassianJiraGateway`'s existing 404 behavior — not a new contract |
| `source` is an unrecognized string | `JiraError` at `build_jira_gateway` | Fails at construction, before any fetch is attempted |

## Assumptions

- An agent producing the cache file has already resolved the correct set of
  PR-linked issue keys (the same regex-based extraction Collect already does)
  before fetching — this ADR doesn't change how keys are discovered, only how
  the resulting issues are supplied to `kc`.

## Open Questions

- Whether the cache file's schema should eventually be validated more
  strictly (e.g. reject unknown fields) — not needed yet at this file's
  current, single-agent-authored scale.
- Whether a future non-interactive Atlassian service-account credential would
  make `source = "file"` unnecessary for CI-adjacent use cases — not pursued
  here; `source = "rest"` already covers that if/when such a credential
  exists.

## Impact

Affected documents: `pipeline.md` §3.1, `CLAUDE.md`, `kc.toml`,
`docs/kc-cli-reference.md`, `CHANGELOG.md`.
Affected compiler stages: Collect only (`collectors/jira.py`) — no
Extract/Normalize/Persist/Emit changes.

## Alternatives Rejected

- **Give `kc` its own MCP client (A)** — doesn't solve the actual credential
  problem; the connector's grant belongs to the interactive session, not to a
  subprocess it spawns, regardless of what client library that subprocess
  embeds.

## Future Reconsideration

Revisit if a non-interactive Atlassian service-account credential becomes
available — at that point `source = "rest"` (pointed at that credential) may
make `source = "file"` redundant for automated use, though it would remain
useful for one-off/offline compiles regardless.

## References

- [ADR-002](ADR-002-ci-trigger.md) — the unattended-compile invariant `source
  = "file"` cannot satisfy
- [ADR-007](ADR-007-plugin-architecture.md) — explicit activation, fail-loud
  policy, plugin-seam precedent
- `collectors/jira.py` — `JiraGateway` Protocol, `AtlassianJiraGateway`,
  `FileJiraGateway`, `build_jira_gateway`
- `docs/pipeline.md` §3.1 — Collect stage contract

## Self-Review

- **Truly architectural?** Yes — it decides where Jira gateway selection lives
  (an explicit, fail-loud `kc.toml` key) and sets an invariant (the file
  backend is interactive-only, never CI) that constrains any future backend
  added the same way.
- **Already made?** No — this is the first decision giving the Jira collector
  more than one gateway.
- **Reversible?** Two-way door — `source` defaults safely, and a third
  backend could be added the same way without touching the other two.
- **Dependent future documents:** none new; updates `pipeline.md`,
  `kc-cli-reference.md` additively.
- **Exposes unresolved decisions:** whether a non-interactive Atlassian
  service-account credential is worth provisioning later (tracked above, not
  newly introduced by this ADR).
