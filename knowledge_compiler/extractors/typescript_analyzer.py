"""TypeScript language analyzer (ADR-006): tree-sitter backbone, deterministic facts.

The plugin-interface stress test (vision success criterion 5): emits exactly the
same fact vocabulary as the Python analyzer — everything downstream of extraction
is untouched by this file's existence.

Module convention: file paths map to dotted module ids (src/billing/rules.ts ->
src.billing.rules); index.ts plays __init__.py's role (directory module, kind
"package"). Relative import specifiers are internal; bare specifiers are external
dependency coordinates — except when they match a tsconfig.json path alias
(e.g. "@/*" -> "./src/*"), a widely-used convention for internal imports that
would otherwise be misclassified as external (dogfood finding).
"""

from __future__ import annotations

import json
import posixpath
import re
from importlib.metadata import version as pkg_version

import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from knowledge_compiler.ir import Anchor, Artifact, Extraction, Fact, content_hash

_TS = Language(tsts.language_typescript())
_TSX = Language(tsts.language_tsx())
_PARSERS = {".ts": Parser(_TS), ".tsx": Parser(_TSX)}

_EXTRACTION = Extraction(
    method="deterministic",
    extractor="typescript-analyzer",
    extractor_version="0.1",
    grammar_version=pkg_version("tree-sitter-typescript"),
)

# Express-style routes: <obj>.<verb>('/path', handler). The leading-'/' requirement
# keeps map.get('key') and friends out.
_ROUTE = re.compile(
    r"\b[\w.]+\.(get|post|put|delete|patch|head|options)\s*\(\s*['\"`](/[^'\"`]*)['\"`]")

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


