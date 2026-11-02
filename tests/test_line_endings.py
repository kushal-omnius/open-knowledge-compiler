"""
Full and incremental compiles agree on a CRLF working tree (core.autocrlf=true).

kc-covers:
  - component/knowledge-compiler-collectors-git
  - component/knowledge-compiler-extractors-llm-extractor

A full compile reads the working tree, which is CRLF under autocrlf. A PR or
commit compile reads the LF blob. The collector normalizes both, so they give
the same content_hash and body_hash and `kc verify` stays clean. The LLM cache
still serves answers keyed on the CRLF bytes by older full compiles.

Real git repos, real tree-sitter, real Postgres (integration tests skip loudly
when it is down).
"""

import hashlib
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.collectors.forge import CommitInfo, FakeForge
from knowledge_compiler.collectors.git import GitCollector
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

# The docstring is one string token that spans lines: CRLF vs LF changes its text.
RULES = (
    "def apply_discount(pct):\n"
    '    """Cap a discount.\n'
    "\n"
    "    Never more than 20 percent.\n"
    '    """\n'
    "    return min(pct, 20)\n"
)


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(), reason="Postgres unreachable (docker compose up -d) — integration skipped")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


@pytest.fixture()
def crlf_repo(tmp_path: Path) -> Path:
    """LF blobs, CRLF working tree: what a Windows clone with autocrlf=true has."""
    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_bytes(b"")
    (repo / "billing" / "rules.py").write_bytes(RULES.encode("utf-8"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    git(repo, "config", "core.autocrlf", "true")
    (repo / "billing" / "rules.py").unlink()
    git(repo, "checkout", "--", "billing/rules.py")  # re-smudged as CRLF
    assert b"\r\n" in (repo / "billing" / "rules.py").read_bytes()
    assert "\r" not in git(repo, "cat-file", "-p", "HEAD:billing/rules.py")
    return repo


def test_working_tree_and_blob_reads_give_identical_artifacts(crlf_repo: Path):
    collector = GitCollector(crlf_repo)
    full = {a.source_ref: a for a in collector.collect_full()}
    at_commit = {a.source_ref: a for a in collector.collect_at_commit(
        collector.head_commit(), ["billing/rules.py"])}
    assert at_commit["billing/rules.py"] == full["billing/rules.py"]
    assert "\r" not in full["billing/rules.py"].content


def _rules_symbols(slug: str) -> dict[str, dict]:
    from knowledge_compiler.storage.schema import EntityRow, Repository
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(
            select(Repository.id).where(Repository.slug == slug)).scalar_one()
        payload = session.execute(select(EntityRow.payload).where(
            EntityRow.repo_id == repo_id, EntityRow.slug == "component/billing-rules")).scalar_one()
    return {s["symbol_path"]: s for s in payload["symbols"]}


@needs_db
def test_commit_compile_after_crlf_full_compile_does_not_drift(crlf_repo: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full, reconcile, verify

    repo = crlf_repo
    slug = f"inc-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo)
    before = _rules_symbols(slug)
    assert before["billing.rules.apply_discount"]["body_hash"]

    # A Windows editor saves CRLF; git stores LF. The edit leaves apply_discount alone.
    with (repo / "billing" / "rules.py").open("w", encoding="utf-8", newline="\r\n") as f:
        f.write(RULES + "\n\ndef bonus():\n    return 1\n")
    git(repo, "add", "billing/rules.py")
    git(repo, "commit", "-qm", "Add bonus")
    commit = CommitInfo(sha=git(repo, "rev-parse", "HEAD"), timestamp=T0 + timedelta(minutes=1),
                        message="Add bonus", files=("billing/rules.py",))
    assert len(reconcile(repo, FakeForge(commits=[commit]))) == 1

    after = _rules_symbols(slug)
    assert set(after) == set(before) | {"billing.rules.bonus"}
    assert {k: after[k] for k in before} == before  # unchanged symbols: same body_hash

    report = verify(repo)
    assert report.equivalent, report.entity_divergences


@needs_db
def test_llm_cache_serves_answers_keyed_on_crlf_bytes():
    from knowledge_compiler.extractors.llm_extractor import LLMSemanticExtractor
    from knowledge_compiler.ir import Artifact
    from knowledge_compiler.llm.cache import LLMCache, cache_key
    from knowledge_compiler.llm.provider import FakeLLMProvider
    from knowledge_compiler.llm.templates import TEMPLATE_ID, TEMPLATE_VERSION

    rule = {"business_rules": [{
        "name": "Discount cap", "statement": "Discounts are capped at 20 percent.",
        "intent": "Protect margins.", "symbol_paths": ["billing.rules.apply_discount"],
        "related_components": ["billing.rules"]}], "features": [], "risks": []}
    model_id = f"fake-{uuid.uuid4().hex[:8]}"  # cache is shared across repos and tests
    cache = LLMCache(kcdb.make_engine())
    # what an older full compile of a CRLF working tree cached the answer under
    crlf_hash = hashlib.sha256(RULES.replace("\n", "\r\n").encode("utf-8")).hexdigest()
    cache.put(cache_key(TEMPLATE_ID, TEMPLATE_VERSION, model_id, crlf_hash),
              TEMPLATE_ID, TEMPLATE_VERSION, model_id, rule)

    provider = FakeLLMProvider(model_id=model_id)
    extractor = LLMSemanticExtractor(
        provider, cache, max_calls=5,
        known_symbols={"billing/rules.py": ["billing.rules.apply_discount"]},
        modules={"billing/rules.py": "billing.rules"})
    lf_hash = hashlib.sha256(RULES.encode("utf-8")).hexdigest()
    facts = extractor.extract([Artifact(artifact_type="source_file", source_ref="billing/rules.py",
                                        content_hash=lf_hash, content=RULES)])

    assert provider.calls == 0
    assert [f.fact_type for f in facts] == ["business_rule_candidate"]
    # copied under the LF key, so later lookups hit directly
    assert cache.get(cache_key(TEMPLATE_ID, TEMPLATE_VERSION, model_id, lf_hash)) is not None
