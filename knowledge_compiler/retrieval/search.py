"""Retrieval (architecture.md §7): keyword FTS + semantic KNN + hybrid RRF.

Both branches share the same SQL predicates (repo, entity_type); fusion is
reciprocal-rank (k=60). Semantic search runs only when a `current` embedding
generation exists for the query's embedder — otherwise hybrid degrades to
FTS-only, silently correct (ADR-005 degraded mode).

Every result carries provenance-grade fields (slug, anchors, payload) — an
answer that can't say where it came from doesn't ship (vision DP 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from knowledge_compiler.storage.schema import EmbeddingRow, EntityRow

_RRF_K = 60


@dataclass(frozen=True)
class SearchResult:
    slug: str
    entity_type: str
    name: str
    score: float
    payload: dict
    anchors: list = field(default_factory=list)


def _base(repo_id: int, entity_types: list[str] | None) -> Select:
    stmt = select(EntityRow).where(EntityRow.repo_id == repo_id,
                                   EntityRow.entity_type != "wiki_page")
    if entity_types:
        stmt = stmt.where(EntityRow.entity_type.in_(entity_types))
    return stmt


def keyword_search(session: Session, repo_id: int, query: str,
                   entity_types: list[str] | None = None, limit: int = 10) -> list[SearchResult]:
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(EntityRow.search_vector, tsquery)
    stmt = (_base(repo_id, entity_types)
            .where(EntityRow.search_vector.op("@@")(tsquery))
            .order_by(rank.desc(), EntityRow.slug).limit(limit)
            .add_columns(rank))
    return [_result(row, float(score)) for row, score in session.execute(stmt)]


def semantic_search(session: Session, repo_id: int, query_vector: list[float], model_id: str,
                    entity_types: list[str] | None = None, limit: int = 10) -> list[SearchResult]:
    distance = EmbeddingRow.vector.cosine_distance(query_vector)
    stmt = (_base(repo_id, entity_types)
            .join(EmbeddingRow, EmbeddingRow.entity_id == EntityRow.id)
            .where(EmbeddingRow.model_id == model_id, EmbeddingRow.status == "current")
            .order_by(distance, EntityRow.slug).limit(limit)
            .add_columns(distance))
    return [_result(row, 1.0 - float(dist)) for row, dist in session.execute(stmt)]


def search(session: Session, repo_id: int, query: str, embedder=None,
           entity_types: list[str] | None = None, limit: int = 10) -> list[SearchResult]:
    """The facade: hybrid when a current embedding generation exists, else FTS."""
    kw = keyword_search(session, repo_id, query, entity_types, limit=limit * 2)

    sem: list[SearchResult] = []
    if embedder is not None:
        has_generation = session.execute(
            select(EmbeddingRow.entity_id).where(
                EmbeddingRow.repo_id == repo_id,
                EmbeddingRow.model_id == embedder.model_id,
                EmbeddingRow.status == "current").limit(1)).scalar_one_or_none()
        if has_generation is not None:
            sem = semantic_search(session, repo_id, embedder.embed([query])[0],
                                  embedder.model_id, entity_types, limit=limit * 2)
    if not sem:
        return kw[:limit]

    # reciprocal-rank fusion: robust to the two branches' incomparable scores
    fused: dict[str, float] = {}
    results: dict[str, SearchResult] = {}
    for branch in (kw, sem):
        for rank, result in enumerate(branch):
            fused[result.slug] = fused.get(result.slug, 0.0) + 1.0 / (_RRF_K + rank + 1)
            results.setdefault(result.slug, result)
    ordered = sorted(fused, key=lambda s: (-fused[s], s))[:limit]
    return [SearchResult(slug=s, entity_type=results[s].entity_type, name=results[s].name,
                         score=fused[s], payload=results[s].payload, anchors=results[s].anchors)
            for s in ordered]


def _result(row: EntityRow, score: float) -> SearchResult:
    return SearchResult(slug=row.slug, entity_type=row.entity_type, name=row.name,
                        score=score, payload=row.payload, anchors=row.anchors or [])
