import json
import subprocess

import pytest

import workshop
import workshop_eval


def test_eval_cases_are_valid():
    cases = workshop_eval.load_cases(None)
    assert cases
    for case in cases:
        assert workshop.find_skills(case["skills"], workshop_eval.EVALS_DIR)  # the tool's own skills
        assert (workshop_eval.EVALS_DIR / "fixtures" / case["fixture"] / "repo").is_dir()
        if case.get("kind") == "inspection":
            assert case["expect"] in ("ok-after-review", "action-required", "unable-to-assess")
            assert case["observations"]["functions"] and case["criterion"]
        else:
            assert case["expect"] in ("pull-request", "question")
            assert case["issue"]["title"] and case["issue"]["body"]


def agent_that(tmp_path, monkeypatch, *, extra=None):
    """Run evals with a fake agent, build and judge. Returns each agent run's (prompt, command-line arguments)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    database = tmp_path / "raw.duckdb"
    database.write_text("")
    monkeypatch.setattr(
        workshop_eval,
        "prepare_fixture",
        lambda name, work: workshop_eval.Fixture(
            name,
            workshop_eval.EVALS_DIR / "fixtures" / name / "repo",
            tmp_path / "venv",
            database,
            {"tables": {}, "tests": []},
        ),
    )
    monkeypatch.setattr(workshop_eval, "prepare_agent_env", lambda worktree: None)
    builds = iter([(False, "Compilation Error in model dim_customers")])
    monkeypatch.setattr(
        workshop_eval,
        "dbt_build",
        lambda fixture, project: (
            next(builds, (True, ""))
            if project.parent.name.startswith("add-fields-plan-tier-")
            else (True, "")
        ),
    )
    expected = next(case for case in workshop_eval.load_cases(None) if case["name"] == "add-fields-plan-tier")
    monkeypatch.setattr(workshop_eval, "query", lambda fixture, project, sql: expected["queries"][0]["rows"])
    monkeypatch.setattr(workshop_eval, "snapshot", lambda fixture, project: fixture.baseline)
    monkeypatch.setattr(workshop_eval, "judge", lambda question, criterion, cwd: "contact_email" in question)
    runs = []

    def agent(job, prompt):
        runs.append((prompt, job.agent_args))
        with job.log_path.open("a") as log:
            log.write("=== output:\n" + json.dumps({"type": "result", "total_cost_usd": 0.1}) + "\n")
        if "phone" in prompt:
            (job.handoff / "question.md").write_text("Which table has phone numbers?\n")
        elif "email" in prompt:
            (job.handoff / "question.md").write_text("Use `email` or `contact_email`?\n")
        else:
            model = job.worktree / "models" / "marts" / "dim_customers.sql"
            model.write_text(model.read_text() + f"-- run {len(runs)}\n")
            if extra and "failing checks" not in prompt:
                extra(job.worktree)
            (job.handoff / "pull-request.md").write_text("Add plan_tier\n\nCloses #1\n")
            subprocess.run(["git", "-C", job.worktree, "add", "--all"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    job.worktree,
                    "-c",
                    "user.name=agent",
                    "-c",
                    "user.email=agent@localhost",
                    "commit",
                    "--quiet",
                    "-m",
                    "Change",
                ],
                check=True,
            )
        return 0

    monkeypatch.setattr(workshop, "invoke_agent", agent)
    return runs


def eval_results(tmp_path) -> dict:
    return {run["case"]: run for run in map(json.loads, (tmp_path / ".workshop/logs/evals.jsonl").open())}


def test_eval_grades_each_case(tmp_path, monkeypatch, capsys):
    runs = agent_that(tmp_path, monkeypatch)
    assert workshop.main(["eval", "--skill", "add-fields"]) == 0
    results = eval_results(tmp_path)
    assert results["add-fields-two-emails"]["passed"]  # the judge accepted the question
    assert not results["add-fields-not-loaded"]["passed"]
    assert "does not say that no loaded table" in results["add-fields-not-loaded"]["why"]
    plan_tier = results["add-fields-plan-tier"]
    assert plan_tier["passed"] and plan_tier["fix_attempts"] == 1  # the first build failed
    assert results["add-fields-misnamed-source"]["passed"]
    [fix] = [prompt for prompt, _ in runs if prompt.startswith("Your change has failing checks.")]
    assert "Compilation Error" in fix
    [phone] = [prompt for prompt, _ in runs if "# Add phone number to dim_customers" in prompt]
    assert "This job has no github tools" in phone
    share = f"{5 / len(workshop_eval.load_cases('add-fields')):.2f}"  # an equal share of the $5 default
    assert runs[0][1] == ["--tools", "Bash,Read,Edit,Write", "--max-budget-usd", share]
    assert "| add-fields-plan-tier | 1 of 1 |" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra, why",
    [
        (
            lambda worktree: (worktree / "dbt_project.yml").write_text("name: other\n"),
            "outside models/: dbt_project.yml",
        ),
        (lambda worktree: (worktree / "models" / "secret.sql").symlink_to("/etc/hosts"), "committed links"),
    ],
    ids=["outside models", "link"],
)
def test_eval_refuses_to_build(tmp_path, monkeypatch, extra, why):
    agent_that(tmp_path, monkeypatch, extra=extra)
    assert workshop.main(["eval", "--skill", "add-fields"]) == 0
    result = eval_results(tmp_path)["add-fields-plan-tier"]
    assert not result["passed"] and why in result["why"] and result["fix_attempts"] == 0


def test_eval_budget(tmp_path, monkeypatch, capsys):
    agent_that(tmp_path, monkeypatch)
    assert workshop.main(["eval", "--budget", "0.1"]) == 0
    assert len(eval_results(tmp_path)) == 1  # the first run spent it all
    assert "skipped: the budget is spent" in capsys.readouterr().out


def test_eval_rejects_tiers_assigned_to_the_wrong_customers(tmp_path, monkeypatch):
    agent_that(tmp_path, monkeypatch)
    case = next(case for case in workshop_eval.load_cases(None) if case["name"] == "add-fields-plan-tier")
    wrong = [row[:] for row in case["queries"][0]["rows"]]
    wrong[0][1], wrong[1][1] = wrong[1][1], wrong[0][1]  # the old distribution check would pass
    monkeypatch.setattr(workshop_eval, "query", lambda *args: wrong)
    assert workshop.main(["eval", "--skill", "add-fields"]) == 0
    result = eval_results(tmp_path)["add-fields-plan-tier"]
    assert not result["passed"] and "returned" in result["why"]


@pytest.mark.parametrize("change", [None, "committed", "staged", "unstaged", "untracked"])
def test_question_case_requires_unchanged_files(tmp_path, monkeypatch, change):
    agent_that(tmp_path, monkeypatch)
    case = next(
        case for case in workshop_eval.load_cases(None) if case["name"] == "add-fields-multiple-ambiguous"
    )
    monkeypatch.setattr(workshop_eval, "load_cases", lambda skill: [case])
    ask = workshop.invoke_agent

    def agent(job, prompt):
        result = ask(job, prompt)
        if change:
            model = (
                job.worktree
                / "models"
                / "marts"
                / ("partial.sql" if change == "untracked" else "dim_customers.sql")
            )
            model.write_text("select 1 as partial_change\n")
            if change in ("committed", "staged"):
                workshop.git("-C", job.worktree, "add", "--all")
            if change == "committed":
                workshop.git(
                    "-C",
                    job.worktree,
                    "-c",
                    "user.name=agent",
                    "-c",
                    "user.email=agent@localhost",
                    "commit",
                    "--quiet",
                    "-m",
                    "Partial change before asking",
                )
        return result

    monkeypatch.setattr(workshop, "invoke_agent", agent)
    assert workshop.main(["eval", "--skill", "add-fields"]) == 0
    result = eval_results(tmp_path)[case["name"]]
    assert result["passed"] is (change is None)
    assert result["why"] == ("asked" if change is None else "changed files before asking")


@pytest.mark.parametrize(
    "options",
    [
        ["--case", "add-fields-plan-tier,add-fields-two-emails"],
        ["--case", "add-fields-plan-tier", "--case", "add-fields-two-emails"],
    ],
    ids=["comma-separated", "repeated"],
)
def test_eval_runs_named_cases(tmp_path, monkeypatch, options):
    runs = agent_that(tmp_path, monkeypatch)
    assert workshop.main(["eval", *options, "--budget", "1"]) == 0
    assert eval_results(tmp_path).keys() == {"add-fields-plan-tier", "add-fields-two-emails"}
    assert runs[0][1][-1] == "0.50"  # the budget is split between the cases that run


@pytest.mark.parametrize(
    "options",
    [["--case", "add-fields-plan-teir"], ["--skill", "remove-fields", "--case", "add-fields-plan-tier"]],
)
def test_eval_rejects_unknown_cases(tmp_path, monkeypatch, capsys, options):
    runs = agent_that(tmp_path, monkeypatch)
    assert workshop.main(["eval", *options]) == 1
    error = capsys.readouterr().err
    assert options[-1] in error and "remove-fields-multiple" in error  # lists the valid names
    assert not runs


@pytest.mark.parametrize(
    "changed, selected",
    [
        ({"README.md", "tests/test_eval.py"}, lambda case: False),
        ({"evals/cases/add-fields-plan-tier.toml"}, lambda case: case["name"] == "add-fields-plan-tier"),
        (
            {"evals/fixtures/scheduled-build/repo/README.md"},
            lambda case: case["fixture"] == "scheduled-build",
        ),
        ({"skills/remove-fields/SKILL.md"}, lambda case: "remove-fields" in case["skills"]),
        ({"skills/workshop/SKILL.md"}, lambda case: case.get("kind") != "inspection"),
        ({"workshop_inspect.py"}, lambda case: True),
    ],
    ids=["other files", "case file", "fixture", "skill", "workshop skill", "tool code"],
)
def test_changed_cases(changed, selected):
    cases = workshop_eval.load_cases(None)
    expected = [case["name"] for case in cases if selected(case)]
    assert [case["name"] for case in workshop_eval.select_cases(cases, [], changed)] == expected


def test_eval_runs_changed_cases(tmp_path, monkeypatch, capsys):
    agent_that(tmp_path, monkeypatch)
    changed = {"evals/cases/add-fields-plan-tier.toml", "evals/cases/remove-fields-multiple.toml"}
    monkeypatch.setattr(workshop_eval, "changed_files", lambda: changed)
    assert workshop.main(["eval", "--changed", "--skill", "add-fields"]) == 0
    assert eval_results(tmp_path).keys() == {"add-fields-plan-tier"}


def test_eval_with_no_changed_cases_runs_nothing(tmp_path, monkeypatch, capsys):
    runs = agent_that(tmp_path, monkeypatch)
    monkeypatch.setattr(workshop_eval, "changed_files", lambda: {"README.md"})
    assert workshop.main(["eval", "--changed"]) == 0
    assert not runs and "No case's inputs differ" in capsys.readouterr().out


def test_changed_files_since_the_default_branch(tmp_path, monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@localhost")
    origin, work = tmp_path / "origin", tmp_path / "work"

    def commit(repo, names):
        for name in names:
            (repo / name).write_text(f"{repo.name}\n")
        workshop.git("-C", repo, "add", "--all")
        workshop.git("-C", repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Change")

    origin.mkdir()
    workshop.git("init", "--quiet", "--initial-branch", "main", origin)
    commit(origin, ["committed", "staged", "unstaged", "unchanged"])
    workshop.git("clone", "--quiet", origin, work)
    workshop.git("-C", work, "switch", "--quiet", "-c", "feature")
    commit(work, ["committed"])
    (work / "staged").write_text("changed\n")
    workshop.git("-C", work, "add", "staged")
    (work / "unstaged").write_text("changed\n")
    (work / "untracked").write_text("new\n")
    commit(origin, ["later"])  # on the default branch after this branch left it
    workshop.git("-C", work, "fetch", "--quiet")
    assert workshop_eval.changed_files(work) == {"committed", "staged", "unstaged", "untracked"}


@pytest.mark.parametrize(
    "text, why",
    [
        (
            "Add plan_tier\n\nChecked: dbt build passed.\n\nCloses #1\n",
            "wrote no Kind line in pull-request.md",
        ),
        ("Add plan_tier\n\nKind: add field\n\nCloses #1\n", "the pull request does not name a check"),
        ("Add plan_tier\n\nChecked: dbt build passed.\n\nKind: add field\n\nCloses #1\n", None),
    ],
    ids=["no kind line", "criterion not met", "both"],
)
def test_pull_request_case_checks_kind_and_criterion(tmp_path, monkeypatch, text, why):
    agent_that(tmp_path, monkeypatch)
    case = next(case for case in workshop_eval.load_cases(None) if case["name"] == "add-fields-plan-tier")
    case = case | {"kind_line": True, "pull_request": "name a check"}
    monkeypatch.setattr(workshop_eval, "load_cases", lambda skill: [case])
    judged = []

    def judge(text, criterion, cwd, label="question"):
        judged.append((criterion, label))
        return "Checked:" in text

    monkeypatch.setattr(workshop_eval, "judge", judge)
    work = workshop.invoke_agent

    def agent(job, prompt):
        result = work(job, prompt)
        (job.handoff / "pull-request.md").write_text(text)
        return result

    monkeypatch.setattr(workshop, "invoke_agent", agent)
    assert workshop.main(["eval", "--skill", "add-fields"]) == 0
    result = eval_results(tmp_path)[case["name"]]
    assert result["passed"] is (why is None)
    assert result["why"] == (why or "changed it, and every check passes")
    assert judged == ([] if why and "Kind" in why else [("name a check", "pull request")])
