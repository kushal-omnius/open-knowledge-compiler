"""Rendering an imported component in a test counts as `call` evidence (dogfood
finding on frida: `render(<BundleEditor ... />)` linked BundleEditor only by
its import, so component tests looked like gaps).

kc-covers:
  - component/knowledge-compiler-extractors-js-test-evidence

Real tree-sitter TypeScript/JavaScript analyzers and the real Normalize over
fixture sources.
"""

import pytest

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_typescript")
pytest.importorskip("tree_sitter_javascript")

from knowledge_compiler.extractors.javascript_analyzer import JavaScriptAnalyzer  # noqa: E402
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer  # noqa: E402


def _art(ref: str, src: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": src}), content=src)


def _coverage(analyzer, files: dict[str, str]) -> dict[str, dict[str, str]]:
    facts = analyzer.analyze([_art(r, s) for r, s in files.items()])
    candidate = normalize(facts, CurrentState(), Thresholds(), "t")
    return {e.payload["node_id"]: {c["component"]: c["mechanism"] for c in e.payload["coverage"]}
            for e in candidate.entities if e.entity_type == "test_coverage"}


TSX = {
    "src/pages/BundleEditor.tsx": "export function BundleEditor(p: {id: number}) { return <div>{p.id}</div>; }\n",
    "src/pages/Panels.tsx": "export const Card = () => <section />;\n",
    "src/pages/Setup.tsx": "export const Setup = () => <main />;\n",
    "src/pages/__tests__/BundleEditor.test.tsx": (
        "import { render } from '@testing-library/react';\n"
        "import { BundleEditor } from '../BundleEditor';\n"
        "import * as Panels from '../Panels';\n"
        "import { Setup } from '../Setup';\n\n"
        "beforeEach(() => { render(<Setup />); });\n\n"
        "it('renders the editor', () => {\n"
        "  render(<BundleEditor id={1}><div /></BundleEditor>);\n"
        "});\n\n"
        "it('renders a namespaced card', () => {\n"
        "  render(<Panels.Card />);\n"
        "});\n"),
}


def test_rendering_an_imported_component_is_call_evidence():
    cov = _coverage(TypeScriptAnalyzer(), TSX)
    editor = cov["src/pages/__tests__/BundleEditor.test.tsx::renders the editor"]
    assert editor["component/src-pages-bundleeditor"] == "call"
    card = cov["src/pages/__tests__/BundleEditor.test.tsx::renders a namespaced card"]
    assert card["component/src-pages-panels"] == "call"


def test_setup_renders_and_intrinsic_tags_do_not_count():
    cov = _coverage(TypeScriptAnalyzer(), TSX)
    editor = cov["src/pages/__tests__/BundleEditor.test.tsx::renders the editor"]
    # rendered only in beforeEach: imported, so still a weak import link, never a call
    assert editor.get("component/src-pages-setup") == "import"
    assert editor.get("component/src-pages-panels") == "import"


def test_javascript_jsx_render_is_call_evidence_too():
    files = {
        "src/Widget.jsx": "export function Widget() { return <span />; }\n",
        "src/__tests__/Widget.test.jsx": (
            "import { render } from '@testing-library/react';\n"
            "import { Widget } from '../Widget';\n\n"
            "test('renders', () => { render(<Widget />); });\n"),
    }
    cov = _coverage(JavaScriptAnalyzer(), files)
    assert cov["src/__tests__/Widget.test.jsx::renders"]["component/src-widget"] == "call"
