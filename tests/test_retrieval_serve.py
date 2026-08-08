"""Milestone 2 tests: embeddings emitter (FakeEmbedder — never a live call),
retrieval (FTS / hybrid / degraded), and the MCP query functions, all against
a real compiled fixture repo.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.llm.embeddings import FakeEmbedder, embedding_text
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


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def compiled(tmp_path: Path):
    """Fixture repo compiled WITH embeddings via FakeEmbedder (injected — no config,
    no keys, no network)."""
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full

    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rules.py").write_text(
        "from billing.rules import apply_discount\n\ndef test_cap():\n    assert True\n",
        encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"m2-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    config = (repo / "kc.toml").read_text(encoding="utf-8")
    config = config.replace("[embeddings]\n# Semantic vectors", "[embeddings]\n# Semantic vectors")
    # enable embeddings (the second 'enabled = false' block) via targeted replace
    config = config.replace("enabled = false\nprovider = \"openai\"",
                            "enabled = true\nprovider = \"openai\"")
    (repo / "kc.toml").write_text(config, encoding="utf-8")

    embedder = FakeEmbedder(model_id=f"fake-embed-{uuid.uuid4().hex[:6]}")
    summary = compile_full(repo, embedder=embedder)
    assert summary.warnings == []
    return repo, slug, embedder


def repo_id_of(session: Session, slug: str) -> int:
    from knowledge_compiler.mcp.queries import resolve_repo
    return resolve_repo(session, slug).id


def test_embeddings_written_for_all_entities_and_skipped_when_unchanged(compiled):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.storage.schema import EmbeddingRow, EntityRow

    repo, slug, embedder = compiled
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        entities = session.execute(select(EntityRow).where(EntityRow.repo_id == rid)).scalars().all()
        rows = session.execute(select(EmbeddingRow).where(
            EmbeddingRow.repo_id == rid, EmbeddingRow.model_id == embedder.model_id)).scalars().all()
    assert len(rows) == len(entities) and all(r.status == "current" for r in rows)

    # recompile unchanged: emitter must not re-embed anything
    calls_before = embedder.calls
    assert calls_before > 0
    compile_full(repo, embedder=embedder)
    assert embedder.calls == calls_before


def test_provider_outage_marks_pending_and_search_degrades(tmp_path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.llm.provider import LLMProviderError
    from knowledge_compiler.retrieval.search import search
    from knowledge_compiler.storage.schema import EmbeddingRow

    class DownEmbedder:
        model_id = "down-embed"

        def embed(self, texts):
            raise LLMProviderError("simulated outage")

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "x")
    slug = f"m2o-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    config = (repo / "kc.toml").read_text(encoding="utf-8").replace(
        "enabled = false\nprovider = \"openai\"", "enabled = true\nprovider = \"openai\"")
    (repo / "kc.toml").write_text(config, encoding="utf-8")

    summary = compile_full(repo, embedder=DownEmbedder())
    assert any("pending" in w for w in summary.warnings), summary.warnings

    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        statuses = set(session.execute(select(EmbeddingRow.status).where(
            EmbeddingRow.repo_id == rid)).scalars())
        assert statuses == {"pending"}
        # degraded retrieval: hybrid facade falls back to FTS and still answers
        hits = search(session, rid, "mod", embedder=DownEmbedder())
        assert any(h.slug == "component/pkg-mod" for h in hits)


def test_keyword_search_finds_component_with_provenance_fields(compiled):
    from knowledge_compiler.retrieval.search import keyword_search

    _, slug, _ = compiled
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        hits = keyword_search(session, rid, "discount", entity_types=["component"])
    assert hits and hits[0].slug == "component/billing-rules"
    assert hits[0].payload["path"] == "billing.rules"


def test_hybrid_search_fuses_and_is_deterministic(compiled):
    from knowledge_compiler.retrieval.search import search

    _, slug, embedder = compiled
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)
        a = search(session, rid, "discount rules", embedder=embedder, limit=5)
        b = search(session, rid, "discount rules", embedder=embedder, limit=5)
    assert a and [r.slug for r in a] == [r.slug for r in b]
    assert all(r.entity_type != "wiki_page" for r in a)


def test_mcp_query_functions(compiled):
    from knowledge_compiler.mcp import queries

    _, slug, _ = compiled
    with Session(kcdb.make_engine()) as session:
        rid = repo_id_of(session, slug)

        entity = queries.get_entity(session, rid, "component/billing-rules")
        assert entity["payload"]["path"] == "billing.rules"
        assert {"relation": "covers", "from": "test-coverage/tests-test-rules-py-test-cap",
                "to": "component/billing-rules"} in entity["relationships"]
        assert entity["provenance"]  # every answer carries how-do-we-know

        cov = queries.coverage_for(session, rid, "component/billing-rules")
        assert cov["covered"] and cov["tests"][0]["node_id"] == "tests/test_rules.py::test_cap"

        intro = queries.which_pr_introduced(session, rid, "component/billing-rules")
        assert intro["pr_number"] is None and "bootstrap" in intro["note"]

        recent = queries.recent_changes(session, rid, runs=1)
        assert recent and any(c["slug"] == "component/billing-rules"
                              for c in recent[0]["changes"])

        stats = queries.knowledge_stats(session, rid)
        assert stats["entity_counts"]["component"] >= 2
        assert stats["last_compile"]["degraded"] is False


def test_mcp_server_builds_and_registers_tools(compiled):
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    import anyio

    from knowledge_compiler.mcp.server import build_server

    repo, _, _ = compiled
    server = build_server(repo)
    tools = {t.name for t in anyio.run(server.list_tools)}
    assert {"search_knowledge", "get_entity", "list_entities", "recent_changes",
            "which_pr_introduced", "coverage_for", "knowledge_stats",
            "linked_context", "journey_coverage"} <= tools
