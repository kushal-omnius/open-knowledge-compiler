"""Python language analyzer (ADR-006): tree-sitter backbone, deterministic facts only.

Emits (ir.md §2.3): component_observed, symbol_observed, dependency_observed,
api_endpoint_observed (FastAPI/Flask decorator patterns), test_case_observed,
test_target_observed, state_transition_observed (ADR-023).

Parse failures skip the file and are reported as warnings, never compile failures
(ADR-006). Unparseable regions degrade gracefully — tree-sitter is error-tolerant.
"""

from __future__ import annotations

import re
from importlib.metadata import version as pkg_version

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from knowledge_compiler.extractors.body_hash import body_hash
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
# An empty path is valid under a prefixed router (`@router.post("")` -> POST /items).
# group 1 is the decorated object (`router`, `app`, `items.router`): it names the
# router whose prefixes Normalize composes into the full route
_ROUTE_VERB = re.compile(
    r"@\s*([\w.]+)\.(get|post|put|delete|patch|head|options)\s*\(\s*[\"']([^\"']*)[\"']"
)
_ROUTE_FLASK = re.compile(r"@\s*([\w.]+)\.route\s*\(\s*[\"']([^\"']+)[\"'](.*)", re.DOTALL)
# Constructors whose result is a router: FastAPI apps and APIRouter instances.
_ROUTER_CTORS = frozenset({"FastAPI", "APIRouter"})
_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
_FLASK_METHODS = re.compile(r"methods\s*=\s*\[([^\]]*)\]")

# ADR-023: attribute names treated as state fields. `<anything>.<field> = "literal"`
# assignments to these are extracted as state_transition_observed facts, owned by
# the enclosing module (not the mutated object's class — real code, e.g.
# `model_cls`-parameterized helpers, mutates dynamically-typed objects that
# static analysis can't resolve to a class; module granularity matches every
# other cross-cutting signal in this IR, e.g. mutation_kill_rate).
_STATE_FIELDS = frozenset({"status", "state"})


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


def _is_pytest_case(name: str, kind: str, scope: tuple[str, ...]) -> bool:
    """pytest's default collection: module-level `test*` functions and `test*`
    methods of a module-level `Test*` class (dogfood finding on frida, whose
    backend groups most tests in classes)."""
    if not name.startswith("test"):
        return False
    return kind == "function" or (kind == "method" and len(scope) == 1
                                  and scope[0].startswith("Test"))


def _pytest_functions(root: Node):
    """(scope, function_definition) for each test `_is_pytest_case` collects."""
    def definition(node: Node) -> Node | None:
        return node.child_by_field_name("definition") if node.type == "decorated_definition" else node

    def name_of(node: Node) -> str:
        name = node.child_by_field_name("name")
        return name.text.decode("utf-8") if name is not None else ""

    for node in root.children:
        d = definition(node)
        if d is None:
            continue
        if d.type == "function_definition" and name_of(d).startswith("test"):
            yield (), d
        elif d.type == "class_definition" and name_of(d).startswith("Test"):
            body = d.child_by_field_name("body")
            for member in body.children if body is not None else ():
                m = definition(member)
                if m is not None and m.type == "function_definition" and name_of(m).startswith("test"):
                    yield (name_of(d),), m


