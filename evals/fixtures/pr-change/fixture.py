"""The pr-change fixture's commands for workshop's evals: `fixture.py setup|check <run dir> <cache>`.

setup commits the pull request, which adds an optional fee and its test, on the eval branch: with the fee subtracted
by mistake, unless the case says the PR is already compatible. check reads what the agent committed, and runs none of
it: only app.py may change, so the PR's tests are kept, and it adds the fee, which defaults to zero.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

APP = "def total(values, fee=0):\n    return sum(values) + fee\n"
FEE_TEST = """import unittest
from app import total

class FeeTest(unittest.TestCase):
    def test_fee_is_added(self):
        self.assertEqual(total([2, 3], fee=4), 9)
"""


IDENTITY = ("-c", "user.name=workshop", "-c", "user.email=workshop@localhost")


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", path, *args], check=True, capture_output=True, text=True).stdout


def setup(case: dict, run_dir: Path) -> None:
    worktree = run_dir / "worktree"
    (worktree / "app.py").write_text(APP if case.get("compatible") else APP.replace("+ fee", "- fee"))
    (worktree / "tests" / "test_fee.py").write_text(FEE_TEST)
    git(worktree, "add", "--all")
    git(worktree, *IDENTITY, "commit", "--quiet", "-m", "Add optional fee")


def check(case: dict, run_dir: Path) -> str:
    project, start = run_dir / "graded", json.loads((run_dir / "start.json").read_text())
    changed = git(project, "diff", "--name-only", start["branch"], "HEAD").split()  # since the PR's head
    if set(changed) - {"app.py"}:
        return "changes outside the PR's scope"
    if ast.dump(ast.parse((project / "app.py").read_text())) != ast.dump(ast.parse(APP)):
        return "did not implement the expected application behavior"
    return ""


if __name__ == "__main__":
    command, run_dir = sys.argv[1], Path(sys.argv[2])
    case = json.loads((run_dir / "case.json").read_text())
    if command == "setup":
        setup(case, run_dir)
    elif command != "check":
        sys.exit(f"unknown command: {command}")
    elif why := check(case, run_dir):
        print(why)
        sys.exit(1)
