"""Git collector (pipeline.md §3.1): stage the repository's files as artifacts.

V1 scope: full compilation only — reads the working tree and records the HEAD
commit (matches CI checkouts, where the tree IS the compiled commit). PR-scoped
collection (forge diff + source_change_observed facts) lands with incremental
compilation (phase 3).

Both read paths (the working tree for full compiles, git blobs for PR/commit
compiles) normalize CRLF to LF before hashing (pipeline.md §3.1). Under
`core.autocrlf=true` the working tree is CRLF and the blob is LF. Without the
normalization, an unchanged file got a different content_hash (and so a
different LLM cache key) depending on which path read it, and so did the
body_hash of any symbol whose string tokens span lines.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

from knowledge_compiler.ir import Artifact

# Binary sniff: NUL byte in the head of the file (git's own heuristic).
_SNIFF_BYTES = 8192


def _normalize_eol(raw: bytes) -> bytes:
    """CRLF -> LF: the conversion git applies to text files on commit. Lone CRs
    are kept, as git keeps them."""
    return raw.replace(b"\r\n", b"\n")


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

    def head_commit_timestamp(self) -> datetime:
        """Committer timestamp of HEAD (timezone-aware)."""
        return datetime.fromisoformat(_git(self.repo_path, "log", "-1", "--format=%cI", "HEAD").strip())

    def upstream_of(self, branch: str) -> str | None:
        """The remote-tracking ref `branch` is configured to track (e.g.
        `origin/main`), or None when the branch doesn't exist locally or has no
        upstream configured — resolved through git's own config rather than
        assuming a remote named `origin`."""
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "rev-parse", "--abbrev-ref", "--verify",
             "--quiet", f"{branch}@{{upstream}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        upstream = result.stdout.strip()
        return upstream if result.returncode == 0 and upstream else None

    def is_ancestor(self, commit: str, of: str) -> bool:
        """True when `commit` is reachable from `of` (or is `of` itself)."""
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "merge-base", "--is-ancestor", commit, of],
            capture_output=True)
        return result.returncode == 0

    def files_at_commit(self, commit_sha: str) -> list[str]:
        """Every tracked path in the commit's tree, sorted."""
        listing = _git(self.repo_path, "ls-tree", "-r", "--name-only", "-z", commit_sha)
        return sorted(p for p in listing.split("\0") if p)

    def has_commit(self, commit_sha: str) -> bool:
        """True when the commit object exists in the local clone. A forge can list a
        commit (PR merge, direct push) that was never fetched here."""
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
            capture_output=True)
        return result.returncode == 0

    def collect_at_commit(self, commit_sha: str, paths: list[str]) -> list[Artifact]:
        """Stage specific files as they exist at a commit (PR-scoped collection).
        Paths absent at the commit (removed by the PR) are skipped — their absence
        is scope evidence, not an artifact. Any other read failure (notably a commit
        missing from the local clone) raises GitCollectorError: treating it as
        "absent" would make Diff remove every entity anchored to the in-scope files."""
        if paths and not self.has_commit(commit_sha):
            raise GitCollectorError(
                f"commit {commit_sha} is not in the local clone at {self.repo_path}")
        artifacts: list[Artifact] = []
        for rel_path in sorted(set(paths)):
            result = subprocess.run(
                ["git", "-C", str(self.repo_path), "cat-file", "-p", f"{commit_sha}:{rel_path}"],
                capture_output=True)
            if result.returncode != 0:
                if self._absent_or_gitlink(commit_sha, rel_path, result.stderr):
                    continue  # absent at this commit, or a submodule (not a blob)
            raw = result.stdout
            if b"\0" in raw[:_SNIFF_BYTES]:
                continue
            raw = _normalize_eol(raw)
            artifacts.append(Artifact(
                artifact_type="source_file", source_ref=rel_path.replace("\\", "/"),
                content_hash=hashlib.sha256(raw).hexdigest(),
                content=raw.decode("utf-8", errors="replace")))
        return artifacts

    def _absent_or_gitlink(self, commit_sha: str, rel_path: str, cat_file_stderr: bytes) -> bool:
        """After a failed `cat-file`, confirm via `ls-tree` (not localized stderr) that
        the path is genuinely absent at the commit, or a gitlink (mode 160000, type
        `commit`). Anything else is a real read failure."""
        listing = subprocess.run(
            ["git", "-C", str(self.repo_path), "ls-tree", "-z", commit_sha, "--", rel_path],
            capture_output=True)
        if listing.returncode == 0:
            if not listing.stdout:
                return True
            meta = listing.stdout.split(b"	", 1)[0].split()
            if len(meta) >= 2 and meta[1] == b"commit":
                return True
        raise GitCollectorError(
            f"cannot read {rel_path} at commit {commit_sha} in {self.repo_path}: "
            f"{(listing.stderr or cat_file_stderr).decode('utf-8', errors='replace').strip()}")

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
            raw = _normalize_eol(raw)
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