# Strips // and /* */ comments from tsconfig.json (which is JSONC, not strict JSON)
# without touching string contents. Matches a full string OR a comment; comments
# (group 1) are dropped, strings pass through verbatim via group(0).
_JSONC_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|(/\*.*?\*/|//[^\n]*)', re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_jsonc(text: str) -> str:
    text = _JSONC_COMMENT.sub(lambda m: "" if m.group(1) else m.group(0), text)
    return _TRAILING_COMMA.sub(r"\1", text)


# (config_dir, base_url, paths) — config_dir is "" for a repo-root tsconfig.json.
TsconfigAliases = tuple[str, str, dict[str, list[str]]]


def _find_tsconfig_aliases(artifacts: list) -> list[TsconfigAliases]:
    """Locate tsconfig*.json files and extract their path-alias config
    (compilerOptions.baseUrl + .paths). Sorted by config_dir depth descending so
    the nearest enclosing tsconfig wins in a monorepo layout."""
    configs: list[TsconfigAliases] = []
    for artifact in artifacts:
        name = posixpath.basename(artifact.source_ref)
        if artifact.content is None or not (
            name == "tsconfig.json" or (name.startswith("tsconfig.") and name.endswith(".json"))
        ):
            continue
        try:
            data = json.loads(_strip_jsonc(artifact.content))
        except ValueError:
            continue
        paths = (data.get("compilerOptions") or {}).get("paths")
        if not paths:
            continue
        base_url = (data.get("compilerOptions") or {}).get("baseUrl", ".")
        configs.append((posixpath.dirname(artifact.source_ref), base_url, paths))
    configs.sort(key=lambda c: len(c[0]), reverse=True)
    return configs


def _applicable_aliases(importer_ref: str, configs: list[TsconfigAliases]) -> TsconfigAliases | None:
    directory = posixpath.dirname(importer_ref)
    for config_dir, base_url, paths in configs:
        if config_dir == "" or directory == config_dir or directory.startswith(config_dir + "/"):
            return config_dir, base_url, paths
    return None


def _match_alias(spec: str, paths: dict[str, list[str]]) -> str | None:
    """tsconfig `paths` pattern match: "@/*" -> ["./src/*"], or an exact (no "*") pair."""
    for pattern, targets in paths.items():
        if not targets:
            continue
        target = targets[0]
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            if spec == prefix or spec.startswith(prefix + "/"):
                rest = spec[len(prefix):].lstrip("/")
                return target[:-2] + (("/" + rest) if rest else "") if target.endswith("/*") else target
        elif spec == pattern:
            return target
    return None


def _resolve_import(spec: str, importer_ref: str, configs: list[TsconfigAliases]) -> tuple[str, bool]:
    """Returns (target, is_internal). Relative specifiers resolve against the
    importing file's directory. Bare specifiers are checked against the nearest
    tsconfig.json's path aliases before falling back to an external coordinate."""
    if spec.startswith("."):
        directory = posixpath.dirname(importer_ref)
        resolved = posixpath.normpath(posixpath.join(directory, spec))
        base, ext = posixpath.splitext(resolved)
        if ext in (".ts", ".tsx", ".js", ".jsx"):
            resolved = base
        return ".".join(p for p in resolved.split("/") if p and p != "."), True

    aliases = _applicable_aliases(importer_ref, configs)
    if aliases is not None:
        config_dir, base_url, paths = aliases
        aliased = _match_alias(spec, paths)
        if aliased is not None:
            resolved = posixpath.normpath(posixpath.join(config_dir, base_url, aliased))
            base, ext = posixpath.splitext(resolved)
            if ext in (".ts", ".tsx", ".js", ".jsx"):
                resolved = base
            return ".".join(p for p in resolved.split("/") if p and p != "."), True

    return spec, False


class TypeScriptAnalyzer:
    """LanguageAnalyzer plugin (built-in). Facts only — never entities (ADR-009)."""

    def analyze(self, artifacts: list[Artifact]) -> list[Fact]:
        configs = _find_tsconfig_aliases(artifacts)
        facts: list[Fact] = []
        for artifact in artifacts:
            ext = posixpath.splitext(artifact.source_ref)[1]
            if ext not in _PARSERS or artifact.content is None:
                continue
            facts.extend(self._analyze_file(artifact, _PARSERS[ext], configs))
        return facts

    def _analyze_file(self, artifact: Artifact, parser: Parser,
                       configs: list[TsconfigAliases]) -> list[Fact]:
        ref = artifact.source_ref
        module = _module_path(ref)
        tree = parser.parse(artifact.content.encode("utf-8"))
        facts: list[Fact] = []

        def fact(fact_type: str, payload: dict, anchors: tuple[Anchor, ...] = ()) -> None:
            facts.append(Fact(fact_type=fact_type, payload=payload, artifact_refs=(ref,),
                              extraction=_EXTRACTION, content_hash=content_hash(payload),
                              anchors=anchors))

        kind = "package" if posixpath.basename(ref).startswith("index.") else "module"
        fact("component_observed",
             {"path": module, "kind": kind, "language": "typescript", "file": ref})

        is_test = _is_test_file(ref)
        imports: list[str] = []
        self._walk(tree.root_node, artifact, module, fact, imports, scope=(), is_test=is_test)

        for spec in sorted(set(imports)):
            target, _internal = _resolve_import(spec, ref, configs)
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
            if t in ("import_statement", "export_statement"):
                source = child.child_by_field_name("source")
                if source is not None:
                    imports.append(source.text.decode("utf-8").strip("'\"`"))
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

    def _walk_decl(self, node: Node, artifact: Artifact, module: str, fact, imports,
                   scope: tuple[str, ...], is_test: bool) -> None:
        t = node.type
        if t in ("lexical_declaration", "variable_declaration"):
            for declarator in (c for c in node.children if c.type == "variable_declarator"):
                value = declarator.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    self._symbol(declarator, value, artifact, module, fact, scope, "function")
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

    def _test_case(self, stmt: Node, artifact: Artifact, fact) -> None:  # noqa: C901
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
        test_name = strings[0].text.decode("utf-8").strip("'\"`")
        span = (call.start_point[0] + 1, call.end_point[0] + 1)
        fact("test_case_observed",
             {"node_id": f"{artifact.source_ref}::{test_name}", "framework": "jest",
              "file": artifact.source_ref, "module": module},
             anchors=(Anchor(file_path=artifact.source_ref, span=span),))
