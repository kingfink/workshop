import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from conftest import DEFAULT, RUN

import workshop
from workshop import claude_command  # the real one: the fake fixture replaces workshop.claude_command


def test_init_creates_labels_and_is_safe_to_rerun(fake):
    assert fake.run("init", "acme/analytics") == 0
    assert fake.run("init", "acme/analytics") == 0
    assert fake.state["labels"] == list(workshop.LABELS)


def test_bad_arguments_exit_1():
    assert workshop.main(["run", "acme/analytics#12"]) == 1  # --skills is required
    assert workshop.main(["run", "not-a-ref", "--skills", "add-field"]) == 1
    assert workshop.main(["run", "acme/analytics#12", "--skills", "../add-field"]) == 1
    assert workshop.main(["run", "acme/analytics#12", "--skills", "workshop"]) == 1


def test_claude_command_confines_the_agent(fake):
    def flag(cmd, name):
        return cmd[cmd.index(name) + 1]

    home = fake.home / ".workshop"
    skill = workshop.SKILLS_DIR / "add-field" / "SKILL.md"
    job = workshop.Job(
        "acme/analytics",
        12,
        [skill],
        "workshop/issue-12",
        DEFAULT,
        home / "repos" / "acme/analytics",
        {},
        fake.worktree,
        home / "handoff" / "acme-analytics-12",
        None,
        None,
    )
    cmd = claude_command(job)
    assert flag(cmd, "--effort") == "medium"
    allowed = flag(cmd, "--allowedTools").split(",")
    assert f"Edit(/{fake.worktree}/**)" in allowed and "Bash" in allowed  # the sandbox confines commands
    assert [rule for rule in allowed if rule.startswith("Read")] == [f"Read(/{skill.parent}/**)"]
    assert flag(cmd, "--disallowedTools") == "Monitor"
    assert flag(cmd, "--setting-sources") == ""  # no settings file from the agent's branch
    assert "--read-only --lockdown-mode" in flag(cmd, "--mcp-config")
    sandbox = json.loads(flag(cmd, "--settings"))["sandbox"]
    assert (
        sandbox["enabled"] and not sandbox["allowUnsandboxedCommands"] and "excludedCommands" not in sandbox
    )
    assert {"name": "ANTHROPIC_API_KEY", "mode": "deny"} in sandbox["credentials"]["envVars"]
    assert sandbox["filesystem"]["denyRead"] == ["~/"] and sandbox["network"]["allowedDomains"] == []

    # the target repo's default branch adds hosts, but cannot turn the sandbox off
    settings = {"sandbox": {"enabled": False, "network": {"allowedDomains": ["bigquery.googleapis.com"]}}}
    sandbox = json.loads(flag(claude_command(dataclasses.replace(job, settings=settings)), "--settings"))[
        "sandbox"
    ]
    assert sandbox["enabled"] and sandbox["network"]["allowedDomains"] == ["bigquery.googleapis.com"]


def test_agent_gets_no_secrets(fake, monkeypatch):
    monkeypatch.setenv("CLOUD_SECRET", "s3cret")
    monkeypatch.setenv(
        "ANTHROPIC_API_KEY", "sk-test"
    )  # Claude Code's own; the sandbox hides it from commands
    fake.set(default_branch_files={".claude/settings.json": json.dumps({"env": {"DBT_TARGET": "dev it"}})})
    assert fake.outcome(*RUN) == "pr-open"
    [agent] = fake.state["agent_runs"]
    assert "CLOUD_SECRET" not in agent["env"] and "ANTHROPIC_API_KEY" in agent["env"]
    assert agent["env_file"] == "export DBT_TARGET='dev it'\n"  # the target repo's env reaches only commands


def test_target_repo_skills(fake):
    skill = "---\nname: from-the-repo\n---\n"
    names = ("workshop", "add-field", "triage")
    fake.set(default_branch_files={f".workshop/skills/{name}/SKILL.md": skill for name in names})
    assert fake.outcome("run", "acme/analytics#12", "--skills", "add-field,triage") == "pr-open"
    [agent] = fake.state["agent_runs"]
    repo_skills = (
        fake.home / ".workshop" / "logs" / "acme-analytics-12" / "skills"
    )  # this run's copy from GitHub
    assert f"{workshop.SKILLS_DIR / 'workshop' / 'SKILL.md'}\n" in agent["prompt"]  # never the repo's
    assert f"{repo_skills / 'add-field' / 'SKILL.md'}\n" in agent["prompt"]  # the repo's replaces the tool's
    assert str(workshop.SKILLS_DIR / "add-field") not in agent["prompt"]
    assert f"{repo_skills / 'triage' / 'SKILL.md'}\n" in agent["prompt"]
    assert (repo_skills / "triage" / "SKILL.md").read_text() == skill


