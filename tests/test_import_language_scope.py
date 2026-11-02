"""The import-root fallback never crosses languages (dogfood finding on frida:
Python's stdlib `import types` resolved to the TypeScript module
`frontend/src/components/ui-schema/widgets/types.ts`, adding a bogus
`depends_on` edge and an import-covers edge to every Python test importing
`types`; incremental and full compiles disagreed because the fallback only
fires while exactly one `*.types` module exists).

kc-covers:
  - component/knowledge-compiler-compiler-normalize

Real tree-sitter analyzers and the real Normalize over fixture sources.
"""

import pytest

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer  # noqa: E402

PY = {
    "app/util.py": "import types\n\ndef namespace(**kw):\n    return types.SimpleNamespace(**kw)\n",
    "tests/test_util.py": (
        "import types\nfrom app.util import namespace\n\n"
        "def test_namespace():\n    assert isinstance(namespace(a=1), types.SimpleNamespace)\n"),
    # same-language import-root fallback must keep working: `helpers` -> app.lib.helpers
    "app/lib/helpers.py": "def twice(x):\n    return 2 * x\n",
    "app/main.py": "from helpers import twice\n\ndef run():\n    return twice(2)\n",
}
TS = {
    # the only `*.types` module in the repo — the one the Python import used to hit
    "web/src/widgets/types.ts": "export type Widget = { id: string };\n",
    "web/src/widgets/card.ts": (
        "import type { Widget } from './types';\n"
        "export const label = (w: Widget): string => w.id;\n"),
}


def _art(ref: str, src: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": src}), content=src)


@pytest.fixture(scope="module")
def candidate():
    facts = (PythonAnalyzer().analyze([_art(r, s) for r, s in PY.items()])
             + TypeScriptAnalyzer().analyze([_art(r, s) for r, s in TS.items()]))
    return normalize(facts, CurrentState(), Thresholds(), "t")


def _edges(candidate) -> set[tuple[str, str, str]]:
    return {(r.from_slug, r.relation_type, r.to_slug) for r in candidate.relationships}


def _component(candidate, path: str):
    return next(e for e in candidate.entities
                if e.entity_type == "component" and e.payload["path"] == path)


def test_python_stdlib_import_does_not_resolve_to_a_typescript_module(candidate):
    util = _component(candidate, "app.util")
    assert "types" in util.payload["external_dependencies"]
    assert "types" not in util.payload["internal_dependencies"]
    widgets_types = _component(candidate, "web.src.widgets.types").slug
    assert (util.slug, "depends_on", widgets_types) not in _edges(candidate)


def test_python_test_does_not_cover_a_typescript_module_it_never_imports(candidate):
    widgets_types = _component(candidate, "web.src.widgets.types").slug
    test = next(e for e in candidate.entities if e.entity_type == "test_coverage")
    covered = {c["component"] for c in test.payload["coverage"]}
    assert widgets_types not in covered
    assert _component(candidate, "app.util").slug in covered


def test_same_language_import_root_fallback_still_resolves(candidate):
    main = _component(candidate, "app.main")
    assert main.payload["internal_dependencies"] == ["helpers"]
    helpers = _component(candidate, "app.lib.helpers").slug
    assert (main.slug, "depends_on", helpers) in _edges(candidate)


def test_typescript_relative_import_still_resolves(candidate):
    card = _component(candidate, "web.src.widgets.card")
    assert "web.src.widgets.types" in card.payload["internal_dependencies"]
