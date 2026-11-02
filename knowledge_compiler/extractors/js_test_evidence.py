"""Jest test evidence beyond imports, shared by the JavaScript and TypeScript
analyzers (QA-signal review finding 2).

For each `test(...)` / `it(...)` (including `.only` / `.skip` and calls nested
in `describe` blocks), the callback body is scanned for:

- `call` evidence: a call to a name the file imported (ESM or CommonJS), such
  as `applyDiscount(50)` or `rules.applyDiscount(50)`. It resolves to the
  imported module through the same aliases router refs use. Rendering an
  imported component, `<BundleEditor />` or `<Ns.Panel />`, is a call too: that
  is how a component test exercises it (frida dogfood finding). Lowercase tags
  are intrinsic elements and never count.
- `http` evidence: a test-client request with a literal path, such as
  supertest's `request(app).get('/api/items/1')`.

Calls in `beforeEach`/`beforeAll` setup and in local helpers are not test
bodies, so they never count, which mirrors the Python analyzer's
fixture-exclusion rule.

Known limit: setup done inside a test body still counts. Priming a store with
`usePermissionsStore.setState({...})` is a call to `permissionsStore`, though
the test exercises something else; telling setup from exercise would need more
than the call's syntax, so it is not attempted.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from knowledge_compiler.extractors.express_routing import _string, resolve_ref

_TEST_FNS = frozenset({"test", "it"})
_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
_FUNCTION_TYPES = frozenset({"arrow_function", "function_expression", "function"})
# the tag-bearing JSX nodes (a closing tag repeats its opening tag, so it is skipped)
_JSX_TAGS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})


def _test_name(call: Node) -> str | None:
    fn, args = call.child_by_field_name("function"), call.child_by_field_name("arguments")
    if fn is None or args is None:
        return None
    if fn.type == "member_expression":
        fn = fn.child_by_field_name("object")
    if fn is None or fn.type != "identifier" or fn.text.decode("utf-8") not in _TEST_FNS:
        return None
    first = next((c for c in args.children if c.type == "string"), None)
    return _string(first) if first is not None else None


def test_evidence(root: Node, ref: str, module: str, aliases: dict[str, str],
                  fact: Callable[[str, dict], None]) -> None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            name = _test_name(node)
            if name is not None:
                args = node.child_by_field_name("arguments")
                body = next((c for c in reversed(args.children) if c.type in _FUNCTION_TYPES), None)
                if body is not None:
                    _scan(body, f"{ref}::{name}", module, aliases, fact)
                continue  # a test's own body is scanned above, not re-walked for nested tests
        stack.extend(node.children)


def _scan(body: Node, node_id: str, module: str, aliases: dict[str, str], fact) -> None:
    calls: set[str] = set()
    requests: set[tuple[str, str]] = set()
    stack = [body]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.type in _JSX_TAGS:
            tag = n.child_by_field_name("name")
            text = tag.text.decode("utf-8") if tag is not None else ""
            if text[:1].isupper() and text.split(".")[0] in aliases:
                calls.add(resolve_ref(text, module, aliases))
            continue
        if n.type != "call_expression":
            continue
        callee, args = n.child_by_field_name("function"), n.child_by_field_name("arguments")
        if callee is None or args is None:
            continue
        if callee.type == "member_expression":
            prop = callee.child_by_field_name("property")
            first = next((c for c in args.children if c.is_named), None)
            path = _string(first) if first is not None else None
            if prop is not None and prop.text.decode("utf-8") in _HTTP_VERBS \
                    and path is not None and path.startswith("/"):
                requests.add((prop.text.decode("utf-8").upper(), path))
                continue
        if callee.type in ("identifier", "member_expression"):
            text = callee.text.decode("utf-8")
            if text.split(".")[0] in aliases:
                calls.add(resolve_ref(text, module, aliases))
    test_module = module
    for target in sorted(calls):
        fact("test_target_observed", {"test_module": test_module, "target_path": target,
                                      "mechanism": "call", "file": node_id.split("::")[0],
                                      "node_id": node_id})
    for method, path in sorted(requests):
        fact("test_target_observed", {"test_module": test_module, "target_path": path,
                                      "mechanism": "http", "method": method,
                                      "file": node_id.split("::")[0], "node_id": node_id})
