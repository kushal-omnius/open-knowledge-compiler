"""Tests for knowledge_compiler.validation.mutation (PLAN-qa-agent-substrate.md
Tier 0 / B1): anchor-derived mutmut scoping. Pure unit tests — no DB, no
tree-sitter, no mutmut install required; anchor_scope operates on Anchor
data directly.
"""

from knowledge_compiler.ir import Anchor
from knowledge_compiler.validation.mutation import anchor_scope


def test_anchor_scope_single_file():
    anchors = (Anchor(file_path="billing/rules.py", symbol_path="rules.apply_discount",
                      span=(1, 2)),)
    scope = anchor_scope(anchors)
    assert scope.file_globs == ("billing/rules.py",)
    assert scope.only_mutate == "billing/rules.py"
    assert scope.spans_by_file == {"billing/rules.py": ((1, 2),)}


def test_anchor_scope_multi_file_sorted_and_deduped():
    anchors = (
        Anchor(file_path="b.py", span=(5, 6)),
        Anchor(file_path="a.py", span=(1, 2)),
        Anchor(file_path="a.py", span=(1, 2)),  # exact duplicate
        Anchor(file_path="a.py", span=(3, 4)),
    )
    scope = anchor_scope(anchors)
    assert scope.file_globs == ("a.py", "b.py")  # sorted, deduped
    assert scope.only_mutate == "a.py,b.py"
    assert scope.spans_by_file["a.py"] == ((1, 2), (3, 4))  # deduped, sorted


def test_anchor_scope_no_span_omitted_from_spans_but_file_kept():
    anchors = (Anchor(file_path="c.py", symbol_path="c.thing"),)  # no span
    scope = anchor_scope(anchors)
    assert scope.file_globs == ("c.py",)
    assert scope.spans_by_file == {}


def test_anchor_scope_empty():
    scope = anchor_scope(())
    assert scope.file_globs == ()
    assert scope.only_mutate == ""
    assert scope.spans_by_file == {}