def test_github_error_is_not_taken_for_a_repo_without_settings(fake):
    fake.set(failing={"api": 0})
    assert fake.run(*RUN) == 1
    assert fake.calls("issue", "edit") == [] and fake.state["agent_runs"] == []


def test_unknown_skill_changes_no_labels(fake):
    assert fake.run("run", "acme/analytics#12", "--skills", "nope") == 1
    assert fake.calls("issue", "edit") == [] and fake.state["agent_runs"] == []


@pytest.mark.parametrize(
    "issue, state",
    [
        ({"labels": ["bug"]}, {}),
        ({"labels": ["workshop:working"]}, {}),
        ({"state": "CLOSED"}, {}),
        ({}, {"gh_auth_ok": False}),
    ],
    ids=["not queued", "already working", "closed", "gh not authenticated"],
)
def test_refusals_change_nothing(fake, issue, state):
    fake.set_issue(**issue)
    fake.set(**state)
    assert fake.run(*RUN) == 1
    assert fake.calls("issue", "edit") == fake.calls("issue", "comment") == []
    assert fake.state["agent_runs"] == []
    assert not fake.worktree.exists()


def test_pr_with_green_checks(fake):
    assert fake.outcome(*RUN) == "pr-open"
    assert fake.labels == {"workshop:waiting"}
    assert fake.calls("push", "origin", "refs/heads/workshop/issue-12")
    assert fake.state["prs"][0]["title"] == "Add new_field"
    assert "https://github.com/acme/analytics/pull/100" in fake.summaries()[0]
    assert "select new_field from orders" in fake.summaries()[0]
    [agent] = fake.state["agent_runs"]
    assert agent["cwd"] == str(fake.worktree.resolve())
    for skill in ("workshop", "add-field"):
        assert str(workshop.SKILLS_DIR / skill / "SKILL.md") in agent["prompt"]
    assert fake.calls("worktree", "add", "-b", "workshop/issue-12", DEFAULT)
    [log] = (fake.home / ".workshop" / "logs" / "acme-analytics-12").glob("*.log")
    assert "fake agent: pr" in log.read_text()


def test_reruns(fake):
    fake.set(agent_mode="question")
    assert fake.outcome(*RUN) == "needs-info"
    [question] = fake.state["issue"]["comments"]  # the question is in the run's one comment
    assert "\n\nWhich one?\n" in question["body"]
    assert fake.calls("issue", "edit", "--add-label=workshop:working", "--remove-label=workshop:queued")
    assert fake.calls("reset", "--hard", DEFAULT)

    fake.set(agent_mode="silent")  # the first run's question is not left for this one
    assert fake.outcome(*RUN) == "escalated"
    assert fake.calls("issue", "edit", "--add-label=workshop:working", "--remove-label=workshop:waiting")
    escalation = fake.summaries()[-1]
    assert "exited with code 0" in escalation and "~/.workshop/logs/acme-analytics-12/" in escalation
    assert "thinking line" not in escalation  # the log stays on this machine

    fake.set(agent_mode="pr")
    assert fake.outcome(*RUN) == "pr-open"

    fake.set(agent_mode="question")  # a new question wins over the open pull request
    assert fake.outcome(*RUN) == "needs-info"
    assert fake.calls("reset", "--hard", "origin/workshop/issue-12")
    assert fake.labels == {"workshop:waiting"}
    assert len(fake.calls("worktree", "add")) == 2  # both in the first run: try the branch, then create it
    assert len({run["cwd"] for run in fake.state["agent_runs"]}) == 1


