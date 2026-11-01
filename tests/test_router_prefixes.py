"""Router prefixes (QA-signal review finding 6): FastAPI APIRouter(prefix=...) +
include_router(prefix=...) and Express app.use('/x', router) compose into full
API routes, including nested and repeated mounts, across files.

kc-covers:
  - component/knowledge-compiler-extractors-express-routing

Unit tests run real tree-sitter analyzers and the real Normalize over fixture
sources. The integration test runs a full compile and then a PR compile that
touches only one router file (real git, real Postgres, FakeForge). It checks
that routes keep their prefixes and that `kc verify` stays clean.
"""

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
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


def _routes(analyzer, files: dict[str, str]) -> set[str]:
    facts = analyzer.analyze([_art(ref, src) for ref, src in files.items()])
    candidate = normalize(facts, CurrentState(), Thresholds(), "t")
    return {raw for e in candidate.entities if e.entity_type == "api"
            for raw in (f"{e.payload['method']} {r}" for r in e.payload["raw_routes"])}


# -- FastAPI ---------------------------------------------------------------------

FASTAPI = {
    "app/api/items.py": (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/items', tags=['items'])\n\n"
        "@router.get('/{item_id}')\ndef get_item(item_id: int): ...\n\n"
        "@router.post('')\ndef create_item(): ...\n"),
    "app/api/users.py": (
        "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
        "@router.get('/users/me')\ndef me(): ...\n"),
    # package router: nests both sub-routers under /v1, and items again under /v2
    "app/api/__init__.py": (
        "from fastapi import APIRouter\n"
        "from . import items\n"
        "from .users import router as users_router\n"
        "api_router = APIRouter(prefix='/v1')\n"
        "api_router.include_router(items.router)\n"
        "api_router.include_router(users_router, prefix='/accounts')\n"
        "legacy = APIRouter(prefix='/v2')\n"
        "legacy.include_router(items.router)\n"),
    "app/main.py": (
        "from fastapi import FastAPI\n"
        "from app.api import api_router, legacy\n\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.include_router(api_router, prefix='/api')\n"
        "    app.include_router(legacy, prefix='/api')\n"
        "    return app\n\n"
        "health = FastAPI()\n\n"
        "@health.get('/healthz')\ndef healthz(): ...\n"),
}


def test_fastapi_nested_prefixes_compose_into_full_routes():
    assert _routes(PythonAnalyzer(), FASTAPI) == {
        "GET /api/v1/items/{item_id}",
        "POST /api/v1/items",
        "GET /api/v2/items/{item_id}",       # same router, second mount
        "POST /api/v2/items",
        "GET /api/v1/accounts/users/me",     # mount prefix + router-less prefix
        "GET /healthz",                      # app routes: no prefix
    }


def test_fastapi_router_with_no_mount_keeps_its_own_prefix():
    """A router included from code we can't see (a plugin, a test app) is
    still served at least under its own prefix."""
    assert _routes(PythonAnalyzer(), {"app/api/items.py": FASTAPI["app/api/items.py"]}) == {
        "GET /items/{item_id}", "POST /items"}


def test_fastapi_without_prefixes_is_unchanged():
    """Repos with no prefixes must keep exactly their old routes (and slugs)."""
    src = ("from fastapi import FastAPI\napp = FastAPI()\n\n"
           "@app.get('/invoices/{id}')\ndef read_invoice(id: int): ...\n")
    assert _routes(PythonAnalyzer(), {"app.py": src}) == {"GET /invoices/{id}"}


def test_import_root_below_repo_root_still_resolves():
    """`from api.items import router` where the component is src.api.items
    (import root = src/): the dotted-suffix fallback must find it."""
    files = {
        "src/api/items.py": FASTAPI["app/api/items.py"],
        "src/main.py": ("from fastapi import FastAPI\nfrom api.items import router\n"
                        "app = FastAPI()\napp.include_router(router, prefix='/api')\n"),
    }
    assert _routes(PythonAnalyzer(), files) == {"GET /api/items/{item_id}", "POST /api/items"}


# -- Express (JavaScript + TypeScript) -------------------------------------------------

