"""GitHubGateway lists every file of a large PR (dogfood finding on frida: PR
#384 changed 240 files but only the first page of 100 was ever compiled).

kc-covers:
  - component/knowledge-compiler-collectors-forge

No mocks: the real gateway talks HTTP to a local stand-in for the GitHub API
via KC_GITHUB_API, which pages the PR files list exactly like GitHub does.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from knowledge_compiler.collectors.forge import (
    PR_FILES_CAP,
    PR_FILES_PAGE_SIZE,
    ForgeError,
    GitHubGateway,
)


def _files(n: int) -> list[dict]:
    files = [{"filename": f"src/m{i:04d}.py", "status": "modified"} for i in range(n)]
    if n:
        files[-1] = {"filename": "src/renamed.py", "status": "renamed",
                     "previous_filename": "src/original.py"}
    return files


@pytest.fixture()
def fake_github(monkeypatch):
    """Serves /repos/o/r/pulls/<n>/files for PRs registered in `prs`, paged by
    per_page/page. Records every requested page."""
    prs: dict[int, list[dict]] = {}
    requested: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            number = int(url.path.split("/")[-2])
            qs = parse_qs(url.query)
            per_page, page = int(qs["per_page"][0]), int(qs.get("page", ["1"])[0])
            requested.append(page)
            body = json.dumps(prs[number][(page - 1) * per_page:page * per_page]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("KC_GITHUB_API", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("KC_GITHUB_TOKEN", "test-token")
    yield prs, requested
    server.shutdown()
    server.server_close()


def test_pr_with_more_than_one_page_lists_every_file(fake_github):
    prs, requested = fake_github
    prs[384] = _files(240)
    files = GitHubGateway("o", "r")._pr_files(384)
    assert [f.path for f in files] == [f["filename"] for f in prs[384]]
    assert requested == [1, 2, 3]
    assert files[-1].change == "renamed" and files[-1].old_path == "src/original.py"


def test_pr_with_exactly_one_full_page_stops_on_the_empty_next_page(fake_github):
    prs, requested = fake_github
    prs[7] = _files(PR_FILES_PAGE_SIZE)
    assert len(GitHubGateway("o", "r")._pr_files(7)) == PR_FILES_PAGE_SIZE
    assert requested == [1, 2]


def test_pr_at_githubs_listing_cap_fails_loudly(fake_github):
    prs, _ = fake_github
    prs[9] = _files(PR_FILES_CAP)
    with pytest.raises(ForgeError) as exc_info:
        GitHubGateway("o", "r")._pr_files(9)
    assert "#9" in str(exc_info.value)
    assert "kc compile --full" in str(exc_info.value)
