# ADR-022 — External-key inline annotations (`kc:external-key:`)

**Status:** Accepted  
**Date:** 2026-08-10  
**Deciders:** Knowledge Compiler team  

---

## Context

LLM-derived entities (features, business rules, risks) use a three-rule identity cascade
(ADR-004): external key → anchor overlap → name similarity. Rules 2 and 3 are evidence-based
and tolerant of ordinary refactors, but they can both fail when:

1. **LLM stochasticity on cache miss** — a file changes, the cache entry is invalidated, the
   live LLM re-extracts and does not emit an entity it emitted previously. The entity's old
   wiki page is orphaned and pruned (`_prune_orphaned_pages`).

2. **Anchor drift** — the LLM anchors the same concept to a different call site across
   re-extractions (e.g. the CLI entry point `cli.py::compile_cmd` instead of the
   implementation `run.py::compile_full`). The anchor overlap with the stored entity scores 0
   and name similarity falls below `T_name = 0.8`, so a new slug is minted and the old page
   is pruned as orphaned.

Both failure modes were observed in KC's self-compile (run 1807 → run 1919) when
`schema.py` changed, causing cache misses across several files. Five business rules and four
features were false-deleted from the wiki.

The root cause is that the LLM cannot guarantee stable anchor selection — it is a
probabilistic function of the file content at call time. The cascade's Rule 1
(`external_key`) is immune to both problems because it is a deterministic lookup, but it
was only used for Jira stories and PR entities (natural-key types), never for LLM-derived
entities. There was no mechanism to set `external_key` on a feature or business rule.

## Decision

Add a source-code annotation syntax:

```python
# kc:external-key: <key>
def compile_full(ctx: dict) -> CompileRun:
    ...
```

The annotation is parsed deterministically from the source file before the LLM is called.
When the LLM output includes a symbol among the annotated function's symbol paths, the
external key is injected into the fact payload **post-validation** (so the LLM cache key
is unaffected and the LLM never sees or assigns identity).

Normalize's existing Rule 1 cascade step matches the injected `external_key` against the
persisted entity's `payload["external_key"]` — no changes required to the cascade.

### Scope

- V1 supports Python `def` and `class` annotations only (the failure modes observed were
  all in Python).
- The annotation must appear on the line(s) immediately before the `def` or `class`
  statement (blank lines skipped). A non-blank, non-`kc:` line between annotation and
  target cancels the pending annotation.
- The external key namespace is flat and global within a repo. Choose keys that describe
  the concept, not the implementation (`compile-full`, not `run.compile_full`).
- Only architecturally stable entry points and long-lived invariants should be annotated.
  Annotating everything defeats the purpose — the LLM is good at tracking moving targets
  via anchor overlap; `external_key` is for concepts that must survive wholesale rewrites.

### What is NOT changed

- The LLM prompt and cache key are unchanged — annotations do not cause extra LLM calls.
- The cascade rules 2 and 3 are unchanged — unannotated entities continue to use anchor
  overlap and name similarity.
- The `ExtractionOut` schema is unchanged — the LLM never sees the external key.

## Consequences

**Good:**
- Architecturally stable features and rules survive file content changes, LLM stochasticity,
  and anchor drift without any identity churn or wiki-page deletions.
- Zero cost when the cache is warm — injection happens in `_to_facts`, which runs on both
  cache hits and live LLM calls.
- No new LLM budget impact.

**Neutral:**
- Developers must manually annotate the concepts they want pinned. This is intentional:
  over-annotating would bypass the cascade for entities that benefit from LLM anchor
  tracking (concepts that move across files, get renamed, or split).

**Risk:**
- A key collision (two functions in the same repo share a `kc:external-key:` value) merges
  two distinct entities into one. Normalize's co-match conflict detection will surface this
  as a conflict record. Guidance: keys should be concept-level nouns, not symbol names.

## Implementation

- `knowledge_compiler/extractors/annotation_parser.py` — `parse_external_keys(source)`
  returns `{local_name: external_key}`.
- `knowledge_compiler/extractors/llm_extractor.py` — `LLMSemanticExtractor` accepts
  `known_annotations: dict[str, dict[str, str]]`; injects `external_key` in `_to_facts`.
- `knowledge_compiler/compiler/run.py` — `_extract_semantic` pre-computes annotations from
  artifact source text and passes them to the extractor.

## References

- [ADR-004](ADR-004-entity-identity.md) — identity cascade; Rule 1 external key
- [ADR-008](ADR-008-llm-extraction-contract.md) — LLM cache key contract
- [normalize.md](../normalize.md) §5.2 — cascade pseudocode
