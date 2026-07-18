"""GitHub branch publisher (ADR-010): ship the wiki publication to knowledge/wiki.

Pure git plumbing — builds the commit off to the side (temp index + commit-tree)
and only moves the branch ref. The user's working tree, index, and checked-out
branch are never touched. Loop-safe structurally: a push to a non-default branch
is not the ADR-002 trigger event ([skip ci] is added as defense in depth only).

Snapshot semantics: each publish commits the ENTIRE publication tree as the
branch's new state; the branch is publisher-owned and force-pushed (the delta
log is the history of record — ADR-003).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_BOT_NAME = "knowledge-compiler"
_BOT_EMAIL = "knowledge-compiler@noreply.local"


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class PublishResult:
    committed: bool          # False when the tree is identical (converged — no commit)
    commit_sha: str | None
    pushed: bool


@dataclass(frozen=True)
class PublisherConfig:
    enabled: bool = False    # explicit opt-in (kc.toml [publisher]); local render always works
    branch: str = "knowledge/wiki"
    remote: str = "origin"
    push: bool = True


class GitBranchPublisher:
    """Publisher plugin (built-in, reference destination per ADR-010)."""

    def __init__(self, repo_dir: Path, config: PublisherConfig) -> None:
        self.repo_dir = repo_dir
        self.config = config

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        full_env = {**os.environ, **(env or {})}
        result = subprocess.run(["git", "-C", str(self.repo_dir), *args],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=full_env)
        if result.returncode != 0:
            raise PublishError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def publish(self, publication_dir: Path, message: str) -> PublishResult:
        if not publication_dir.is_dir():
            raise PublishError(f"publication directory missing: {publication_dir}")
        ref = f"refs/heads/{self.config.branch}"

        # 1. Build a tree object from the publication dir via a throwaway index.
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._git("--work-tree", str(publication_dir), "add", "-A", env=env)
            tree = self._git("write-tree", env=env)

        # 2. Converged? Identical tree => no commit (nightly no-change runs stay silent).
        parent = None
        try:
            parent = self._git("rev-parse", "--verify", "--quiet", ref)
        except PublishError:
            pass  # first publish: orphan commit
        if parent and self._git("rev-parse", f"{parent}^{{tree}}") == tree:
            return PublishResult(committed=False, commit_sha=parent,
                                 pushed=self._maybe_push(force=False))

        # 3. Commit off to the side; bot identity unless git config provides one.
        commit_env = {
            "GIT_AUTHOR_NAME": _BOT_NAME, "GIT_AUTHOR_EMAIL": _BOT_EMAIL,
            "GIT_COMMITTER_NAME": _BOT_NAME, "GIT_COMMITTER_EMAIL": _BOT_EMAIL,
        }
        args = ["commit-tree", tree, "-m", f"{message} [skip ci]"]
        if parent:
            args += ["-p", parent]
        sha = self._git(*args, env=commit_env)

        # 4. Move only the branch ref — the checked-out branch is untouched.
        self._git("update-ref", ref, sha)

        return PublishResult(committed=True, commit_sha=sha,
                             pushed=self._maybe_push(force=True))

    def _maybe_push(self, force: bool) -> bool:
        if not self.config.push:
            return False
        # publisher-owned branch (ADR-010): force resolves any divergence in our favor
        args = ["push", "--quiet"] + (["--force"] if force else []) + \
               [self.config.remote, f"refs/heads/{self.config.branch}"]
        self._git(*args)
        return True
