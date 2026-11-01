"""Express router prefixes, shared by the JavaScript and TypeScript analyzers.

`app.use('/api', router)` mounts a router under a prefix, and the router's own
`router.get('/items')` is then served at `/api/items`. Before this, the
analyzers recorded only the `/items` part.

Facts stay per-file (same contract as the Python analyzer's FastAPI support):

- `router_observed`: `const r = express.Router()` / `const app = express()`.
  Express routers carry no prefix of their own, so prefix is always "".
- `router_mount_observed`: each `<parent>.use(['/prefix',] ..., child)`. Every
  identifier or member argument is recorded as a possible child, because Express
  can't tell middleware from routers syntactically. A child that is not a router
  with routes contributes nothing in Normalize.
- Export aliases (`export default router`, `module.exports = router`) are
  recorded as a mount of `<module>.router` under `<module>.default` with an
  empty prefix. That lets an importer's `import users from './users'` (which
  names `users.default`) reach the router the routes were registered on.

Router refs are absolute dotted paths: `<module>.<name>`, with imported names
resolved through the file's ESM imports and CommonJS requires. Normalize
composes them into full routes.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

_ROUTER_CTORS = frozenset({"Router", "express"})


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


def _string(node: Node) -> str | None:
    if node.type == "string":
        frag = next((c for c in node.children if c.type == "string_fragment"), None)
        return _text(frag) if frag is not None else ""
    if node.type == "template_string" and not any(c.type == "template_substitution"
                                                  for c in node.children):
        return _text(node).strip("`")
    return None


def _require_spec(node: Node) -> str | None:
    if node.type != "call_expression":
        return None
    fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    if fn is None or args is None or _text(fn) != "require":
        return None
    first = next((c for c in args.children if c.is_named), None)
    return _string(first) if first is not None else None


def import_aliases(root: Node, resolve: Callable[[str], str]) -> dict[str, str]:
    """Local name -> absolute dotted ref, from ESM imports and CJS requires."""
    aliases: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            spec = _string(source) if source is not None else None
            if spec is None:
                continue
            target = resolve(spec)
            clause = next((c for c in node.children if c.type == "import_clause"), None)
            for part in clause.children if clause is not None else ():
                if part.type == "identifier":
                    aliases[_text(part)] = f"{target}.default"
                elif part.type == "namespace_import":
                    name = next((c for c in part.children if c.type == "identifier"), None)
                    if name is not None:
                        aliases[_text(name)] = target
                elif part.type == "named_imports":
                    for spec_node in (c for c in part.children if c.type == "import_specifier"):
                        name = spec_node.child_by_field_name("name")
                        alias = spec_node.child_by_field_name("alias") or name
                        if name is not None and alias is not None:
                            aliases[_text(alias)] = f"{target}.{_text(name)}"
            continue
        if node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            spec = _require_spec(value) if value is not None else None
            if name is not None and spec is not None:
                target = resolve(spec)
                if name.type == "identifier":
                    aliases[_text(name)] = f"{target}.default"
                elif name.type == "object_pattern":
                    for prop in name.children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            aliases[_text(prop)] = f"{target}.{_text(prop)}"
                        elif prop.type == "pair_pattern":
                            key, val = prop.child_by_field_name("key"), prop.child_by_field_name("value")
                            if key is not None and val is not None and val.type == "identifier":
                                aliases[_text(val)] = f"{target}.{_text(key)}"
                continue
        stack.extend(node.children)
    return aliases


def resolve_ref(expr: str, module: str, aliases: dict[str, str]) -> str:
    head, _, rest = expr.partition(".")
    base = aliases.get(head)
    if base is None:
        return f"{module}.{expr}"
    return f"{base}.{rest}" if rest else base


def routing_facts(root: Node, module: str, ref: str, aliases: dict[str, str],
                  fact: Callable[[str, dict], None]) -> None:
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        t = node.type
        if t == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier" \
                    or value.type != "call_expression":
                continue
            fn = value.child_by_field_name("function")
            if fn is not None and _text(fn).rsplit(".", 1)[-1] in _ROUTER_CTORS:
                fact("router_observed", {"router": f"{module}.{_text(name)}", "prefix": "",
                                         "file": ref})
        elif t == "call_expression":
            fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
            if fn is None or args is None or fn.type != "member_expression":
                continue
            prop, obj = fn.child_by_field_name("property"), fn.child_by_field_name("object")
            if prop is None or obj is None or _text(prop) != "use":
                continue
            named = [c for c in args.children if c.is_named]
            prefix = ""
            if named and _string(named[0]) is not None:
                prefix = _string(named[0]) or ""
                named = named[1:]
            parent = resolve_ref(_text(obj), module, aliases)
            for child in named:
                if child.type in ("identifier", "member_expression"):
                    fact("router_mount_observed", {
                        "parent": parent, "child": resolve_ref(_text(child), module, aliases),
                        "prefix": prefix, "file": ref,
                    })
        elif t == "export_statement" and any(c.type == "default" for c in node.children):
            value = next((c for c in node.children if c.type == "identifier"), None)
            if value is not None:
                _export_alias(fact, module, resolve_ref(_text(value), module, aliases), ref)
        elif t == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None and _text(left) == "module.exports" \
                    and right.type == "identifier":
                _export_alias(fact, module, resolve_ref(_text(right), module, aliases), ref)


def _export_alias(fact, module: str, target: str, ref: str) -> None:
    fact("router_mount_observed", {"parent": f"{module}.default", "child": target,
                                   "prefix": "", "file": ref})
