"""prepare_worktree against real git: the git in tests/fake.py only records what it was asked."""

import os
import subprocess
from types import SimpleNamespace

import pytest

import workshop

BRANCH = "workshop/issue-12"


def git(*args) -> str:
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A stand-in for GitHub with one commit on main, and the tool's clone of it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@localhost")
    github, seed, clone = tmp_path / "github.git", tmp_path / "seed", tmp_path / "clone"
    git("init", "--quiet", "--bare", "--initial-branch", "main", github)
    git("init", "--quiet", "--initial-branch", "main", seed)
    (seed / "README.md").write_text("as on GitHub\n")
    git("-C", seed, "add", "--all")
    git("-C", seed, "commit", "--quiet", "-m", "Start")
    git("-C", seed, "push", "--quiet", github, "main")
    git("clone", "--quiet", github, clone)  # as `gh repo clone` would
    return SimpleNamespace(
        clone=clone,
        worktree=tmp_path / "worktree",
        other=tmp_path / "other",
        tip=git("-C", seed, "rev-parse", "HEAD"),
    )


@pytest.mark.parametrize(
    "move",
    [
        lambda worktree, tip, mine: git(
            "-C", worktree, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{BRANCH}"
        ),
        lambda worktree, tip, mine: git("-C", worktree, "replace", tip, mine),
    ],
    ids=["origin/HEAD", "replacement ref"],
)
def test_default_branch_is_githubs_whatever_the_clone_says(repo, move):
    """The agent can write the clone's refs from its worktree, so the tool asks GitHub for the default branch."""
    assert workshop.prepare_worktree("acme/analytics", BRANCH, repo.clone, repo.worktree) == repo.tip
    (repo.worktree / "README.md").write_text("as the agent would have it\n")
    git("-C", repo.worktree, "commit", "--quiet", "--all", "-m", "Mine")
    git(
        "-C", repo.clone, "push", "--quiet", "origin", f"refs/heads/{BRANCH}"
    )  # the tool publishes the branch
    move(repo.worktree, repo.tip, git("-C", repo.worktree, "rev-parse", "HEAD"))

    # the next job in the same clone starts from GitHub's default branch, not the agent's commit
    assert (
        workshop.prepare_worktree("acme/analytics", "workshop/issue-13", repo.clone, repo.other) == repo.tip
    )
    assert (repo.other / "README.md").read_text() == "as on GitHub\n"
    assert git("-C", repo.clone, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"
