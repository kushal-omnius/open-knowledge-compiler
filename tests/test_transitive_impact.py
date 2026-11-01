"""Transitive impact_plan (QA-signal review finding 4): affected entities are
walked over depends_on/governs/implemented_by/affects up to max_depth (default
3), each reported once at its shortest depth with the entity it was reached via,
and dependency cycles terminate.

kc-covers:
  - component/knowledge-compiler-mcp-queries

Real git repo, real tree-sitter, real Postgres (same conventions as
test_qa_grounding.py). The import graph is the compiled depends_on graph:

    core <- svc <- api <- app <- cli            (a chain: cli is depth 4)
    core <- util <- api                         (diamond: api is depth 2 via svc and via util)
    core <- report, api <- report               (report: depth 1 directly, 3 via api)
    core <- loop_a <-> loop_b                   (cycle)
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")


def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres unreachable — integration skipped")

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def base():\n    return 1\n",
    "pkg/svc.py": "from pkg.core import base\n\ndef svc():\n    return base()\n",
    "pkg/util.py": "from pkg.core import base\n\ndef util():\n    return base()\n",
    "pkg/api.py": ("from pkg.svc import svc\nfrom pkg.util import util\n\n"
                   "def api():\n    return svc() + util()\n"),
    "pkg/app.py": "from pkg.api import api\n\ndef app():\n    return api()\n",
    "pkg/report.py": ("from pkg.api import api\nfrom pkg.core import base\n\n"
                      "def report():\n    return api() + base()\n"),
    "pkg/cli.py": "from pkg.app import app\n\ndef main():\n    return app()\n",
    "pkg/loop_a.py": "from pkg.core import base\nimport pkg.loop_b\n\ndef a():\n    return base()\n",
    "pkg/loop_b.py": "import pkg.loop_a\n\ndef b():\n    return 2\n",
}


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp.queries import resolve_repo

    repo = tmp_path_factory.mktemp("impact") / "repo"
    for rel, src in FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(src, encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.st"),
                 ("config", "user.name", "t"), ("add", "-A"), ("commit", "-qm", "baseline")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    slug = f"ti-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)
    with Session(kcdb.make_engine()) as session:
        repo_id = resolve_repo(session, slug).id
    return repo_id


def _plan(repo_id: int, slug: str, **kw) -> dict:
    from knowledge_compiler.mcp import queries
    with Session(kcdb.make_engine()) as session:
        return queries.impact_plan(session, repo_id, slug, **kw)


def _depths(plan: dict) -> dict[str, int]:
    return {a["slug"]: a["depth"] for a in plan["affected"]}


def test_default_depth_is_three_with_shortest_depth_per_entity(compiled):
    plan = _plan(compiled, "component/pkg-core")
    assert plan["max_depth"] == 3
    assert _depths(plan) == {
        "component/pkg-svc": 1, "component/pkg-util": 1, "component/pkg-loop-a": 1,
        "component/pkg-report": 1,     # also reachable at depth 3 via api: shortest wins
        "component/pkg-api": 2, "component/pkg-loop-b": 2,
        "component/pkg-app": 3,
        # cli is depth 4, beyond the default; core (the changed entity) never appears
    }


def test_via_names_the_previous_hop_and_ties_resolve_deterministically(compiled):
    affected = {a["slug"]: a for a in _plan(compiled, "component/pkg-core")["affected"]}
    assert affected["component/pkg-svc"]["via"] == "component/pkg-core"
    assert affected["component/pkg-report"]["via"] == "component/pkg-core"
    assert affected["component/pkg-app"]["via"] == "component/pkg-api"
    # api is depth 2 via both svc and util: the smallest (relation, via) wins
    assert affected["component/pkg-api"]["via"] == "component/pkg-svc"
    assert {a["relation"] for a in affected.values()} == {"depends_on"}


def test_max_depth_extends_and_one_hop_matches_the_old_shape(compiled):
    deep = _depths(_plan(compiled, "component/pkg-core", max_depth=4))
    assert deep["component/pkg-cli"] == 4

    one_hop = _plan(compiled, "component/pkg-core", max_depth=1)["affected"]
    assert {a["depth"] for a in one_hop} == {1}
    assert [(a["relation"], a["slug"]) for a in one_hop] == sorted(
        (a["relation"], a["slug"]) for a in one_hop)
    assert {a["slug"] for a in one_hop} == {
        "component/pkg-svc", "component/pkg-util", "component/pkg-loop-a", "component/pkg-report"}


def test_cycle_terminates_and_never_reenters(compiled):
    plan = _plan(compiled, "component/pkg-loop-a", max_depth=10)
    depths = _depths(plan)
    assert depths == {"component/pkg-loop-b": 1}
    assert "component/pkg-loop-a" not in depths


def test_coverage_gaps_include_transitively_affected_components(compiled):
    plan = _plan(compiled, "component/pkg-core")
    assert "component/pkg-app" in plan["coverage_gaps"]   # depth 3, untested


def test_out_of_range_depth_is_rejected(compiled):
    with pytest.raises(ValueError, match="max_depth"):
        _plan(compiled, "component/pkg-core", max_depth=0)
    with pytest.raises(ValueError, match="max_depth"):
        _plan(compiled, "component/pkg-core", max_depth=11)
