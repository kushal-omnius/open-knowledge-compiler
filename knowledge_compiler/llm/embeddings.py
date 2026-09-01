"""Embedding providers (ADR-005): one thin interface — embed(texts) -> vectors.

Same pattern as llm/provider.py: providers selected by explicit kc.toml config
([embeddings] provider = ...), credentials strictly from the environment, and a
deterministic fake for tests/offline. The compiler never requires embeddings —
retrieval degrades to FTS when they're absent or pending.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from knowledge_compiler.llm.provider import LLMProviderError


class OpenAIEmbedder:
    """OPENAI_API_KEY from environment; pip install 'open-knowledge-compiler[llm-openai]'."""

    DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        try:
            import openai
        except ImportError as exc:
            raise LLMProviderError(
                "openai SDK not installed — pip install 'open-knowledge-compiler[llm-openai]'") from exc
        self.model_id = model_id
        self._openai = openai
        self.last_usage: dict[str, int] | None = None  # set by embed(): {"input_tokens"}
        try:
            self._client = openai.OpenAI()
        except openai.OpenAIError as exc:
            raise LLMProviderError(f"openai client init failed: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.embeddings.create(model=self.model_id, input=texts)
        except self._openai.OpenAIError as exc:
            raise LLMProviderError(f"openai embeddings error: {exc}") from exc
        usage = getattr(response, "usage", None)
        self.last_usage = {"input_tokens": getattr(usage, "prompt_tokens", 0) or 0}
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]


class AzureOpenAIEmbedder(OpenAIEmbedder):
    """Azure deployment for embeddings. Env: OPENAI_AZURE_ENDPOINT / OPENAI_AZURE_API_KEY,
    deployment from [embeddings] model or OPENAI_AZURE_EMBEDDING_DEPLOYMENT."""

    def __init__(self, model_id: str | None = None) -> None:
        import os

        try:
            import openai
        except ImportError as exc:
            raise LLMProviderError(
                "openai SDK not installed — pip install 'open-knowledge-compiler[llm-openai]'") from exc

        endpoint = os.environ.get("OPENAI_AZURE_ENDPOINT") or os.environ.get("AZURE_OPENAI_ENDPOINT")
        api_key = os.environ.get("OPENAI_AZURE_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")
        deployment = model_id or os.environ.get("OPENAI_AZURE_EMBEDDING_DEPLOYMENT")
        if not (endpoint and api_key and deployment):
            raise LLMProviderError(
                "azure-openai embeddings need OPENAI_AZURE_ENDPOINT, OPENAI_AZURE_API_KEY and a "
                "deployment ([embeddings] model or OPENAI_AZURE_EMBEDDING_DEPLOYMENT)")
        self.model_id = deployment
        self._openai = openai
        self.last_usage: dict[str, int] | None = None
        try:
            self._client = openai.AzureOpenAI(
                azure_endpoint=endpoint, api_key=api_key,
                api_version=os.environ.get("KC_AZURE_OPENAI_API_VERSION", "2024-10-21"))
        except openai.OpenAIError as exc:
            raise LLMProviderError(f"azure-openai client init failed: {exc}") from exc


@dataclass
class FakeEmbedder:
    """Deterministic 8-dim vectors from content hashes — tests/offline only.
    Similar texts do NOT get similar vectors; tests assert plumbing, not geometry."""

    model_id: str = "fake-embed"
    dim: int = 8
    calls: int = 0  # texts embedded, for cache/skip assertions in tests
    last_usage: dict[str, int] | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        self.last_usage = {"input_tokens": sum(len(t) // 4 for t in texts)}
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [b / 255.0 for b in digest[: self.dim]]
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            vectors.append([x / norm for x in raw])
        return vectors


def build_embedder(embeddings_config: dict[str, Any]):
    """Factory (ADR-007): explicit config selection, mirroring build_provider."""
    name = embeddings_config.get("provider", "openai")
    model = embeddings_config.get("model")
    if name == "openai":
        return OpenAIEmbedder(model_id=model or OpenAIEmbedder.DEFAULT_MODEL)
    if name == "azure-openai":
        return AzureOpenAIEmbedder(model_id=model)
    raise LLMProviderError(
        f"unknown embeddings provider '{name}' (supported: openai, azure-openai)")


def embedding_text(name: str, entity_type: str, payload: dict) -> str:
    """The text an entity embeds as — deterministic (sorted payload rendering)."""
    parts = [entity_type.replace("_", " "), name]
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)
