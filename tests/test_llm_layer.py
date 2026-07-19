"""Phase-4 tests: the semantic layer with a fake provider (no API keys, no network).

Pins ADR-008's invariants (validation gate, content-addressed cache, budget) and
pipeline.md §6 degraded semantics, end-to-end through a real compile.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from knowledge_compiler.llm.provider import FakeLLMProvider
from knowledge_compiler.llm.templates import SCHEMA, ExtractionOut
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


RULE_OUTPUT = {
    "business_rules": [{
        "name": "Discount cap",
        "statement": "Discounts are capped at 20 percent.",
        "intent": "Protect margins from excessive discounting.",
        "symbol_paths": ["billing.rules.apply_discount"],
        "related_components": ["billing.rules"],
    }],
    "features": [],
    "risks": [],
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo_env(tmp_path: Path):
    from knowledge_compiler.compiler.bootstrap import init_repository

    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "billing" / "rules.py").write_text(
        "def apply_discount(pct):\n    return min(pct, 20)\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")

    slug = f"llm-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    # enable the semantic layer in kc.toml
    config = (repo / "kc.toml").read_text(encoding="utf-8").replace(
        "[llm]\n# Semantic layer", "[llm]\n# Semantic layer", 1)
    # target the [llm] section precisely — [embeddings] has the same enabled/provider shape
    config = config.replace('enabled = false\nprovider = "anthropic"',
                            'enabled = true\nprovider = "anthropic"')
    assert "enabled = true" in config, "kc.toml template changed — fix the toggle above"
    (repo / "kc.toml").write_text(config, encoding="utf-8")
    return repo, slug


def test_provider_factory_dispatch():
    from knowledge_compiler.llm.provider import LLMProviderError, build_provider

    with pytest.raises(LLMProviderError, match="unknown llm provider"):
        build_provider({"provider": "no-such-provider"})
    # anthropic/openai paths raise a helpful LLMProviderError (never a raw crash)
    # when their SDK or key is absent — which degrades the compile per §6.1
    for name in ("anthropic", "openai"):
        try:
            build_provider({"provider": name})
        except LLMProviderError:
            pass  # acceptable: missing SDK or credentials in the test environment


def test_schema_is_structured_outputs_compatible():
    # structured outputs require additionalProperties: false on every object
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(SCHEMA)


def test_validation_gate_rejects_malformed():
    with pytest.raises(ValidationError):
        ExtractionOut.model_validate({"business_rules": [{"name": "x"}],  # missing fields
                                      "features": [], "risks": []})
    with pytest.raises(ValidationError):
        ExtractionOut.model_validate({"business_rules": [], "features": [], "risks": [],
                                      "extra_field": True})  # extra="forbid"


def test_end_to_end_semantic_compile(repo_env):
    from knowledge_compiler.compiler.run import compile_full

    repo, slug = repo_env
    provider = FakeLLMProvider(responses={"billing/rules.py": RULE_OUTPUT})
    summary = compile_full(repo, llm_provider=provider)
    assert summary.warnings == []

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from knowledge_compiler.storage.schema import EntityRow, ProvenanceRow, Repository
    with Session(kcdb.make_engine()) as session:
        repo_id = session.execute(select(Repository.id).where(Repository.slug == slug)).scalar_one()
        rule = session.execute(select(EntityRow).where(
            EntityRow.repo_id == repo_id,
            EntityRow.slug == "business-rule/discount-cap")).scalar_one()
        assert rule.payload["statement"] == "Discounts are capped at 20 percent."
        # anchors from validated symbol paths (ADR-004 evidence discipline)
        assert rule.anchors[0]["symbol_path"] == "billing.rules.apply_discount"
        # provenance records the LLM extraction (model + template version, ADR-008)
        prov = session.execute(select(ProvenanceRow).where(
            ProvenanceRow.entity_id == rule.id)).scalars().all()
        assert any(p.extraction.get("model_id") == "fake-llm"
                   and p.extraction.get("template_version") == "1" for p in prov)

    # the rule's wiki page rendered with the statement
    page = repo / "kc-wiki" / "business-rule" / "discount-cap.md"
    assert "capped at 20 percent" in page.read_text(encoding="utf-8")


def test_cache_makes_recompile_free_and_stable(repo_env):
    from knowledge_compiler.compiler.run import compile_full

    repo, _ = repo_env
    # unique model_id: the cache is deliberately repo-agnostic (data-model.md),
    # so a shared id would serve answers cached by OTHER tests' compiles
    provider = FakeLLMProvider(responses={"billing/rules.py": RULE_OUTPUT},
                               model_id=f"fake-{uuid.uuid4().hex[:8]}")
    compile_full(repo, llm_provider=provider)
    calls_after_first = provider.calls
    assert calls_after_first > 0

    second = compile_full(repo, llm_provider=provider)
    assert provider.calls == calls_after_first  # unchanged inputs => 100% cache hits
    assert (second.added, second.changed, second.removed) == (0, 0, 0)  # incl. LLM entities


def test_no_llm_flag_degrades_and_preserves_semantic_entities(repo_env):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.storage.schema import CompileRun

    repo, slug = repo_env
    provider = FakeLLMProvider(responses={"billing/rules.py": RULE_OUTPUT})
    compile_full(repo, llm_provider=provider)

    summary = compile_full(repo, no_llm=True)  # no provider needed at all
    assert summary.removed == 0  # pipeline.md §6.1: LLM entities survive

    from sqlalchemy import select
    from sqlalchemy.orm import Session
    with Session(kcdb.make_engine()) as session:
        run = session.get(CompileRun, summary.compile_run_id)
        assert run.degraded is True


def test_provider_failure_degrades_not_fails(repo_env):
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.llm.provider import LLMProviderError

    class DownProvider:
        model_id = "down"

        def complete(self, prompt, schema):
            raise LLMProviderError("simulated outage")

    repo, _ = repo_env
    summary = compile_full(repo, llm_provider=DownProvider())
    assert summary.entities > 0  # deterministic pass unaffected (ADR-006 invariant)
    assert any("degraded" in w for w in summary.warnings)


def test_budget_exhaustion_fails_resumably(repo_env):
    from knowledge_compiler.compiler.run import CompileError, compile_full

    repo, _ = repo_env
    config = (repo / "kc.toml").read_text(encoding="utf-8").replace(
        "max_calls_per_run = 200", "max_calls_per_run = 0")
    (repo / "kc.toml").write_text(config, encoding="utf-8")

    provider = FakeLLMProvider(responses={"billing/rules.py": RULE_OUTPUT},
                               model_id=f"fake-{uuid.uuid4().hex[:8]}")
    with pytest.raises(CompileError, match="budget"):
        compile_full(repo, llm_provider=provider)
    # nothing persisted to compiled state; a re-run with budget succeeds
    config = config.replace("max_calls_per_run = 0", "max_calls_per_run = 200")
    (repo / "kc.toml").write_text(config, encoding="utf-8")
    summary = compile_full(repo, llm_provider=provider)
    assert summary.warnings == []


def test_malformed_output_retries_once_then_skips_loudly(repo_env):
    from knowledge_compiler.compiler.run import compile_full

    class MalformedProvider:
        model_id = "bad"

        def __init__(self):
            self.calls = 0

        def complete(self, prompt, schema):
            self.calls += 1
            return {"not": "the schema"}

    repo, _ = repo_env
    provider = MalformedProvider()
    summary = compile_full(repo, llm_provider=provider)
    assert provider.calls >= 2  # retried once per file
    assert any("failed validation" in w for w in summary.warnings)
    assert summary.entities > 0  # deterministic skeleton persisted regardless
