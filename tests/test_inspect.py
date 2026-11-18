import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import workshop
import workshop_eval
import workshop_inspect as inspect

CONFIG = '[modal]\nprofile = "example"\nenvironment = "main"\nfunctions = ["imports/sync"]\n'
SINCE, UNTIL = "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"


def call(state, identifier="fc-example", hour=6):
    return {"status": state, "function_call_id": identifier, "enqueued_at": f"2026-01-02T{hour:02}:00:00Z"}


def collector(monkeypatch, calls, logs="source logs"):
    commands = []

    def read(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(calls))

    def capture(command, path):
        commands.append(command)
        call_id = command[command.index("--function-call") + 1] if "--function-call" in command else ""
        value = logs.get(call_id, "source logs") if isinstance(logs, dict) else logs
        if isinstance(value, Exception):
            raise value
        path.write_bytes(value.encode()[: inspect.MAX_LOG_BYTES])
        if len(value.encode()) > inspect.MAX_LOG_BYTES:
            raise workshop.ToolError("log capture stopped; file is incomplete")
        return bool(value.strip())

    monkeypatch.setattr(inspect, "sh", read)
    monkeypatch.setattr(inspect, "capture_log", capture)
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
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "ok" and not runs
    assert len(commands) == 1 and commands[0][2:4] == ["calls", "imports/sync"]


@pytest.mark.parametrize("state", sorted(inspect.FAILURES))
def test_failures_survive_a_later_success_and_trigger_review(tmp_path, monkeypatch, state):
    commands = collector(monkeypatch, [call(state), call("success", "fc-recovered")])
    runs = agent(monkeypatch)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
    result = inspect.assess(job(tmp_path), evidence)
    assert evidence["functions"][0]["failures"] == 1
    assert result["status"] == "ok-after-review" and len(runs) == 1
    assert commands[-1][2] == "logs"


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
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "unable-to-assess" and result["gaps"] and not runs


@pytest.mark.parametrize("logs", ["", "x" * (inspect.MAX_LOG_BYTES + 1), workshop.ToolError("read failed")])
def test_incomplete_logs_cannot_produce_a_green_result(tmp_path, monkeypatch, logs):
    collector(monkeypatch, [call("failure")], logs)
    agent(monkeypatch)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
    assert inspect.assess(job(tmp_path), evidence)["status"] == "unable-to-assess"


def test_partial_collection_keeps_known_failures_visible(tmp_path, monkeypatch):
    config = inspect.read_config(CONFIG.replace('["imports/sync"]', '["imports/sync", "other/sync"]'))

    collector(monkeypatch, [call("failure")], "missing source column")

    def read(command, **kwargs):
        if command[3] == "other/sync":
            raise workshop.ToolError("not authorized")
        return subprocess.CompletedProcess(command, 0, json.dumps([call("failure")]))

    monkeypatch.setattr(inspect, "sh", read)
    agent(monkeypatch, status="action-required")
    evidence = inspect.collect(config, SINCE, UNTIL, tmp_path / "run")
    result = inspect.assess(job(tmp_path), evidence)
    assert result["status"] == "action-required" and result["gaps"]
    output = inspect.report("acme/analytics", evidence, result, verbose=True)
    assert "incomplete coverage" in output and "not authorized" in output and "failure: 1" in output


def test_agent_timeout_does_not_accept_a_partial_green_report(tmp_path, monkeypatch):
    collector(monkeypatch, [call("failure")])
    agent(monkeypatch, code=124)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
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
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
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
    assert f"Read(/{inspection.log_path.parent / 'logs'}/**)" in flag("--allowedTools")
    assert "MODAL_TOKEN_SECRET" not in workshop.agent_env(inspection)
    assert (inspection.log_path.parent / "env.sh").read_text() == ""


def test_modal_uses_explicit_identity_and_full_time_range():
    config = inspect.read_config(CONFIG)
    for kind in ("calls", "logs"):
        command = inspect.modal_command(config, kind, "imports/sync", SINCE, UNTIL)
        assert command[:2] == ["modal", "function"]
        assert "--tail" not in command and "--follow" not in command
        assert command[command.index("--profile") + 1] == "example"
        assert command[command.index("--env") + 1] == "main"
        assert command[command.index("--since") + 1] == SINCE
        assert command[command.index("--until") + 1] == UNTIL


def test_call_logs_are_deduplicated_and_include_later_recovery(tmp_path, monkeypatch):
    history = [
        call("success", "fc-before", 0),
        call("failure", "fc-failed", 6),
        call("failure", "fc-failed", 6),
        call("success", "fc-recovered", 12),
    ]
    commands = collector(monkeypatch, history[::-1])
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path)
    logs = evidence["functions"][0]["log_files"]
    assert [item["call_id"] for item in logs] == ["fc-failed", "fc-recovered"]
    assert all(item["scope"] == "call" and Path(item["path"]).exists() for item in logs)
    assert [c[c.index("--function-call") + 1] for c in commands[1:]] == ["fc-failed", "fc-recovered"]


def test_early_error_survives_long_logs_without_an_incomplete_result(tmp_path, monkeypatch):
    logs = "Database Error: missing permission\n" + "routine progress\n" * 5000
    collector(monkeypatch, [call("failure")], logs)
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path)
    item = evidence["functions"][0]
    assert not item["gaps"] and "logs" not in item
    assert Path(item["log_files"][0]["path"]).read_text() == logs


