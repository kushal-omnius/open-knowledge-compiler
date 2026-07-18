# ADR-010: Wiki Publishing Destination — Dedicated Branch in the Compiled Repository

## Status

Accepted

## Date

2026-07-18

## Context

The wiki is the V1 wedge, and success criterion 2 ("engineers voluntarily use the wiki") is unevaluable until the wiki has a *home*. Architecture.md §11 deliberately deferred this; pipeline.md §8 shows it blocking the publisher plugin contract; the dogfood milestone gives it a deadline. The dogfood context is now known: the repository lives on **GitHub**, and the team wants to read the wiki **in a git repository** (forge-rendered Markdown), not on a separate hosting surface.

Within "a git repo," the real design questions are *which* git location — same repo or separate, branch or default-branch tree, or GitHub's native Wiki tab — and how publishing avoids re-triggering compilation (ADR-002's CI trigger fires on merges to the default branch; a publisher that creates such merges would loop).

## Decision Drivers

- Accuracy of the success-criterion test — the wiki must be where engineers already look, with zero extra accounts/hosting
- Simplicity — no new infrastructure (vision DP 4); publishing is one step in Emit
- Determinism — publishing must never re-trigger compilation (loop safety)
- Maintainability — wiki structure must survive the slug scheme (`entity_type/key` implies directories)
- Extensibility — destination is a publisher plugin (pipeline.md §3.6); this ADR picks the *reference* destination, not the only one

## Considered Options

### Option A — Dedicated wiki branch in the compiled repository

Emit commits generated Markdown to a dedicated branch (`knowledge/wiki`), read via GitHub's branch file browsing; `index.md` at the branch root is the entry point.

**Pros**

- Wiki lives *with* the code: same repo, same access control, zero setup, discoverable from the repo page.
- Directory structure preserved — slugs (`component/billing-invoice.md`) map directly to paths; GitHub renders Markdown and relative links natively.
- **Loop-safe by construction:** ADR-002's trigger is "PR merged to default branch"; a push to a non-default branch is not that event. No `[skip ci]` fragility required (though the publisher sets it anyway, defense in depth).
- Wiki state is versioned and diffable — though the delta log remains the authoritative history.

**Cons**

- Branch file browsing is plainer than a docs site: no search box, basic navigation (mitigated: generated index pages per directory; the wiki is *also* queryable via MCP at milestone 2).
- An orphan branch in the repo is mildly unconventional.

### Option B — `docs/wiki/` tree in the default branch

**Pros**

- Most discoverable location of all.

**Cons**

- **Publishing requires merging to the default branch — the exact event that triggers compilation.** The compiler would compile its own wiki commits (loop, or fragile `[skip ci]`-dependent suppression of the core trigger), and wiki updates would spam the PR flow or require direct pushes to a protected branch. Structurally wrong.

### Option C — Separate dedicated wiki repository

**Pros**

- Total separation; no trigger interaction at all; independent access control possible.

**Cons**

- "Another repo to find" — weakens the voluntary-use test during dogfood; cross-repo permissions and clone setup are real friction.
- Multi-repo future actually *favors* per-repo co-located wikis over a growing constellation of shadow repos.

### Option D — GitHub Wiki tab (`.wiki.git`)

**Pros**

- Native, discoverable tab; it is a pushable git repo.

**Cons**

- **Flat page namespace — no directories.** The slug scheme (`entity_type/key`) would have to be mangled into flat filenames, breaking ir.md's slug-as-filename/anchor design for the one destination that can't represent it.
- No PRs/review on wiki pushes; GitHub-specific in a way branches are not (GitLab/Bitbucket equivalents differ more than their branch rendering does).

### Option E — Static site (MkDocs → GitHub Pages)

**Pros**

- Best reading experience: search, navigation, theming.

**Cons**

- A build-and-hosting step (Pages config, site build in Emit) before the wedge has proven anything; contradicts "read it in a git repo" for dogfood. Correct as a *second* publisher plugin once the wiki content earns it — deferred, not rejected.

## Decision

**Option A — a dedicated `knowledge/wiki` branch in the compiled repository**, written by the reference GitHub publisher plugin.

Publishing mechanics (normative):

