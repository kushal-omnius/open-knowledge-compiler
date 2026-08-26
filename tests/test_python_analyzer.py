"""Python analyzer tests: real tree-sitter parsing over fixture source strings.

Intent: each test pins one fact family's behavior so route-pattern or grammar
regressions surface here, not in wrong wiki pages.
"""

import pytest

from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_python", reason="tree-sitter deps not installed yet")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402


def art(ref: str, content: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": content}), content=content)


def by_type(facts, fact_type):
    return [f for f in facts if f.fact_type == fact_type]


APP_PY = '''\
from fastapi import FastAPI
import billing.core
from billing.rules import validate

app = FastAPI()


class InvoiceService:
    def total(self, items):
        return sum(items)


@app.get("/invoices/{id}")
def read_invoice(id: int):
    return validate(id)


@app.route("/legacy", methods=["POST", "PUT"])
def legacy_handler():
    pass
'''


def facts_for(ref: str, source: str):
    return PythonAnalyzer().analyze([art(ref, source)])


def test_component_and_module_path():
    facts = facts_for("svc/api/app.py", APP_PY)
    comp = by_type(facts, "component_observed")
    assert comp[0].payload == {"path": "svc.api.app", "kind": "module", "language": "python", "file": "svc/api/app.py"}

    init_facts = facts_for("svc/api/__init__.py", "")
    assert by_type(init_facts, "component_observed")[0].payload["path"] == "svc.api"
    assert by_type(init_facts, "component_observed")[0].payload["kind"] == "package"


def test_symbols_with_anchors_and_kinds():
    facts = facts_for("svc/api/app.py", APP_PY)
    symbols = {f.payload["symbol_path"]: f for f in by_type(facts, "symbol_observed")}
    assert symbols["svc.api.app.InvoiceService"].payload["kind"] == "class"
    assert symbols["svc.api.app.InvoiceService.total"].payload["kind"] == "method"
    assert symbols["svc.api.app.read_invoice"].payload["kind"] == "function"
    # every symbol fact carries an anchor with a span (ADR-004 extractor obligation)
    for f in by_type(facts, "symbol_observed"):
        assert f.anchors and f.anchors[0].span is not None


def test_dependencies_deduplicated_and_sorted():
    facts = facts_for("svc/api/app.py", APP_PY)
    deps = [f.payload["to_path"] for f in by_type(facts, "dependency_observed")]
    assert deps == sorted(deps)
    assert "fastapi" in deps and "billing.core" in deps and "billing.rules" in deps


def test_fastapi_route_pattern():
    facts = facts_for("svc/api/app.py", APP_PY)
    routes = {(f.payload["method"], f.payload["route"]): f for f in by_type(facts, "api_endpoint_observed")}
    assert ("GET", "/invoices/{id}") in routes
    assert routes[("GET", "/invoices/{id}")].payload["handler"] == "read_invoice"
    assert routes[("GET", "/invoices/{id}")].payload["source"] == "code_pattern"


def test_flask_route_with_methods_list():
    facts = facts_for("svc/api/app.py", APP_PY)
    routes = {(f.payload["method"], f.payload["route"]) for f in by_type(facts, "api_endpoint_observed")}
    assert ("POST", "/legacy") in routes and ("PUT", "/legacy") in routes


def test_test_files_produce_cases_and_targets():
    src = "import billing.rules\n\ndef test_discount():\n    assert True\n\ndef helper():\n    pass\n"
    facts = facts_for("tests/test_rules.py", src)
    cases = by_type(facts, "test_case_observed")
    assert [c.payload["node_id"] for c in cases] == ["tests/test_rules.py::test_discount"]
    targets = by_type(facts, "test_target_observed")
    assert targets[0].payload == {"test_module": "tests.test_rules", "target_path": "billing.rules",
                                  "mechanism": "import", "file": "tests/test_rules.py"}


def test_analyzer_is_deterministic():
    # normalize.md §9 gate applies to extraction inputs too: same source => identical facts
    assert facts_for("a/b.py", APP_PY) == facts_for("a/b.py", APP_PY)


def test_broken_source_degrades_gracefully():
    # tree-sitter is error-tolerant: garbage must not raise, and the module component survives
    facts = facts_for("bad.py", "def broken(:::\n  ???")
    assert by_type(facts, "component_observed")


def test_grammar_version_recorded():
    facts = facts_for("a.py", "x = 1\n")
    assert facts[0].extraction.grammar_version  # ADR-006: pinned + recorded


# -- ADR-023: state_transition_observed --------------------------------------

JOB_PY = '''\
def _run(job):
    job.status = "running"
    try:
        job.status = "succeeded"
    except Exception:
        job.status = "failed"
'''


def test_state_transition_sequential_and_branch_structure():
    facts = by_type(facts_for("pkg/jobs.py", JOB_PY), "state_transition_observed")
    edges = [(f.payload["from_state"], f.payload["to_state"]) for f in facts]
    assert edges == [(None, "running"), ("running", "succeeded"), ("running", "failed")]
    assert all(f.payload["confidence"] == "structural" for f in facts)
    assert all(f.payload["field"] == "status" for f in facts)
    # never a false edge between the two mutually-exclusive branch outcomes
    assert ("succeeded", "failed") not in edges
    assert ("failed", "succeeded") not in edges


def test_state_transition_anchored_to_owning_module():
    facts = by_type(facts_for("pkg/jobs.py", JOB_PY), "state_transition_observed")
    assert all(f.anchors[0].symbol_path == "pkg.jobs" for f in facts)
    assert all(f.anchors[0].file_path == "pkg/jobs.py" for f in facts)


def test_state_field_scoped_per_function():
    # a second function's assignments must not see the first function's state
    source = JOB_PY + '\ndef _activate(target):\n    target.status = "active"\n'
    facts = by_type(facts_for("pkg/jobs.py", source), "state_transition_observed")
    last = facts[-1]
    assert last.payload == {"field": "status", "from_state": None, "to_state": "active",
                            "confidence": "structural", "file": "pkg/jobs.py"}


def test_non_status_fields_are_ignored():
    facts = by_type(facts_for("a.py", 'x.color = "red"\n'), "state_transition_observed")
    assert facts == []


def test_if_elif_else_branches_do_not_leak_into_each_other():
    source = '''\
def f(x):
    if a:
        x.status = "a"
    elif b:
        x.status = "b"
    else:
        x.status = "c"
'''
    facts = by_type(facts_for("m.py", source), "state_transition_observed")
    edges = {(f.payload["from_state"], f.payload["to_state"]) for f in facts}
    assert edges == {(None, "a"), (None, "b"), (None, "c")}
