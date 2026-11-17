import argparse
import json
import subprocess
from datetime import datetime, timezone

import pytest

import workshop
import workshop_eval
import workshop_inspect as inspect

CONFIG = '[modal]\nprofile = "example"\nenvironment = "main"\nfunctions = ["imports/sync"]\n'
SINCE, UNTIL = "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"


def call(state, identifier="fc-example"):
    return {"status": state, "function_call_id": identifier, "enqueued_at": "2026-01-02T06:00:00Z"}


def collector(monkeypatch, calls, logs="source logs"):
    commands = []

    def read(config, command, function, since, until):
        commands.append((command, function, since, until))
        value = json.dumps(calls) if command == "calls" else logs
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(inspect, "modal_read", read)
    return commands


def job(tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()
    return inspect.inspection_job(
        "acme/analytics",
        "fixture",
        worktree,
        worktree,
        tmp_path / "run",
        argparse.Namespace(agent_timeout=60),
    )


def agent(monkeypatch, status="ok-after-review", code=0):
    runs = []

    def invoke(job, prompt):
        runs.append(job)
        (job.handoff / "assessment.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "summary": "The replay covered the failed interval.",
                    "evidence": ["fc-recovered committed the failed interval."],
                    "next_step": "No action remains.",
                }
            )
        )
        return code

    monkeypatch.setattr(workshop, "invoke_agent", invoke)
    return runs


def test_clean_inputs_need_neither_logs_nor_agent(tmp_path, monkeypatch):
    commands = collector(monkeypatch, [call("success")])
    runs = agent(monkeypatch)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL)
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "ok" and not runs
    assert commands == [("calls", "imports/sync", SINCE, UNTIL)]


@pytest.mark.parametrize("state", sorted(inspect.FAILURES))
def test_failures_survive_a_later_success_and_trigger_review(tmp_path, monkeypatch, state):
    commands = collector(monkeypatch, [call(state), call("success", "fc-recovered")])
    runs = agent(monkeypatch)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL)
    result = inspect.assess(job(tmp_path), evidence)
    assert evidence["functions"][0]["failures"] == 1
    assert result["status"] == "ok-after-review" and len(runs) == 1
    assert commands[-1][0] == "logs"


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [call("running")],
        [call("pending")],
        [call("new status")],
        {"unexpected": "object"},
        [{"status": "success"}],
    ],
)
def test_missing_incomplete_or_unknown_history_is_not_ok(tmp_path, monkeypatch, calls):
    collector(monkeypatch, calls)
    runs = agent(monkeypatch)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL)
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "unable-to-assess" and result["gaps"] and not runs


@pytest.mark.parametrize("logs", ["", "x" * (inspect.MAX_LOG_CHARS + 1), workshop.ToolError("read failed")])
def test_incomplete_logs_cannot_produce_a_green_result(tmp_path, monkeypatch, logs):
    collector(monkeypatch, [call("failure")], logs)
    agent(monkeypatch)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL)
    assert inspect.assess(job(tmp_path), evidence)["status"] == "unable-to-assess"


def test_partial_collection_keeps_known_failures_visible(tmp_path, monkeypatch):
    config = inspect.read_config(CONFIG.replace('["imports/sync"]', '["imports/sync", "other/sync"]'))

    def read(config, command, function, since, until):
        if function == "other/sync":
            raise workshop.ToolError("not authorized")
        return json.dumps([call("failure")]) if command == "calls" else "missing source column"

    monkeypatch.setattr(inspect, "modal_read", read)
    agent(monkeypatch, status="action-required")
    evidence = inspect.collect(config, SINCE, UNTIL)
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "action-required" and result["gaps"]
    output = inspect.report("acme/analytics", evidence, result)
    assert "incomplete coverage" in output and "not authorized" in output and "failure: 1" in output


def test_agent_timeout_does_not_accept_a_partial_green_report(tmp_path, monkeypatch):
    collector(monkeypatch, [call("failure")])
    agent(monkeypatch, code=124)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL)
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "unable-to-assess" and "124" in result["summary"]


