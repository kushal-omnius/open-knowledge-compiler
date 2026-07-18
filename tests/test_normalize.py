"""Normalize tests (docs/normalize.md).

The §9 determinism checklist is the review gate for this module; the property
test here is its executable form. Cascade behaviors are pinned with synthetic
LLM-candidate facts — identity logic is pure and needs no LLM.
"""

import pytest

from knowledge_compiler.compiler.normalize import (
    CurrentState, Thresholds, anchor_overlap, name_similarity, normalize, normalize_route,
)
from knowledge_compiler.ir import Anchor, Artifact, Extraction, Fact, content_hash

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402

CFG = Thresholds()  # defaults under test: t_anchor=0.5, t_name=0.8

_LLM_EXTRACTION = Extraction(method="llm", extractor="test-fixture", extractor_version="0",
                             model_id="fake", template_version="1")


def candidate(fact_type: str, name: str, anchors=(), **payload) -> Fact:
    p = {"name": name, **payload}
    return Fact(fact_type=fact_type, payload=p, artifact_refs=("fixture",),
                extraction=_LLM_EXTRACTION, content_hash=content_hash(p), anchors=tuple(anchors))


def source_facts():
    """Real analyzer facts over a small fixture codebase."""
    files = {
        "billing/__init__.py": "",
        "billing/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
        "billing/api.py": (
            "from fastapi import FastAPI\nfrom billing.rules import apply_discount\n"
            "app = FastAPI()\n\n"
            "@app.get(\"/discounts/{id}\")\ndef read_discount(id: int):\n"
            "    return apply_discount(id)\n"
        ),
        "tests/test_rules.py": (
            "from billing.rules import apply_discount\n\n"
            "def test_cap():\n    assert apply_discount(50) == 20\n"
        ),
    }
    artifacts = [Artifact(artifact_type="source_file", source_ref=ref,
                          content_hash=content_hash({"c": c}), content=c)
                 for ref, c in files.items()]
    return PythonAnalyzer().analyze(artifacts)


# --- scoring primitives -------------------------------------------------------


def test_route_normalization_is_positional():
    assert normalize_route("/users/{id}") == normalize_route("/users/{user_id}") == "/users/{}"
    assert normalize_route("/users/<int:uid>/x") == "/users/{}/x"


def test_name_similarity_is_word_order_robust():
    assert name_similarity("discount validation rule", "rule validation discount") == 1.0
    assert name_similarity("discount cap", "retry policy") == 0.0


def test_anchor_overlap_is_candidate_relative():
    a = (Anchor(file_path="a.py", symbol_path="a.f"),)
    big = tuple(Anchor(file_path=f"m{i}.py", symbol_path=f"m{i}.g") for i in range(9)) + a
    assert anchor_overlap(a, big) == 1.0  # small candidate matches a grown entity


# --- deterministic entities -----------------------------------------------------


def test_deterministic_entities_from_real_facts():
    state = normalize(source_facts(), CurrentState(), CFG, repo_slug="repo-a")
    by_slug = {e.slug: e for e in state.entities}

    assert "project/repo-a" in by_slug
    assert by_slug["component/billing-rules"].payload["kind"] == "module"
    assert by_slug["component/billing"].payload["kind"] == "package"

    api = by_slug["api/get-discounts"]
    assert api.payload["method"] == "GET" and api.payload["route"] == "/discounts/{}"
    assert api.payload["sources"] == ["code_pattern"]

    cov = by_slug["test-coverage/tests-test-rules-py-test-cap"]
    assert cov.payload["targets"] == ["billing.rules"]


def test_relationships_and_wiki_pages():
    state = normalize(source_facts(), CurrentState(), CFG, repo_slug="repo-a")
    rels = {(r.from_slug, r.relation_type, r.to_slug) for r in state.relationships}

    assert ("component/billing", "contains", "component/billing-rules") in rels
    assert ("project/repo-a", "contains", "component/billing") in rels
    assert ("component/billing-api", "depends_on", "component/billing-rules") in rels
    assert ("api/get-discounts", "defined_in", "component/billing-api") in rels
    assert ("test-coverage/tests-test-rules-py-test-cap", "covers", "component/billing-rules") in rels
    # wiki pages derived in Normalize (ADR-009 boundary), documenting their owners
    assert ("wiki-page/component-billing-rules", "documents", "component/billing-rules") in rels


def test_route_params_unify_api_identity():
    # ir.md API key note: adding a param *name* must not churn identity
    f1 = source_facts()
    state = normalize(f1, CurrentState(), CFG, repo_slug="repo-a")
    assert any(e.slug == "api/get-discounts" for e in state.entities)


# --- the determinism property (normalize.md §9, the review gate) ------------------


def test_normalize_is_byte_identical_on_same_inputs():
    facts = source_facts() + [
        candidate("business_rule_candidate", "Discount cap",
                  anchors=[Anchor(file_path="billing/rules.py", symbol_path="billing.rules.apply_discount")],
                  statement="Discount cannot exceed 20%", related_components=["billing.rules"]),
    ]
    a = normalize(facts, CurrentState(), CFG, repo_slug="repo-a")
    b = normalize(list(reversed(facts)), CurrentState(), CFG, repo_slug="repo-a")  # input order too
    assert [e.model_dump() for e in a.entities] == [e.model_dump() for e in b.entities]
    assert a.relationships == b.relationships
    assert {s: v.rule for s, v in a.evidence.items()} == {s: v.rule for s, v in b.evidence.items()}


# --- the cascade -------------------------------------------------------------------


RULE_ANCHOR = Anchor(file_path="billing/rules.py", symbol_path="billing.rules.apply_discount")