def _express(ext: str) -> dict[str, str]:
    return {
        f"src/routes/users.{ext}": (
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "router.get('/:id', (req, res) => res.json({}));\n"
            "router.post('/', (req, res) => res.sendStatus(201));\n"
            "module.exports = router;\n"),
        f"src/routes/orders.{ext}": (
            "import { Router } from 'express';\n"
            "export const ordersRouter = Router();\n"
            "ordersRouter.get('/recent', (req, res) => res.json([]));\n"),
        # index router: nests users and orders, with middleware between prefix and router
        f"src/routes/index.{ext}": (
            "import { Router } from 'express';\n"
            "import users from './users';\n"
            "import { ordersRouter as orders } from './orders';\n"
            "const api = Router();\n"
            "api.use('/users', requireAuth, users);\n"
            "api.use('/orders', orders);\n"
            "export default api;\n"),
        f"src/app.{ext}": (
            "const express = require('express');\n"
            "const api = require('./routes');\n"
            "const app = express();\n"
            "app.use('/api/v1', api);\n"
            "app.get('/health', (req, res) => res.send('ok'));\n"),
    }


EXPRESS_ROUTES = {
    "GET /api/v1/users/:id", "POST /api/v1/users/",
    "GET /api/v1/orders/recent", "GET /health",
}


def test_express_nested_app_use_composes_full_routes_js():
    assert _routes(JavaScriptAnalyzer(), _express("js")) == EXPRESS_ROUTES


def test_express_nested_app_use_composes_full_routes_ts():
    assert _routes(TypeScriptAnalyzer(), _express("ts")) == EXPRESS_ROUTES


def test_mount_cycle_terminates():
    src = ("const express = require('express');\n"
           "const a = express.Router();\nconst b = express.Router();\n"
           "a.use('/a', b);\nb.use('/b', a);\n"
           "b.get('/x', (req, res) => res.send());\n")
    routes = _routes(JavaScriptAnalyzer(), {"src/cyc.js": src})
    assert routes == {"GET /a/x"}


# -- integration: a PR touching one router file keeps full routes, verify clean ---------

def _db_available() -> bool:
    try:
        with kcdb.make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.skipif(not _db_available(), reason="Postgres unreachable — integration skipped")
def test_pr_touching_only_a_router_file_keeps_prefixes_and_verifies_clean(tmp_path):
    from knowledge_compiler.collectors.forge import FakeForge, MergedPR, PRFile
    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import compile_full, compile_pr, verify
    from knowledge_compiler.mcp.queries import resolve_repo
    from knowledge_compiler.storage.schema import EntityRow

    repo = tmp_path / "repo"
    for rel, src in FASTAPI.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(src, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    slug = f"rp-{uuid.uuid4().hex[:8]}"
    init_repository(repo, slug, f"github.com/test/{slug}", "main")
    compile_full(repo, llm_provider=None)

    # the PR adds a route to items.py only. main.py and __init__.py (which hold
    # the prefixes) are not in the PR's file list.
    (repo / "app/api/items.py").write_text(
        FASTAPI["app/api/items.py"] + "\n@router.delete('/{item_id}')\ndef delete_item(item_id: int): ...\n",
        encoding="utf-8")
    _git(repo, "commit", "-qam", "add delete")
    pr = MergedPR(number=7, title="add delete", body="",
                  merged_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                  merge_commit_sha=_git(repo, "rev-parse", "HEAD"),
                  files=(PRFile(path="app/api/items.py", change="modified"),))
    compile_pr(repo, FakeForge(prs=[pr]), expect_pr=7)

    with Session(kcdb.make_engine()) as session:
        repo_id = resolve_repo(session, slug).id
        apis = {(p["method"], r) for p in session.execute(select(EntityRow.payload).where(
            EntityRow.repo_id == repo_id, EntityRow.entity_type == "api")).scalars()
            for r in p["raw_routes"]}
    assert ("DELETE", "/api/v1/items/{item_id}") in apis
    assert ("DELETE", "/api/v2/items/{item_id}") in apis
    assert not any(route.startswith("/items") for _, route in apis)

    report = verify(repo)
    assert report.equivalent, report.entity_divergences