- One commit per compile run, message carrying the compile-run reference and source PR (provenance in git form); `[skip ci]` set as defense in depth.
- The branch is **publisher-owned**: hand edits are overwritten without ceremony (vision DP 5 — the wiki is a build artifact; if it's wrong, fix the compiler). The publisher may force-reset the branch to regenerated state; wiki git history is a convenience, the delta log is the history of record (ADR-003).
- Directory layout mirrors slugs; generated `index.md` per directory plus a root index and recent-changes page (from the delta log).

Why A over the alternatives: B creates a trigger loop at the architecture's most sensitive joint; C taxes the voluntary-use test with a second repo; D's flat namespace breaks the slug design; E buys polish the wedge hasn't earned. A is the only option that is simultaneously zero-infrastructure, loop-safe *by construction*, and faithful to the slug scheme.

## Architectural Invariants

- Publishing never produces the event that triggers compilation (loop safety) — for this publisher, guaranteed structurally (non-default branch), not by convention.
- The wiki destination is publisher-owned and fully regenerable; no un-recompilable state may live there.
- Destination choice stays behind the publisher plugin interface; adding destinations (static site, Confluence) is additive and requires no new ADR unless one becomes the new *reference* destination.

## Consequences

### Positive

- Success criterion 2 becomes testable at dogfood with zero new infrastructure or accounts.
- The publisher plugin contract (pipeline.md §8 blocker) is now implementable: input = dirty pages + delta, output = one branch commit.
- Loop safety needs no CI-config discipline from adopters.

### Negative

- Reading experience is capped at forge Markdown rendering until a static-site publisher earns its place.
- Non-GitHub forges need their own reference publisher eventually (branch mechanics port trivially; only the reference implementation is GitHub-first).

### Tradeoffs Accepted

- **Polish traded for zero setup** during the exact phase where friction would falsify the voluntary-use signal.
- Wiki git history is redundant with the delta log and may be truncated by force-resets — accepted; one history of record is the point.

## Failure Modes

| Failure | Effect | Handling |
|---|---|---|
| Hand edits to the wiki branch | Lost on next publish | By design (DP 5); the root index states it prominently; real fixes go to source or compiler |
| Branch protection rules block the publisher | Publish fails after Persist | Emit failure is non-rolling-back and re-runnable (pipeline.md §3.6); setup docs flag the required permission |
| Very large wikis (repo-size growth from wiki blobs) | Repo bloat over many compiles | Force-reset publishing keeps the branch shallow; blob dedup across regenerated identical pages is inherent (content-identical files) |
| Engineers don't find the branch | Voluntary-use signal falsely negative | Root README badge/link to the wiki branch — part of the reference publisher's setup step |

## Assumptions

- Dogfood engineers accept branch-browsing UX for the evaluation period (the point of dogfooding is to test exactly this).
- The CI trigger remains scoped to default-branch PR merges (ADR-002) — the loop-safety argument depends on it.
- Repo access control is the right wiki access control during dogfood.

## Open Questions

- Static-site publisher (Option E) trigger condition: adopt if dogfood feedback names navigation/search as the blocking complaint — post-dogfood, evidence-driven.
- Whether the recent-changes page paginates or windows the delta log — implementation detail of the wiki emitter.

## Impact

Affected documents: architecture.md §11 (destination now decided), pipeline.md §8 (publisher contract unblocked).
Affected compiler stages: **Emit** (reference GitHub branch publisher).

## Alternatives Rejected

- **Default-branch tree (B)** — trigger loop at the core; structurally unsound.
- **Separate repo (C)** — friction taxes the exact signal being measured.
- **GitHub Wiki tab (D)** — flat namespace breaks the slug design.
- **Static site (E)** — deferred as a future publisher plugin, not rejected on merit.

## Future Reconsideration

Revisit when dogfood feedback demands a better reading surface (static-site publisher as the new reference), when non-GitHub reference support becomes a priority, or if the multi-repo story wants an aggregated cross-repo wiki (which would reopen destination architecture, not just plugins).

## Terminology Note (additive, 2026-07-18)

The "publisher" named throughout this ADR is one instance of the generalized **Publisher** concept defined in pipeline.md §3.6: Emit produces *publications* (the wiki, as OKF-conformant Markdown), and a Publisher ships a publication to a destination — GitHub branch (this ADR's reference), GitHub Pages, a separate knowledge repo, Confluence, or an OKF bundle export. This note generalizes naming only; the decision (reference destination = `knowledge/wiki` branch) and all invariants are unchanged. The canonical home of compiled knowledge remains the database (ADR-001); publications are renders.

## References

- `docs/vision.md` — success criterion 2; Design Principles 4, 5
- `docs/architecture.md` — §11 (A8); [ADR-002](ADR-002-ci-trigger.md) — trigger scoping the loop-safety argument relies on; [ADR-003](ADR-003-current-state-delta-log.md) — delta log as history of record; `docs/pipeline.md` §3.6, §8

## Self-Review

- **Truly architectural?** Yes — it fixes where the V1 product surface lives and adds the loop-safety invariant that constrains every future publisher.
- **Already made?** No — this was the one explicitly undecided item; decided now with the dogfood context (GitHub, git-repo reading) it was waiting for.
- **Reversible?** Two-way — publisher plugins make destination migration additive; only the loop-safety invariant is permanent.
- **Dependent future documents:** none new — it unblocks pipeline.md §8's publisher contract and closes architecture.md §11's deferral.
- **Exposes unresolved decisions:** static-site adoption trigger (recorded above); nothing requiring a new ADR now.
