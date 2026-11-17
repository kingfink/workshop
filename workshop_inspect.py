"""Inspect Modal call history, and ask the agent to assess observed failures."""

import argparse
import json
import re
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workshop import (
    SKILLS_DIR,
    Job,
    ToolError,
    agent_usage,
    github_file,
    handoff_text,
    markdown_table,
    parse_issue_ref,
    prepare_worktree,
    require_gh_login,
    save_record,
    sh,
    skill_records,
    timestamp,
)

CONFIG = ".workshop/inspect.toml"
FAILURES = {
    "failure",
    "timeout",
    "terminated",
    "init failure",
    "internal failure",
    "idle timeout",
    "memory manager eviction",
}
PENDING = {"pending", "running"}
STATUSES = {
    "ok": "OK",
    "ok-after-review": "OK after review",
    "action-required": "Action required",
    "unable-to-assess": "Unable to assess",
}
MAX_LOG_CHARS = 60_000
MAX_CALLS = 10_000


def read_config(text: str | None) -> dict:
    if text is None:
        raise ToolError(f"the target repository needs {CONFIG}; see README.md's inspect command")
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ToolError(f"invalid {CONFIG}: {exc}") from None
    modal = config.get("modal")
    if set(config) != {"modal"} or not isinstance(modal, dict):
        raise ToolError(f"{CONFIG} must contain a [modal] table")
    if set(modal) != {"profile", "environment", "functions"}:
        raise ToolError("[modal] requires profile, environment and functions, with no other settings")
    for key in ("profile", "environment"):
        if not isinstance(modal[key], str) or not re.fullmatch(r"[\w.-]+", modal[key], re.ASCII):
            raise ToolError(f"[modal].{key} must be a nonempty profile or environment name")
    functions = modal["functions"]
    if (
        not isinstance(functions, list)
        or not functions
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", name, re.ASCII)
            for name in functions
        )
    ):
        raise ToolError("[modal].functions must be a nonempty list of app/function names")
    if len(set(functions)) != len(functions):
        raise ToolError("[modal].functions contains duplicates")
    return modal


def time_window(since: str, now: datetime) -> tuple[str, str]:
    # Modal's calls endpoint rejects fractional seconds in --since and --until.
    now = now.replace(microsecond=0)
    match = re.fullmatch(r"([1-9][0-9]*)(m|h|d)", since)
    if not match:
        raise ToolError("--since must be a positive duration, such as 30m, 24h or 7d")
    seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > 7 * 86400:
        raise ToolError("--since must be at most 7d; inspect a smaller window")
    return (now - timedelta(seconds=seconds)).isoformat(), now.isoformat()


def modal_read(config: dict, command: str, function: str, since: str, until: str) -> str:
    args = [
        "modal",
        "function",
        command,
        function,
        "--profile",
        config["profile"],
        "--env",
        config["environment"],
        "--since",
        since,
        "--until",
        until,
    ]
    args += ["--json"] if command == "calls" else ["--timestamps", "--show-function-call-id"]
    return sh(args, timeout=120).stdout


def collect(config: dict, since: str, until: str) -> dict:
    evidence = {
        "since": since,
        "until": until,
        "profile": config["profile"],
        "environment": config["environment"],
        "functions": [],
    }
    for name in config["functions"]:
        item = {"function": name, "calls": [], "counts": {}, "failures": 0, "gaps": []}
        evidence["functions"].append(item)
        try:
            calls = json.loads(modal_read(config, "calls", name, since, until))
            if not isinstance(calls, list) or any(
                not isinstance(call, dict)
                or not isinstance(call.get("status"), str)
                or not isinstance(call.get("function_call_id"), str)
                or not call["function_call_id"]
                or not isinstance(call.get("enqueued_at"), str)
                for call in calls
            ):
                raise ToolError("unrecognized Modal call history; check the installed Modal CLI version")
            if len(calls) > MAX_CALLS:
                raise ToolError(f"more than {MAX_CALLS} inputs; inspect a smaller window")
            item["calls"] = calls
            item["counts"] = dict(Counter(call["status"] for call in calls))
            item["failures"] = sum(count for state, count in item["counts"].items() if state in FAILURES)
            unknown = set(item["counts"]) - FAILURES - PENDING - {"success"}
            if unknown:
                item["gaps"].append(f"unrecognized input states: {', '.join(sorted(unknown))}")
            if not calls:
                item["gaps"].append("no inputs in this window; expected schedules are not checked")
            if PENDING & item["counts"].keys():
                item["gaps"].append("some inputs are still pending or running")
        except (ToolError, json.JSONDecodeError) as exc:
            item["gaps"].append(f"could not read call history: {str(exc)[-1000:]}")
        if not item["failures"]:
            continue
        try:
            logs = modal_read(config, "logs", name, since, until)
            item["logs"] = logs[-MAX_LOG_CHARS:]
            if not logs.strip():
                item["gaps"].append("Modal returned no logs for the failed function")
            if len(logs) > MAX_LOG_CHARS:
                item["gaps"].append(f"logs truncated to the last {MAX_LOG_CHARS} characters")
        except ToolError as exc:
            item["gaps"].append(f"could not read logs: {str(exc)[-1000:]}")
    return evidence


