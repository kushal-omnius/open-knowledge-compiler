"""Tests for commit-fill reconcile (Option C, BRAINSTORM-commit-reconcile.md):
- FakeForge.list_commits dedup against PR SHAs
- Direct-commit runs produce scope='commit' with no pr_observed fact
- Jira issue keys extracted from commit messages
- Idempotence of direct-commit runs
- Jira→Feature normalisation: linked_feature_names in jira_story payload,
  motivates→Feature edges from _p5_relationships
"""

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.collectors.forge import (
    CommitInfo, FakeForge, MergedPR, PRFile,
)
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
    not _db_available(), reason="Postgres unreachable — integration skipped")

T0 = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


@pytest.fixture()
def env(tmp_path: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in [
        ("billing/__init__.py", ""),
        ("billing/rules.py", "def apply_discount(pct):\n    return min(pct, 20)\n"),
    ]:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"cr-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo)
    return repo, slug


def make_commit(repo: Path, path: str, content: str, message: str, ts_offset: int) -> CommitInfo:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-qm", message)
    sha = git(repo, "rev-parse", "HEAD")
    return CommitInfo(sha=sha, timestamp=T0 + timedelta(minutes=ts_offset),
                      message=message, files=(path,))


def current_slugs(slug: str) -> set[str]:
    from knowledge_compiler.storage.schema import EntityRow, Repository
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(
            select(Repository.id).where(Repository.slug == slug)).scalar_one()
        return set(session.execute(
            select(EntityRow.slug).where(EntityRow.repo_id == repo_id)).scalars())


def current_run_scopes(slug: str) -> list[str]:
    from knowledge_compiler.storage.schema import CompileRun, Repository
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(
            select(Repository.id).where(Repository.slug == slug)).scalar_one()
        return list(session.execute(
            select(CompileRun.scope).where(
                CompileRun.repo_id == repo_id,
                CompileRun.status == "succeeded").order_by(CompileRun.id)
        ).scalars())


def test_direct_commit_produces_commit_scope_run(env):
    from knowledge_compiler.compiler.run import reconcile

    repo, slug = env
    c = make_commit(repo, "billing/extra.py", "def bonus():\n    return 1\n",
                    "Add bonus function", ts_offset=1)
    forge = FakeForge(prs=[], commits=[c])
    summaries = reconcile(repo, forge)
    assert len(summaries) == 1
    assert summaries[0].pr_number is None
    assert "component/billing-extra" in current_slugs(slug)
    scopes = current_run_scopes(slug)
    assert "commit" in scopes


def test_direct_commit_idempotent(env):
    from knowledge_compiler.compiler.run import reconcile

    repo, slug = env
    c = make_commit(repo, "billing/extra.py", "def bonus():\n    return 1\n",
                    "Add bonus", ts_offset=1)
    forge = FakeForge(commits=[c])
    first = reconcile(repo, forge)
    assert len(first) == 1
    second = reconcile(repo, forge)
    assert second == []  # watermark advanced; no new commits


def test_commit_covered_by_pr_not_processed_twice(env):
    from knowledge_compiler.compiler.run import reconcile

    repo, slug = env
    # A PR that points at a commit SHA; the same SHA also appears in list_commits
    p = repo / "billing/extra.py"
    p.write_text("def extra():\n    return 2\n")
    git(repo, "add", "billing/extra.py")
    git(repo, "commit", "-qm", "Extra feature")
    sha = git(repo, "rev-parse", "HEAD")

    pr = MergedPR(number=42, title="Extra feature", merged_at=T0 + timedelta(minutes=1),
                  merge_commit_sha=sha,
                  files=(PRFile(path="billing/extra.py", change="added"),))
    # The same SHA also appears as a raw commit — should be deduped
    ci = CommitInfo(sha=sha, timestamp=T0 + timedelta(minutes=1),
                    message="Extra feature", files=("billing/extra.py",))

    forge = FakeForge(prs=[pr], commits=[ci])
    summaries = reconcile(repo, forge)
    # SHA covered by PR — only one compile run, not two
    assert len(summaries) == 1
    scopes = current_run_scopes(slug)
    # The PR path was taken (scope='pr'), commit path skipped
    assert scopes.count("pr") == 1
    assert "commit" not in scopes


def test_jira_key_extracted_from_commit_message(env):
    import json
    import re

    from knowledge_compiler.compiler.run import reconcile

    repo, slug = env
    c = make_commit(repo, "billing/extra.py", "def bonus():\n    return 1\n",
                    "DCA-9999: Add bonus function for discount rounding", ts_offset=1)

    # Enable file-based Jira with a cache containing DCA-9999
    cache = repo / "jira-cache.json"
    cache.write_text(json.dumps({
        "DCA-9999": {"summary": "Add bonus", "status": "Done",
                     "description": "", "issue_type": "Story"},
    }))

    # Patch the [jira] section in the bootstrapped kc.toml (which has it disabled)
    config_path = repo / "kc.toml"
    original = config_path.read_text()
    patched = re.sub(r"\[jira\].*?(?=\n\[|\Z)", "[jira]\nenabled = true\n"
                     "source = \"file\"\ncache_file = \"jira-cache.json\"\n",
                     original, flags=re.DOTALL)
    config_path.write_text(patched)

    forge = FakeForge(commits=[c])
    summaries = reconcile(repo, forge)
    assert len(summaries) == 1
    # Jira story should have been compiled from the commit message key
    slugs = current_slugs(slug)
    assert "jira-story/dca-9999" in slugs

    config_path.write_text(original)


# --- Unit tests for Jira→Feature normalisation (no DB) ----------------------------

def _entity_by_slug(candidate, slug):
    return next((e for e in candidate.entities if e.slug == slug), None)


def _has_rel(candidate, from_slug, rel, to_slug):
    return any(r.from_slug == from_slug and r.relation_type == rel and r.to_slug == to_slug
               for r in candidate.relationships)


def test_jira_feature_link_stored_in_jira_story_payload():
    from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
    from knowledge_compiler.ir import Anchor, Extraction, Fact, content_hash

    DET = Extraction(method="deterministic", extractor="test", extractor_version="0")
    LLM = Extraction(method="llm", extractor="llm-semantic", extractor_version="0.1",
                     model_id="test", template_version="1")
    LINK = Extraction(method="llm", extractor="jira-feature-match",
                      extractor_version="0.1", model_id="test", template_version="1")

    facts = [
        Fact(fact_type="jira_observed",
             payload={"key": "DCA-1", "summary": "Claim form", "status": "Done",
                      "description": "", "issue_type": "Story", "linked_pr": None},
             artifact_refs=("jira:DCA-1",), extraction=DET,
             content_hash=content_hash({"key": "DCA-1"})),
        Fact(fact_type="feature_candidate",
             payload={"name": "Claim Submission", "narrative": "Lets users submit claims.",
                      "related_components": [], "external_key": None},
             artifact_refs=("billing/rules.py",), extraction=LLM,
             content_hash=content_hash({"name": "Claim Submission"}),
             anchors=(Anchor(file_path="billing/rules.py"),)),
        Fact(fact_type="jira_feature_link_observed",
             payload={"jira_key": "DCA-1", "feature_names": ["Claim Submission"]},
             artifact_refs=("jira:DCA-1",), extraction=LINK,
             content_hash=content_hash({"jira_key": "DCA-1",
                                        "feature_names": ["Claim Submission"]})),
    ]
    candidate = normalize(facts, CurrentState(), Thresholds(), "test-repo")

    jira_entity = _entity_by_slug(candidate, "jira-story/dca-1")
    assert jira_entity is not None
    assert jira_entity.payload.get("linked_feature_names") == ["Claim Submission"]


def test_jira_story_motivates_feature_edge():
    from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
    from knowledge_compiler.ir import Anchor, Extraction, Fact, content_hash

    DET = Extraction(method="deterministic", extractor="test", extractor_version="0")
    LLM = Extraction(method="llm", extractor="llm-semantic", extractor_version="0.1",
                     model_id="test", template_version="1")
    LINK = Extraction(method="llm", extractor="jira-feature-match",
                      extractor_version="0.1", model_id="test", template_version="1")

    facts = [
        Fact(fact_type="jira_observed",
             payload={"key": "DCA-2", "summary": "Add chat", "status": "Done",
                      "description": "", "issue_type": "Story", "linked_pr": None},
             artifact_refs=("jira:DCA-2",), extraction=DET,
             content_hash=content_hash({"key": "DCA-2"})),
        Fact(fact_type="feature_candidate",
             payload={"name": "AI Chat Interface", "narrative": "Interactive claim chat.",
                      "related_components": [], "external_key": None},
             artifact_refs=("chat/api.py",), extraction=LLM,
             content_hash=content_hash({"name": "AI Chat Interface"}),
             anchors=(Anchor(file_path="chat/api.py"),)),
        Fact(fact_type="jira_feature_link_observed",
             payload={"jira_key": "DCA-2", "feature_names": ["AI Chat Interface"]},
             artifact_refs=("jira:DCA-2",), extraction=LINK,
             content_hash=content_hash({"jira_key": "DCA-2",
                                        "feature_names": ["AI Chat Interface"]})),
    ]
    candidate = normalize(facts, CurrentState(), Thresholds(), "test-repo")

    feature_entity = next((e for e in candidate.entities if e.entity_type == "feature"), None)
    assert feature_entity is not None, "feature entity not minted"
    assert _has_rel(candidate, "jira-story/dca-2", "motivates", feature_entity.slug)


def test_jira_feature_link_unknown_name_silently_dropped():
    """A feature name in jira_feature_link_observed that doesn't match any
    compiled feature is dropped without error — same 'absent = data' discipline
    as unresolvable step slugs in user journeys."""
    from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
    from knowledge_compiler.ir import Extraction, Fact, content_hash

    DET = Extraction(method="deterministic", extractor="test", extractor_version="0")
    LINK = Extraction(method="llm", extractor="jira-feature-match",
                      extractor_version="0.1", model_id="test", template_version="1")

    facts = [
        Fact(fact_type="jira_observed",
             payload={"key": "DCA-3", "summary": "Ghost feature", "status": "Open",
                      "description": "", "issue_type": "Story", "linked_pr": None},
             artifact_refs=("jira:DCA-3",), extraction=DET,
             content_hash=content_hash({"key": "DCA-3"})),
        Fact(fact_type="jira_feature_link_observed",
             payload={"jira_key": "DCA-3", "feature_names": ["NonexistentFeature"]},
             artifact_refs=("jira:DCA-3",), extraction=LINK,
             content_hash=content_hash({"jira_key": "DCA-3",
                                        "feature_names": ["NonexistentFeature"]})),
    ]
    candidate = normalize(facts, CurrentState(), Thresholds(), "test-repo")

    jira_entity = _entity_by_slug(candidate, "jira-story/dca-3")
    assert jira_entity is not None
    # linked_feature_names stored in payload...
    assert jira_entity.payload.get("linked_feature_names") == ["NonexistentFeature"]
    # ...but no motivates→feature edge (no feature entity compiled)
    assert not any(r.relation_type == "motivates" and r.from_slug == "jira-story/dca-3"
                   for r in candidate.relationships)
