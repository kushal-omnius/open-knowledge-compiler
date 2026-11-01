"""Coverage quality (QA-signal review finding 2): `covers` evidence by mechanism.
http (0.8, a test-client request matched to an API route, attributed to the
component that defines it) > call (0.7, a test body calls a name imported from
the module) > import (0.3). Fixture-only and setup-only imports never count as
`call`. Below 0.5 a component is a gap.

kc-covers:
  - component/knowledge-compiler-extractors-js-test-evidence

Analyzer tests use real tree-sitter on fixture strings. The integration test
compiles a real git repo into real Postgres.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge_compiler.compiler.normalize import CurrentState, Thresholds, normalize
from knowledge_compiler.ir import Artifact, content_hash
from knowledge_compiler.storage import db as kcdb

pytest.importorskip("tree_sitter_python")

from knowledge_compiler.extractors.javascript_analyzer import JavaScriptAnalyzer  # noqa: E402
from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer  # noqa: E402
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer  # noqa: E402


def _art(ref: str, source: str) -> Artifact:
    return Artifact(artifact_type="source_file", source_ref=ref,
                    content_hash=content_hash({"c": source}), content=source)


def _evidence(facts) -> set[tuple]:
    return {(f.payload.get("node_id", "").split("::")[-1], f.payload["mechanism"],
             f.payload["target_path"]) for f in facts if f.fact_type == "test_target_observed"
            and f.payload["mechanism"] != "import"}


def _coverage(facts) -> dict[str, dict[str, tuple[str, float]]]:
    candidate = normalize(facts, CurrentState(), Thresholds(), "t")
    return {e.name.split("::")[-1]: {c["component"]: (c["mechanism"], c["confidence"])
                                     for c in e.payload["coverage"]}
            for e in candidate.entities if e.entity_type == "test_coverage"}


# -- Python ---------------------------------------------------------------------------

APP = {
    "app/__init__.py": "",
    "app/items.py": ("from fastapi import APIRouter\nrouter = APIRouter(prefix='/items')\n\n"
                     "@router.get('/recent')\ndef recent(): ...\n\n"
                     "@router.get('/{item_id}')\ndef get_item(item_id: int): ...\n"),
    "app/pricing.py": "def load():\n    return {}\n\ndef price(x):\n    return x\n",
    "app/rules.py": "def apply_discount(pct):\n    return min(pct, 20)\n",
    "app/main.py": ("from fastapi import FastAPI\nfrom app.items import router\n"
                    "app = FastAPI()\napp.include_router(router, prefix='/api')\n"),
    "tests/conftest.py": ("import pytest\nfrom app.pricing import load\n\n"
                          "@pytest.fixture\ndef prices():\n    return load()\n"),
    "tests/test_app.py": (
        "import pytest\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.rules import apply_discount\n"
        "from app import pricing\n\n"
        "client = TestClient(app)\n\n"
        "@pytest.fixture\ndef setup():\n    return pricing.load()\n\n"
        "def _helper():\n    return 1\n\n"
        "def test_item(setup):\n    assert client.get('/api/items/42').status_code == 200\n\n"
        "def test_recent():\n    assert client.get('/api/items/recent?limit=5').status_code == 200\n\n"
        "def test_cap():\n    assert apply_discount(50) == 20 and _helper()\n"),
}


def test_python_call_and_http_evidence_skip_fixtures_helpers_and_conftest():
    facts = PythonAnalyzer().analyze([_art(r, s) for r, s in APP.items()])
    assert _evidence(facts) == {
        ("test_item", "http", "/api/items/42"),
        ("test_recent", "http", "/api/items/recent?limit=5"),
        ("test_cap", "call", "app.rules.apply_discount"),
        # not: pricing.load (fixture only), _helper (local), conftest's load
    }


def test_python_strongest_mechanism_per_component():
    coverage = _coverage(PythonAnalyzer().analyze([_art(r, s) for r, s in APP.items()]))
    # the module imports app.main, app.rules and app.pricing (via `from app import`
    # -> package `app`): import-level evidence for every test in the module
    assert coverage["test_item"]["component/app-items"] == ("http", 0.8)
    assert coverage["test_item"]["component/app-main"] == ("import", 0.3)
    assert coverage["test_cap"]["component/app-rules"] == ("call", 0.7)
    assert coverage["test_cap"].get("component/app-items") is None
    # the most specific route wins: /items/recent, not /items/{item_id}; both
    # live in app.items here, so the component is the same either way
    assert coverage["test_recent"]["component/app-items"] == ("http", 0.8)


def test_http_prefers_the_most_specific_route_across_components():
    files = {
        "svc/by_id.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/items/{item_id}')\ndef one(item_id): ...\n",
        "svc/recent.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/items/recent')\ndef recent(): ...\n",
        "tests/test_svc.py": ("def test_recent(client):\n    assert client.get('/items/recent').ok\n\n"
                              "def test_one(client):\n    assert client.get('/items/9').ok\n"),
    }
    coverage = _coverage(PythonAnalyzer().analyze([_art(r, s) for r, s in files.items()]))
    assert coverage["test_recent"] == {"component/svc-recent": ("http", 0.8)}
    assert coverage["test_one"] == {"component/svc-by-id": ("http", 0.8)}


# -- JavaScript / TypeScript -------------------------------------------------------

def _express(ext: str) -> dict[str, str]:
    return {
        f"src/users.{ext}": ("const express = require('express');\nconst router = express.Router();\n"
                             "router.get('/:id', (req, res) => res.json({}));\nmodule.exports = router;\n"),
        f"src/app.{ext}": ("const express = require('express');\nconst users = require('./users');\n"
                           "const app = express();\napp.use('/users', users);\nmodule.exports = app;\n"),
        f"src/rules.{ext}": "export function applyDiscount(p) { return Math.min(p, 20); }\n",
        f"src/seed.{ext}": "export function seed() { return 1; }\n",
        f"tests/app.test.{ext}": (
            "import request from 'supertest';\n"
            "import app from '../src/app';\n"
            "import { applyDiscount } from '../src/rules';\n"
            "import { seed } from '../src/seed';\n"
            "beforeEach(() => { seed(); });\n"
            "test('get user', async () => { await request(app).get('/users/7').expect(200); });\n"
            "test('cap', () => { expect(applyDiscount(50)).toBe(20); });\n"),
    }


@pytest.mark.parametrize("analyzer,ext", [(JavaScriptAnalyzer, "js"), (TypeScriptAnalyzer, "ts")])
def test_jest_http_and_call_evidence_skip_setup(analyzer, ext):
    facts = analyzer().analyze([_art(r, s) for r, s in _express(ext).items()])
    evidence = _evidence(facts)
    assert ("get user", "http", "/users/7") in evidence
    assert ("cap", "call", "src.rules.applyDiscount") in evidence
    assert not any("seed" in target for _, _, target in evidence)   # beforeEach only

    coverage = _coverage(facts)
    assert coverage["get user"]["component/src-users"] == ("http", 0.8)   # :id matches 7
    assert coverage["cap"]["component/src-rules"] == ("call", 0.7)
    assert coverage["cap"]["component/src-seed"] == ("import", 0.3)


# -- integration: coverage_for exposes it; impact_plan treats weak as a gap ---------

def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="Postgres unreachable — integration skipped")
def test_coverage_for_and_impact_plan_use_confidence(tmp_path):
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full
    from knowledge_compiler.mcp import queries

    repo = tmp_path / "repo"
    for rel, src in APP.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(src, encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.st"),
                 ("config", "user.name", "t"), ("add", "-A"), ("commit", "-qm", "baseline")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    slug = f"cq-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)

    with Session(kcdb.make_engine()) as session:
        repo_id = queries.resolve_repo(session, slug).id

        items = queries.coverage_for(session, repo_id, "component/app-items")
        assert items["covered"] and not items["weakly_covered"]
        assert items["max_confidence"] == 0.8
        assert {t["mechanism"] for t in items["tests"]} == {"http"}

        # app.main is only imported by the test module: listed, weak, a gap
        main = queries.coverage_for(session, repo_id, "component/app-main")
        assert main["tests"] and {t["mechanism"] for t in main["tests"]} == {"import"}
        assert not main["covered"] and main["weakly_covered"]
        assert main["max_confidence"] == 0.3

        # app.pricing is used only by fixtures and conftest: no call evidence.
        # The `from app import pricing` import resolves to the `app` package.
        pricing = queries.coverage_for(session, repo_id, "component/app-pricing")
        assert not pricing["covered"]

        assert "component/app-main" in queries.impact_plan(
            session, repo_id, "component/app-main")["coverage_gaps"]
        assert "component/app-rules" not in queries.impact_plan(
            session, repo_id, "component/app-rules")["coverage_gaps"]
