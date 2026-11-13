"""workshop eval: run the agent on the cases in evals/ and grade what it did. It publishes nothing.

Only `./workshop.py eval` imports this. The test project and its grading are dbt on DuckDB.
"""

import argparse
import json
import os
import shutil
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from workshop import (
    FAILED_LOG_CHARS,
    REPO_SKILLS,
    TOOL_ROOT,
    Job,
    ToolError,
    agent_usage,
    duration,
    find_skills,
    fix_prompt,
    git,
    handoff_text,
    initial_prompt,
    kept_env,
    markdown_table,
    save_record,
    sh,
    skill_records,
    status,
    timestamp,
)

EVALS_DIR = TOOL_ROOT / "evals"
EVAL_BRANCH = "workshop/eval"
EVAL_TOOLS = "Bash,Read,Edit,Write"
LOAD = """import duckdb, pathlib, sys
con = duckdb.connect(sys.argv[1])
con.execute("create schema raw")
for csv in sorted(pathlib.Path(sys.argv[2]).glob("*.csv")):
    con.execute(f"create table raw.{csv.stem} as select * from read_csv(?, header = true)", [str(csv)])
"""
QUERY = """import duckdb, json, sys
print(json.dumps(duckdb.connect(sys.argv[1], read_only=True).execute(sys.argv[2]).fetchall(), default=str))
"""
GRADE_PROFILE = """fixture:
  target: grade
  outputs:
    grade:
      type: duckdb
      path: {path}
      threads: 1
      settings:
        enable_external_access: false
"""


@dataclass
class Fixture:
    """A test project: evals/fixtures/<name>/repo is copied for each run as the target repo, and raw/ is its data."""

    name: str
    repo: Path
    venv: Path  # for loading and grading, outside anything the agent can write
    database: Path  # the raw data, loaded
    baseline: dict  # complete model outputs, schemas and test definitions from the unchanged project


def load_cases(skill: str | None) -> list[dict]:
    cases = [
        {"name": path.stem, **tomllib.loads(path.read_text())}
        for path in sorted(EVALS_DIR.glob("cases/*.toml"))
    ]
    return [case for case in cases if skill is None or skill in case["skills"]]


def prepare_fixture(name: str, work: Path) -> Fixture:
    source = EVALS_DIR / "fixtures" / name
    status(f"preparing the {name} test project in {work}")
    shutil.rmtree(work, ignore_errors=True)
    venv, database = work / "venv", work / "fixture.duckdb"  # named as in the project: "raw" is the schema
    sh(
        ["uv", "sync", "--frozen", "--quiet"],
        cwd=source / "repo",
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)},
    )
    sh([venv / "bin" / "python", "-c", LOAD, database, source / "raw"])
    fixture = Fixture(name, source / "repo", venv, database, {})
    base = work / "base"
    shutil.copytree(fixture.repo, base)
    built, log = dbt_build(fixture, base)
    if not built:
        raise ToolError(f"the unchanged {name} test project does not build:\n{log}")
    fixture.baseline = snapshot(fixture, base)
    return fixture


def dbt_build(fixture: Fixture, project: Path) -> tuple[bool, str]:
    """Build `project` from a fresh copy of the raw data. This runs outside the sandbox, so DuckDB gets no file or
    network access, and only SQL and YAML the agent wrote are built: see grade."""
    shutil.copy(fixture.database, project / "fixture.duckdb")
    profiles = project.with_name(project.name + "-profiles")
    profiles.mkdir(exist_ok=True)
    (profiles / "profiles.yml").write_text(
        GRADE_PROFILE.format(path=json.dumps(str(project / "fixture.duckdb")))
    )
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    result = sh(
        [fixture.venv / "bin" / "dbt", "build", "--project-dir", project, "--profiles-dir", profiles],
        cwd=project,
        env=env,
        check=False,
    )
    return result.returncode == 0, (result.stdout + result.stderr)[-FAILED_LOG_CHARS:]


def query(fixture: Fixture, project: Path, sql: str) -> list | str:
    """The rows `sql` returns from the project's built database, or the error."""
    result = sh([fixture.venv / "bin" / "python", "-c", QUERY, project / "fixture.duckdb", sql], check=False)
    return json.loads(result.stdout) if result.returncode == 0 else result.stderr.strip().splitlines()[-1]


def snapshot(fixture: Fixture, project: Path) -> dict:
    """The small fixture's complete model outputs and tests, read independently of the agent."""

    def rows(sql):
        result = query(fixture, project, sql)
        if isinstance(result, str):
            raise ToolError(f"could not inspect the built fixture: {result}")
        return result

    tables = {}
    for table, column, kind in rows(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = 'main' order by table_name, ordinal_position"
    ):
        tables.setdefault(table, {"columns": {}, "rows": []})["columns"][column] = kind
    for table, data in tables.items():
        quoted = '"' + table.replace('"', '""') + '"'
        data["rows"] = rows(f"select * from main.{quoted}")
    manifest = json.loads((project / "target" / "manifest.json").read_text())
    tests = [
        {
            "model": node.get("attached_node") or "",
            "depends_on": sorted(node["depends_on"]["nodes"]),
            "column": node.get("column_name"),
            "definition": node.get("test_metadata"),
            "code": node.get("raw_code"),
            "config": node["config"],
        }
        for node in manifest["nodes"].values()
        if node["resource_type"] == "test"
    ]
    return {"tables": tables, "tests": tests}


