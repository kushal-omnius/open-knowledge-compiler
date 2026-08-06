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
# A fresh Parser is constructed per file (see analyze()) rather than shared,
# since tree_sitter.Parser.parse() mutates internal state and a module-level
# singleton would race under any future concurrent Extract stage.
_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")

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


def _collect_requires(node: Node, imports: list[str]) -> None:
    """Finds every require('spec') call anywhere in the tree, regardless of
    statement shape (bare call, chained call, assignment RHS, callback
    argument, ...) — not just ones sitting as a variable declarator's value."""
    if node.type == "call_expression":
        spec = _require_spec(node)
        if spec is not None:
            imports.append(spec)
    for child in node.children:
        _collect_requires(child, imports)


def _cjs_export_target(left: Node) -> tuple[str, str] | None:
    """Classifies the LHS of an assignment_expression as a CommonJS export
    target. Returns ("named", name) for exports.<name>/module.exports.<name>
    = ..., ("bare", "") for module.exports = ..., or None otherwise."""
    if left.type != "member_expression":
        return None
    prop = left.child_by_field_name("property")
    obj = left.child_by_field_name("object")
    if prop is None or prop.type != "property_identifier" or obj is None:
        return None
    prop_name = prop.text.decode("utf-8")
    if obj.type == "identifier":
        obj_name = obj.text.decode("utf-8")
        if obj_name == "exports":
            return ("named", prop_name)
        if obj_name == "module" and prop_name == "exports":
            return ("bare", "")
    elif obj.type == "member_expression":
        inner_obj = obj.child_by_field_name("object")
        inner_prop = obj.child_by_field_name("property")
        if (inner_obj is not None and inner_obj.type == "identifier"
                and inner_obj.text == b"module"
                and inner_prop is not None and inner_prop.text == b"exports"):
            return ("named", prop_name)
    return None


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
            if ext not in _EXTENSIONS or artifact.content is None:
                continue
            facts.extend(self._analyze_file(artifact, Parser(_JS_LANGUAGE)))
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
        _collect_requires(tree.root_node, imports)
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
                       "lexical_declaration", "variable_declaration", "assignment_expression"):
                self._walk_decl(child, artifact, module, fact, imports, scope, is_test)
            elif t == "expression_statement" and is_test:
                self._test_statement(child, artifact, module, fact, imports, scope)
            else:
                self._walk(child, artifact, module, fact, imports, scope, is_test)

    def _walk_decl(self, node: Node, artifact: Artifact, module: str, fact, imports: list[str],
                   scope: tuple[str, ...], is_test: bool) -> None:
        t = node.type
        if t in ("lexical_declaration", "variable_declaration"):
            for declarator in (c for c in node.children if c.type == "variable_declarator"):
                value = declarator.child_by_field_name("value")
                name_node = declarator.child_by_field_name("name")
                if value is None or name_node is None or name_node.type != "identifier":
                    continue
                name = name_node.text.decode("utf-8")
                if value.type in ("arrow_function", "function_expression"):
                    self._symbol(name, value, artifact, module, fact, scope, "function")
                elif value.type == "class":
                    self._symbol(name, value, artifact, module, fact, scope, "class")
            return
        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self._symbol(name_node.text.decode("utf-8"), node, artifact, module, fact,
                             scope, "function")
        elif t == "class_declaration":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is None:
                return
            cname = name_node.text.decode("utf-8")
            self._symbol(cname, node, artifact, module, fact, scope, "class")
            if body is not None:
                class_scope = (*scope, cname)
                for member in body.children:
                    if member.type == "method_definition":
                        self._member_symbol(member, artifact, module, fact, class_scope, "method")
        elif t == "method_definition":
            self._member_symbol(node, artifact, module, fact, scope, "method")
        elif t == "assignment_expression":
            self._cjs_assignment(node, artifact, module, fact, scope)

    def _cjs_assignment(self, node: Node, artifact: Artifact, module: str, fact,
                        scope: tuple[str, ...]) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            return
        target = _cjs_export_target(left)
        if target is None:
            return
        export_kind, name = target
        if export_kind == "named":
            if right.type in ("function_expression", "arrow_function"):
                self._symbol(name, right, artifact, module, fact, scope, "function")
            elif right.type == "class":
                self._symbol(name, right, artifact, module, fact, scope, "class")
        elif export_kind == "bare" and right.type == "object":
            for member in right.children:
                self._object_member_symbol(member, artifact, module, fact, scope)

    def _object_member_symbol(self, member: Node, artifact: Artifact, module: str, fact,
                              scope: tuple[str, ...]) -> None:
        if member.type == "pair":
            key = member.child_by_field_name("key")
            value = member.child_by_field_name("value")
            if key is None or value is None or key.type != "property_identifier":
                return
            name = key.text.decode("utf-8")
            if value.type in ("function_expression", "arrow_function"):
                self._symbol(name, value, artifact, module, fact, scope, "function")
            elif value.type == "class":
                self._symbol(name, value, artifact, module, fact, scope, "class")
        elif member.type == "method_definition":
            self._member_symbol(member, artifact, module, fact, scope, "method")

    def _member_symbol(self, node: Node, artifact: Artifact, module: str, fact,
                       scope: tuple[str, ...], kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type != "property_identifier":
            return
        self._symbol(name_node.text.decode("utf-8"), node, artifact, module, fact, scope, kind)

    def _symbol(self, name: str, body_node: Node, artifact: Artifact, module: str,
                fact, scope: tuple[str, ...], kind: str) -> None:
        symbol_path = f"{module}." + ".".join((*scope, name))
        span = (body_node.start_point[0] + 1, body_node.end_point[0] + 1)
        params = body_node.child_by_field_name("parameters")
        payload = {"symbol_path": symbol_path, "kind": kind,
                   "signature": params.text.decode("utf-8") if params is not None else None,
                   "file": artifact.source_ref, "span": list(span)}
        fact("symbol_observed", payload,
             anchors=(Anchor(file_path=artifact.source_ref, symbol_path=symbol_path, span=span),))

    def _test_statement(self, stmt: Node, artifact: Artifact, module: str, fact,
                        imports: list[str], scope: tuple[str, ...]) -> None:
        """Handles an expression_statement in a test file: emits a
        test_case_observed fact for a direct test()/it()/.skip/.only call, or
        — for a container call like describe()/beforeEach() that isn't itself
        a test case — recurses into its function-argument body so nested
        test()/it() calls (the near-universal describe()-wrapped Jest shape)
        are still found."""
        call = stmt.children[0] if stmt.children else None
        if call is None or call.type != "call_expression":
            return
        if self._test_case(call, artifact, module, fact):
            return
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        for arg in args.children:
            if arg.type in ("arrow_function", "function_expression"):
                body = arg.child_by_field_name("body")
                if body is not None:
                    self._walk(body, artifact, module, fact, imports, scope, is_test=True)

    def _test_case(self, call: Node, artifact: Artifact, module: str, fact) -> bool:
        """Emits a test_case_observed fact if `call` is a test()/it() (or
        .skip/.only) invocation. Returns whether it matched."""
        fn = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if fn is None or args is None:
            return False
        if fn.type == "identifier":
            name = fn.text.decode("utf-8")
        elif fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            if obj is None or obj.type != "identifier":
                return False
            name = obj.text.decode("utf-8")
        else:
            return False
        if not _TEST_CALL.match(name):
            return False
        strings = [c for c in args.children if c.type == "string"]
        if not strings:
            return False
        test_name = _string_value(strings[0])
        span = (call.start_point[0] + 1, call.end_point[0] + 1)
        fact("test_case_observed",
             {"node_id": f"{artifact.source_ref}::{test_name}", "framework": "jest",
              "file": artifact.source_ref, "module": module},
             anchors=(Anchor(file_path=artifact.source_ref, span=span),))
        return True
