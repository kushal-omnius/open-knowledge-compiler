"""GitHubGateway HTTP error translation — no live network call: a real 404
against a private repo isn't reproducible without one, so the HTTP layer
itself is mocked here (unlike collector-level tests, which use FakeForge)."""

import io
import subprocess
import urllib.error
from unittest.mock import patch

import pytest

from knowledge_compiler.collectors.forge import ForgeError, GitHubGateway


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.setenv("KC_GITHUB_TOKEN", "test-token")
    return GitHubGateway(owner="omni-us-ea", repo="frida")


@pytest.fixture()
def no_env_token(monkeypatch):
    """Both explicit sources unset — isolates the gh-CLI-fallback path from
    whatever's ambient in the machine running these tests."""
    monkeypatch.delenv("KC_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("KC_GITHUB_API", raising=False)


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


def test_falls_back_to_gh_cli_token_when_no_env_var_set(no_env_token):
    gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="gho_fromghcli\n")
    with patch("knowledge_compiler.collectors.forge.subprocess.run", return_value=gh_result) as run:
        gateway = GitHubGateway(owner="omni-us-ea", repo="frida")
    run.assert_called_once_with(["gh", "auth", "token"], capture_output=True,
                                text=True, timeout=5)
    assert gateway.token == "gho_fromghcli"


def test_falls_back_to_gh_cli_token_for_ghe_host(no_env_token, monkeypatch):
    monkeypatch.setenv("KC_GITHUB_API", "https://github.example.com/api/v3")
    gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="gho_fromghcli\n")
    with patch("knowledge_compiler.collectors.forge.subprocess.run", return_value=gh_result) as run:
        gateway = GitHubGateway(owner="omni-us-ea", repo="frida")
    run.assert_called_once_with(["gh", "auth", "token", "--hostname", "github.example.com"],
                                capture_output=True, text=True, timeout=5)
    assert gateway.token == "gho_fromghcli"


def test_env_var_takes_precedence_over_gh_cli(monkeypatch):
    monkeypatch.setenv("KC_GITHUB_TOKEN", "explicit-token")
    with patch("knowledge_compiler.collectors.forge.subprocess.run") as run:
        gateway = GitHubGateway(owner="omni-us-ea", repo="frida")
    run.assert_not_called()  # explicit config must win without even trying gh
    assert gateway.token == "explicit-token"


def test_no_token_and_gh_cli_unavailable_raises_helpful_error(no_env_token):
    with patch("knowledge_compiler.collectors.forge.subprocess.run",
              side_effect=FileNotFoundError):
        with pytest.raises(ForgeError) as exc_info:
            GitHubGateway(owner="omni-us-ea", repo="frida")
    message = str(exc_info.value)
    assert "KC_GITHUB_TOKEN" in message
    assert "gh auth login" in message


def test_no_token_and_gh_cli_not_logged_in_raises_helpful_error(no_env_token):
    gh_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
    with patch("knowledge_compiler.collectors.forge.subprocess.run", return_value=gh_result):
        with pytest.raises(ForgeError) as exc_info:
            GitHubGateway(owner="omni-us-ea", repo="frida")
    assert "KC_GITHUB_TOKEN" in str(exc_info.value)
