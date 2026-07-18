"""Phase-1 schema: the deterministic compiler's tables (docs/data-model.md §2).

Deferred to their own phases (additive migrations, per data-model.md):
  embeddings (semantic layer, ADR-005) · llm_cache (LLM layer, ADR-008).

entities.search_vector (generated tsvector) and the append-only grants on
delta tables are raw SQL in the migration — they have no portable ORM form.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    forge_ref: Mapped[str] = mapped_column(Text)          # e.g. github.com/org/repo
    default_branch: Mapped[str] = mapped_column(Text)
    config_ref: Mapped[str] = mapped_column(Text)         # where kc.toml was read from


class CompileRun(Base):
    __tablename__ = "compile_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    scope: Mapped[str] = mapped_column(Text)              # 'full' | 'pr'
    pr_number: Mapped[int | None] = mapped_column(Integer)
    commit_sha: Mapped[str] = mapped_column(Text)
    merged_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))  # reconcile watermark (ADR-002)
    status: Mapped[str] = mapped_column(Text)             # 'running' | 'succeeded' | 'failed'
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)  # --no-llm (pipeline.md §6.1)
    fact_vocabulary_version: Mapped[str] = mapped_column(Text)
    knowledge_model_version: Mapped[str] = mapped_column(Text)
    started_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Idempotence check (ADR-002): succeeded run per (repo, pr) => re-trigger is a no-op.
        Index("ix_compile_runs_repo_pr", "repo_id", "pr_number"),
        Index("ix_compile_runs_repo_merged", "repo_id", "merged_at"),
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))
    artifact_type: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)     # staged; prunable (data-model.md §4)
    collected_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FactRow(Base):
    __tablename__ = "facts"  # staging, prunable; provenance snapshots survive pruning

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))
    fact_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    extraction: Mapped[dict] = mapped_column(JSONB)
    artifact_ids: Mapped[list] = mapped_column(JSONB)
    anchors: Mapped[list] = mapped_column(JSONB, default=list)


class EntityRow(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    slug: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    anchors: Mapped[list] = mapped_column(JSONB, default=list)  # anchor currency (ir.md §2.2)
    # search_vector: generated tsvector column added in the migration (raw SQL)
    first_compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))
    last_compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))

    __table_args__ = (
        UniqueConstraint("repo_id", "slug", name="uq_entities_repo_slug"),
        Index("ix_entities_repo_type", "repo_id", "entity_type"),
    )


class RelationshipRow(Base):
    __tablename__ = "relationships"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    from_entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    relation_type: Mapped[str] = mapped_column(Text)
    to_entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))

    __table_args__ = (
        UniqueConstraint("repo_id", "from_entity_id", "relation_type", "to_entity_id",
                         name="uq_relationships_edge"),
    )


class ProvenanceRow(Base):
    __tablename__ = "provenance"  # denormalized: survives fact pruning (data-model.md §4)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))
    fact_type: Mapped[str] = mapped_column(Text)
    extraction: Mapped[dict] = mapped_column(JSONB)
    artifact_refs: Mapped[list] = mapped_column(JSONB)
    anchors: Mapped[list] = mapped_column(JSONB, default=list)
    match_evidence: Mapped[dict | None] = mapped_column(JSONB)  # ADR-004: rule + numeric signals


class DeltaChangeRow(Base):
    __tablename__ = "delta_changes"  # append-only (enforced by grants in the migration)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))
    op: Mapped[str] = mapped_column(Text)                 # added|changed|removed|moved
    # SET NULL: the append-only log outlives removed entities (slug stays denormalized)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id", ondelete="SET NULL"))
    slug: Mapped[str] = mapped_column(Text)               # denormalized: log outlives entities
    entity_type: Mapped[str] = mapped_column(Text)
    change_summary: Mapped[dict] = mapped_column(JSONB)   # paths with old->new values
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_delta_changes_repo_type", "repo_id", "entity_type", "compile_run_id"),
        Index("ix_delta_changes_slug", "repo_id", "slug"),
    )


class LLMCacheRow(Base):
    """Content-addressed LLM cache (ADR-008): load-bearing, not an optimization.
    Entries are immutable and never expire by time; committed EAGERLY (outside
    the compile transaction) so budget-halted runs keep their prepaid answers
    (pipeline.md §6.2)."""

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    template_id: Mapped[str] = mapped_column(Text)
    template_version: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    output: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeltaRelationshipChangeRow(Base):
    __tablename__ = "delta_relationship_changes"  # append-only

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    compile_run_id: Mapped[int] = mapped_column(ForeignKey("compile_runs.id"))
    op: Mapped[str] = mapped_column(Text)                 # added|removed
    relation_type: Mapped[str] = mapped_column(Text)
    from_slug: Mapped[str] = mapped_column(Text)
    to_slug: Mapped[str] = mapped_column(Text)
