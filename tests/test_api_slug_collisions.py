"""API slug collisions (ADR-004's dedup-suffix rule): `GET /users`, `GET /users/{id}`
and `GET /users/` all slugify to `api/get-users`. Each must become its own API
entity, and no slug an API already has may change.

kc-covers:
  - component/knowledge-compiler-compiler-normalize

Unit tests drive the real Python analyzer and Normalize. The integration test
compiles a real git repo into real Postgres twice and checks `kc verify`.
"""

import subprocess
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402


def _facts(routes: list[str]):
    src = "from fastapi import FastAPI\napp = FastAPI()\n" + "".join(
        f"\n@app.get('{r}')\ndef h{i}(): ...\n" for i, r in enumerate(routes))
    return PythonAnalyzer().analyze([Artifact(artifact_type="source_file", source_ref="app.py",
                                              content_hash=content_hash({"c": src}), content=src)])


def _apis(candidate) -> dict[str, str]:
    return {e.payload["route"]: e.slug for e in candidate.entities if e.entity_type == "api"}


def test_colliding_routes_each_get_an_entity_with_a_deterministic_suffix():
    candidate = normalize(_facts(["/users/{id}", "/users", "/users/"]), CurrentState(),
                          Thresholds(), "t")
    # sorted key order: "/users" < "/users/" < "/users/{}"
    assert _apis(candidate) == {"/users": "api/get-users", "/users/": "api/get-users-2",
                                "/users/{}": "api/get-users-3"}
    again = normalize(_facts(["/users", "/users/", "/users/{id}"]), CurrentState(),
                      Thresholds(), "t")
    assert _apis(again) == _apis(candidate)


def test_existing_slugs_are_preserved_and_newcomers_take_the_suffix():
    """Before the fix only one colliding route survived: here `/users/{}` held
    `api/get-users`. It must keep that slug. The route that never had an entity
    (`/users`) gets the suffix, even though it sorts first."""
    before = normalize(_facts(["/users/{id}"]), CurrentState(), Thresholds(), "t")
    assert _apis(before) == {"/users/{}": "api/get-users"}

    current = CurrentState(entities=list(before.entities), relationships=list(before.relationships))
    after = normalize(_facts(["/users/{id}", "/users"]), current, Thresholds(), "t")
    assert _apis(after) == {"/users/{}": "api/get-users", "/users": "api/get-users-2"}


def test_non_colliding_routes_keep_their_plain_slugs():
    candidate = normalize(_facts(["/orders", "/orders/{id}/items"]), CurrentState(),
                          Thresholds(), "t")
    assert _apis(candidate) == {"/orders": "api/get-orders",
                                "/orders/{}/items": "api/get-orders-items"}


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="Postgres unreachable — integration skipped")
def test_recompile_keeps_slugs_and_verifies_clean(tmp_path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full, verify
    from knowledge_compiler.mcp.queries import resolve_repo
    from knowledge_compiler.storage.schema import EntityRow

    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("from fastapi import FastAPI\napp = FastAPI()\n\n"
                   "@app.get('/users/{id}')\ndef one(id): ...\n", encoding="utf-8")

    def commit(msg: str) -> None:
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, capture_output=True)

    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.st"),
                 ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    commit("baseline")
    slug = f"sc-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)

    app.write_text(app.read_text() + "\n@app.get('/users')\ndef many(): ...\n", encoding="utf-8")
    commit("list users")
    compile_full(repo, llm_provider=None)

    with Session(kcdb.make_engine()) as session:
        repo_id = resolve_repo(session, slug).id
        apis = dict(session.execute(select(EntityRow.payload["route"].astext, EntityRow.slug).where(
            EntityRow.repo_id == repo_id, EntityRow.entity_type == "api")).all())
    assert apis == {"/users/{}": "api/get-users", "/users": "api/get-users-2"}
    report = verify(repo)
    assert report.equivalent, report.entity_divergences