def inspection_prompt(job: Job) -> str:
    return (
        "\n".join(
            [
                "Assess the observed failures in this Modal inspection. Read and follow these skills:",
                *(str(path) for path in job.skills),
                f"Repository: {job.repo}. Checkout commit: {job.base} (not necessarily the deployed revision).",
                f"Read the evidence in {job.log_path.parent / 'evidence.json'}.",
                "Read the repository's AGENTS.md if present, and relevant code and documentation.",
                f"Repair skills are in {SKILLS_DIR} and .workshop/skills/ in the checkout.",
                "Read a repair skill's scope before recommending it; do not perform the repair.",
                f"Handoff directory: {job.handoff}",
            ]
        )
        + "\n"
    )


def read_assessment(job: Job) -> dict:
    try:
        result = json.loads(handoff_text(job, "assessment.json"))
    except json.JSONDecodeError:
        raise ToolError("the agent did not leave a valid assessment.json") from None
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("status"), str)
        or result["status"] not in STATUSES.keys() - {"ok"}
    ):
        raise ToolError("assessment.json has an invalid status")
    for key in ("summary", "next_step"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ToolError(f"assessment.json needs a nonempty {key}")
    evidence = result.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
    ):
        raise ToolError("assessment.json needs a nonempty list of evidence")
    return result


def assess(job: Job, evidence: dict) -> dict:
    (job.log_path.parent / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    result = {
        "status": "ok",
        "summary": "No failed inputs were reported in the inspected window.",
        "evidence": [],
        "next_step": "No action required for the inspected calls.",
    }
    if any(item["failures"] for item in evidence["functions"]):
        try:
            code = job.run_agent(inspection_prompt(job))
            if code:
                raise ToolError(f"the assessment agent exited with code {code}")
            result = read_assessment(job)
        except ToolError as exc:
            result = {
                "status": "unable-to-assess",
                "summary": str(exc),
                "evidence": [],
                "next_step": "Review the captured evidence and agent log, then repeat the inspection.",
            }
    gaps = [f"{item['function']}: {gap}" for item in evidence["functions"] for gap in item["gaps"]]
    result["gaps"] = gaps
    if gaps and result["status"] in {"ok", "ok-after-review"}:
        result["status"] = "unable-to-assess"
        result["summary"] = "The inspection is incomplete. " + result["summary"]
        result["next_step"] = "Resolve the coverage gaps listed below and repeat the inspection."
    return result


def report(repo: str, evidence: dict, result: dict) -> str:
    label = STATUSES[result["status"]] + (" (incomplete coverage)" if result["gaps"] else "")
    lines = [
        f"# {repo}: {label}",
        "",
        result["summary"],
        "",
        f"Modal profile `{evidence['profile']}`, environment `{evidence['environment']}`. "
        f"Inputs enqueued from {evidence['since']} to {evidence['until']} (exclusive). "
        "States are those reported at collection time.",
        "",
        markdown_table(
            ["Function", "Input states"],
            [
                [
                    item["function"],
                    ", ".join(f"{state}: {n}" for state, n in item["counts"].items()) or "No data",
                ]
                for item in evidence["functions"]
            ],
        ),
    ]
    if result["evidence"]:
        lines += ["", "Evidence:", "", *(f"- {item}" for item in result["evidence"])]
    if result["gaps"]:
        lines += ["", "Coverage gaps:", "", *(f"- {gap}" for gap in result["gaps"])]
    lines += [
        "",
        f"Next step: {result['next_step']}",
        "",
        "This checks reported inputs for the configured functions, not missed schedules, "
        "data freshness, or failures hidden by application code.",
    ]
    return "\n".join(lines) + "\n"


def inspection_job(
    repo: str,
    base: str,
    clone: Path,
    worktree: Path,
    run_dir: Path,
    args: argparse.Namespace,
    skills: list[Path] | None = None,
) -> Job:
    run_dir.mkdir(parents=True, exist_ok=True)
    handoff = run_dir / "handoff"
    handoff.mkdir()
    return Job(
        repo,
        0,
        skills or [SKILLS_DIR / "assess-failure" / "SKILL.md"],
        "",
        base,
        clone,
        {},
        worktree,
        handoff,
        run_dir / "agent.log",
        args,
        github=False,
        inspection=True,
    )


def cmd_inspect(args: argparse.Namespace) -> int:
    parse_issue_ref(args.repo + "#1")  # apply the same owner/repo validation as run
    since, until = time_window(args.since, datetime.now(timezone.utc))
    if args.agent_timeout <= 0:
        raise ToolError("--agent-timeout must be positive")
    require_gh_login()
    home = Path.home() / ".workshop"
    name = args.repo.replace("/", "-") + "-inspect"
    clone, worktree = home / "repos" / args.repo, home / "worktrees" / name
    base = prepare_worktree(args.repo, "workshop/inspect", clone, worktree)
    config = read_config(github_file(args.repo, base, CONFIG))
    run_dir = home / "logs" / name / timestamp("%Y%m%dT%H%M%S.%fZ")
    job = inspection_job(args.repo, base, clone, worktree, run_dir, args)
    evidence = collect(config, since, until)
    result = assess(job, evidence)
    output = report(args.repo, evidence, result)
    (run_dir / "report.md").write_text(output, encoding="utf-8")
    save_record(
        {
            "finished": timestamp(),
            "repo": args.repo,
            "base": base,
            "outcome": result["status"],
            "skills": skill_records(job.skills),
            "since": since,
            "until": until,
            **agent_usage(job.log_path),
        },
        home / "logs" / "inspections.jsonl",
    )
    print(output)
    print(f"Evidence and report: {run_dir}")
    return {"ok": 0, "ok-after-review": 0, "action-required": 2, "unable-to-assess": 1}[result["status"]]
