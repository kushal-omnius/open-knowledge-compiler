"""The two-layer IR (ADR-009, docs/ir.md).

Fact IR: extraction output — per-compile, disposable, identity-free (no slug fields).
Knowledge IR: durable entities + relationships — slug-bearing, produced only by Normalize.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# --- Vocabulary (ir.md §2.3, §3.2, §3.3) -------------------------------------

ENTITY_TYPES = frozenset({
    "project", "component", "api", "test_coverage", "pull_request",
    "jira_story", "feature", "business_rule", "risk", "wiki_page",
    # user_journey (ADR-017, item 3 of the QA-agent-grounding backlog): an
    # ordered, end-to-end sequence of already-compiled entity slugs. V1 scope
    # is deterministic-only, declared in kc.toml `[[journeys]]` — LLM-candidate
    # extraction from Feature narratives and deterministic mining of existing
    # hand-written E2E test headers are explicitly deferred future work
    # (ADR-017's Option A extraction-source list), not attempted here.
    "user_journey",
})

# LLM-derived types go through the match-then-mint cascade (ADR-004).
LLM_DERIVED_TYPES = frozenset({"feature", "business_rule", "risk"})

RELATION_TYPES = frozenset({
    "implemented_by", "exposes", "governs", "verified_by", "covers",
    "defined_in", "depends_on", "contains", "affects", "motivates", "documents",
    "traverses",  # user_journey -> each step entity (ADR-017); order lives in payload, not here
})

DETERMINISTIC_FACT_TYPES = frozenset({
    "component_observed", "symbol_observed", "dependency_observed",
    "api_endpoint_observed", "test_case_observed", "test_target_observed",
    "source_change_observed", "pr_observed", "jira_observed",
    "doc_section_observed", "mutation_score_observed", "user_journey_observed",
})

LLM_CANDIDATE_FACT_TYPES = frozenset({
    "feature_candidate", "business_rule_candidate", "risk_candidate",
})

FACT_TYPES = DETERMINISTIC_FACT_TYPES | LLM_CANDIDATE_FACT_TYPES


def content_hash(payload: dict[str, Any]) -> str:
    """Deterministic hash of a payload: canonical JSON, sha256 (ir.md envelopes)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Shared -------------------------------------------------------------------


class Anchor(BaseModel):
    """Source-location reference (ir.md §2.2). Recorded raw; rename mapping is
    applied by Normalize at match time, never baked in."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    symbol_path: str | None = None
    span: tuple[int, int] | None = None  # least stable; disambiguation only


class Extraction(BaseModel):
    """How a fact was produced (ir.md §2.1 envelope)."""

    model_config = ConfigDict(frozen=True)

    method: Literal["deterministic", "llm"]
    extractor: str
    extractor_version: str
    grammar_version: str | None = None   # analyzers (ADR-006)
    model_id: str | None = None          # LLM facts (ADR-008)
    template_version: str | None = None  # LLM facts (ADR-008)


# --- Fact IR (ir.md §2) — identity-free by construction ------------------------


class Artifact(BaseModel):
    """A collected input (staged; prunable per data-model.md §4).

    `content` is staged payload for extractors within the compile — it is not
    part of the envelope identity (content_hash covers it) and is prunable.
    """

    model_config = ConfigDict(frozen=True)

    artifact_type: str
    source_ref: str
    content_hash: str
    content: str | None = None


class Fact(BaseModel):
    """Fact IR envelope (ir.md §2.1). Carries no slug — identity exists only in
    the Knowledge IR (ADR-009 boundary invariant)."""

    model_config = ConfigDict(frozen=True)

    fact_type: str
    payload: dict[str, Any]
    artifact_refs: tuple[str, ...]              # provenance
    extraction: Extraction
    content_hash: str
    anchors: tuple[Anchor, ...] = ()            # mandatory for LLM candidates (ADR-004)


# --- Knowledge IR (ir.md §3) — produced only by Normalize ----------------------


class Entity(BaseModel):
    """Knowledge IR entity envelope (ir.md §3.1)."""

    model_config = ConfigDict(frozen=True)

    slug: str
    entity_type: str
    repo_id: str
    name: str
    payload: dict[str, Any]
    content_hash: str
    anchors: tuple[Anchor, ...] = ()            # LLM-derived entities; anchor currency (ir.md §2.2)


class Relationship(BaseModel):
    model_config = ConfigDict(frozen=True)

    relation_type: str
    from_slug: str
    to_slug: str


# --- Delta (ir.md §3.4) — derived artifact in Knowledge IR vocabulary ----------


class EntityChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    op: Literal["added", "changed", "removed", "moved"]
    slug: str
    entity_type: str
    # changed payload paths with old->new values (data-model.md: backward-replayable)
    change_summary: dict[str, Any]
    evidence: dict[str, Any] = {}


class RelationshipChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    op: Literal["added", "removed"]
    relation_type: str
    from_slug: str
    to_slug: str


class Delta(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_changes: tuple[EntityChange, ...]
    relationship_changes: tuple[RelationshipChange, ...]
