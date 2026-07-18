"""Python language analyzer (ADR-006): tree-sitter backbone, deterministic facts only.

Emits (ir.md §2.3): component_observed, symbol_observed, dependency_observed,
api_endpoint_observed (FastAPI/Flask decorator patterns), test_case_observed,
test_target_observed.

Parse failures skip the file and are reported as warnings, never compile failures
(ADR-006). Unparseable regions degrade gracefully — tree-sitter is error-tolerant.
"""

from __future__ import annotations

import re
from importlib.metadata import version as pkg_version

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from knowledge_compiler.ir import Anchor, Artifact, Extraction, Fact, content_hash

_PY_LANGUAGE = Language(tspython.language())
_PARSER = Parser(_PY_LANGUAGE)

_EXTRACTION = Extraction(
    method="deterministic",
    extractor="python-analyzer",
    extractor_version="0.1",
    grammar_version=pkg_version("tree-sitter-python"),  # pinned + recorded (ADR-006)
)

# Route decorators: @<obj>.<verb>("/path") for FastAPI-style, @<obj>.route("/path", methods=[...]) for Flask.
_ROUTE_VERB = re.compile(
    r"@\s*[\w.]+\.(get|post|put|delete|patch|head|options)\s*\(\s*[\"']([^\"']+)[\"']"
)
_ROUTE_FLASK = re.compile(r"@\s*[\w.]+\.route\s*\(\s*[\"']([^\"']+)[\"'](.*)", re.DOTALL)
_FLASK_METHODS = re.compile(r"methods\s*=\s*\[([^\]]*)\]")


def _module_path(source_ref: str) -> str:
    """pkg/mod.py -> pkg.mod ; pkg/__init__.py -> pkg"""
    path = source_ref[:-3] if source_ref.endswith(".py") else source_ref
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_test_file(source_ref: str) -> bool:
    name = source_ref.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


class PythonAnalyzer:
    """LanguageAnalyzer plugin (built-in). Facts only — never entities (ADR-009)."""

    def analyze(self, artifacts: list[Artifact]) -> list[Fact]:
        facts: list[Fact] = []
        for artifact in artifacts:
            if not artifact.source_ref.endswith(".py") or artifact.content is None:
                continue
            facts.extend(self._analyze_file(artifact))
        return facts

    # -- per-file ---------------------------------------------------------------

    def _analyze_file(self, artifact: Artifact) -> list[Fact]:
        ref = artifact.source_ref
        module = _module_path(ref)
        tree = _PARSER.parse(artifact.content.encode("utf-8"))
        facts: list[Fact] = []

        def fact(fact_type: str, payload: dict, anchors: tuple[Anchor, ...] = ()) -> None:
            facts.append(Fact(
                fact_type=fact_type, payload=payload, artifact_refs=(ref,),
                extraction=_EXTRACTION, content_hash=content_hash(payload), anchors=anchors,
            ))

        # component: one per module; packages emerge from __init__.py refs
        kind = "package" if ref.endswith("__init__.py") else "module"
        fact("component_observed", {"path": module, "kind": kind, "language": "python", "file": ref})

        is_test = _is_test_file(ref)
        imports: list[str] = []
        self._walk(tree.root_node, artifact, module, fact, imports, scope=(), is_test=is_test)

        for target in sorted(set(imports)):
            fact("dependency_observed", {"from_path": module, "to_path": target})
            if is_test:
                fact("test_target_observed", {
                    "test_module": module, "target_path": target, "mechanism": "import", "file": ref,
                })
        return facts

    def _walk(self, node: Node, artifact: Artifact, module: str, fact, imports: list[str],
              scope: tuple[str, ...], is_test: bool) -> None:
        for child in node.children:
            t = child.type
            if t in ("import_statement", "import_from_statement"):
                imports.extend(self._import_targets(child))
            elif t == "decorated_definition":
                self._routes(child, artifact, module, fact)
                inner = child.child_by_field_name("definition")
                if inner is not None:
                    self._definition(inner, artifact, module, fact, imports, scope, is_test)
            elif t in ("function_definition", "class_definition"):
                self._definition(child, artifact, module, fact, imports, scope, is_test)
            else:
                # module-level compound statements (if TYPE_CHECKING:, try:) may hold imports
                self._walk(child, artifact, module, fact, imports, scope, is_test)

    def _definition(self, node: Node, artifact: Artifact, module: str, fact, imports,
                    scope: tuple[str, ...], is_test: bool) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8")
        qualified = (*scope, name)
        symbol_path = f"{module}." + ".".join(qualified)
        span = (node.start_point[0] + 1, node.end_point[0] + 1)
        anchor = Anchor(file_path=artifact.source_ref, symbol_path=symbol_path, span=span)

        if node.type == "class_definition":
            kind = "class"
        elif scope:
            kind = "method"
        else:
            kind = "function"

        params = node.child_by_field_name("parameters")
        payload = {
            "symbol_path": symbol_path, "kind": kind,
            "signature": params.text.decode("utf-8") if params is not None else None,
            "file": artifact.source_ref, "span": list(span),
        }
        fact("symbol_observed", payload, anchors=(anchor,))

        if is_test and kind == "function" and name.startswith("test"):
            fact("test_case_observed", {
                "node_id": f"{artifact.source_ref}::{name}", "framework": "pytest",
                "file": artifact.source_ref, "module": module,
            }, anchors=(anchor,))

        body = node.child_by_field_name("body")
        if body is not None:
            self._walk(body, artifact, module, fact, imports, qualified, is_test)

    def _routes(self, decorated: Node, artifact: Artifact, module: str, fact) -> None:
        for child in decorated.children:
            if child.type != "decorator":
                continue
            text = child.text.decode("utf-8")
            m = _ROUTE_VERB.search(text)
            if m:
                self._route_fact(fact, artifact, decorated, m.group(1).upper(), m.group(2))
                continue
            m = _ROUTE_FLASK.search(text)
            if m:
                methods_match = _FLASK_METHODS.search(m.group(2))
                methods = ([s.strip().strip("'\"").upper() for s in methods_match.group(1).split(",") if s.strip()]
                           if methods_match else ["GET"])
                for method in methods:
                    self._route_fact(fact, artifact, decorated, method, m.group(1))

    def _route_fact(self, fact, artifact: Artifact, decorated: Node, method: str, route: str) -> None:
        inner = decorated.child_by_field_name("definition")
        handler = None
        if inner is not None:
            name_node = inner.child_by_field_name("name")
            if name_node is not None:
                handler = name_node.text.decode("utf-8")
        span = (decorated.start_point[0] + 1, decorated.end_point[0] + 1)
        payload = {
            "method": method, "route": route, "handler": handler,
            "source": "code_pattern", "file": artifact.source_ref,
        }
        fact("api_endpoint_observed", payload,
             anchors=(Anchor(file_path=artifact.source_ref, symbol_path=handler, span=span),))

    @staticmethod
    def _import_targets(node: Node) -> list[str]:
        targets: list[str] = []
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    targets.append(child.text.decode("utf-8"))
                elif child.type == "aliased_import":
                    dotted = child.child_by_field_name("name")
                    if dotted is not None:
                        targets.append(dotted.text.decode("utf-8"))
        elif node.type == "import_from_statement":
            base = node.child_by_field_name("module_name")
            if base is not None:
                targets.append(base.text.decode("utf-8"))
        return targets
