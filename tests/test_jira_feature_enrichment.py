"""Jira→Feature enrichment pass observability (run.py _jira_feature_enrichment_facts):
progress lines on cache misses, failed issues surfaced as a compile warning,
and its calls counted in the run's llm totals and the per-run budget (ADR-008).

kc-covers:
  - component/knowledge-compiler-compiler-run
"""

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from knowledge_compiler.llm.provider import FakeLLMProvider
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable (docker compose up -d) — integration skipped")


FEATURE_OUTPUT = {
    "business_rules": [],
    "features": [{
        "name": "Discount capping",
        "narrative": "Customers never receive more than a 20 percent discount.",
        "symbol_paths": ["billing.rules.apply_discount"],
        "related_components": ["billing.rules"],
    }],
    "risks": [],
}
MATCH_OUTPUT = {"motivates": ["Discount capping"]}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class FlakyProvider(FakeLLMProvider):
    """Fails every call whose prompt mentions a key in `failing` — stands in for
    a provider whose SDK retries were exhausted (e.g. repeated HTTP 429)."""

    def __init__(self, failing: tuple[str, ...], **kwargs) -> None:
        super().__init__(**kwargs)
        self.failing = failing

    def complete(self, prompt, schema):
        if any(key in prompt for key in self.failing):
            self.calls += 1
            raise RuntimeError("Error code: 429 - Too Many Requests")
        return super().complete(prompt, schema)


@pytest.fixture()
def repo_env(tmp_path: Path):
    """LLM + file-backed Jira enabled; a full compile loads every cached issue."""
    from knowledge_compiler.compiler.bootstrap import init_repository

    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    (repo / "jira-cache.json").write_text(json.dumps({
        "DCA-42": {"summary": "Cap discounts at 20%", "status": "Done",
                   "description": "", "issue_type": "Story"},
        "DCA-99": {"summary": "Show discount on invoice", "status": "Done",
                   "description": "", "issue_type": "Story"},
    }), encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"jfe-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    config = (repo / "kc.toml").read_text(encoding="utf-8")
    config = config.replace('enabled = false\nprovider = "anthropic"',
                            'enabled = true\nprovider = "anthropic"')
    config = config.replace('enabled = false\n# source = "rest"',
                            'enabled = true\nsource = "file"\n# source = "rest"')
    assert config.count("enabled = true") == 2, "kc.toml template changed — fix the toggles above"
    (repo / "kc.toml").write_text(config, encoding="utf-8")
    return repo, slug


def _repo_lock_held(slug: str) -> bool:
    from knowledge_compiler.storage.db import repo_lock_key

    with kcdb.make_engine().connect() as conn:
        return bool(conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND objid = :k"), {"k": repo_lock_key(slug)}).scalar())


def _provider(cls=FakeLLMProvider, **kwargs):
    # unique model_id: llm_cache is repo-agnostic, a shared id would serve other tests' answers
    return cls(responses={"billing/rules.py": FEATURE_OUTPUT, "DCA-": MATCH_OUTPUT},
               model_id=f"fake-{uuid.uuid4().hex[:8]}", **kwargs)


def test_jira_calls_stream_progress_and_count_in_llm_totals(repo_env):
    from knowledge_compiler.compiler.run import compile_full

    repo, slug = repo_env
    provider = _provider()
    lines: list[tuple[str, int, int, str]] = []
    summary = compile_full(repo, llm_provider=provider,
                           progress=lambda *args: lines.append(args))

    # regression: the Jira pass's cache reads open a second pooled connection;
    # the repo lock must still be released, or the next compile here deadlocks
    assert not _repo_lock_held(slug)
    assert summary.warnings == []
    jira_lines = [ln for ln in lines if ln[0] == "llm" and ln[3].startswith("jira:")]
    assert [(i, n) for _, i, n, _ in jira_lines] == [(1, 2), (2, 2)]
    assert jira_lines[0][3].startswith("jira:DCA-42 --changed (tokens in=")
    # every real call is reported: file pass (rules.py + __init__.py) + both Jira issues
    assert summary.llm_calls == provider.calls == 4
    file_lines = [ln for ln in lines if ln[0] == "llm" and not ln[3].startswith("jira:")]
    assert len(file_lines) == 2
    assert summary.llm_output_tokens == 50 * 4  # FakeLLMProvider reports 50 out per call

    # cache hits are silent and cost nothing — same contract as the file pass
    lines.clear()
    second = compile_full(repo, llm_provider=provider, progress=lambda *args: lines.append(args))
    assert [ln for ln in lines if ln[0] == "llm"] == []
    assert second.llm_calls == 0 and provider.calls == 4


def test_failed_issue_is_warned_not_silently_dropped(repo_env):
    from knowledge_compiler.compiler.run import compile_full

    repo, _ = repo_env
    provider = _provider(FlakyProvider, failing=("DCA-99",))
    lines: list[tuple[str, int, int, str]] = []
    summary = compile_full(repo, llm_provider=provider,
                           progress=lambda *args: lines.append(args))

    # the compile still succeeds; the healthy issue's links are kept
    warning = [w for w in summary.warnings if "Jira→feature matching" in w]
    assert len(warning) == 1
    assert "1/2 issue(s)" in warning[0] and "DCA-99" in warning[0]
    assert "DCA-42" not in warning[0] and "429" in warning[0]
    assert ("llm", 2, 2, "jira:DCA-99 --failed (RuntimeError)") in lines
    assert summary.llm_calls == 4  # a failed call is still a call against the budget

    # the failure was not cached: a re-run retries only the failed issue
    healed = _provider()
    healed.model_id = provider.model_id
    again = compile_full(repo, llm_provider=healed)
    assert healed.calls == 1
    assert not [w for w in again.warnings if "Jira→feature matching" in w]


def test_jira_calls_count_against_max_calls_per_run(repo_env):
    from knowledge_compiler.compiler.run import CompileError, compile_full

    repo, _ = repo_env
    config = (repo / "kc.toml").read_text(encoding="utf-8")
    # the 2 file calls fit; the first Jira call does not
    (repo / "kc.toml").write_text(
        config.replace("max_calls_per_run = 200", "max_calls_per_run = 2"), encoding="utf-8")
    provider = _provider()
    with pytest.raises(CompileError, match="budget .* exhausted at jira:DCA-42"):
        compile_full(repo, llm_provider=provider)
    assert provider.calls == 2

    # resumable: the file call was cached eagerly, so a re-run spends the
    # budget only on the two Jira calls it didn't reach (pipeline.md §6.2)
    (repo / "kc.toml").write_text(
        config.replace("max_calls_per_run = 200", "max_calls_per_run = 2"), encoding="utf-8")
    summary = compile_full(repo, llm_provider=provider)
    assert summary.warnings == []
    assert provider.calls == 4 and summary.llm_calls == 2
