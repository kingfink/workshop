"""The dbt-duckdb fixture's commands for workshop's evals: `fixture.py setup|check <run dir> <cache>`.

setup builds, once per eval, a Python environment, the raw data and the unchanged project's outputs, and gives each
run's worktree a copy of the raw data and an environment of its own. check grades only what the agent committed:
SQL and YAML under models/ may change, the project builds, the complete outputs and tests are preserved outside the
case's permitted column changes, and the case's queries return its rows. Both run outside the agent's sandbox, so
DuckDB gets no file or network access, and the build runs only SQL and YAML the agent wrote.
"""

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_CHARS = 10_000  # kept from the end of a failed build's log, as workshop keeps of a failed check's
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


def run(cmd: list, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    cmd = [str(part) for part in cmd]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kwargs)
    if check and result.returncode:
        sys.exit(f"`{' '.join(cmd)}` failed: {(result.stderr or result.stdout).strip()}")
    return result


def setup(run_dir: Path, cache: Path) -> None:
    venv, database = cache / "venv", cache / "fixture.duckdb"  # named as in the project: "raw" is the schema
    if not (cache / "baseline.json").exists():
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
        run(["uv", "sync", "--frozen", "--quiet"], cwd=HERE / "repo", env=env, check=True)
        run([venv / "bin" / "python", "-c", LOAD, database, HERE / "raw"], check=True)
        base = cache / "base"
        shutil.rmtree(base, ignore_errors=True)
        shutil.copytree(HERE / "repo", base)
        built, log = build(cache, base)
        if not built:
            sys.exit(f"the unchanged project does not build:\n{log}")
        (cache / "baseline.json").write_text(json.dumps(snapshot(cache, base)))
    worktree = run_dir / "worktree"
    shutil.copy(database, worktree / "fixture.duckdb")  # the agent's own copy of the raw data
    run(["uv", "sync", "--frozen", "--offline", "--quiet"], cwd=worktree, check=True)  # from what uv fetched


def build(cache: Path, project: Path) -> tuple[bool, str]:
    """Build `project` from a fresh copy of the raw data."""
    shutil.copy(cache / "fixture.duckdb", project / "fixture.duckdb")
    profiles = project.with_name(project.name + "-profiles")
    profiles.mkdir(exist_ok=True)
    (profiles / "profiles.yml").write_text(
        GRADE_PROFILE.format(path=json.dumps(str(project / "fixture.duckdb")))
    )
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    dbt = [cache / "venv" / "bin" / "dbt", "build", "--project-dir", project, "--profiles-dir", profiles]
    result = run(dbt, cwd=project, env=env)
    return result.returncode == 0, (result.stdout + result.stderr)[-LOG_CHARS:]


def query(cache: Path, project: Path, sql: str) -> list | str:
    """The rows `sql` returns from the project's built database, or the error."""
    result = run([cache / "venv" / "bin" / "python", "-c", QUERY, project / "fixture.duckdb", sql])
    return json.loads(result.stdout) if result.returncode == 0 else result.stderr.strip().splitlines()[-1]


def snapshot(cache: Path, project: Path) -> dict:
    """The small fixture's complete model outputs and tests, read independently of the agent."""

    def rows(sql):
        result = query(cache, project, sql)
        if isinstance(result, str):
            raise RuntimeError(f"could not inspect the built fixture: {result}")
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


def check(run_dir: Path, cache: Path) -> tuple[str, str]:
    """Why the committed change fails, or "", and the build's log if it is the build that fails."""
    case = json.loads((run_dir / "case.json").read_text())
    start = json.loads((run_dir / "start.json").read_text())["main"]
    project = run_dir / "graded"
    changed = run(["git", "-C", project, "diff", "--name-only", f"{start}...HEAD"], check=True).stdout.split()
    if outside := [p for p in changed if not (p.startswith("models/") and p.endswith((".sql", ".yml")))]:
        return f"changed files outside models/: {', '.join(outside)}", ""
    built, log = build(cache, project)
    if not built:
        return "dbt build failed", log
    baseline = json.loads((cache / "baseline.json").read_text())
    if error := preservation_error(baseline, snapshot(cache, project), case):
        return error, ""
    for item in case.get("queries", []):
        if (rows := query(cache, project, item["sql"])) != item["rows"]:
            return f"`{item['sql']}` returned {rows}", ""
    return "", ""


if __name__ == "__main__":
    command, run_dir, cache = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if command == "setup":
        setup(run_dir, cache)
    elif command != "check":
        sys.exit(f"unknown command: {command}")
    elif (failure := check(run_dir, cache))[0]:
        print(*failure, sep="\n")
        sys.exit(1)