def preservation_error(before: dict, after: dict, case: dict) -> str:
    """Only the case's declared columns may change. Compare rows with their duplicate counts intact."""
    if before["tables"].keys() != after["tables"].keys():
        return "added or removed a model"
    removed = case.get("remove_columns", {})
    for table, original in before["tables"].items():
        actual = after["tables"][table]
        kept = [name for name in original["columns"] if name not in removed.get(table, [])]
        expected = {name: original["columns"][name] for name in kept}
        expected.update(case.get("add_columns", {}).get(table, {}))
        if actual["columns"] != expected:
            return f"{table}: schema changed beyond the permitted columns"

        def projected(data, columns):
            positions = [list(data["columns"]).index(name) for name in columns]
            return Counter(json.dumps([row[i] for i in positions], sort_keys=True) for row in data["rows"])

        if projected(original, kept) != projected(actual, kept):
            return f"{table}: existing rows or values changed"
    required = []
    for test in before["tests"]:
        # Removing a column permits removing its tests, but no tests of other columns or models.
        model = test["model"]
        if model.startswith("model.") and test["column"] in removed.get(model.split(".")[-1], []):
            continue
        required.append(test)

    def signatures(tests):
        return Counter(json.dumps(test, sort_keys=True) for test in tests)

    if signatures(required) - signatures(after["tests"]):
        return "removed or changed an unrelated test"
    return ""


def judge(question: str, criterion: str, cwd: Path) -> bool:
    """Ask a small model, with no tools, whether the agent's question meets the case's criterion. Its cost, about a
    cent, is left out of the totals."""
    prompt = (
        f"An agent working on a task asked this question:\n\n<question>\n{question}\n</question>\n\n"
        f"Does the question {criterion}? Answer with one word: yes or no."
    )
    result = sh(
        [
            "claude",
            "-p",
            "--model",
            "haiku",
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--output-format",
            "json",
        ],
        input=prompt,
        cwd=cwd,
        env=kept_env(),
    )
    return json.loads(result.stdout).get("result", "").strip().lower().startswith("yes")


def issue_thread(case: dict) -> str:
    issue = case["issue"]
    thread = [f"# {issue['title']}", "", issue["body"].strip()]
    for number, comment in enumerate(issue.get("comments", []), 1):
        thread += ["", f"## Comment {number}, by the owner", "", comment.strip()]
    return "\n".join(thread)


def grade(job: Job, case: dict, fixture: Fixture) -> tuple[bool, str, str]:
    """Whether the agent did what the case expects. Returns (passed, why, the log if the build failed).

    Git runs in the clone, not the worktree, where the agent could change what git runs.
    """
    question = handoff_text(job, "question.md")
    changed = git("-C", job.clone, "diff", "--name-only", f"main...{EVAL_BRANCH}").stdout.split()
    if case["expect"] == "question":
        if not question:
            return (
                False,
                "changed files instead of asking" if changed else "neither asked nor changed anything",
                "",
            )
        if "question" not in case:
            return True, "asked", ""
        good = judge(question, case["question"], job.clone.parent)  # not where the agent can write
        return good, "asked" if good else f"asked, but the question does not {case['question']}", ""
    if question:
        return False, f"asked instead: {question.splitlines()[0][:120]}", ""
    if not changed:
        return False, "committed nothing", ""
    if not handoff_text(job, "pull-request.md"):
        return False, "wrote no pull-request.md", ""
    if outside := [
        path for path in changed if not (path.startswith("models/") and path.endswith((".sql", ".yml")))
    ]:
        return False, f"changed files outside models/: {', '.join(outside)}", ""
    project = job.clone.with_name("graded")
    shutil.rmtree(project, ignore_errors=True)
    git("clone", "--quiet", "--branch", EVAL_BRANCH, job.clone, project)  # only what the agent committed
    if links := [str(path.relative_to(project)) for path in project.rglob("*") if path.is_symlink()]:
        return False, f"committed links, which the build would follow: {', '.join(links)}", ""
    built, log = dbt_build(fixture, project)
    if not built:
        return False, "dbt build failed", log
    if error := preservation_error(fixture.baseline, snapshot(fixture, project), case):
        return False, error, ""
    for check in case.get("queries", []):
        if (rows := query(fixture, project, check["sql"])) != check["rows"]:
            return False, f"`{check['sql']}` returned {rows}", ""
    return True, "changed it, and every check passes", ""


