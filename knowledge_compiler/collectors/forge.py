"""Forge gateway (ADR-002): merged-PR listing and PR file diffs from the forge API.

PR association comes from the forge, never commit parentage — squash/rebase safe
(pipeline.md §3.1). The gateway is an interface; GitHub is the reference
implementation. Tests use an in-memory fake — the reconciliation semantics are
forge-independent.

Configuration (never hardcoded): token from KC_GITHUB_TOKEN or GITHUB_TOKEN;
API base from KC_GITHUB_API (default https://api.github.com, override for GHE).
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PRFile:
    path: str
    change: str                 # added | modified | removed | renamed
    old_path: str | None = None  # renamed only


@dataclass(frozen=True)
class MergedPR:
    number: int
    title: str
    merged_at: datetime
    merge_commit_sha: str
    body: str = ""
    files: tuple[PRFile, ...] = ()


@dataclass(frozen=True)
class CommitInfo:
    """A commit on the default branch not necessarily associated with a PR.
    Used by the commit-fill pass in reconcile (BRAINSTORM-commit-reconcile.md Option C):
    direct pushes, squash commits, and repos without a PR workflow."""
    sha: str
    timestamp: datetime
    message: str
    files: tuple[str, ...]  # paths present after the commit (added + modified; not removed)


@runtime_checkable
class ForgeGateway(Protocol):
    def list_merged_prs(self, base_branch: str, since: datetime | None) -> list[MergedPR]:
        """Merged PRs into base_branch after `since`, ascending by merged_at."""
        ...

    def list_commits(self, base_branch: str, since: datetime | None) -> list[CommitInfo]:
        """Commits on base_branch after `since`, ascending by timestamp.
        Returns [] if the forge does not support commit listing — callers must
        handle an empty list gracefully (commit-fill pass is best-effort)."""
        ...


class ForgeError(Exception):
    pass


class GitHubGateway:
    """Reference implementation (REST, stdlib urllib — no extra dependency)."""

    def __init__(self, owner: str, repo: str) -> None:
        self.owner, self.repo = owner, repo
        self.base = os.environ.get("KC_GITHUB_API", "https://api.github.com").rstrip("/")
        self.token = os.environ.get("KC_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise ForgeError("no forge token: set KC_GITHUB_TOKEN (or GITHUB_TOKEN)")

    def _get(self, path: str) -> object:
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "knowledge-compiler"})
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise ForgeError(f"GitHub API {path} failed: {exc}") from exc

    def list_merged_prs(self, base_branch: str, since: datetime | None) -> list[MergedPR]:
        prs: list[MergedPR] = []
        page = 1
        while page <= 10:  # bounded: 1000 PRs per reconcile is far beyond any real backlog
            data = self._get(f"/repos/{self.owner}/{self.repo}/pulls"
                             f"?state=closed&base={base_branch}&sort=updated"
                             f"&direction=desc&per_page=100&page={page}")
            if not data:
                break
            exhausted = False
            for item in data:
                if not item.get("merged_at"):
                    continue
                merged_at = datetime.fromisoformat(item["merged_at"].replace("Z", "+00:00"))
                if since is not None and merged_at <= since:
                    exhausted = True
                    continue
                prs.append(MergedPR(number=item["number"], title=item["title"],
                                    merged_at=merged_at,
                                    merge_commit_sha=item["merge_commit_sha"],
                                    body=item.get("body") or "",
                                    files=self._pr_files(item["number"])))
            if exhausted:
                break
            page += 1
        return sorted(prs, key=lambda p: (p.merged_at, p.number))

    def _pr_files(self, number: int) -> tuple[PRFile, ...]:
        data = self._get(f"/repos/{self.owner}/{self.repo}/pulls/{number}/files?per_page=100")
        files = []
        for f in data:
            files.append(PRFile(path=f["filename"], change=f["status"],
                                old_path=f.get("previous_filename")))
        return tuple(files)

    def list_commits(self, base_branch: str, since: datetime | None) -> list[CommitInfo]:
        """Commits on base_branch after `since`.
        GitHub returns newest-first from /commits; we reverse to ascending order."""
        commits: list[CommitInfo] = []
        page = 1
        since_str = since.isoformat().replace("+00:00", "Z") if since else None
        while page <= 10:
            qs = f"sha={base_branch}&per_page=100&page={page}"
            if since_str:
                qs += f"&since={since_str}"
            data = self._get(f"/repos/{self.owner}/{self.repo}/commits?{qs}")
            if not data:
                break
            for item in data:
                sha = item["sha"]
                ts_str = item["commit"]["committer"]["date"]
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                message = item["commit"]["message"]
                # fetch changed files for this commit
                detail = self._get(f"/repos/{self.owner}/{self.repo}/commits/{sha}")
                files = tuple(
                    f["filename"] for f in (detail.get("files") or [])
                    if f.get("status") != "removed"
                )
                commits.append(CommitInfo(sha=sha, timestamp=timestamp,
                                          message=message, files=files))
            page += 1
        return sorted(commits, key=lambda c: (c.timestamp, c.sha))


@dataclass
class FakeForge:
    """In-memory gateway for tests and offline development."""

    prs: list[MergedPR] = field(default_factory=list)
    commits: list[CommitInfo] = field(default_factory=list)

    def list_merged_prs(self, base_branch: str, since: datetime | None) -> list[MergedPR]:
        hits = [p for p in self.prs if since is None or p.merged_at > since]
        return sorted(hits, key=lambda p: (p.merged_at, p.number))

    def list_commits(self, base_branch: str, since: datetime | None) -> list[CommitInfo]:
        hits = [c for c in self.commits if since is None or c.timestamp > since]
        return sorted(hits, key=lambda c: (c.timestamp, c.sha))
