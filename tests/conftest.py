from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workshop  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake.py"
RUN = ["run", "acme/analytics#12", "--skills", "add-field"]
DEFAULT = "d" * 40  # the default branch's commit on the fake GitHub


class Fake:
    """The fake GitHub and git state, plus a way to run workshop against it."""

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "state.json"
        self.home = tmp_path / "home"
        self.worktree = self.home / ".workshop" / "worktrees" / "acme-analytics-12"
        self.path.write_text(json.dumps({
            "labels": [],
            "default_commit": DEFAULT,
            "issue": {"state": "OPEN", "labels": ["workshop:queued"], "comments": []},
            "prs": [],
            "checks": [{"build": "pass"}],  # one set of checks per push; the last set repeats
            "commits": 0,  # on the job branch and not on the default branch
            "pushed_commits": 0,
            "pushes": 0,
            "remote_branches": [],
            "local_branches": [],
            "calls": [],
            "agent_runs": [],
            "agent_mode": "pr",
            "run_logs": {"555": "ERROR: column new_field not found"},
        }))

    @property
    def state(self) -> dict:
        return json.loads(self.path.read_text())

    def set(self, **values) -> None:
        self.path.write_text(json.dumps({**self.state, **values}))

    def set_issue(self, **values) -> None:
        state = self.state
        state["issue"].update(values)
        self.path.write_text(json.dumps(state))

    def run(self, *args: str) -> int:
        return workshop.main(list(args))

    @property
    def labels(self) -> set[str]:
        return set(self.state["issue"]["labels"])

    def summaries(self) -> list[str]:
        return [c["body"] for c in self.state["issue"]["comments"] if c["body"].startswith("**workshop:")]

    def outcome(self, *args: str) -> str:
        """Run a job that must exit 0, and return the outcome its summary comment names."""
        assert self.run(*args) == 0
        return re.match(r"\*\*workshop: ([\w-]+)\*\*", self.summaries()[-1])[1]

    def runs(self) -> list[dict]:
        """The run records in runs.jsonl."""
        path = self.home / ".workshop" / "logs" / "runs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def calls(self, *words: str) -> list[list[str]]:
        """Recorded calls whose arguments contain `words` in order."""
        def matches(call):
            rest = iter(call)
            return all(word in rest for word in words)
        return [call for call in self.state["calls"] if matches(call)]


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Fake:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    f = Fake(tmp_path)
    for name in ("gh", "git", "agent"):  # the agent gets only the environment the tool gives it, so name the state here
        (bin_dir / name).write_text(f'#!/bin/sh\nFAKE_STATE="{f.path}" exec "{sys.executable}" "{FAKE}" {name} "$@"\n')
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(f.home))
    monkeypatch.setattr(workshop, "claude_command", lambda job: [str(bin_dir / "agent")])
    monkeypatch.setattr(workshop, "CHECKS_POLL_S", 0.05)
    monkeypatch.setattr(workshop, "NO_CHECKS_GRACE_S", 0.3)
    return f
