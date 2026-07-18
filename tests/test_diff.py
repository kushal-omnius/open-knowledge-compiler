"""Diff stage tests: removal evidence is the load-bearing rule here."""

from knowledge_compiler.compiler.diff import CompileScope, compute_diff
from knowledge_compiler.compiler.normalize import CandidateState, CurrentState
from knowledge_compiler.ir import Anchor, Entity, Relationship, content_hash


def entity(slug: str, entity_type: str = "component", payload: dict | None = None,
           anchors=(), name: str | None = None) -> Entity:
    payload = payload if payload is not None else {"v": 1}
    return Entity(slug=slug, entity_type=entity_type, repo_id="r", name=name or slug,
                  payload=payload, content_hash=content_hash(payload), anchors=tuple(anchors))


def cand(entities, relationships=()) -> CandidateState:
    return CandidateState(entities=list(entities), relationships=list(relationships),
                          evidence={}, provenance={}, conflicts=[], warnings=[])


FULL_DET = CompileScope(full=True, ran_families=frozenset({"deterministic"}))
FULL_ALL = CompileScope(full=True, ran_families=frozenset({"deterministic", "llm"}))


def ops(delta):
    return {(c.op, c.slug) for c in delta.entity_changes}


def test_added_changed_removed():
    current = CurrentState(entities=[entity("component/a"), entity("component/b")])
    candidate = cand([entity("component/a", payload={"v": 2}), entity("component/c")])
    delta, dirty = compute_diff(candidate, current, FULL_DET)
    assert ops(delta) == {("changed", "component/a"), ("added", "component/c"),
                          ("removed", "component/b")}
    assert delta.entity_changes[0].change_summary == {"v": {"old": 1, "new": 2}}  # old->new values
    assert dirty == {"component/a", "component/c"}  # removed is not emission input


def test_moved_when_anchors_relocate_content_same():
    old = entity("business-rule/x", "business_rule", {"s": "same"},
                 anchors=[Anchor(file_path="a.py")])
    new = entity("business-rule/x", "business_rule", {"s": "same"},
                 anchors=[Anchor(file_path="b.py")])
    delta, _ = compute_diff(cand([new]), CurrentState(entities=[old]), FULL_ALL)
    assert ops(delta) == {("moved", "business-rule/x")}
    assert delta.entity_changes[0].change_summary["anchors"] == {"old": ["a.py"], "new": ["b.py"]}


def test_llm_entities_survive_deterministic_only_compile():
    # pipeline.md §6.1: a --no-llm compile can never mass-remove LLM-derived entities,
    # and their wiki pages / relationship edges survive with them.
    rule = entity("business-rule/x", "business_rule", {"s": "r"})
    page = entity("wiki-page/business-rule-x", "wiki_page",
                  {"owner_slug": "business-rule/x", "page_type": "entity"})
    comp = entity("component/a")
    edge = Relationship(relation_type="governs", from_slug="business-rule/x", to_slug="component/a")
    current = CurrentState(entities=[rule, page, comp], relationships=[edge])

    candidate = cand([comp])  # deterministic pass re-observed only the component
    delta, _ = compute_diff(candidate, current, FULL_DET)
    assert ops(delta) == set()  # nothing removed, nothing changed
    assert delta.relationship_changes == ()  # the governs edge is protected too

    # with the llm family ran and the rule still unobserved: now it IS removable
    delta, _ = compute_diff(candidate, current, FULL_ALL)
    removed = {c.slug for c in delta.entity_changes if c.op == "removed"}
    assert removed == {"business-rule/x", "wiki-page/business-rule-x"}


def test_dirty_includes_relationship_endpoints():
    # ir.md §3.4 dirty rule: relationship-only change dirties both ends
    a, b = entity("component/a"), entity("component/b")
    current = CurrentState(entities=[a, b], relationships=[])
    candidate = cand([a, b], [Relationship(relation_type="depends_on",
                                           from_slug="component/a", to_slug="component/b")])
    delta, dirty = compute_diff(candidate, current, FULL_DET)
    assert ops(delta) == set()  # no entity changed...
    assert dirty == {"component/a", "component/b"}  # ...but both pages must regenerate


def test_no_changes_empty_delta():
    a = entity("component/a")
    delta, dirty = compute_diff(cand([a]), CurrentState(entities=[a]), FULL_DET)
    assert delta.entity_changes == () and delta.relationship_changes == () and dirty == set()
