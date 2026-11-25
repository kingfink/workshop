"""The dependency-update fixture's commands for workshop's evals: `fixture.py setup|check <run dir> <cache>`.

setup commits the pull request, which updates widget to version 2, on the eval branch, and any later commits on the
base that the case asks for. check reads what the agent committed, and runs none of it: only the dependency files and
the caller may change, the PR's and the base's updates are both kept in the manifest and the lockfile, and the caller
uses widget 2's keyword.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

APP = (Path(__file__).resolve().parent / "repo" / "app.py").read_text()
COMPATIBLE_APP = APP.replace("uppercase=True", "upper=True")
IDENTITY = ("-c", "user.name=workshop", "-c", "user.email=workshop@localhost")


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", path, *args], check=True, capture_output=True, text=True).stdout


def versions(widget: int, helper: int) -> dict[str, str]:
    """The manifest and the lockfile the resolver writes for them."""
    return {
        "dependencies.json": json.dumps({"widget": widget, "helper": helper}) + "\n",
        "dependencies.lock": json.dumps({"helper": helper, "widget": widget}) + "\n",
    }


def commit(path: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", "--all")
    git(path, *IDENTITY, "commit", "--quiet", "-m", message)


def setup(case: dict, run_dir: Path) -> None:
    if case.get("phase") == "assessment":  # the agent assesses the case's evidence, not a branch
        return
    app = {"app.py": COMPATIBLE_APP} if case.get("compatible") else {}
    commit(run_dir / "worktree", {**versions(2, 1), **app}, "Update widget")
    if case.get("conflict"):
        commit(run_dir / "clone", versions(1, 2), "Update helper on base")
    if case.get("behind"):
        unrelated = {"unrelated.txt": "An unrelated change on the base.\n"}
        commit(run_dir / "clone", unrelated, "Advance base without a conflict")


def check(case: dict, run_dir: Path) -> str:
    project, start = run_dir / "graded", json.loads((run_dir / "start.json").read_text())
    changed = git(project, "diff", "--name-only", start["branch"], "HEAD").split()  # since the PR's head
    if set(changed) - {"app.py", *versions(0, 0)}:
        return "changes outside the PR's scope"
    for name, text in versions(2, 2 if case.get("conflict") else 1).items():
        if json.loads((project / name).read_text()) != json.loads(text):
            return "lost requested updates or produced an inconsistent lockfile"
    if ast.dump(ast.parse((project / "app.py").read_text())) != ast.dump(ast.parse(COMPATIBLE_APP)):
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
