"""Stage plugin interfaces (ADR-007): one Protocol per stage.

Discovery: entry points under the groups below. Activation: explicit kc.toml only —
installing a package never changes compilation output (ADR-007 invariant).
Signatures are provisional until the built-in plugins land (see v1.0 freeze note).
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from knowledge_compiler.ir import Artifact, Delta, Entity, Fact, Relationship

# Entry-point groups (ADR-007). Built-ins and third parties use the same groups.
EP_COLLECTORS = "knowledge_compiler.collectors"
EP_EXTRACTORS = "knowledge_compiler.extractors"
EP_ANALYZERS = "knowledge_compiler.analyzers"
EP_EMITTERS = "knowledge_compiler.emitters"
EP_PUBLISHERS = "knowledge_compiler.publishers"
EP_RETRIEVAL = "knowledge_compiler.retrieval"
EP_LLM_PROVIDERS = "knowledge_compiler.llm_providers"

INTERFACE_VERSION = "0.1"  # plugins declare compatibility; mismatch fails at activation


@runtime_checkable
class Collector(Protocol):
    """Fetch raw artifacts for the compile scope (pipeline.md §3.1)."""

    def collect(self, scope: Any) -> Iterable[Artifact]: ...


@runtime_checkable
class Extractor(Protocol):
    """Turn artifacts into Fact IR (pipeline.md §3.2). Never emits entities (ADR-009)."""

    def extract(self, artifacts: Iterable[Artifact]) -> Iterable[Fact]: ...


@runtime_checkable
class LanguageAnalyzer(Protocol):
    """Deterministic source analysis via tree-sitter (ADR-006). A specialized Extractor."""

    def analyze(self, files: Iterable[str]) -> Iterable[Fact]: ...


@runtime_checkable
class Emitter(Protocol):
    """Render publications/embeddings from Knowledge IR only (pipeline.md §3.6)."""

    def emit(self, dirty: Iterable[Entity], relationships: Iterable[Relationship], delta: Delta) -> None: ...


@runtime_checkable
class Publisher(Protocol):
    """Ship a publication to a destination (pipeline.md §3.6, ADR-010)."""

    def publish(self, publication_dir: str) -> None: ...


@runtime_checkable
class RetrievalProvider(Protocol):
    """Search over compiled knowledge (architecture.md §7)."""

    def search(self, query: str, filters: dict[str, Any]) -> list[Entity]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Schema-validated completion (ADR-008). The only LLM access path."""

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...
