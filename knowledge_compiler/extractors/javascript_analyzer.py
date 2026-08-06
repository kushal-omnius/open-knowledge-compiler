"""JavaScript language analyzer (ADR-006 / ADR-015): tree-sitter backbone, deterministic facts.

Covers .js/.jsx/.mjs/.cjs files not claimed by the TypeScript analyzer. Both ESM
(import/export) and CommonJS (require()) import styles are extracted as
dependency_observed facts. JSX is parsed natively by tree-sitter-javascript.

Emits the same fact vocabulary as the Python and TypeScript analyzers:
component_observed, symbol_observed, dependency_observed, api_endpoint_observed
(Express/Fastify patterns), test_case_observed, test_target_observed.

See ADR-015 for design rationale and open questions (module-system ambiguity,
shared helper code with TypeScriptAnalyzer, JSX specifics).
"""

from __future__ import annotations

import posixpath
import re
from importlib.metadata import version as pkg_version

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Node, Parser

from knowledge_compiler.ir import Anchor, Artifact, Extraction, Fact, content_hash

_JS_LANGUAGE = Language(tsjs.language())
# All four extensions share the same grammar; JSX is natively supported.
_PARSERS = {
    ".js": Parser(_JS_LANGUAGE),
    ".jsx": Parser(_JS_LANGUAGE),
    ".mjs": Parser(_JS_LANGUAGE),
    ".cjs": Parser(_JS_LANGUAGE),
}

_EXTRACTION = Extraction(
    method="deterministic",
    extractor="javascript-analyzer",
    extractor_version="0.1",
    grammar_version=pkg_version("tree-sitter-javascript"),
)

# Express/Fastify-style routes: <obj>.<verb>('/path', handler). Leading '/'
# requirement keeps map.get('key') and similar calls out (same guard as TS analyzer).
_ROUTE = re.compile(
    r"\b[\w.]+\.(get|post|put|delete|patch|head|options)\s*\(\s*['\"`](/[^'\"`]*)['\"`]"
)

_TEST_CALL = re.compile(r"^(test|it)$")


def _module_path(source_ref: str) -> str:
    base, _ext = posixpath.splitext(source_ref)
    parts = [p for p in base.split("/") if p]
    if parts and parts[-1] == "index":
        parts = parts[:-1]
    return ".".join(parts)


def _is_test_file(source_ref: str) -> bool:
    name = source_ref.rsplit("/", 1)[-1]
    return ".test." in name or ".spec." in name


def _string_value(node: Node) -> str:
    """Extract the string content from a string node (strips surrounding quotes)."""
    frag = next((c for c in node.children if c.type == "string_fragment"), None)
    if frag is not None:
        return frag.text.decode("utf-8")
    return node.text.decode("utf-8").strip("'\"`")


def _require_spec(call_node: Node) -> str | None:
    """If call_node is require('spec'), return 'spec'; else None."""
    fn = call_node.child_by_field_name("function")
    args = call_node.child_by_field_name("arguments")
    if fn is None or args is None or fn.type != "identifier":
        return None
    if fn.text.decode("utf-8") != "require":
        return None
    strings = [c for c in args.children if c.type == "string"]
    if not strings:
        return None
    return _string_value(strings[0])


def _resolve_import(spec: str, importer_ref: str) -> tuple[str, bool]:
    """Returns (target_path, is_internal). Relative specifiers are internal;
    bare specifiers are external dependency coordinates.
    Unlike TypeScriptAnalyzer there are no tsconfig aliases to resolve."""
    if spec.startswith("."):
        directory = posixpath.dirname(importer_ref)
        resolved = posixpath.normpath(posixpath.join(directory, spec))
        base, ext = posixpath.splitext(resolved)
        if ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
            resolved = base
        return ".".join(p for p in resolved.split("/") if p and p != "."), True
    return spec, False


