"""Embeddings emitter (ADR-005): post-Persist stage, delta-driven like the wiki.

Embeds dirty entities plus any rows left `pending` by earlier outages. Unchanged
embedded text (content hash) is skipped — recompiles cost nothing. A provider
failure marks the affected rows `pending` and degrades: search falls back to FTS.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_compiler.ir import content_hash
from knowledge_compiler.llm.embeddings import embedding_text
from knowledge_compiler.llm.provider import LLMProviderError
from knowledge_compiler.storage.schema import EmbeddingRow, EntityRow

_BATCH = 64


def emit_embeddings(session: Session, repo_id: int, embedder, dirty_slugs: set[str],
                    on_progress: Callable[[int, int, str], None] | None = None,
                    ) -> tuple[int, list[str]]:
    """Returns (embedded_count, warnings). Commits via the caller's session.
    on_progress: called (entities_done, total, "embeddings") after each batch."""
    rows = {r.entity_id: r for r in session.execute(
        select(EmbeddingRow).where(EmbeddingRow.repo_id == repo_id,
                                   EmbeddingRow.model_id == embedder.model_id)).scalars()}
    entities = session.execute(select(EntityRow).where(EntityRow.repo_id == repo_id)).scalars().all()

    todo: list[tuple[EntityRow, str, str]] = []  # (entity, text, text_hash)
    for entity in sorted(entities, key=lambda e: e.slug):
        existing = rows.get(entity.id)
        pending = existing is not None and existing.status == "pending"
        if entity.slug not in dirty_slugs and not pending and existing is not None:
            continue
        text = embedding_text(entity.name, entity.entity_type, entity.payload)
        text_hash = content_hash({"text": text})
        if existing is not None and existing.status == "current" and existing.content_hash == text_hash:
            continue  # unchanged: recompile costs nothing
        todo.append((entity, text, text_hash))

    embedded = 0
    for start in range(0, len(todo), _BATCH):
        batch = todo[start:start + _BATCH]
        try:
            vectors = embedder.embed([text for _, text, _ in batch])
        except LLMProviderError as exc:
            for entity, _, text_hash in todo[start:]:
                _upsert(session, rows, repo_id, embedder.model_id, entity, text_hash,
                        vector=None, status="pending")
            session.commit()
            return embedded, [f"embedding provider failed — {len(todo) - start} entities "
                              f"pending, search degrades to FTS: {exc}"]
        for (entity, _, text_hash), vector in zip(batch, vectors):
            _upsert(session, rows, repo_id, embedder.model_id, entity, text_hash,
                    vector=vector, status="current")
            embedded += 1
        if on_progress:
            on_progress(min(start + _BATCH, len(todo)), len(todo), "embeddings")
    session.commit()
    return embedded, []


def _upsert(session: Session, rows: dict, repo_id: int, model_id: str,
            entity: EntityRow, text_hash: str, vector, status: str) -> None:
    row = rows.get(entity.id)
    if row is None:
        row = EmbeddingRow(entity_id=entity.id, model_id=model_id, repo_id=repo_id,
                           vector=vector, content_hash=text_hash, status=status)
        session.add(row)
        rows[entity.id] = row
    else:
        row.vector = vector
        row.content_hash = text_hash
        row.status = status
