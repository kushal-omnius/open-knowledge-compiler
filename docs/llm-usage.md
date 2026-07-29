# LLM-USAGE

**This Example uses AZURE OPENAI Deployments. Two separate LLM deployments are needed, of two different model families.**, because a chat model and an embedding model do fundamentally different jobs and Azure treats them as distinct deployments even on the same resource.

| Purpose | Config section | Model family needed | Reads env var |
|---|---|---|---|
| Semantic extraction (business rules / features / risks) | `[llm]` | A **chat/completion GPT model** with Structured Outputs support — `gpt-4o` or `gpt-4o-mini` | `OPENAI_AZURE_DEPLOYMENT` |
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