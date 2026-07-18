"""Phase-0 smoke tests: the package imports, the CLI mounts, the IR is honest.

Intent (why these matter): the IR boundary invariants (ADR-009) and hash determinism
are load-bearing for everything downstream; catching drift here is cheapest.
"""

from click.testing import CliRunner

from knowledge_compiler import ir
from knowledge_compiler.cli import main


def test_cli_mounts_all_run_modes():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "compile", "reconcile", "verify", "inspect", "serve"):
        assert cmd in result.output


def test_compile_requires_exactly_one_scope():
    # pipeline.md §1: scope is --full XOR --pr
    assert CliRunner().invoke(main, ["compile"]).exit_code != 0
    assert CliRunner().invoke(main, ["compile", "--full", "--pr", "1"]).exit_code != 0


def test_content_hash_is_deterministic_and_order_independent():
    # normalize.md §9: same input => byte-identical output. Key order must not matter.
    a = ir.content_hash({"x": 1, "y": [1, 2]})
    b = ir.content_hash({"y": [1, 2], "x": 1})
    assert a == b
    assert a != ir.content_hash({"x": 1, "y": [2, 1]})


def test_fact_ir_is_identity_free():
    # ADR-009 boundary invariant: fact types carry no slug field.
    assert "slug" not in ir.Fact.model_fields
    assert "slug" in ir.Entity.model_fields


def test_vocabulary_matches_ir_spec():
    # ir.md §3.2 lists exactly ten entity types; §3.3 exactly eleven relations.
    assert len(ir.ENTITY_TYPES) == 10
    assert len(ir.RELATION_TYPES) == 11
    assert ir.LLM_DERIVED_TYPES <= ir.ENTITY_TYPES
