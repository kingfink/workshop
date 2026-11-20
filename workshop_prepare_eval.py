"""Offline preparation evals. Grade committed files without executing agent-authored code outside its sandbox."""

import ast
import json
import shutil
from pathlib import Path

import workshop as w
import workshop_prepare as p
from workshop_eval import EVAL_BRANCH, EVAL_TOOLS, EVALS_DIR, eval_result, judge


def commit(path: Path, message: str) -> str:
    w.git("-C", path, "add", "--all")
    w.git(
        "-C",
        path,
        "-c",
        "user.name=workshop",
        "-c",
        "user.email=workshop@localhost",
        "commit",
        "--quiet",
        "-m",
        message,
    )
    return w.git("-C", path, "rev-parse", "HEAD").stdout.strip()


def eval_preparation(case: dict, run_dir: Path, args, budget: float) -> dict:
    clone, worktree, handoff = [run_dir / name for name in ("clone", "worktree", "handoff")]
    shutil.copytree(EVALS_DIR / "fixtures" / case["fixture"] / "repo", clone)
    handoff.mkdir()
    w.git("init", "--quiet", "--initial-branch", "main", clone)
    base = commit(clone, "Base")
    w.git("-C", clone, "worktree", "add", "--quiet", "-b", EVAL_BRANCH, worktree)
    dependency = case["fixture"] == "dependency-update"
    expected_app = (
        (clone / "app.py").read_text().replace("uppercase=True", "upper=True")
        if dependency
        else "def total(values, fee=0):\n    return sum(values) + fee\n"
    )
    if case.get("phase") != "assessment":
        if dependency:
            (worktree / "dependencies.json").write_text('{"widget": 2, "helper": 1}\n')
            (worktree / "dependencies.lock").write_text('{"helper": 1, "widget": 2}\n')
        else:
            (worktree / "app.py").write_text(expected_app.replace("+ fee", "- fee"))
            (worktree / "tests" / "test_fee.py").write_text(
                "import unittest\nfrom app import total\n\n"
                "class FeeTest(unittest.TestCase):\n"
                "    def test_fee_is_added(self):\n"
                "        self.assertEqual(total([2, 3], fee=4), 9)\n"
            )
        if case.get("compatible"):
            (worktree / "app.py").write_text(expected_app)
        head = commit(worktree, "Update widget" if dependency else "Add optional fee")
        if case.get("conflict"):
            (clone / "dependencies.json").write_text('{"widget": 1, "helper": 2}\n')
            (clone / "dependencies.lock").write_text('{"helper": 2, "widget": 1}\n')
            base = commit(clone, "Update helper on base")
    else:
        head = base
    job = w.Job(
        "evals/preparation",
        1,
        [w.SKILLS_DIR / p.SKILL / "SKILL.md"],
        EVAL_BRANCH,
        base,
        clone,
        {},
        worktree,
        handoff,
        run_dir / "agent.log",
        args,
        github=False,
        inspection=case.get("phase") == "assessment",
        budget=budget,
    )
    if not job.inspection:
        job.agent_args = ["--tools", EVAL_TOOLS]
    before = w.git("-C", clone, "rev-parse", EVAL_BRANCH).stdout.strip()
    try:
        if job.inspection:
            evidence = case["evidence"]
            result = p.assess(job, evidence)
            passed = all(result[item["number"]]["status"] == case["expect"] for item in evidence)
            if case.get("shared"):
                passed = passed and len({item["reason"] for item in result.values()}) == 1
        else:
            evidence = {
                "number": 1,
                "request": case.get("request", {}),
                "feedback": case.get("feedback", []),
                "checks": [],
                "diff": w.git("-C", clone, "diff", f"main...{EVAL_BRANCH}").stdout,
            }
            code = job.run_agent(p.repair_prompt(job, evidence, "Review this PR and prepare it."))
            result = p.preparation(job)
            passed = code == 0 and result["status"] == case["expect"]
        why = "decision matched" if passed else "wrong decision"
        current = w.git("-C", clone, "rev-parse", EVAL_BRANCH).stdout.strip()
        changed = w.git("-C", clone, "diff", "--name-only", head, current).stdout.splitlines()
        dirty = w.git("-C", worktree, "status", "--porcelain").stdout.strip()
        if case["expect"] != "ready" or job.inspection:
            if current != before or dirty:
                passed, why = False, "changed files before reporting the blocker or decision"
        else:
            permitted = {"app.py"}
            if dependency:
                permitted.update(("dependencies.json", "dependencies.lock"))
            if dirty or set(changed) - permitted:
                passed, why = False, "uncommitted changes or changes outside the PR's scope"
            for ancestor in (head, base):
                if w.git(
                    "-C", clone, "merge-base", "--is-ancestor", ancestor, current, check=False
                ).returncode:
                    passed, why = False, "lost original commits or did not merge the base"

            def blob(name):
                return w.git("-C", clone, "show", f"{current}:{name}").stdout

            if dependency:
                versions = {"widget": 2, "helper": 2 if case.get("conflict") else 1}
                if (
                    json.loads(blob("dependencies.json")) != versions
                    or json.loads(blob("dependencies.lock")) != versions
                ):
                    passed, why = False, "lost requested updates or produced an inconsistent lockfile"
            if ast.dump(ast.parse(blob("app.py"))) != ast.dump(ast.parse(expected_app)):
                passed, why = False, "did not implement the expected application behavior"
            if case.get("compatible") and not case.get("conflict") and changed:
                passed, why = False, "changed an already compatible PR"
        if passed and not judge(json.dumps(result), case["criterion"], run_dir, "preparation report"):
            passed, why = False, "report did not meet the evidence criterion"
        if passed:
            why = "prepared and preserved existing work" if case["expect"] == "ready" else case["expect"]
    except (w.ToolError, ValueError) as exc:
        passed, why = False, str(exc)
    return eval_result(job, case, passed, why)