def test_empty_call_logs_have_labelled_fallback_and_keep_the_gap(tmp_path, monkeypatch):
    collector(monkeypatch, [call("failure")], {"fc-example": "", "": "container initialization error"})
    evidence = inspect.collect(inspect.read_config(CONFIG), SINCE, UNTIL, tmp_path / "run")
    item = evidence["functions"][0]
    assert [entry["scope"] for entry in item["log_files"]] == ["call", "function"]
    assert "initialization error" in Path(item["log_files"][1]["path"]).read_text()
    agent(monkeypatch)
    assert inspect.assess(job(tmp_path), evidence)["status"] == "unable-to-assess"


def test_capture_count_limit_is_shared_across_functions(tmp_path, monkeypatch):
    monkeypatch.setattr(inspect, "MAX_LOG_FILES", 1)
    commands = collector(monkeypatch, [call("failure"), call("success", "fc-later", 12)])
    config = inspect.read_config(CONFIG.replace('["imports/sync"]', '["imports/sync", "other/sync"]'))
    evidence = inspect.collect(config, SINCE, UNTIL, tmp_path)
    assert sum(command[2] == "logs" for command in commands) == 1
    assert all(item["gaps"] for item in evidence["functions"])


@pytest.mark.parametrize("size", [64, 65])
def test_streamed_log_capture_enforces_size_limit(tmp_path, monkeypatch, size):
    monkeypatch.setattr(inspect, "MAX_LOG_BYTES", 64)
    path = tmp_path / "call.log"
    command = [sys.executable, "-c", f"import sys; sys.stdout.write('X' * {size})"]
    if size > 64:
        with pytest.raises(workshop.ToolError, match="incomplete"):
            inspect.capture_log(command, path)
    else:
        assert inspect.capture_log(command, path)
    assert path.read_bytes() == b"X" * 64


def test_streamed_log_capture_times_out_and_keeps_partial_output(tmp_path):
    path = tmp_path / "call.log"
    command = [sys.executable, "-I", "-S", "-c", "import time; print('started', flush=True); time.sleep(5)"]
    with pytest.raises(workshop.ToolError, match="timed out"):
        inspect.capture_log(command, path, timeout=0.5)
    assert path.read_text() == "started\n"


def test_streamed_log_capture_preserves_command_errors(tmp_path):
    path = tmp_path / "call.log"
    command = [sys.executable, "-c", "import sys; print('access denied', file=sys.stderr); sys.exit(2)"]
    with pytest.raises(workshop.ToolError, match="code 2"):
        inspect.capture_log(command, path)
    assert "access denied" in path.read_text()


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
    now = datetime(2026, 1, 3, microsecond=478744, tzinfo=timezone.utc)
    assert inspect.time_window("24h", now) == (SINCE, UNTIL)
    for since in ("0h", "tomorrow", "999999d"):
        with pytest.raises(workshop.ToolError):
            inspect.time_window(since, now)


@pytest.mark.parametrize("local_config", [False, True])
def test_manual_command_reports_without_github_writes(fake, tmp_path, monkeypatch, capsys, local_config):
    args = []
    if local_config:
        path = tmp_path / "inspect.toml"
        path.write_text(CONFIG)
        args = ["--config", str(path)]

        def no_remote_config(*args):
            pytest.fail("local config should not require a config on the default branch")

        monkeypatch.setattr(inspect, "github_file", no_remote_config)
    else:
        fake.set(default_branch_files={inspect.CONFIG: CONFIG})
    collector(monkeypatch, [call("success")])
    assert fake.run("inspect", "acme/analytics", *args) == 0
    output = capsys.readouterr().out
    assert "acme/analytics: OK" in output and "Next step:" in output
    assert fake.state["agent_runs"] == []
    assert not fake.calls("issue", "edit") and not fake.calls("issue", "comment")
    assert not fake.calls("push") and not fake.calls("pr", "create")
    records = list((fake.home / ".workshop/logs").glob("*-inspect/*/report.md"))
    assert len(records) == 1
    assert "Modal profile" not in output and "Input states" not in output
    assert "Report:" in output and str(records[0]) in output
    assert "Modal profile" in records[0].read_text() and "Input states" in records[0].read_text()


@pytest.mark.parametrize("verbose", [False, True])
def test_failed_inspection_keeps_evidence_in_saved_report(fake, monkeypatch, capsys, verbose):
    fake.set(default_branch_files={inspect.CONFIG: CONFIG})
    collector(monkeypatch, [call("failure")], logs="")
    agent(monkeypatch, status="action-required")
    assert fake.run("inspect", "acme/analytics", *(["--verbose"] if verbose else [])) == 2
    output = capsys.readouterr().out
    saved = next((fake.home / ".workshop/logs").glob("*-inspect/*/report.md")).read_text()
    for text in ("Action required", "incomplete coverage", "Modal returned no logs", "Next step:"):
        assert text in output and text in saved
    for text in ("fc-recovered", "Modal profile", "Input states"):
        assert (text in output) is verbose
        assert text in saved


@pytest.mark.parametrize("contents", [None, b"not TOML", b"\xff"])
def test_unreadable_or_invalid_local_config_stops_before_collection(fake, tmp_path, capsys, contents):
    path = tmp_path / "inspect.toml"
    if contents is not None:
        path.write_bytes(contents)
    assert fake.run("inspect", "acme/analytics", "--config", str(path)) == 1
    assert "inspect.toml" in capsys.readouterr().err
    assert fake.state["calls"] == []


def test_missing_default_branch_config_explains_local_override(fake, capsys):
    assert fake.run("inspect", "acme/analytics") == 1
    error = capsys.readouterr().err
    assert "default branch" in error and "--config PATH" in error
    assert fake.state["agent_runs"] == []


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
