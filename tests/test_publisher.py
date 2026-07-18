"""Branch publisher tests: real git repos + a bare 'remote', no mocks.

The invariants under test are ADR-010's: snapshot semantics, convergence
(no commit when nothing changed), working-tree isolation, force-push ownership.
"""

import subprocess
from pathlib import Path

import pytest

from knowledge_compiler.wiki.publisher import GitBranchPublisher, PublisherConfig


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


@pytest.fixture()
def setup(tmp_path: Path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.st")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "code")
    git(repo, "remote", "add", "origin", str(remote))

    pub = tmp_path / "wiki-out"
    pub.mkdir()
    (pub / "index.md").write_text("# wiki\n", encoding="utf-8")
    (pub / "component").mkdir()
    (pub / "component" / "a.md").write_text("# a\n", encoding="utf-8")

    publisher = GitBranchPublisher(repo, PublisherConfig(enabled=True))
    return repo, remote, pub, publisher


def branch_files(repo: Path, branch: str = "knowledge/wiki") -> set[str]:
    return set(git(repo, "ls-tree", "-r", "--name-only", branch).splitlines())


def test_first_publish_creates_orphan_branch_and_pushes(setup):
    repo, remote, pub, publisher = setup
    result = publisher.publish(pub, "kc: compile run 1 @ abc")
    assert result.committed and result.pushed
    assert branch_files(repo) == {"index.md", "component/a.md"}
    # orphan: no parents; [skip ci] in message
    assert git(repo, "log", "-1", "--format=%P", "knowledge/wiki") == ""
    assert "[skip ci]" in git(repo, "log", "-1", "--format=%B", "knowledge/wiki")
    # pushed to the bare remote
    assert git(remote, "ls-tree", "-r", "--name-only", "knowledge/wiki")


def test_convergence_no_commit_on_identical_tree(setup):
    _, _, pub, publisher = setup
    first = publisher.publish(pub, "run 1")
    second = publisher.publish(pub, "run 2")
    assert second.committed is False
    assert second.commit_sha == first.commit_sha  # branch unmoved


def test_snapshot_semantics_removed_pages_vanish(setup):
    repo, _, pub, publisher = setup
    publisher.publish(pub, "run 1")
    (pub / "component" / "a.md").unlink()
    (pub / "component" / "b.md").write_text("# b\n", encoding="utf-8")
    result = publisher.publish(pub, "run 2")
    assert result.committed
    assert branch_files(repo) == {"index.md", "component/b.md"}
    # chained: second commit has the first as parent
    assert git(repo, "log", "-1", "--format=%P", "knowledge/wiki") != ""


def test_working_tree_and_current_branch_untouched(setup):
    repo, _, pub, publisher = setup
    (repo / "dirty.txt").write_text("uncommitted work", encoding="utf-8")
    publisher.publish(pub, "run 1")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "dirty.txt").read_text(encoding="utf-8") == "uncommitted work"
    status = git(repo, "status", "--porcelain")
    assert "dirty.txt" in status and "index.md" not in status  # wiki never enters the index


def test_force_push_recovers_publisher_ownership(setup):
    repo, remote, pub, publisher = setup
    publisher.publish(pub, "run 1")
    # someone hand-commits to the remote branch (simulate divergence)
    intruder = repo.parent / "intruder"
    subprocess.run(["git", "clone", "-q", str(remote), str(intruder)], check=True,
                   capture_output=True)
    git(intruder, "config", "user.email", "i@e.st")
    git(intruder, "config", "user.name", "i")
    git(intruder, "checkout", "-q", "knowledge/wiki")
    (intruder / "hand-edit.md").write_text("sneaky\n", encoding="utf-8")
    git(intruder, "add", "-A")
    git(intruder, "commit", "-qm", "hand edit")
    git(intruder, "push", "-q", "origin", "knowledge/wiki")

    (pub / "index.md").write_text("# wiki v2\n", encoding="utf-8")
    result = publisher.publish(pub, "run 2")
    assert result.committed and result.pushed
    remote_files = set(git(remote, "ls-tree", "-r", "--name-only", "knowledge/wiki").splitlines())
    assert "hand-edit.md" not in remote_files  # publisher-owned: hand edits overwritten


def test_push_disabled_stays_local(setup):
    repo, remote, pub, _ = setup
    publisher = GitBranchPublisher(repo, PublisherConfig(enabled=True, push=False))
    result = publisher.publish(pub, "run 1")
    assert result.committed and result.pushed is False
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "rev-parse", "--verify", "knowledge/wiki")  # nothing on the remote
