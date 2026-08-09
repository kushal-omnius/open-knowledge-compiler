"""Jira collector (pipeline.md §3.1 Collect stage): fetches issues linked from
merged PRs' titles/bodies, producing `jira_observed` facts (ir.md §2.3).

Scope: not all-of-Jira ingestion — only issues reachable via a PR's linked
issue keys (extracted from `pr_observed.linked_issue_keys`) are fetched,
matching the PR-scoped Collect model already used for forge PR facts. Opt-in
via `kc.toml` `[jira] enabled = true` (ADR-007: activation is explicit).

Two gateway backends, selected by `[jira] source` (ADR-021):
- `"rest"` (default): live Atlassian Cloud REST API v3, HTTP Basic auth.
  Configuration (never hardcoded): JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
  from the environment. The only backend usable from an unattended CI compile
  (ADR-002) — it needs no interactive session.
- `"file"`: reads a pre-fetched JSON cache (`[jira] cache_file`) instead of
  calling the live API. For interactive compiles where an agent has its own
  access to Jira (e.g. an Atlassian MCP connector) but no Jira API token is
  configured — the agent fetches the PR-linked issue keys itself and writes
  the cache; KC never talks to that access path directly (ADR-021).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str
    status: str
    # Freeform text; stands in for ir.md's "acceptance criteria" slot — Jira
    # Cloud has no universal AC field, description is the closest generally
    # available source.
    description: str = ""
    issue_type: str = ""


@runtime_checkable
class JiraGateway(Protocol):
    def get_issues(self, keys: list[str]) -> list[JiraIssue]:
        """Fetch issues by key. A key with no matching issue (deleted, wrong
        project, typo in a PR title) is silently omitted from the result —
        absence of one key is data, not a collector outage; only a *source*
        that can't be reached at all (bad credentials, network) fails loud
        (ADR-007)."""
        ...


class JiraError(Exception):
    pass


class AtlassianJiraGateway:
    """Reference implementation (REST, stdlib urllib — no extra dependency)."""

    def __init__(self) -> None:
        self.base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL")
        token = os.environ.get("JIRA_API_TOKEN")
        if not (self.base and email and token):
            raise JiraError(
                "jira collector needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN "
                "in the environment")
        auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
        self._auth_header = f"Basic {auth}"

    def _get(self, path: str) -> dict | None:
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": self._auth_header,
                     "Accept": "application/json",
                     "User-Agent": "knowledge-compiler"})
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # unknown key — data, not an outage
            raise JiraError(f"Jira API {path} failed: {exc}") from exc
        except Exception as exc:
            raise JiraError(f"Jira API {path} failed: {exc}") from exc

    def get_issues(self, keys: list[str]) -> list[JiraIssue]:
        issues: list[JiraIssue] = []
        for key in sorted(set(keys)):
            data = self._get(f"/rest/api/3/issue/{key}"
                             f"?fields=summary,status,description,issuetype")
            if data is None:
                continue
            fields = data.get("fields", {})
            issues.append(JiraIssue(
                key=data["key"],
                summary=fields.get("summary") or "",
                status=(fields.get("status") or {}).get("name", ""),
                description=_render_description(fields.get("description")),
                issue_type=(fields.get("issuetype") or {}).get("name", ""),
            ))
        return issues


def _render_description(desc: object) -> str:
    """Jira Cloud descriptions are Atlassian Document Format (nested JSON), not
    plain text. Extracts text nodes only — good enough for a fact payload, not
    a full ADF renderer."""
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    parts: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(desc)
    return " ".join(parts)


@dataclass
class FileJiraGateway:
    """Reads a pre-fetched JSON cache instead of calling the live API (ADR-021).

    Cache shape: `{"<KEY>": {"summary": ..., "status": ..., "description": ...,
    "issue_type": ...}, ...}` — one object per issue, fields matching
    `JiraIssue`'s own (all but `key` optional). Same "absence is data, not an
    outage" contract as `AtlassianJiraGateway.get_issues`: a key missing from
    the cache is silently omitted, not an error — indistinguishable from a key
    that genuinely doesn't exist in Jira, which is the existing, accepted
    behavior this backend inherits rather than a new gap it introduces.
    """

    cache_path: Path

    def get_issues(self, keys: list[str]) -> list[JiraIssue]:
        try:
            raw = self.cache_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JiraError(f"jira cache file '{self.cache_path}' unreadable: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JiraError(f"jira cache file '{self.cache_path}' is not valid JSON: {exc}") from exc
        return [JiraIssue(key=k, **data[k]) for k in sorted(set(keys)) if k in data]


@dataclass
class FakeJira:
    """In-memory gateway for tests and offline development."""

    issues: dict[str, JiraIssue] = field(default_factory=dict)

    def get_issues(self, keys: list[str]) -> list[JiraIssue]:
        return [self.issues[k] for k in sorted(set(keys)) if k in self.issues]


def build_jira_gateway(jira_cfg: dict) -> JiraGateway | None:
    """Factory (ADR-007): explicit opt-in only. Returns None when disabled —
    callers must not construct a gateway for a repo that hasn't turned this on.

    `source` (ADR-021): "rest" (default, backward-compatible with every
    existing `kc.toml` that predates this key) or "file". Any other value
    fails loud — a typo silently falling back to REST, or silently reading a
    stale file cache, would be a worse failure mode than an explicit error at
    startup (ADR-007's fail-loud posture)."""
    if not jira_cfg.get("enabled", False):
        return None
    source = jira_cfg.get("source", "rest")
    if source == "rest":
        return AtlassianJiraGateway()
    if source == "file":
        cache_file = jira_cfg.get("cache_file", "jira-cache.json")
        return FileJiraGateway(cache_path=Path(cache_file))
    raise JiraError(f"[jira] source '{source}' is not recognized (expected 'rest' or 'file')")