def test_fix_loop(fake):
    fake.set(checks=[{"build": "fail", "lint": "pass"}, {"build": "pass", "lint": "pass"}])
    assert fake.outcome(*RUN) == "pr-open"
    _, fix = (run["prompt"] for run in fake.state["agent_runs"])
    assert fix.startswith("Pull request #100 has failing checks.")
    assert "Failing checks: build\n" in fix and "column new_field not found" in fix
    assert "Fix attempts: 1 of 2" in fake.summaries()[0]
    assert "API-equivalent cost: $0.50." in fake.summaries()[0]  # both agent runs
    [run] = fake.runs()
    assert run["outcome"] == "pr-open" and run["fix_attempts"] == 1
    assert [(skill["name"], skill["from"]) for skill in run["skills"]] == [
        ("workshop",) * 2,
        ("add-field", "workshop"),
    ]
    assert (run["agent_version"], run["model"], run["turns"]) == ("0.0.1", "fake-model", 6)
    assert run["tokens"]["output"] == 200
    assert run["tool_calls"] == {"Bash": 2} and run["denied_tool_calls"] == {"WebFetch": 2}
    assert set(run["seconds"]) == {"total", "agent", "checks"}
    assert run["pr_url"] == "https://github.com/acme/analytics/pull/100" and run["pushed"] == f"{2:040x}"
    assert run["validation"]["status"] == "passed" and run["validation"]["head_sha"] == run["pushed"]
    assert run["validation"]["observed_at"] and run["validation"]["checks"][0]["link"]
    [hidden] = workshop.RECORD.findall(fake.summaries()[0])  # the copy for `workshop score`
    assert json.loads(hidden)["pushed"] == run["pushed"]


@pytest.mark.parametrize(
    "pr, ending",
    [
        ({}, "Open"),
        ({"state": "MERGED"}, "Merged as-is"),
        ({"state": "MERGED", "head_oid": "f" * 40}, "Merged after changes"),
        ({"state": "CLOSED"}, "Closed unmerged"),
    ],
    ids=["open", "merged as-is", "merged after changes", "closed"],
)
def test_score(fake, capsys, pr, ending):
    fake.set(agent_mode="question")
    assert fake.outcome(*RUN) == "needs-info"
    fake.set(agent_mode="pr", checks=[{"build": "fail"}, {"build": "pass"}])
    assert fake.outcome(*RUN) == "pr-open"
    state = fake.state
    state["prs"][0].update(pr)
    forged = json.loads(
        workshop.RECORD.findall(fake.summaries()[0])[0]
    )  # someone else's comment does not count
    state["issue"]["comments"].append(
        {"body": f"<!-- workshop-run: {json.dumps(forged)} -->", "viewerDidAuthor": False}
    )
    fake.set(**state)
    capsys.readouterr()
    assert fake.run("score", "acme/analytics") == 0
    headers, _, row = capsys.readouterr().out.splitlines()
    scores = dict(zip(headers.strip("| ").split(" | "), row.strip("| ").split(" | "), strict=True))
    assert scores["Skills"] == "add-field" and scores["Jobs"] == "1" and scores[ending] == "1"
    assert (scores["Runs"], scores["Questions"], scores["Fix attempts"]) == ("2", "1", "1")
    assert scores["API-equivalent cost"] == "$0.75"  # three agent runs
    assert scores["Per merged PR"] == ("$0.75" if ending.startswith("Merged") else "-")
    assert scores["CI passed"] == "1" and scores["CI unknown"] == "0"

    assert fake.run("score", "acme/analytics", "--since", "2999-01-01") == 0
    assert "no run records" in capsys.readouterr().err


@pytest.mark.parametrize(
    "checks, options, outcome, reason, validation",
    [
        ([{"build": "fail"}], [], "escalated", "still failing after 2 fix attempts", "failed"),
        ([], [], "pr-open", "no checks ran", "unverified"),
        ([{"build": "skipping"}], [], "pr-open", "skipped or neutral", "unverified"),
        ([{"build": "pending"}], ["--checks-timeout", "1"], "escalated", "did not finish", "unverified"),
    ],
    ids=["fix attempts run out", "no checks", "all skipped", "checks time out"],
)
def test_check_end_states(fake, checks, options, outcome, reason, validation):
    fake.set(checks=checks)
    assert fake.outcome(*RUN, *options) == outcome
    assert reason in fake.summaries()[0]
    assert fake.runs()[0]["validation"]["status"] == validation
    assert f"GitHub checks: {validation}" in fake.summaries()[0]


def test_agent_timeout(fake):
    fake.set(agent_mode="sleep")
    assert fake.outcome(*RUN, "--agent-timeout", "1") == "escalated"
    assert "timed out after 1s" in fake.summaries()[0]


