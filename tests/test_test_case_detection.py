"""Test-case detection for pytest classes and describe()-nested TypeScript tests
(dogfood finding on frida: 26 of 27 missed covering tests sat in files where no
test case was detected at all, so they never became test_coverage entities).

kc-covers:
  - component/knowledge-compiler-extractors-python-analyzer
  - component/knowledge-compiler-extractors-typescript-analyzer

Real tree-sitter analyzers and the real Normalize over fixture sources.
"""

import pytest

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter_typescript")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer  # noqa: E402


def _art(ref: str, src: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": src}), content=src)


def _node_ids(facts) -> set[str]:
    return {f.payload["node_id"] for f in facts if f.fact_type == "test_case_observed"}


def _coverage(facts) -> dict[str, dict[str, str]]:
    candidate = normalize(facts, CurrentState(), Thresholds(), "t")
    return {e.payload["node_id"]: {c["component"]: c["mechanism"] for c in e.payload["coverage"]}
            for e in candidate.entities if e.entity_type == "test_coverage"}


PY = {
    "billing/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
    "tests/test_rules.py": (
        "import pytest\nfrom billing.rules import apply_discount\n\n"
        "def test_top_level():\n    assert apply_discount(50) == 20\n\n"
        "class TestDiscount:\n"
        "    def test_caps(self):\n        assert apply_discount(50) == 20\n\n"
        "    @pytest.mark.parametrize('pct', [1, 2])\n"
        "    def test_passes_small(self, pct):\n        assert apply_discount(pct) == pct\n\n"
        "    def helper(self):\n        return apply_discount(1)\n\n"
        "class Helpers:\n"
        "    def test_not_collected(self):\n        assert apply_discount(1) == 1\n"),
}


def test_pytest_class_methods_are_test_cases_with_pytest_node_ids():
    facts = PythonAnalyzer().analyze([_art(r, s) for r, s in PY.items()])
    assert _node_ids(facts) == {
        "tests/test_rules.py::test_top_level",          # unchanged format
        "tests/test_rules.py::TestDiscount::test_caps",
        "tests/test_rules.py::TestDiscount::test_passes_small",
    }


def test_pytest_class_methods_carry_call_coverage():
    facts = PythonAnalyzer().analyze([_art(r, s) for r, s in PY.items()])
    cov = _coverage(facts)
    assert cov["tests/test_rules.py::TestDiscount::test_caps"] == {"component/billing-rules": "call"}
    assert cov["tests/test_rules.py::test_top_level"] == {"component/billing-rules": "call"}


TS = {
    "src/lib/toText.ts": "export const toText = (v: unknown): string => String(v ?? '');\n",
    "src/lib/__tests__/toText.test.ts": (
        "import { describe, it, expect } from 'vitest';\n"
        "import { toText } from '../toText';\n\n"
        "it('top level', () => { expect(toText(1)).toBe('1'); });\n\n"
        "describe('toText', () => {\n"
        "  it('renders numbers', () => { expect(toText(2)).toBe('2'); });\n"
        "  it.skip('skipped one', () => { expect(toText(3)).toBe('3'); });\n"
        "  describe('nullish', () => {\n"
        "    test('renders empty', () => { expect(toText(null)).toBe(''); });\n"
        "  });\n"
        "});\n"),
}


def test_describe_nested_and_skip_tests_are_test_cases():
    facts = TypeScriptAnalyzer().analyze([_art(r, s) for r, s in TS.items()])
    assert _node_ids(facts) == {
        "src/lib/__tests__/toText.test.ts::top level",   # unchanged format
        "src/lib/__tests__/toText.test.ts::renders numbers",
        "src/lib/__tests__/toText.test.ts::skipped one",
        "src/lib/__tests__/toText.test.ts::renders empty",
    }


def test_describe_nested_tests_carry_call_coverage():
    facts = TypeScriptAnalyzer().analyze([_art(r, s) for r, s in TS.items()])
    cov = _coverage(facts)
    assert cov["src/lib/__tests__/toText.test.ts::renders numbers"].get(
        "component/src-lib-totext") == "call"
    assert cov["src/lib/__tests__/toText.test.ts::renders empty"].get(
        "component/src-lib-totext") == "call"