class PythonAnalyzer:
    """LanguageAnalyzer plugin (built-in). Facts only — never entities (ADR-009)."""

    def analyze(self, artifacts: list[Artifact]) -> list[Fact]:
        facts: list[Fact] = []
        self.files_seen = 0
        self.failed_files: list[str] = []
        for artifact in artifacts:
            if not artifact.source_ref.endswith(".py") or artifact.content is None:
                continue
            self.files_seen += 1
            try:
                facts.extend(self._analyze_file(artifact))
            except Exception:  # noqa: BLE001 — skip the file, never the compile (ADR-006)
                self.failed_files.append(artifact.source_ref)
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
        self._aliases = self._import_aliases(tree.root_node, module, ref.endswith("__init__.py"))
        self._walk(tree.root_node, artifact, module, fact, imports, scope=(), is_test=is_test)
        self._routing(tree.root_node, module, ref, fact)
        if is_test:
            self._test_evidence(tree.root_node, module, ref, fact)

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
            "body_hash": body_hash(node),
        }
        fact("symbol_observed", payload, anchors=(anchor,))

        if is_test and _is_pytest_case(name, kind, scope):
            fact("test_case_observed", {
                "node_id": "::".join((artifact.source_ref, *scope, name)), "framework": "pytest",
                "file": artifact.source_ref, "module": module,
            }, anchors=(anchor,))

        body = node.child_by_field_name("body")
        if body is not None:
            if kind in ("function", "method"):
                self._walk_state_block(list(body.children), {}, artifact, module, fact)
            self._walk(body, artifact, module, fact, imports, qualified, is_test)

    # -- ADR-023: state_model extraction -----------------------------------------
    #
    # Per-function structural scan: sequential same-level `<x>.<field> = "lit"`
    # assignments chain from_state -> to_state in source order. Branch constructs
    # (if/elif/else, try/except) run each branch from an independent copy of the
    # pre-branch state, so branches never see each other's assignments — after
    # the branch construct, any field touched in ANY branch resets to unknown
    # (None) rather than guessing which branch ran (never fabricate a transition
    # between two mutually-exclusive branch outcomes). Loops/`with` bodies are
    # walked sequentially without a branch reset — a known imprecision, accepted
    # for V1 rather than building general control-flow analysis.

    def _walk_state_block(self, stmts: list[Node], last_state: dict[str, str | None],
                          artifact: Artifact, module: str, fact) -> None:
        for stmt in stmts:
            if stmt.type == "if_statement":
                self._branch_state(self._if_branches(stmt), last_state, artifact, module, fact)
            elif stmt.type == "try_statement":
                self._branch_state(self._try_branches(stmt), last_state, artifact, module, fact)
            elif stmt.type in ("with_statement", "for_statement", "while_statement"):
                inner = stmt.child_by_field_name("body")
                if inner is not None:
                    self._walk_state_block(list(inner.children), last_state, artifact, module, fact)
            elif stmt.type == "expression_statement" and stmt.children:
                self._maybe_state_assignment(stmt.children[0], last_state, artifact, module, fact)

    def _branch_state(self, branches: list[list[Node]], last_state: dict[str, str | None],
                      artifact: Artifact, module: str, fact) -> None:
        touched: dict[str, str | None] = {}
        for branch_stmts in branches:
            branch_state = dict(last_state)
            self._walk_state_block(branch_stmts, branch_state, artifact, module, fact)
            for field_name, value in branch_state.items():
                if last_state.get(field_name) != value:
                    touched[field_name] = None  # divergent across branches -> unknown after merge
        last_state.update(touched)

    @staticmethod
    def _if_branches(node: Node) -> list[list[Node]]:
        branches = []
        consequence = node.child_by_field_name("consequence")
        if consequence is not None:
            branches.append(list(consequence.children))
        for child in node.children:
            if child.type == "elif_clause":
                inner = child.child_by_field_name("consequence")
                if inner is not None:
                    branches.append(list(inner.children))
            elif child.type == "else_clause":
                inner = child.child_by_field_name("body")
                if inner is not None:
                    branches.append(list(inner.children))
        return branches

    @staticmethod
    def _try_branches(node: Node) -> list[list[Node]]:
        branches = []
        body = node.child_by_field_name("body")
        if body is not None:
            branches.append(list(body.children))
        for child in node.children:
            if child.type == "except_clause":
                inner = next((c for c in child.children if c.type == "block"), None)
                if inner is not None:
                    branches.append(list(inner.children))
        return branches

    def _maybe_state_assignment(self, node: Node, last_state: dict[str, str | None],
                                artifact: Artifact, module: str, fact) -> None:
        if node.type != "assignment":
            return
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "attribute" or right.type != "string":
            return
        attr_node = left.child_by_field_name("attribute")
        if attr_node is None:
            return
        field_name = attr_node.text.decode("utf-8")
        if field_name not in _STATE_FIELDS:
            return
        to_state = self._string_literal_value(right)
        if to_state is None:
            return
        from_state = last_state.get(field_name)
        span = (node.start_point[0] + 1, node.end_point[0] + 1)
        fact("state_transition_observed", {
            "field": field_name, "from_state": from_state, "to_state": to_state,
            "confidence": "structural", "file": artifact.source_ref,
        }, anchors=(Anchor(file_path=artifact.source_ref, symbol_path=module, span=span),))
        last_state[field_name] = to_state

    # -- test evidence beyond imports (QA-signal review finding 2) ----------------

    def _test_evidence(self, root: Node, module: str, ref: str, fact) -> None:
        """Per test function: `call` evidence for each call to a name imported
        from another module, and `http` evidence for each test-client request
        (`client.get("/items/1")`). Only test function bodies are scanned, so
        names a test file imports just for fixtures, types or setup never count
        as a call. conftest.py is not a test file, so its imports never count."""
        for scope, fn in _pytest_functions(root):
            name_node, body = fn.child_by_field_name("name"), fn.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            node_id = "::".join((ref, *scope, name_node.text.decode("utf-8")))
            calls: set[str] = set()
            requests: set[tuple[str, str]] = set()
            stack = [body]
            while stack:
                n = stack.pop()
                stack.extend(n.children)
                if n.type != "call":
                    continue
                callee, args = n.child_by_field_name("function"), n.child_by_field_name("arguments")
                if callee is None or args is None:
                    continue
                if callee.type == "attribute":
                    attr = callee.child_by_field_name("attribute")
                    verb = attr.text.decode("utf-8") if attr is not None else ""
                    first = next((a for a in args.children if a.is_named), None)
                    path = self._string_literal_value(first) if first is not None and first.type == "string" else None
                    if verb in _HTTP_VERBS and path is not None and path.startswith("/"):
                        requests.add((verb.upper(), path))
                        continue
                if callee.type in ("identifier", "attribute"):
                    text = callee.text.decode("utf-8")
                    if text.split(".")[0] in self._aliases:  # imported, not a local helper
                        calls.add(self._resolve_ref(text, module))
            for target in sorted(calls):
                fact("test_target_observed", {"test_module": module, "target_path": target,
                                              "mechanism": "call", "file": ref, "node_id": node_id})
            for method, path in sorted(requests):
                fact("test_target_observed", {"test_module": module, "target_path": path,
                                              "mechanism": "http", "method": method,
                                              "file": ref, "node_id": node_id})

    # -- router prefixes (FastAPI APIRouter / include_router) ---------------------
    #
    # Facts stay per-file: a router's own prefix (router_observed) and each
    # include_router call (router_mount_observed) are emitted where they appear,
    # with router refs as dotted paths resolved through this file's imports.
    # Normalize composes them into full routes, so the result doesn't depend on
    # which files a PR compile happened to collect.

    @staticmethod
    def _absolute_module(spec: str, module: str, is_package: bool) -> str:
        """Relative `from ..x import y` module spec -> absolute dotted path."""
        dots = len(spec) - len(spec.lstrip("."))
        if dots == 0:
            return spec
        parts = module.split(".") if module else []
        package = parts if is_package else parts[:-1]
        base = package[: len(package) - (dots - 1)] if dots > 1 else package
        rest = spec[dots:]
        return ".".join([*base, *([rest] if rest else [])])

    def _import_aliases(self, root: Node, module: str, is_package: bool) -> dict[str, str]:
        """Local name -> absolute dotted path, from every import in the file
        (including function-local ones, e.g. inside create_app())."""
        aliases: dict[str, str] = {}
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "import_statement":
                for child in node.children_by_field_name("name"):
                    if child.type == "aliased_import":
                        dotted, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                        if dotted is not None and alias is not None:
                            aliases[alias.text.decode("utf-8")] = dotted.text.decode("utf-8")
                    else:
                        head = child.text.decode("utf-8").split(".")[0]
                        aliases[head] = head
            elif node.type == "import_from_statement":
                base = node.child_by_field_name("module_name")
                if base is None:
                    continue
                source = self._absolute_module(base.text.decode("utf-8"), module, is_package)
                for child in node.children_by_field_name("name"):
                    if child.type == "aliased_import":
                        dotted, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                        if dotted is not None and alias is not None:
                            aliases[alias.text.decode("utf-8")] = f"{source}.{dotted.text.decode('utf-8')}"
                    else:
                        name = child.text.decode("utf-8")
                        aliases[name] = f"{source}.{name}"
            else:
                stack.extend(node.children)
        return aliases

    def _resolve_ref(self, expr: str, module: str) -> str:
        head, _, rest = expr.partition(".")
        base = getattr(self, "_aliases", {}).get(head)
        if base is None:
            return f"{module}.{expr}"
        return f"{base}.{rest}" if rest else base

    def _keyword_string(self, args: Node, keyword: str) -> str | None:
        for arg in args.children:
            if arg.type == "keyword_argument":
                name, value = arg.child_by_field_name("name"), arg.child_by_field_name("value")
                if name is not None and value is not None and name.text.decode("utf-8") == keyword \
                        and value.type == "string":
                    return self._string_literal_value(value)
        return None

    def _routing(self, root: Node, module: str, ref: str, fact) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.type == "assignment":
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                if left is None or right is None or left.type != "identifier" or right.type != "call":
                    continue
                fn, args = right.child_by_field_name("function"), right.child_by_field_name("arguments")
                if fn is None or args is None or fn.text.decode("utf-8").rsplit(".", 1)[-1] not in _ROUTER_CTORS:
                    continue
                fact("router_observed", {
                    "router": f"{module}.{left.text.decode('utf-8')}",
                    "prefix": self._keyword_string(args, "prefix") or "", "file": ref,
                })
            elif node.type == "call":
                fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
                if fn is None or args is None or fn.type != "attribute":
                    continue
                attr, obj = fn.child_by_field_name("attribute"), fn.child_by_field_name("object")
                if attr is None or obj is None or attr.text.decode("utf-8") != "include_router":
                    continue
                child = next((a for a in args.children if a.type in ("identifier", "attribute")), None)
                if child is None:
                    continue
                fact("router_mount_observed", {
                    "parent": self._resolve_ref(obj.text.decode("utf-8"), module),
                    "child": self._resolve_ref(child.text.decode("utf-8"), module),
                    "prefix": self._keyword_string(args, "prefix") or "", "file": ref,
                })

    @staticmethod
    def _string_literal_value(node: Node) -> str | None:
        if any(c.type == "interpolation" for c in node.children):
            return None  # f-string with interpolation: not a literal, skip
        content = next((c for c in node.children if c.type == "string_content"), None)
        if content is not None:
            return content.text.decode("utf-8")
        text = node.text.decode("utf-8")
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            return text[1:-1]
        return None

    def _routes(self, decorated: Node, artifact: Artifact, module: str, fact) -> None:
        for child in decorated.children:
            if child.type != "decorator":
                continue
            text = child.text.decode("utf-8")
            m = _ROUTE_VERB.search(text)
            if m:
                self._route_fact(fact, artifact, decorated, m.group(2).upper(), m.group(3),
                                 self._resolve_ref(m.group(1), module))
                continue
            m = _ROUTE_FLASK.search(text)
            if m:
                methods_match = _FLASK_METHODS.search(m.group(3))
                methods = ([s.strip().strip("'\"").upper() for s in methods_match.group(1).split(",") if s.strip()]
                           if methods_match else ["GET"])
                for method in methods:
                    self._route_fact(fact, artifact, decorated, method, m.group(2),
                                     self._resolve_ref(m.group(1), module))

    def _route_fact(self, fact, artifact: Artifact, decorated: Node, method: str, route: str,
                    router: str) -> None:
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
            # dotted ref of the decorated object; Normalize resolves it to a router
            # and prepends that router's composed prefixes (APIRouter/include_router)
            "router": router,
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
