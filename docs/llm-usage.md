# LLM-USAGE

**This Example uses AZURE OPENAI Deployments. Two separate LLM deployments are needed, of two different model families.**, because a chat model and an embedding model do fundamentally different jobs and Azure treats them as distinct deployments even on the same resource.

| Purpose | Config section | Model family needed | Reads env var |
|---|---|---|---|
| Semantic extraction (business rules / features / risks) | `[llm]` | A **chat/completion GPT model** with Structured Outputs support — `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, or `gpt-4.1-mini` | `OPENAI_AZURE_DEPLOYMENT` |
| Embeddings (semantic search vectors) | `[embeddings]` | A dedicated **embedding model** — `text-embedding-3-small` or `text-embedding-3-large` | `OPENAI_AZURE_EMBEDDING_DEPLOYMENT` ([embeddings.py:60](../knowledge_compiler/llm/embeddings.py)) — separate variable from the chat deployment |

You **can't** use a GPT chat model for embeddings, and you can't use an embedding model for extraction — they're architecturally different (embeddings produce a fixed-size vector, not text/JSON). Azure OpenAI requires you to deploy each as its own named deployment even under the same resource/endpoint.

**So in your Azure OpenAI resource, deploy two things:**
1. `gpt-4o-mini` (or `gpt-4o`) → note its deployment name → `OPENAI_AZURE_DEPLOYMENT`
2. `text-embedding-3-small` (or `-large` for higher quality/dimensionality, at higher cost) → note its deployment name → `OPENAI_AZURE_EMBEDDING_DEPLOYMENT`

Both deployments share the same `OPENAI_AZURE_ENDPOINT` and `OPENAI_AZURE_API_KEY` — only the deployment-name env var differs per purpose.

```bash
# .env
OPENAI_AZURE_ENDPOINT=https://<your-resource>.openai.azure.com
OPENAI_AZURE_API_KEY=<key>
OPENAI_AZURE_DEPLOYMENT=gpt-4o-mini              # [llm] extraction
OPENAI_AZURE_EMBEDDING_DEPLOYMENT=text-embedding-3-small   # [embeddings]
```

And in each repo's `kc.toml`:
```toml
[llm]
enabled = true
provider = "azure-openai"

[embeddings]
enabled = true
provider = "azure-openai"
```

`text-embedding-3-small` is the cheaper/lower-dimensional option (1536 dims) and is plenty for this scale (1626 entities across both repos); `-large` (3072 dims) only matters if you later need finer semantic discrimination at much larger corpus size.

## Empirical model comparison (repoAdogfood, 2026-09-15)

`gpt-4o-mini`'s extraction was spot-checked against real repoAsource (business_rule/risk entities from a live compile) and found a repeated failure shape: it reliably caught the *obvious* rule/risk in a file but missed or mischaracterized the *nuanced, counter-intuitive* one next to it. Three confirmed gaps drove this comparison:

1. **Missed rule**: a co-insurance skill file (`backend/tools/skills/co_insurance_proportionality_rules.py`) states shares *not* summing to 100% is valid and must never be normalized — `gpt-4o-mini` extracted a different rule from the same file under a slug named after this one, never surfacing the actual content.
2. **False positive**: flagged a routine, correct API-key-in-header auth pattern (`backend/integrations/ccc/ccc_policy_claims_client.py:_get_headers`) as a "security risk."
3. **Inverted risk**: described `frontend/src/hooks/useAnalysisCardResult.ts`'s claim-switch guard as if its *absence* were the actual behavior — the guard clause that prevents writing stale results onto the wrong claim was described as if results get silently discarded and confuse users.

### Method

`gpt-4.1-mini` and `gpt-4.1` were run against the same 6 files, same prompt/schema the compiler itself uses (`llm/templates.py`), via a throwaway comparison script (not shipped — reconstructable from `build_prompt`/`SCHEMA` + `AzureOpenAIProvider` against any deployment name). `gpt-4.1-nano` was skipped — it's a smaller/cheaper tier than `gpt-4o-mini` itself, already the source of the gaps, so unlikely to help. `gpt-4o` and a matched `gpt-4o-mini` re-run were both blocked (`DeploymentNotFound` — not deployed on the Azure resource used for this test), so this comparison covers `gpt-4.1-mini`/`gpt-4.1` only; `gpt-4o` remains untested.

### Results

| Known `gpt-4o-mini` gap | `gpt-4.1-mini` | `gpt-4.1` |
|---|---|---|
| Missed "shares not summing to 100%" rule | **Fixed** — extracted explicitly, plus all 6 other rules in the file | **Fixed** — same, plus richer coverage of the same file |
| False-positive API-key "security risk" | **Fixed** — not flagged; reasonable missing-error-handling note instead | **Fixed** — extracted nothing for that snippet (most conservative, arguably most correct — nothing genuinely risky there) |
| Inverted claim-switch risk | **Improved** — correctly scoped to the pre-flush window, doesn't claim results are discarded, but doesn't name the mitigation | **Fixed** — correctly modeled the guard as a business rule ("autosave only if claim unchanged") *and* separately flagged the `AUTOSAVE_ENABLED=false` risk window — no other model (including `gpt-4.1-mini`) caught that config-gated nuance |

One caveat surfaced along the way: `gpt-4.1-mini` misattributed a real, correctly-worded rule (`backend/auth_util.py`'s admin/role check) to the wrong function's anchor — but this traced back to a test-script setup gap (only one of the file's two functions was supplied as a valid `symbol_paths` target), not a model flaw; `gpt-4.1` stayed disciplined under the same constraint and didn't force the misattribution.

### Cost (this test only — 6 files, same prompts, so input tokens are identical across models)

| Model | Input tokens | Output tokens | Cost |
|---|---|---|---|
| `gpt-4.1-mini` | 10,563 | 1,898 | $0.00726 |
| `gpt-4.1` | 10,563 | 2,026 | $0.03733 (5.1x `gpt-4.1-mini`) |

For the full repoArepo at reconcile scale (~691 historical PR/commit compiles), the same per-token rates extrapolate to roughly: `gpt-4o-mini` ≈$3.33, `gpt-4.1-mini` ≈$8.84, `gpt-4.1` ≈$44, `gpt-4o` ≈$55 (`gpt-4o` cost is a re-pricing of `gpt-4o-mini`'s real token volumes at `gpt-4o`'s rate, not a real `gpt-4o` run — it was never deployed). At this repo's scale, cost was not the deciding factor for any of these models; extraction quality was.

### Conclusion

`gpt-4.1` fixed all three confirmed gaps outright and found a nuance (`AUTOSAVE_ENABLED`) nothing else caught; `gpt-4.1-mini` fixed two of three and meaningfully improved the third, at roughly a fifth of `gpt-4.1`'s cost for this workload. `gpt-4o` remains untested (no deployment available). For a repo where domain nuance carries real stakes (RepoA's claims/insurance logic), `gpt-4.1` is the recommended `[llm] model` override; `gpt-4.1-mini` is a reasonable middle ground if cost sensitivity increases. `gpt-4o-mini` (the previous default) is not recommended for compliance-adjacent extraction given the confirmed gaps above, though it remains adequate for repos without that kind of domain nuance at stake.