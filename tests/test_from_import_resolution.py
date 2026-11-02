"""Calls through `from package import module` reach the module (dogfood finding
on frida: `from storage import blob_utils` + `blob_utils.read_bytes(...)` under
the `backend/` import root arrived as `storage.blob_utils.read_bytes`, matched
no component, and left the test linked only weakly to the `storage` package).

kc-covers:
  - component/knowledge-compiler-compiler-normalize

Real tree-sitter Python analyzer and the real Normalize over fixture sources.
"""

import pytest

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402

LAYOUT = {
    # import root is backend/, so components are backend.* but imports are bare
    "backend/storage/__init__.py": "def storage_root():\n    return '/data'\n",
    "backend/storage/blob_utils.py": "def read_bytes(path):\n    return path.encode()\n",
    "backend/storage/other.py": "def unused():\n    return 1\n",
    "backend/tests/test_blob.py": (
        "from storage import blob_utils\nfrom storage import storage_root\n\n"
        "def test_read():\n    assert blob_utils.read_bytes('x') == b'x'\n\n"
        "def test_root():\n    assert storage_root() == '/data'\n"),
}


def _art(ref: str, src: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": src}), content=src)


def _coverage(files: dict[str, str]) -> dict[str, dict[str, str]]:
    """test node_id -> {covered component slug: mechanism}."""
    facts = PythonAnalyzer().analyze([_art(r, s) for r, s in files.items()])
    candidate = normalize(facts, CurrentState(), Thresholds(), "t")
    return {e.payload["node_id"]: {c["component"]: c["mechanism"] for c in e.payload["coverage"]}
            for e in candidate.entities if e.entity_type == "test_coverage"}


def test_call_through_imported_module_covers_that_module():
    cov = _coverage(LAYOUT)["backend/tests/test_blob.py::test_read"]
    assert cov.get("component/backend-storage-blob-utils") == "call"
    assert "component/backend-storage-other" not in cov


def test_call_to_a_function_imported_from_a_package_covers_the_package():
    cov = _coverage(LAYOUT)["backend/tests/test_blob.py::test_root"]
    assert cov.get("component/backend-storage") == "call"


def test_ambiguous_module_prefix_stays_unresolved_instead_of_falling_back():
    files = {
        "a/storage/__init__.py": "",
        "a/storage/blob_utils.py": "def read_bytes(path):\n    return b''\n",
        "b/storage/__init__.py": "",
        "b/storage/blob_utils.py": "def read_bytes(path):\n    return b''\n",
        "tests/test_blob.py": (
            "from storage import blob_utils\n\n"
            "def test_read():\n    assert blob_utils.read_bytes('x') == b''\n"),
    }
    cov = _coverage(files)["tests/test_blob.py::test_read"]
    # `storage.blob_utils` matches two modules: no call link, and no fallback to a
    # coarser `storage` package either (that is ambiguous too)
    assert not any(mech == "call" for mech in cov.values())