def rule_entity_in(state):
    return [e for e in state.entities if e.entity_type == "business_rule"]


def test_mint_on_empty_state_records_evidence():
    facts = source_facts() + [candidate("business_rule_candidate", "Discount cap",
                                        anchors=[RULE_ANCHOR], statement="max 20%")]
    state = normalize(facts, CurrentState(), CFG, repo_slug="repo-a")
    rules = rule_entity_in(state)
    assert [r.slug for r in rules] == ["business-rule/discount-cap"]
    assert state.evidence["business-rule/discount-cap"].rule == "minted"


def test_anchor_overlap_matches_across_rewording():
    # same anchors, completely different wording => same entity (rule 2 outranks names)
    prior = normalize(source_facts() + [candidate("business_rule_candidate", "Discount cap",
                                                  anchors=[RULE_ANCHOR], statement="max 20%")],
                      CurrentState(), CFG, repo_slug="repo-a")
    current = CurrentState(entities=prior.entities)

    reworded = candidate("business_rule_candidate", "Maximum percentage limit for price reductions",
                         anchors=[RULE_ANCHOR], statement="price reductions capped at 20 percent")
    state = normalize(source_facts() + [reworded], current, CFG, repo_slug="repo-a")
    rules = rule_entity_in(state)
    assert [r.slug for r in rules] == ["business-rule/discount-cap"]  # identity preserved
    ev = state.evidence["business-rule/discount-cap"]
    assert ev.rule == "anchor_overlap" and ev.signals["anchor_overlap"] == 1.0


def test_rename_map_bridges_one_compile_gap():
    prior = normalize(source_facts() + [candidate("business_rule_candidate", "Discount cap",
                                                  anchors=[RULE_ANCHOR], statement="max 20%")],
                      CurrentState(), CFG, repo_slug="repo-a")
    current = CurrentState(entities=prior.entities)

    moved = candidate("business_rule_candidate", "Discount cap",
                      anchors=[Anchor(file_path="billing/discounts.py",
                                      symbol_path="billing.discounts.apply_discount")],
                      statement="max 20%")
    rename = Fact(fact_type="source_change_observed",
                  payload={"change": "renamed", "old_path": "billing/rules.py",
                           "new_path": "billing/discounts.py"},
                  artifact_refs=("git",), extraction=_LLM_EXTRACTION.model_copy(
                      update={"method": "deterministic"}),
                  content_hash="r1")
    # note: symbol paths moved too — file-level overlap only; entity anchors get mapped
    state = normalize(source_facts() + [moved, rename], current, CFG, repo_slug="repo-a")
    slugs = [r.slug for r in rule_entity_in(state)]
    assert slugs == ["business-rule/discount-cap"]
    # anchor currency (P7): stored anchors rewritten to the new location
    entity = next(e for e in state.entities if e.slug == "business-rule/discount-cap")
    assert entity.anchors[0].file_path == "billing/discounts.py"


def test_below_threshold_splits_not_merges():
    # vision DP 8: uncertain => new entity, never silent merge
    prior = normalize(source_facts() + [candidate("business_rule_candidate", "Discount cap",
                                                  anchors=[RULE_ANCHOR], statement="max 20%")],
                      CurrentState(), CFG, repo_slug="repo-a")
    unrelated = candidate("business_rule_candidate", "Retry policy",
                          anchors=[Anchor(file_path="net/client.py", symbol_path="net.client.retry")],
                          statement="3 retries")
    state = normalize(source_facts() + [unrelated], CurrentState(entities=prior.entities),
                      CFG, repo_slug="repo-a")
    # minted as new — NOT absorbed into the existing discount-cap entity.
    # (discount-cap itself stays in current state; unobserved ≠ removed — Diff's job.)
    assert [r.slug for r in rule_entity_in(state)] == ["business-rule/retry-policy"]
    assert state.evidence["business-rule/retry-policy"].rule == "minted"


def test_intra_compile_dedup_two_extractions_one_entity():
    # normalize.md §5.1: candidates match against minted-this-compile too
    c1 = candidate("business_rule_candidate", "Discount cap", anchors=[RULE_ANCHOR],
                   statement="max 20%")
    c2 = candidate("business_rule_candidate", "Cap on discounts",
                   anchors=[RULE_ANCHOR,
                            Anchor(file_path="billing/api.py", symbol_path="billing.api.read_discount")],
                   statement="max 20%")
    state = normalize(source_facts() + [c1, c2], CurrentState(), CFG, repo_slug="repo-a")
    rules = rule_entity_in(state)
    assert len(rules) == 1
    # P4 merge: anchors unioned
    assert len(rules[0].anchors) == 2


def test_slug_collision_gets_deterministic_suffix():
    c1 = candidate("business_rule_candidate", "Discount cap", anchors=[RULE_ANCHOR], statement="a")
    c2 = candidate("business_rule_candidate", "Discount cap!",  # same slugified name, no overlap
                   anchors=[Anchor(file_path="other/mod.py", symbol_path="other.mod.f")],
                   statement="b", related_components=[])
    state = normalize(source_facts() + [c1, c2], CurrentState(), CFG, repo_slug="repo-a")
    slugs = sorted(r.slug for r in rule_entity_in(state))
    assert slugs == ["business-rule/discount-cap", "business-rule/discount-cap-2"]


def test_candidate_without_anchors_is_rejected_loudly():
    facts = source_facts() + [candidate("business_rule_candidate", "Ghost rule", statement="x")]
    state = normalize(facts, CurrentState(), CFG, repo_slug="repo-a")
    assert not rule_entity_in(state)
    assert any("missing anchors" in w for w in state.warnings)
