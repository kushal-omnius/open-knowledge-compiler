"""Jira collector gateway tests (ADR-021): the source-selecting factory and
the file-cache backend. Pipeline-level jira_story minting is covered end to
end in test_incremental.py via FakeJira; this file is pure unit-level, no
database, no network.
"""

import json

import pytest

from knowledge_compiler.collectors.jira import (
    FileJiraGateway, JiraError, JiraIssue, build_jira_gateway,
)


def test_build_jira_gateway_disabled_returns_none():
    assert build_jira_gateway({"enabled": False}) is None
    assert build_jira_gateway({}) is None  # enabled defaults to False


def test_build_jira_gateway_defaults_to_rest(monkeypatch):
    """No `source` key at all -- every kc.toml that predates ADR-021 -- must
    keep resolving to the REST gateway, not silently change behavior."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    gateway = build_jira_gateway({"enabled": True})
    assert type(gateway).__name__ == "AtlassianJiraGateway"


def test_build_jira_gateway_explicit_rest_same_as_default(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    gateway = build_jira_gateway({"enabled": True, "source": "rest"})
    assert type(gateway).__name__ == "AtlassianJiraGateway"


def test_build_jira_gateway_file_source(tmp_path):
    cache = tmp_path / "jira-cache.json"
    cache.write_text("{}", encoding="utf-8")
    gateway = build_jira_gateway({"enabled": True, "source": "file",
                                  "cache_file": str(cache)})
    assert isinstance(gateway, FileJiraGateway)
    assert gateway.cache_path == cache


def test_build_jira_gateway_file_source_default_cache_name():
    gateway = build_jira_gateway({"enabled": True, "source": "file"})
    assert isinstance(gateway, FileJiraGateway)
    assert str(gateway.cache_path) == "jira-cache.json"


def test_build_jira_gateway_unrecognized_source_fails_loud():
    """A typo ('flie') must never silently fall back to REST (confusing
    missing-env-var error) or silently misread a stale file cache."""
    with pytest.raises(JiraError, match="not recognized"):
        build_jira_gateway({"enabled": True, "source": "flie"})


def test_file_jira_gateway_reads_cache(tmp_path):
    cache = tmp_path / "jira-cache.json"
    cache.write_text(json.dumps({
        "DCA-42": {"summary": "Cap discounts at 20%", "status": "Done",
                   "description": "Business rule", "issue_type": "Story"},
    }), encoding="utf-8")
    gateway = FileJiraGateway(cache_path=cache)
    issues = gateway.get_issues(["DCA-42", "DCA-99"])
    assert issues == [JiraIssue(key="DCA-42", summary="Cap discounts at 20%",
                                status="Done", description="Business rule",
                                issue_type="Story")]


def test_file_jira_gateway_missing_key_is_silently_omitted(tmp_path):
    """Same 'absence is data, not an outage' contract as the REST gateway --
    a key with no cache entry is indistinguishable from one that doesn't
    exist in Jira."""
    cache = tmp_path / "jira-cache.json"
    cache.write_text("{}", encoding="utf-8")
    gateway = FileJiraGateway(cache_path=cache)
    assert gateway.get_issues(["DCA-1"]) == []


def test_file_jira_gateway_missing_file_fails_loud(tmp_path):
    gateway = FileJiraGateway(cache_path=tmp_path / "does-not-exist.json")
    with pytest.raises(JiraError, match="unreadable"):
        gateway.get_issues(["DCA-1"])


def test_file_jira_gateway_invalid_json_fails_loud(tmp_path):
    cache = tmp_path / "jira-cache.json"
    cache.write_text("not json", encoding="utf-8")
    gateway = FileJiraGateway(cache_path=cache)
    with pytest.raises(JiraError, match="not valid JSON"):
        gateway.get_issues(["DCA-1"])
