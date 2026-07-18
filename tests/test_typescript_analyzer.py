"""TypeScript analyzer tests — mirroring the Python analyzer suite, plus the
success-criterion-5 proof: TS facts flow through Normalize with ZERO changes
downstream of extraction."""

import pytest

from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_typescript", reason="tree-sitter TS grammar not installed")

from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer  # noqa: E402


def art(ref: str, content: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": content}), content=content)


def by_type(facts, fact_type):
    return [f for f in facts if f.fact_type == fact_type]


APP_TS = """\
import express from 'express';
import { applyDiscount } from './rules';
import { audit } from '../lib/audit';

const app = express();

export class InvoiceService {
  total(items: number[]): number {
    return items.reduce((a, b) => a + b, 0);
  }
}

export function readInvoice(id: number) {
  return applyDiscount(id);
}

const format = (x: number) => `${x}`;

app.get('/invoices/:id', (req, res) => res.json(readInvoice(1)));
app.post('/invoices', (req, res) => res.sendStatus(201));
"""


def facts_for(ref: str, source: str):
    return TypeScriptAnalyzer().analyze([art(ref, source)])


def test_component_module_and_index_package():
    facts = facts_for("src/billing/app.ts", APP_TS)
    comp = by_type(facts, "component_observed")[0]
    assert comp.payload == {"path": "src.billing.app", "kind": "module",
                            "language": "typescript", "file": "src/billing/app.ts"}
    idx = facts_for("src/billing/index.ts", "export * from './rules';\n")
    assert by_type(idx, "component_observed")[0].payload["path"] == "src.billing"
    assert by_type(idx, "component_observed")[0].payload["kind"] == "package"


def test_symbols_functions_classes_methods_arrows():
    facts = facts_for("src/billing/app.ts", APP_TS)
    symbols = {f.payload["symbol_path"]: f.payload["kind"]
               for f in by_type(facts, "symbol_observed")}
    assert symbols["src.billing.app.InvoiceService"] == "class"
    assert symbols["src.billing.app.InvoiceService.total"] == "method"
    assert symbols["src.billing.app.readInvoice"] == "function"
    assert symbols["src.billing.app.format"] == "function"  # arrow const
    for f in by_type(facts, "symbol_observed"):
        assert f.anchors and f.anchors[0].symbol_path  # ADR-004 anchor obligation


def test_import_resolution_relative_internal_bare_external():
    facts = facts_for("src/billing/app.ts", APP_TS)
    deps = {f.payload["to_path"] for f in by_type(facts, "dependency_observed")}
    assert "src.billing.rules" in deps     # ./rules resolved against the file's dir
    assert "src.lib.audit" in deps         # ../lib/audit
    assert "express" in deps               # bare specifier kept as external coordinates


def test_express_routes():
    facts = facts_for("src/billing/app.ts", APP_TS)
    routes = {(f.payload["method"], f.payload["route"])
              for f in by_type(facts, "api_endpoint_observed")}
    assert routes == {("GET", "/invoices/:id"), ("POST", "/invoices")}


def test_map_get_is_not_a_route():
    facts = facts_for("src/x.ts", "const m = new Map();\nm.get('key');\n")
    assert by_type(facts, "api_endpoint_observed") == []


def test_test_files_produce_cases_and_targets():
    src = ("import { applyDiscount } from './rules';\n\n"
           "test('caps discount', () => {\n  expect(applyDiscount(50)).toBe(20);\n});\n"
           "it('handles zero', () => {});\n")
    facts = facts_for("src/billing/rules.test.ts", src)
    cases = {f.payload["node_id"] for f in by_type(facts, "test_case_observed")}
    assert cases == {"src/billing/rules.test.ts::caps discount",
                     "src/billing/rules.test.ts::handles zero"}
    targets = by_type(facts, "test_target_observed")
    assert targets[0].payload["target_path"] == "src.billing.rules"


def test_analyzer_is_deterministic():
    assert facts_for("a/b.ts", APP_TS) == facts_for("a/b.ts", APP_TS)


def test_broken_source_degrades_gracefully():
    facts = facts_for("bad.ts", "export class {{{ ???")
    assert by_type(facts, "component_observed")


def test_success_criterion_5_zero_downstream_changes():
    """TS facts flow through the same Normalize with the same entity/relationship
    outcomes — nothing downstream knows TypeScript exists."""
    from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize

    files = {
        "src/billing/index.ts": "export * from './rules';\n",
        "src/billing/rules.ts": "export function applyDiscount(p: number) { return p; }\n",
        "src/billing/rules.test.ts":
            "import { applyDiscount } from './rules';\ntest('t', () => {});\n",
    }
    facts = TypeScriptAnalyzer().analyze([art(r, c) for r, c in files.items()])
    state = normalize(facts, CurrentState(), Thresholds(), repo_slug="ts-repo")
    slugs = {e.slug for e in state.entities}
    assert "component/src-billing" in slugs and "component/src-billing-rules" in slugs
    rels = {(r.from_slug, r.relation_type, r.to_slug) for r in state.relationships}
    assert ("component/src-billing", "contains", "component/src-billing-rules") in rels
    assert ("test-coverage/src-billing-rules-test-ts-t", "covers",
            "component/src-billing-rules") in rels
    assert state.warnings == []
