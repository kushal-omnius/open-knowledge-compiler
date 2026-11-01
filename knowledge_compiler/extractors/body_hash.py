"""Per-symbol `body_hash`: a fingerprint of a definition's code that ignores
comments and formatting.

Why: a component's content_hash is the hash of its payload, and before this the
`symbols` list held only symbol_path + kind. An edit that only touched a
function body (`min(pct, 20)` -> `min(pct, 30)`) produced an identical payload,
so no delta, no change detection and no stale-coverage signal. Putting a
body_hash on each symbol makes every logic edit visible, while comment and
whitespace edits stay invisible (so no false churn).

What gets hashed: a pre-order walk of the definition's tree-sitter subtree,
skipping comment nodes. Leaves contribute their source text. Named inner nodes
also contribute open/close markers with their node type. The markers matter for
Python, where indentation is structure but not a token:

    if x:              if x:
        a()                a()
        b()            b()

have identical leaf tokens but different trees. With the markers they hash
differently; whitespace that doesn't change the tree still hashes the same.

Format: the first 16 hex chars of sha256 over the token stream. That is enough
to tell apart revisions of one symbol, and short enough to keep component
payloads small.
"""

from __future__ import annotations

import hashlib

from tree_sitter import Node

# tree-sitter-python/-javascript/-typescript all call it "comment"; JS also
# parses legacy `<!--` / `-->` line comments as html_comment
_COMMENT_TYPES = frozenset({"comment", "html_comment"})
_SEP = b"\x1f"


def body_hash(node: Node) -> str:
    h = hashlib.sha256()
    stack: list[tuple[Node, bool]] = [(node, False)]
    while stack:
        current, closing = stack.pop()
        if closing:
            h.update(b")" + _SEP)
            continue
        if current.type in _COMMENT_TYPES:
            continue
        if current.child_count == 0:
            h.update(b"L" + current.text + _SEP)
            continue
        if current.is_named:
            h.update(b"(" + current.type.encode("utf-8") + _SEP)
            stack.append((current, True))
        stack.extend((c, False) for c in reversed(current.children))
    return h.hexdigest()[:16]
