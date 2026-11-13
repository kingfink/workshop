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
        assert case["expect"] in ("pull-request", "question")
        assert case["issue"]["title"] and case["issue"]["body"]


def agent_that(tmp_path, monkeypatch, *, extra=None):
    """Run evals with a fake agent, build and judge. Returns each agent run's (prompt, command-line arguments)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    database = tmp_path / "raw.duckdb"
    database.write_text("")
    monkeypatch.setattr(workshop_eval, "prepare_fixture", lambda name, work: workshop_eval.Fixture(
        name, workshop_eval.EVALS_DIR / "fixtures" / name / "repo", tmp_path / "venv", database,
        {"tables": {}, "tests": []}))
    monkeypatch.setattr(workshop_eval, "prepare_agent_env", lambda worktree: None)
    builds = iter([(False, "Compilation Error in model dim_customers"), (True, "")])
    monkeypatch.setattr(workshop_eval, "dbt_build", lambda fixture, project: next(builds))
    expected = next(case for case in workshop_eval.load_cases(None) if case["name"] == "add-field-plan-tier")
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
            subprocess.run(["git", "-C", job.worktree, "-c", "user.name=agent", "-c", "user.email=agent@localhost",
                            "commit", "--quiet", "-m", "Change"], check=True)
        return 0
    monkeypatch.setattr(workshop, "invoke_agent", agent)
    return runs


def eval_results(tmp_path) -> dict:
    return {run["case"]: run for run in map(json.loads, (tmp_path / ".workshop/logs/evals.jsonl").open())}


def test_eval_grades_each_case(tmp_path, monkeypatch, capsys):
    runs = agent_that(tmp_path, monkeypatch)
    assert workshop.main(["eval", "--skill", "add-field"]) == 0
    results = eval_results(tmp_path)
    assert results["add-field-two-emails"]["passed"]  # the judge accepted the question
    assert not results["add-field-not-loaded"]["passed"]
    assert "does not say that no loaded table" in results["add-field-not-loaded"]["why"]
    plan_tier = results["add-field-plan-tier"]
    assert plan_tier["passed"] and plan_tier["fix_attempts"] == 1  # the first build failed
    [fix] = [prompt for prompt, _ in runs if prompt.startswith("Your change has failing checks.")]
    assert "Compilation Error" in fix
    [phone] = [prompt for prompt, _ in runs if "# Add phone number to dim_customers" in prompt]
    assert "This job has no github tools" in phone
    share = f"{2 / len(workshop_eval.load_cases('add-field')):.2f}"  # an equal share of the $2 default
    assert runs[0][1] == ["--tools", "Bash,Read,Edit,Write", "--max-budget-usd", share]
    assert "| add-field-plan-tier | 1 of 1 |" in capsys.readouterr().out


@pytest.mark.parametrize("extra, why", [
    (lambda worktree: (worktree / "dbt_project.yml").write_text("name: other\n"), "outside models/: dbt_project.yml"),
    (lambda worktree: (worktree / "models" / "secret.sql").symlink_to("/etc/hosts"), "committed links"),
], ids=["outside models", "link"])
def test_eval_refuses_to_build(tmp_path, monkeypatch, extra, why):
    agent_that(tmp_path, monkeypatch, extra=extra)
    assert workshop.main(["eval", "--skill", "add-field"]) == 0
    result = eval_results(tmp_path)["add-field-plan-tier"]
    assert not result["passed"] and why in result["why"] and result["fix_attempts"] == 0


def test_eval_budget(tmp_path, monkeypatch, capsys):
    agent_that(tmp_path, monkeypatch)
    assert workshop.main(["eval", "--budget", "0.1"]) == 0
    assert len(eval_results(tmp_path)) == 1  # the first run spent it all
    assert "skipped: the budget is spent" in capsys.readouterr().out


def test_eval_rejects_tiers_assigned_to_the_wrong_customers(tmp_path, monkeypatch):
    agent_that(tmp_path, monkeypatch)
    case = next(case for case in workshop_eval.load_cases(None) if case["name"] == "add-field-plan-tier")
    wrong = [row[:] for row in case["queries"][0]["rows"]]
    wrong[0][1], wrong[1][1] = wrong[1][1], wrong[0][1]  # the old distribution check would pass
    monkeypatch.setattr(workshop_eval, "query", lambda *args: wrong)
    assert workshop.main(["eval", "--skill", "add-field"]) == 0
    result = eval_results(tmp_path)["add-field-plan-tier"]
    assert not result["passed"] and "returned" in result["why"]