class JavaScriptAnalyzer:
    """LanguageAnalyzer plugin (built-in). Facts only — never entities (ADR-009)."""

    def analyze(self, artifacts: list[Artifact]) -> list[Fact]:
        facts: list[Fact] = []
        for artifact in artifacts:
            ext = posixpath.splitext(artifact.source_ref)[1]
            if ext not in _PARSERS or artifact.content is None:
                continue
            facts.extend(self._analyze_file(artifact, _PARSERS[ext]))
        return facts

    def _analyze_file(self, artifact: Artifact, parser: Parser) -> list[Fact]:
        ref = artifact.source_ref
        module = _module_path(ref)
        tree = parser.parse(artifact.content.encode("utf-8"))
        facts: list[Fact] = []

        def fact(fact_type: str, payload: dict, anchors: tuple[Anchor, ...] = ()) -> None:
            facts.append(Fact(
                fact_type=fact_type, payload=payload, artifact_refs=(ref,),
                extraction=_EXTRACTION, content_hash=content_hash(payload), anchors=anchors,
            ))

        kind = "package" if posixpath.basename(ref).startswith("index.") else "module"
        fact("component_observed",
             {"path": module, "kind": kind, "language": "javascript", "file": ref})

        is_test = _is_test_file(ref)
        imports: list[str] = []
        self._walk(tree.root_node, artifact, module, fact, imports, scope=(), is_test=is_test)

        for spec in sorted(set(imports)):
            target, _internal = _resolve_import(spec, ref)
            fact("dependency_observed", {"from_path": module, "to_path": target})
            if is_test:
                fact("test_target_observed", {"test_module": module, "target_path": target,
                                              "mechanism": "import", "file": ref})

        for m in _ROUTE.finditer(artifact.content):
            line = artifact.content[: m.start()].count("\n") + 1
            payload = {"method": m.group(1).upper(), "route": m.group(2), "handler": None,
                       "source": "code_pattern", "file": ref}
            fact("api_endpoint_observed", payload,
                 anchors=(Anchor(file_path=ref, span=(line, line)),))
        return facts

    def _walk(self, node: Node, artifact: Artifact, module: str, fact, imports: list[str],
              scope: tuple[str, ...], is_test: bool) -> None:
        for child in node.children:
            t = child.type
            if t == "import_statement":
                source = child.child_by_field_name("source")
                if source is not None:
                    imports.append(_string_value(source))
                decl = child.child_by_field_name("declaration")
                if decl is not None:
                    self._walk_decl(decl, artifact, module, fact, imports, scope, is_test)
            elif t == "export_statement":
                source = child.child_by_field_name("source")
                if source is not None:
                    imports.append(_string_value(source))
                decl = child.child_by_field_name("declaration")
                if decl is not None:
                    self._walk_decl(decl, artifact, module, fact, imports, scope, is_test)
            elif t in ("function_declaration", "class_declaration", "method_definition",
                       "lexical_declaration", "variable_declaration"):
                self._walk_decl(child, artifact, module, fact, imports, scope, is_test)
            elif t == "expression_statement" and is_test:
                self._test_case(child, artifact, fact)
            else:
                self._walk(child, artifact, module, fact, imports, scope, is_test)

    def _walk_decl(self, node: Node, artifact: Artifact, module: str, fact, imports: list[str],
                   scope: tuple[str, ...], is_test: bool) -> None:
        t = node.type
        if t in ("lexical_declaration", "variable_declaration"):
            for declarator in (c for c in node.children if c.type == "variable_declarator"):
                value = declarator.child_by_field_name("value")
                if value is None:
                    continue
                if value.type in ("arrow_function", "function_expression"):
                    self._symbol(declarator, value, artifact, module, fact, scope, "function")
                elif value.type == "class":
                    self._symbol(declarator, value, artifact, module, fact, scope, "class")
                elif value.type == "call_expression":
                    spec = _require_spec(value)
                    if spec is not None:
                        imports.append(spec)
            return
        if t == "function_declaration":
            self._symbol(node, node, artifact, module, fact, scope, "function")
        elif t == "class_declaration":
            self._symbol(node, node, artifact, module, fact, scope, "class")
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None and body is not None:
                class_scope = (*scope, name_node.text.decode("utf-8"))
                for member in body.children:
                    if member.type == "method_definition":
                        self._symbol(member, member, artifact, module, fact, class_scope, "method")
        elif t == "method_definition":
            self._symbol(node, node, artifact, module, fact, scope, "method")

    def _symbol(self, name_holder: Node, body_node: Node, artifact: Artifact, module: str,
                fact, scope: tuple[str, ...], kind: str) -> None:
        name_node = name_holder.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8")
        symbol_path = f"{module}." + ".".join((*scope, name))
        span = (body_node.start_point[0] + 1, body_node.end_point[0] + 1)
        params = body_node.child_by_field_name("parameters")
        payload = {"symbol_path": symbol_path, "kind": kind,
                   "signature": params.text.decode("utf-8") if params is not None else None,
                   "file": artifact.source_ref, "span": list(span)}
        fact("symbol_observed", payload,
             anchors=(Anchor(file_path=artifact.source_ref, symbol_path=symbol_path, span=span),))

    def _test_case(self, stmt: Node, artifact: Artifact, fact) -> None:
        module = _module_path(artifact.source_ref)
        call = stmt.children[0] if stmt.children else None
        if call is None or call.type != "call_expression":
            return
        fn = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if fn is None or args is None or not _TEST_CALL.match(fn.text.decode("utf-8")):
            return
        strings = [c for c in args.children if c.type == "string"]
        if not strings:
            return
        test_name = _string_value(strings[0])
        span = (call.start_point[0] + 1, call.end_point[0] + 1)
        fact("test_case_observed",
             {"node_id": f"{artifact.source_ref}::{test_name}", "framework": "jest",
              "file": artifact.source_ref, "module": module},
             anchors=(Anchor(file_path=artifact.source_ref, span=span),))
