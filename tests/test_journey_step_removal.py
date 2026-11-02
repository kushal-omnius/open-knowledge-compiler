"""Journey steps resolve against the post-compile state (dogfood finding on
frida, 1.5.0): a compile that itself removes a step's target must not persist
the journey as `complete`, or the next `kc verify` diverges.

kc-covers:
  - component/knowledge-compiler-compiler-normalize
  - component/knowledge-compiler-compiler-diff
  - component/knowledge-compiler-compiler-run

Unit tests drive the real Python analyzer, Normalize and Diff. The integration
test compiles a real git repo into real Postgres twice and checks `kc verify`.
"""

import json
import subprocess
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.compiler.diff import CompileScope
from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, Extraction, Fact, content_hash
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402

JOURNEY = "user-journey/create-a-claim"
FULL = CompileScope(full=True, ran_families=frozenset({"deterministic"}))


def _app(prefix: str) -> str:
    return ("from fastapi import FastAPI\napp = FastAPI()\n\n"
            f"@app.post('{prefix}/claims')\ndef create(): ...\n")


def _facts(app_src: str, steps: list[str]) -> list[Fact]:
    facts = PythonAnalyzer().analyze([Artifact(
        artifact_type="source_file", source_ref="app.py",
        content_hash=content_hash({"c": app_src}), content=app_src)])
    payload = {"name": "Create a claim", "steps": steps}
    return facts + [Fact(
        fact_type="user_journey_observed", payload=payload,
        artifact_refs=("kc.toml:journeys:Create a claim",),
        extraction=Extraction(method="deterministic", extractor="test", extractor_version="0"),
        content_hash=content_hash(payload))]


def _journey(candidate):
    return next(e for e in candidate.entities if e.slug == JOURNEY)


def _baseline() -> CurrentState:
    before = normalize(_facts(_app(""), ["api/post-claims"]), CurrentState(), Thresholds(), "t")
    assert _journey(before).payload["status"] == "complete"
    return CurrentState(entities=list(before.entities), relationships=list(before.relationships))


def test_step_removed_by_this_compile_is_unresolved():
    """The route gains a prefix, so `api/post-claims` becomes `api/post-api-claims`.
    The full compile removes the old slug, so the step must not resolve."""
    after = normalize(_facts(_app("/api"), ["api/post-claims"]), _baseline(),
                      Thresholds(), "t", FULL)
    journey = _journey(after)
    assert journey.payload["status"] == "invalid"
    assert journey.payload["unresolved_steps"] == ["api/post-claims"]
    assert any("'api/post-claims' does not resolve" in w for w in after.warnings)


def test_step_untouched_by_a_pr_slice_still_resolves():
    """A PR slice that doesn't touch app.py extracts no API facts, but the API
    survives (Diff's removal evidence), so the step still resolves."""
    current = _baseline()
    journey_only = [f for f in _facts(_app(""), ["api/post-claims"])
                    if f.fact_type == "user_journey_observed"]
    pr_scope = CompileScope(full=False, ran_families=frozenset({"deterministic"}),
                            in_scope_files=frozenset({"other.py"}))
    after = normalize(journey_only, current, Thresholds(), "t", pr_scope)
    assert _journey(after).payload["status"] == "complete"


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="Postgres unreachable — integration skipped")
def test_compile_that_renames_a_step_target_verifies_clean(tmp_path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full, verify
    from knowledge_compiler.mcp import queries

    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text(_app(""), encoding="utf-8")

    def commit(msg: str) -> None:
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, capture_output=True)

    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.st"),
                 ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    commit("baseline")
    slug = f"js-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    with (repo / "kc.toml").open("a", encoding="utf-8") as f:
        f.write('\n[[journeys]]\nname = "Create a claim"\nsteps = '
                + json.dumps(["api/post-claims"]) + "\n")

    first = compile_full(repo, llm_provider=None)
    assert first.warnings == []

    app.write_text(_app("/api"), encoding="utf-8")
    commit("prefix claims routes")
    second = compile_full(repo, llm_provider=None)
    assert any("'api/post-claims' does not resolve" in w for w in second.warnings)

    with Session(kcdb.make_engine()) as session:
        rid = queries.resolve_repo(session, slug).id
        journey = queries.get_entity(session, rid, JOURNEY)
    assert journey["payload"]["status"] == "invalid"
    assert journey["payload"]["unresolved_steps"] == ["api/post-claims"]

    report = verify(repo)
    assert report.equivalent, report.entity_divergences
