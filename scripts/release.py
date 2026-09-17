"""Release automation for .github/workflows/release.yml: bump the version in
pyproject.toml/knowledge_compiler/__init__.py and date the CHANGELOG's
'## [Unreleased]' section, leaving a fresh empty one above it — the exact
transform CHANGELOG.md's own documented release process describes. The
workflow is the only intended caller; a maintainer can still do a release by
hand per that same documented process.

Usage: python scripts/release.py <patch|minor|major>
Prints the new version to stdout on success (the workflow reads it from
there); everything else goes to stderr via SystemExit.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT_PY = ROOT / "knowledge_compiler" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

_VERSION_RE = re.compile(r'^version = "([^"]+)"', re.MULTILINE)


def current_version() -> str:
    match = _VERSION_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"could not find a version = \"...\" line in {PYPROJECT}")
    return match.group(1)


def bump(version: str, kind: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"current version {version!r} is not MAJOR.MINOR.PATCH")
    major, minor, patch = (int(p) for p in parts)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unknown bump kind {kind!r} — expected patch, minor, or major")


def write_version(old: str, new: str) -> None:
    for path, needle in ((PYPROJECT, f'version = "{old}"'),
                        (INIT_PY, f'__version__ = "{old}"')):
        text = path.read_text(encoding="utf-8")
        if needle not in text:
            raise SystemExit(f"expected to find {needle!r} in {path}")
        path.write_text(text.replace(needle, needle.replace(old, new), 1), encoding="utf-8")


def date_changelog(new: str, today: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    marker = "## [Unreleased]\n"
    count = text.count(marker)
    if count == 0:
        raise SystemExit(f"no {marker!r} section found in {CHANGELOG}")
    if count > 1:
        raise SystemExit(f"expected exactly one {marker!r} section in {CHANGELOG}, found {count}")
    replacement = f"## [Unreleased]\n\n## [{new}] — {today}\n"
    CHANGELOG.write_text(text.replace(marker, replacement, 1), encoding="utf-8")


def release(kind: str) -> str:
    old = current_version()
    new = bump(old, kind)
    today = datetime.now(timezone.utc).date().isoformat()
    write_version(old, new)
    date_changelog(new, today)
    return new


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("patch", "minor", "major"):
        raise SystemExit("usage: python scripts/release.py <patch|minor|major>")
    print(release(sys.argv[1]))


if __name__ == "__main__":
    main()
