"""Jira collector (pipeline.md §3.1 Collect stage): fetches issues linked from
merged PRs' titles/bodies, producing `jira_observed` facts (ir.md §2.3).

Scope: not all-of-Jira ingestion — only issues reachable via a PR's linked
issue keys (extracted from `pr_observed.linked_issue_keys`) are fetched,
matching the PR-scoped Collect model already used for forge PR facts. Opt-in
via `kc.toml` `[jira] enabled = true` (ADR-007: activation is explicit).

Configuration (never hardcoded): JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
from the environment (Atlassian Cloud REST API v3, HTTP Basic auth).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
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
class FakeJira:
    """In-memory gateway for tests and offline development."""

    issues: dict[str, JiraIssue] = field(default_factory=dict)

    def get_issues(self, keys: list[str]) -> list[JiraIssue]:
        return [self.issues[k] for k in sorted(set(keys)) if k in self.issues]


def build_jira_gateway(jira_cfg: dict) -> JiraGateway | None:
    """Factory (ADR-007): explicit opt-in only. Returns None when disabled —
    callers must not construct a gateway for a repo that hasn't turned this on."""
    if not jira_cfg.get("enabled", False):
        return None
    return AtlassianJiraGateway()