def eval_case(case: dict, fixture: Fixture, run_dir: Path, args: argparse.Namespace, budget: float) -> dict:
    """One run of one case, spending at most `budget`. The layout is a real run's: a clone, and a worktree."""
    clone, worktree, handoff = run_dir / "clone", run_dir / "worktree", run_dir / "handoff"
    shutil.copytree(fixture.repo, clone)
    handoff.mkdir()
    git("init", "--quiet", "--initial-branch", "main", clone)
    git("-C", clone, "add", "--all")
    git(
        "-C",
        clone,
        "-c",
        "user.name=workshop",
        "-c",
        "user.email=workshop@localhost",
        "commit",
        "--quiet",
        "-m",
        "Start",
    )
    git("-C", clone, "worktree", "add", "--quiet", "-b", EVAL_BRANCH, worktree)
    shutil.copy(fixture.database, worktree / "fixture.duckdb")  # the agent's own copy of the raw data
    prepare_agent_env(worktree)
    settings = clone / ".claude" / "settings.json"
    job = Job(
        f"evals/{fixture.name}",
        1,
        find_skills(case["skills"], clone / REPO_SKILLS),
        EVAL_BRANCH,
        "main",
        clone,
        json.loads(settings.read_text()) if settings.exists() else {},
        worktree,
        handoff,
        run_dir / "agent.log",
        args,
        github=False,
    )

    def run(prompt: str) -> None:
        spent = agent_usage(job.log_path)["cost_usd"]
        job.agent_args = ["--tools", EVAL_TOOLS, "--max-budget-usd", f"{max(budget - spent, 0.01):.2f}"]
        job.run_agent(prompt)

    run(initial_prompt(job, issue_thread(case)))
    attempts = 0
    while True:
        try:
            passed, why, log = grade(job, case, fixture)
        except ToolError as exc:  # such as a handoff file that is a link
            passed, why, log = False, str(exc), ""
        if not log or attempts >= args.max_fix_attempts or agent_usage(job.log_path)["cost_usd"] >= budget:
            break
        attempts += 1
        run(fix_prompt(job, None, ["dbt build"], log))
    shutil.rmtree(worktree / ".venv", ignore_errors=True)  # large, and not needed to review the run
    return {
        "finished": timestamp(),
        "case": case["name"],
        "passed": passed,
        "why": why,
        "fix_attempts": attempts,
        "skills": skill_records(job.skills),
        "run_dir": str(run_dir),
        "seconds": {"agent": round(job.agent_s)},
        **agent_usage(job.log_path),
    }


def prepare_agent_env(worktree: Path) -> None:
    """The agent's own Python environment, in its worktree, from the packages prepare_fixture fetched."""
    sh(["uv", "sync", "--frozen", "--offline", "--quiet"], cwd=worktree)


def cmd_eval(args: argparse.Namespace) -> int:
    cases = load_cases(args.skill)
    if not cases:
        raise ToolError(f"no cases in {EVALS_DIR / 'cases'}{f' for {args.skill}' if args.skill else ''}")
    home = Path.home() / ".workshop"
    runs_dir = home / "evals" / "runs" / timestamp("%Y%m%dT%H%M%SZ")
    fixtures: dict[str, Fixture] = {}
    results: dict[str, list[dict]] = {case["name"]: [] for case in cases}
    spent, left = 0.0, len(cases) * args.runs
    for case in cases:
        for number in range(1, args.runs + 1):
            share, left = (
                (args.budget - spent) / left,
                left - 1,
            )  # what earlier runs left unspent carries over
            if spent >= args.budget:
                results[case["name"]].append(
                    {
                        "passed": False,
                        "why": "skipped: the budget is spent",
                        "cost_usd": 0.0,
                        "seconds": {"agent": 0},
                    }
                )
                continue
            if case["fixture"] not in fixtures:
                fixtures[case["fixture"]] = prepare_fixture(case["fixture"], home / "evals" / case["fixture"])
            status(f"{case['name']}: run {number} of {args.runs}")
            result = eval_case(
                case, fixtures[case["fixture"]], runs_dir / f"{case['name']}-{number}", args, share
            )
            status(f"{case['name']}: {'passed' if result['passed'] else 'failed'}: {result['why']}")
            spent += result["cost_usd"]
            save_record(result, home / "logs" / "evals.jsonl")
            results[case["name"]].append(result)
    print(
        markdown_table(
            ["Case", "Passed", "Results", "API-equivalent cost", "Agent time"],
            [
                [
                    name,
                    f"{sum(run['passed'] for run in runs)} of {len(runs)}",
                    "; ".join(dict.fromkeys(run["why"] for run in runs)),
                    f"${sum(run['cost_usd'] for run in runs):.2f}",
                    duration(sum(run["seconds"]["agent"] for run in runs)),
                ]
                for name, runs in results.items()
            ],
        )
    )
    print(f"\nSpent ${spent:.2f} of ${args.budget:.2f}. Each run's files are in {runs_dir}.")
    return 0