def test_agent_deadline_uses_each_invocations_timeout(fake):
    fake.set(checks=[{"build": "fail"}, {"build": "pass"}])
    before = datetime.now(timezone.utc).replace(microsecond=0)
    assert fake.outcome(*RUN, "--agent-timeout", "30") == "pr-open"
    after = datetime.now(timezone.utc)
    assert len(fake.state["agent_runs"]) == 2
    for run in fake.state["agent_runs"]:
        deadline = run["prompt"].split("This invocation stops at ")[1].split(" ")[0]
        assert (
            before + timedelta(seconds=30)
            <= datetime.fromisoformat(deadline)
            <= after + timedelta(seconds=30)
        )
        assert "(30s from now)" in run["prompt"]


def test_handoff_link_is_refused(fake):
    fake.set(agent_mode="link")
    assert fake.run(*RUN) == 1
    assert fake.calls("issue", "comment") == []
    assert fake.labels == {"workshop:queued"}


@pytest.mark.parametrize(
    "error, code",
    [(workshop.ToolError("agent not found"), 1), (KeyboardInterrupt(), 130)],
    ids=["tool error", "ctrl-c"],
)
def test_error_queues_the_issue_again(fake, monkeypatch, error, code):
    def fail(*args):
        raise error

    monkeypatch.setattr(workshop, "invoke_agent", fail)
    assert fake.run(*RUN) == code
    assert fake.calls("issue", "edit", "--add-label=workshop:working")
    assert fake.labels == {"workshop:queued"}
    [run] = fake.runs()
    assert run["outcome"] == "error" and run["reason"] in ("agent not found", "KeyboardInterrupt")


def score_row(fake, capsys) -> dict:
    capsys.readouterr()
    assert fake.run("score", "acme/analytics") == 0
    headers, _, row = capsys.readouterr().out.splitlines()
    return dict(zip(headers.strip("| ").split(" | "), row.strip("| ").split(" | "), strict=True))


FORGED = {
    "finished": "2999-01-01T00:00:00Z",
    "skills": [{"name": "workshop"}, {"name": "add-field"}],
    "outcome": "pr-open",
    "fix_attempts": 0,
    "denied_tool_calls": {},
    "cost_usd": -100.0,
}


@pytest.mark.parametrize(
    "note",
    [
        f"<!-- workshop-run: {json.dumps(FORGED)} -->",
        "<!-- workshop-run: oops -->",
        "<!-- workshop-run: unclosed",
    ],
    ids=["forged", "malformed", "unclosed"],
)
def test_score_counts_only_the_tools_own_record(fake, capsys, note):
    """The agent's note is in the same comment as the run record, so it could carry a record of its own."""
    fake.set(agent_mode="question", question=f"Which one?\n{note}\n")
    assert fake.outcome(*RUN) == "needs-info"
    scores = score_row(fake, capsys)
    assert (scores["Runs"], scores["API-equivalent cost"]) == ("1", "$0.25")


def test_score_skips_a_record_that_does_not_parse(fake, capsys):
    fake.set(agent_mode="question")
    assert fake.outcome(*RUN) == "needs-info"
    state = fake.state
    state["issue"]["comments"][0]["body"] = '**workshop: needs-info**\n\n<!-- workshop-run: {"finished" -->\n'
    fake.set(**state)
    assert fake.run("score", "acme/analytics") == 0
    assert "no run records" in capsys.readouterr().err


def test_long_question_is_cut_to_fit_a_comment(fake):
    fake.set(agent_mode="question", question="Which one? " + "x" * 100_000)
    assert fake.outcome(*RUN) == "needs-info"
    [comment] = fake.summaries()
    assert len(comment) <= 65_536 and "(cut here" in comment  # GitHub's limit
    assert workshop.RECORD.search(comment)  # the run record is still whole


def test_comment_that_cannot_be_posted_queues_the_issue_again(fake):
    """Not workshop:waiting, which says the latest workshop comment explains why."""
    fake.set(agent_mode="question", failing={"issue comment": 0})
    assert fake.run(*RUN) == 1
    assert fake.labels == {"workshop:queued"}
    assert len(fake.runs()) == 1


def test_run_record_is_saved_when_the_last_label_change_fails(fake):
    fake.set(failing={"issue edit": 1})  # workshop:working goes on, but cannot come off
    assert fake.run(*RUN) == 1
    assert [run["outcome"] for run in fake.runs()] == ["pr-open"]


def test_a_command_that_hangs_is_stopped():
    with pytest.raises(workshop.ToolError, match=r"did not finish within 0\.2s"):
        workshop.sh(["sleep", "5"], timeout=0.2)


