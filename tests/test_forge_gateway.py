"""GitHubGateway HTTP error translation — no live network call: a real 404
against a private repo isn't reproducible without one, so the HTTP layer
itself is mocked here (unlike collector-level tests, which use FakeForge)."""

import io
import urllib.error
from unittest.mock import patch

import pytest

from knowledge_compiler.collectors.forge import ForgeError, GitHubGateway


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.setenv("KC_GITHUB_TOKEN", "test-token")
    return GitHubGateway(owner="omni-us-ea", repo="frida")


def test_404_error_explains_private_repo_or_stale_forge_ref(gateway):
    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/omni-us-ea/frida/pulls",
        code=404, msg="Not Found", hdrs=None, fp=io.BytesIO(b""))
    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(ForgeError) as exc_info:
            gateway._get("/repos/omni-us-ea/frida/pulls")
    message = str(exc_info.value)
    assert "404" in message
    assert "private" in message
    assert "forge_ref" in message
    assert "omni-us-ea/frida" in message


def test_non_404_http_error_keeps_generic_message(gateway):
    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/omni-us-ea/frida/pulls",
        code=500, msg="Internal Server Error", hdrs=None, fp=io.BytesIO(b""))
    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(ForgeError) as exc_info:
            gateway._get("/repos/omni-us-ea/frida/pulls")
    message = str(exc_info.value)
    assert "forge_ref" not in message  # the 404-specific hint must not leak onto other codes