@pytest.mark.parametrize(
    "assessment",
    [
        "not JSON",
        "[]",
        '{"status": []}',
        '{"status": "ok"}',
        '{"status": "action-required", "summary": "missing evidence"}',
    ],
)
def test_invalid_assessment_is_reported_as_incomplete(tmp_path, monkeypatch, assessment):
    collector(monkeypatch, [call("failure")])

    def invoke(job, prompt):
        (job.handoff / "assessment.json").write_text(assessment)
        return 0

    monkeypatch.setattr(workshop, "invoke_agent", invoke)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL)
    assert inspect.assess(job(tmp_path), evidence)["status"] == "unable-to-assess"


def test_inspection_agent_has_no_shell_or_service_credentials(tmp_path, monkeypatch):
    inspection = job(tmp_path)
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "do-not-share")
    command = workshop.claude_command(inspection)

    def flag(name):
        return command[command.index(name) + 1]

    assert flag("--tools") == "Read,Glob,Grep,Write"
    assert json.loads(flag("--mcp-config")) == {"mcpServers": {}}
    writes = [rule for rule in flag("--allowedTools").split(",") if rule.startswith(("Write", "Edit"))]
    assert writes == [f"Edit(/{inspection.handoff / 'assessment.json'})"]
    assert "MODAL_TOKEN_SECRET" not in workshop.agent_env(inspection)
    assert (inspection.log_path.parent / "env.sh").read_text() == ""


def test_modal_uses_explicit_identity_and_full_time_range(monkeypatch):
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "[]")

    monkeypatch.setattr(inspect, "sh", run)
    config = inspect.read_config(CONFIG)
    inspect.modal_read(config, "calls", "imports/sync", SINCE, UNTIL)
    inspect.modal_read(config, "logs", "imports/sync", SINCE, UNTIL)
    for command in commands:
        assert command[:2] == ["modal", "function"]
        assert "--tail" not in command and "--follow" not in command
        assert command[command.index("--profile") + 1] == "example"
        assert command[command.index("--env") + 1] == "main"
        assert command[command.index("--since") + 1] == SINCE
        assert command[command.index("--until") + 1] == UNTIL


@pytest.mark.parametrize(
    "text",
    [
        None,
        "not TOML",
        "[other]",
        CONFIG.replace('"example"', '""'),
        CONFIG.replace('["imports/sync"]', "[]"),
        CONFIG + 'schedule = "hourly"\n',
    ],
)
def test_bad_config_is_rejected(text):
    with pytest.raises(workshop.ToolError):
        inspect.read_config(text)


def test_time_window_is_fixed_utc_and_bounded():
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert inspect.time_window("24h", now) == (SINCE, UNTIL)
    for since in ("0h", "tomorrow", "999999d"):
        with pytest.raises(workshop.ToolError):
            inspect.time_window(since, now)


def test_manual_command_reports_without_github_writes(fake, monkeypatch, capsys):
    fake.set(default_branch_files={inspect.CONFIG: CONFIG})
    collector(monkeypatch, [call("success")])
    assert fake.run("inspect", "acme/analytics") == 0
    output = capsys.readouterr().out
    assert "acme/analytics: OK" in output and "Next step:" in output
    assert fake.state["agent_runs"] == []
    assert not fake.calls("issue", "edit") and not fake.calls("issue", "comment")
    assert not fake.calls("push") and not fake.calls("pr", "create")
    records = list((fake.home / ".workshop/logs").glob("*-inspect/*/report.md"))
    assert len(records) == 1


def test_inspection_evals_grade_assessments_without_a_dbt_build(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agent(monkeypatch)
    monkeypatch.setattr(workshop_eval, "judge", lambda *args, **kwargs: True)
    case = next(
        case for case in workshop_eval.load_cases("assess-failure") if case["expect"] == "ok-after-review"
    )
    monkeypatch.setattr(workshop_eval, "load_cases", lambda skill: [case])
    assert workshop.main(["eval", "--skill", "assess-failure"]) == 0
    result = json.loads((tmp_path / ".workshop/logs/evals.jsonl").read_text())
    assert result["passed"] and result["fix_attempts"] == 0
