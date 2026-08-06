"""JavaScript analyzer tests — parallel to the TypeScript suite, plus CJS require()
coverage unique to the JavaScript analyzer.

kc-covers:
  - component/knowledge-compiler-extractors-javascript-analyzer
"""

import pytest

from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_javascript", reason="tree-sitter JS grammar not installed")

from knowledge_compiler.extractors.javascript_analyzer import JavaScriptAnalyzer  # noqa: E402


def art(ref: str, content: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": content}), content=content)


def by_type(facts, fact_type):
    return [f for f in facts if f.fact_type == fact_type]


APP_JS = """\
import express from 'express';
import { applyDiscount } from './rules';
import { audit } from '../lib/audit';

const app = express();

export class InvoiceService {
  total(items) {
    return items.reduce((a, b) => a + b, 0);
  }
}

export function readInvoice(id) {
  return applyDiscount(id);
}

const format = (x) => `${x}`;

app.get('/invoices/:id', (req, res) => res.json(readInvoice(1)));
app.post('/invoices', (req, res) => res.sendStatus(201));
"""


def facts_for(ref: str, source: str):
    return JavaScriptAnalyzer().analyze([art(ref, source)])


def test_component_module_and_index_package():
    facts = facts_for("src/billing/app.js", APP_JS)
    comp = by_type(facts, "component_observed")[0]
    assert comp.payload == {"path": "src.billing.app", "kind": "module",
                            "language": "javascript", "file": "src/billing/app.js"}
    idx = facts_for("src/billing/index.js", "export * from './rules';\n")
    assert by_type(idx, "component_observed")[0].payload["path"] == "src.billing"
    assert by_type(idx, "component_observed")[0].payload["kind"] == "package"


def test_index_variants_are_packages():
    for name in ("index.jsx", "index.mjs", "index.cjs"):
        ref = f"src/{name}"
        facts = facts_for(ref, "export {};\n")
        assert by_type(facts, "component_observed")[0].payload["kind"] == "package", name


def test_symbols_functions_classes_methods_arrows():
    facts = facts_for("src/billing/app.js", APP_JS)
    symbols = {f.payload["symbol_path"]: f.payload["kind"]
               for f in by_type(facts, "symbol_observed")}
    assert symbols["src.billing.app.InvoiceService"] == "class"
    assert symbols["src.billing.app.InvoiceService.total"] == "method"
    assert symbols["src.billing.app.readInvoice"] == "function"
    assert symbols["src.billing.app.format"] == "function"   # arrow const
    for f in by_type(facts, "symbol_observed"):
        assert f.anchors and f.anchors[0].symbol_path  # ADR-004 anchor obligation


def test_esm_import_resolution_relative_internal_bare_external():
    facts = facts_for("src/billing/app.js", APP_JS)
    deps = {f.payload["to_path"] for f in by_type(facts, "dependency_observed")}
    assert "src.billing.rules" in deps    # ./rules resolved relative to file's dir
    assert "src.lib.audit" in deps        # ../lib/audit
    assert "express" in deps              # bare specifier stays as external coordinates


def test_cjs_require_extracted_as_dependency():
    src = """\
const express = require('express');
const { Router } = require('./router');
const path = require('path');
"""
    facts = facts_for("src/server.js", src)
    deps = {f.payload["to_path"] for f in by_type(facts, "dependency_observed")}
    assert "express" in deps
    assert "src.router" in deps
    assert "path" in deps


def test_express_routes():
    facts = facts_for("src/billing/app.js", APP_JS)
    routes = {(f.payload["method"], f.payload["route"])
              for f in by_type(facts, "api_endpoint_observed")}
    assert routes == {("GET", "/invoices/:id"), ("POST", "/invoices")}


def test_map_get_is_not_a_route():
    facts = facts_for("src/x.js", "const m = new Map();\nm.get('key');\n")
    assert by_type(facts, "api_endpoint_observed") == []


def test_test_files_produce_cases_and_targets():
    src = ("import { applyDiscount } from './rules';\n\n"
           "test('caps discount', () => {\n  expect(applyDiscount(50)).toBe(20);\n});\n"
           "it('handles zero', () => {});\n")
    facts = facts_for("src/billing/rules.test.js", src)
    cases = {f.payload["node_id"] for f in by_type(facts, "test_case_observed")}
    assert cases == {"src/billing/rules.test.js::caps discount",
                     "src/billing/rules.test.js::handles zero"}
    targets = by_type(facts, "test_target_observed")
    assert targets[0].payload["target_path"] == "src.billing.rules"


def test_spec_file_name_detected_as_test():
    src = "import { fn } from './fn';\ntest('t', () => {});\n"
    facts = facts_for("src/fn.spec.js", src)
    assert by_type(facts, "test_case_observed")
    assert by_type(facts, "test_target_observed")


def test_jsx_extension_claims_component():
    src = "export default function App() { return null; }\n"
    facts = facts_for("src/App.jsx", src)
    comp = by_type(facts, "component_observed")[0]
    assert comp.payload["language"] == "javascript"
    assert comp.payload["path"] == "src.App"
    symbols = {f.payload["symbol_path"] for f in by_type(facts, "symbol_observed")}
    assert "src.App.App" in symbols


def test_mjs_and_cjs_extensions_claimed():
    for ext in (".mjs", ".cjs"):
        ref = f"src/mod{ext}"
        facts = facts_for(ref, "export const x = 1;\n")
        assert by_type(facts, "component_observed"), f"no component for {ext}"


def test_analyzer_is_deterministic():
    assert facts_for("a/b.js", APP_JS) == facts_for("a/b.js", APP_JS)


def test_broken_source_degrades_gracefully():
    facts = facts_for("bad.js", "export class {{{ ???")
    assert by_type(facts, "component_observed")  # still emits the file-level component


def test_typescript_files_are_not_claimed():
    facts = JavaScriptAnalyzer().analyze([art("src/x.ts", "const x = 1;\n")])
    assert facts == []


def test_python_files_are_not_claimed():
    facts = JavaScriptAnalyzer().analyze([art("src/x.py", "x = 1\n")])
    assert facts == []


def test_describe_wrapped_tests_are_found():
    src = """\
describe('suite', () => {
  test('nested one', () => {});
  it('nested two', () => {});
});
"""
    facts = facts_for("src/pure_describe.test.js", src)
    cases = {f.payload["node_id"] for f in by_type(facts, "test_case_observed")}
    assert cases == {"src/pure_describe.test.js::nested one",
                     "src/pure_describe.test.js::nested two"}


def test_nested_describe_blocks_are_found():
    src = """\
describe('outer', () => {
  describe('inner', () => {
    test('deep', () => {});
  });
});
"""
    facts = facts_for("src/nested_describe.test.js", src)
    cases = {f.payload["node_id"] for f in by_type(facts, "test_case_observed")}
    assert cases == {"src/nested_describe.test.js::deep"}


def test_skip_and_only_variants_are_found():
    src = """\
test.skip('skipped', () => {});
it.only('focused', () => {});
"""
    facts = facts_for("src/variants.test.js", src)
    cases = {f.payload["node_id"] for f in by_type(facts, "test_case_observed")}
    assert cases == {"src/variants.test.js::skipped", "src/variants.test.js::focused"}


def test_bare_require_call_is_a_dependency():
    src = """\
require('dotenv').config();
require('./bootstrap');
"""
    facts = facts_for("src/entry.js", src)
    deps = {f.payload["to_path"] for f in by_type(facts, "dependency_observed")}
    assert "dotenv" in deps
    assert "src.bootstrap" in deps


def test_module_exports_assignment_is_a_dependency():
    facts = facts_for("src/index2.js", "module.exports = require('./thing');\n")
    deps = {f.payload["to_path"] for f in by_type(facts, "dependency_observed")}
    assert "src.thing" in deps


def test_cjs_exports_dot_name_assignment_produces_symbol():
    src = """\
exports.foo = function(req, res) { return 1; };
module.exports.bar = function() { return 2; };
"""
    facts = facts_for("src/cjs.js", src)
    symbols = {f.payload["symbol_path"]: f.payload["kind"]
               for f in by_type(facts, "symbol_observed")}
    assert symbols["src.cjs.foo"] == "function"
    assert symbols["src.cjs.bar"] == "function"


def test_module_exports_object_literal_produces_symbols():
    src = """\
module.exports = {
  baz: function() { return 3; },
  qux() { return 4; },
  arrowed: () => { return 5; }
};
"""
    facts = facts_for("src/cjs_obj.js", src)
    symbols = {f.payload["symbol_path"]: f.payload["kind"]
               for f in by_type(facts, "symbol_observed")}
    assert symbols["src.cjs_obj.baz"] == "function"
    assert symbols["src.cjs_obj.qux"] == "method"
    assert symbols["src.cjs_obj.arrowed"] == "function"


def test_downstream_normalize():
    """JS facts flow through the shared Normalize with the same entity/relationship
    outcomes — downstream is unaware that JavaScript exists (mirrors TS criterion 5)."""
    from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize

    files = {
        "src/billing/index.js": "export * from './rules';\n",
        "src/billing/rules.js": "export function applyDiscount(p) { return p; }\n",
        "src/billing/rules.test.js":
            "import { applyDiscount } from './rules';\ntest('t', () => {});\n",
    }
    facts = JavaScriptAnalyzer().analyze([art(r, c) for r, c in files.items()])
    state = normalize(facts, CurrentState(), Thresholds(), repo_slug="js-repo")
    slugs = {e.slug for e in state.entities}
    assert "component/src-billing" in slugs
    assert "component/src-billing-rules" in slugs
    rels = {(r.from_slug, r.relation_type, r.to_slug) for r in state.relationships}
    assert ("component/src-billing", "contains", "component/src-billing-rules") in rels
    assert ("test-coverage/src-billing-rules-test-js-t", "covers",
            "component/src-billing-rules") in rels
    assert state.warnings == []
