"""Git collector tests against a real throwaway git repo (no mocks — the
collector's whole job is talking to git correctly)."""

import subprocess
from pathlib import Path

import pytest

from knowledge_compiler.collectors.git import GitCollector, GitCollectorError


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    (repo / "logo.bin").write_bytes(b"\x00\x01\x02binary")
    (repo / "untracked.txt").write_text("not committed", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    git("add", "pkg/mod.py", "README.md", "logo.bin")
    git("commit", "-q", "-m", "initial")
    return repo


def test_collects_tracked_text_files_only(sample_repo: Path):
    artifacts = GitCollector(sample_repo).collect_full()
    refs = [a.source_ref for a in artifacts]
    assert refs == ["README.md", "pkg/mod.py"]  # sorted, POSIX paths
    # binary excluded, untracked excluded
    assert "logo.bin" not in refs and "untracked.txt" not in refs


def test_artifacts_are_deterministic(sample_repo: Path):
    a1 = GitCollector(sample_repo).collect_full()
    a2 = GitCollector(sample_repo).collect_full()
    assert a1 == a2  # same tree => byte-identical artifact list (order + hashes)


def test_head_commit_is_a_sha(sample_repo: Path):
    sha = GitCollector(sample_repo).head_commit()
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_non_repo_fails_loud(tmp_path: Path):
    with pytest.raises(GitCollectorError):
        GitCollector(tmp_path).collect_full()