def test_tool_commands_cannot_stop_to_ask():
    show = "import json, os; print(json.dumps(dict(os.environ)))"
    env = json.loads(workshop.sh([sys.executable, "-c", show], env={"PATH": "/usr/bin:/bin"}).stdout)
    assert (env["GIT_TERMINAL_PROMPT"], env["GH_PROMPT_DISABLED"], env["GIT_NO_REPLACE_OBJECTS"]) == (
        "0",
        "1",
        "1",
    )


@pytest.mark.parametrize(
    "rollup, expected, reason",
    [
        ([{"status": "COMPLETED", "conclusion": "SUCCESS"}], "passed", "reported-checks-passed"),
        ([{"status": "COMPLETED", "conclusion": "CANCELLED"}], "failed", "checks-failed"),
        ([{"status": "COMPLETED", "conclusion": "NEUTRAL"}], "unverified", "all-skipped"),
        ([{"status": "QUEUED", "conclusion": None}], "unverified", "timed-out"),
        (
            [{"context": "legacy", "state": "SUCCESS", "targetUrl": "https://example.com/check"}],
            "passed",
            "reported-checks-passed",
        ),
        ([{"context": "legacy", "state": "ERROR"}], "failed", "checks-failed"),
        (
            [{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}],
            "unverified",
            "timed-out",
        ),
        (
            [{"status": "COMPLETED", "conclusion": "FAILURE"}, {"status": "IN_PROGRESS"}],
            "failed",
            "timed-out",
        ),
        ([{"status": "COMPLETED", "conclusion": "FUTURE_VALUE"}], "unverified", "timed-out"),
    ],
)
def test_validation_records_the_observed_commit(monkeypatch, rollup, expected, reason):
    monkeypatch.setattr(workshop, "gh_json", lambda *args: {"headRefOid": "abc", "statusCheckRollup": rollup})
    result = workshop.wait_for_checks("acme/analytics", 1, 0, "abc")
    assert (result["status"], result["reason"], result["head_sha"]) == (expected, reason, "abc")
    assert len(result["checks"]) == len(rollup) and result["observed_at"]


def test_passing_checks_on_a_different_commit_are_unverified(monkeypatch):
    monkeypatch.setattr(
        workshop,
        "gh_json",
        lambda *args: {
            "headRefOid": "new",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        },
    )
    result = workshop.wait_for_checks("acme/analytics", 1, 0, "pushed")
    assert (result["status"], result["reason"], result["head_sha"]) == (
        "unverified",
        "head-changed",
        "pushed",
    )
    assert result["observed_head_sha"] == "new" and result["checks"] == []


def test_score_does_not_infer_validation_for_old_records(fake, capsys):
    assert fake.outcome(*RUN) == "pr-open"
    state = fake.state
    comment = state["issue"]["comments"][0]
    found = workshop.RECORD.search(comment["body"])
    record = json.loads(found[1])
    record.pop("validation")
    comment["body"] = comment["body"][: found.start()] + f"<!-- workshop-run: {json.dumps(record)} -->"
    fake.set(**state)
    scores = score_row(fake, capsys)
    assert scores["CI unknown"] == "1" and scores["CI passed"] == "0"


def test_head_change_escalates_without_attempting_a_fix(fake, monkeypatch):
    monkeypatch.setattr(workshop, "pr_checks", lambda *args: ("different", [{"bucket": "pass"}]))
    assert fake.outcome(*RUN) == "escalated"
    assert len(fake.state["agent_runs"]) == 1
    assert fake.runs()[0]["validation"]["reason"] == "head-changed"


def test_head_change_during_pagination_discards_the_checks(monkeypatch):
    responses = iter(
        [
            {
                "headRefOid": "old",
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}] * 100,
            },
            {"headRefOid": "new"},
        ]
    )
    monkeypatch.setattr(workshop, "gh_json", lambda *args: next(responses))
    result = workshop.wait_for_checks("acme/analytics", 1, 0, "old")
    assert result["reason"] == "head-changed" and result["checks"] == []


def test_large_validation_record_leaves_room_for_the_note(fake, monkeypatch):
    fake.set(checks=[{f"check-{i}": "pass" for i in range(100)}])
    original = workshop.handoff_text
    monkeypatch.setattr(
        workshop,
        "handoff_text",
        lambda job, name: "x" * 100_000 if name == "comment.md" else original(job, name),
    )
    assert fake.outcome(*RUN) == "pr-open"
    [comment] = fake.summaries()
    assert len(comment) <= 65_536 and "(cut here" in comment
    record = json.loads(workshop.RECORD.search(comment)[1])
    assert len(record["validation"]["checks"]) == 100
