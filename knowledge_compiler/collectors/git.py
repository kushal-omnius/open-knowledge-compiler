"""Git collector (pipeline.md §3.1): stage the repository's files as artifacts.

V1 scope: full compilation only — reads the working tree and records the HEAD
commit (matches CI checkouts, where the tree IS the compiled commit). PR-scoped
collection (forge diff + source_change_observed facts) lands with incremental
compilation (phase 3).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from knowledge_compiler.ir import Artifact

# Binary sniff: NUL byte in the head of the file (git's own heuristic).
_SNIFF_BYTES = 8192


class GitCollectorError(Exception):
    pass


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise GitCollectorError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


class GitCollector:
    """Stages tracked, text files at HEAD. Fails loud on anything that isn't a git repo."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    def head_commit(self) -> str:
        return _git(self.repo_path, "rev-parse", "HEAD").strip()

    def collect_at_commit(self, commit_sha: str, paths: list[str]) -> list[Artifact]:
        """Stage specific files as they exist at a commit (PR-scoped collection).
        Paths absent at the commit (removed by the PR) are skipped — their absence
        is scope evidence, not an artifact."""
        artifacts: list[Artifact] = []
        for rel_path in sorted(set(paths)):
            result = subprocess.run(
                ["git", "-C", str(self.repo_path), "cat-file", "-p", f"{commit_sha}:{rel_path}"],
                capture_output=True)
            if result.returncode != 0:
                continue  # absent at this commit
            raw = result.stdout
            if b"\0" in raw[:_SNIFF_BYTES]:
                continue
            artifacts.append(Artifact(
                artifact_type="source_file", source_ref=rel_path.replace("\\", "/"),
                content_hash=hashlib.sha256(raw).hexdigest(),
                content=raw.decode("utf-8", errors="replace")))
        return artifacts

    def collect_full(self) -> list[Artifact]:
        tracked = _git(self.repo_path, "ls-files", "-z").split("\0")
        artifacts: list[Artifact] = []
        for rel_path in sorted(p for p in tracked if p):  # sorted: deterministic order
            file_path = self.repo_path / rel_path
            if not file_path.is_file():
                continue  # deleted-but-staged edge; absent from the tree
            raw = file_path.read_bytes()
            if b"\0" in raw[:_SNIFF_BYTES]:
                continue  # binary: not an engineering-knowledge artifact
            text = raw.decode("utf-8", errors="replace")
            artifacts.append(
                Artifact(
                    artifact_type="source_file",
                    source_ref=rel_path.replace("\\", "/"),
                    content_hash=hashlib.sha256(raw).hexdigest(),
                    content=text,
                )
            )
        return artifacts
