"""Inspect job history, and ask the agent to assess observed failures."""

import argparse
import json
import os
import re
import selectors
import subprocess
import time
import tomllib
import urllib.parse
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path

from workshop import (
    SKILLS_DIR,
    Job,
    ToolError,
    gh_json,
    github_file,
    handoff_text,
    markdown_table,
    parse_issue_ref,
    prepare_worktree,
    require_gh_login,
    sh,
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
MAX_LOG_BYTES = 2_000_000
MAX_LOG_FILES = 20
MAX_CALLS = 10_000
ISSUES_FILE = "issues.json"  # candidate issues, beside evidence.json
TRUSTED_AUTHORS = {"OWNER", "MEMBER", "COLLABORATOR"}  # people with push access


def candidate_issues(repo: str, since: str) -> list[dict]:
    """Open issues, and issues closed since `since`, written by people with push access.

    The tool's equivalent of the GitHub server's lockdown mode: in a public repo, a stranger's issue cannot instruct
    the agent.
    """
    start = datetime.fromisoformat(since)
    items = gh_json("api", f"repos/{repo}/issues?state=open&per_page=100") + gh_json(
        "api", f"repos/{repo}/issues?state=closed&since={urllib.parse.quote(since)}&per_page=100"
    )
    return [
        {
            "number": item["number"],
            "title": item["title"],
            "body": item["body"] or "",
            "state": item["state"],
            "closed_at": item["closed_at"],
        }
        for item in items
        if "pull_request" not in item
        and item["author_association"] in TRUSTED_AUTHORS
        and (item["state"] == "open" or datetime.fromisoformat(item["closed_at"]) >= start)
    ]


def read_config(text: str | None) -> dict:
    if text is None:
        raise ToolError(
            f"the target repository needs {CONFIG} on its default branch; "
            "use --config PATH to inspect with a local configuration"
        )
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


def modal_command(config: dict, command: str, function: str, since: str, until: str) -> list[str]:
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
    return args


def capture_log(command: list[str], path: Path, timeout: float = 120) -> tuple[bool, str]:
    """Save stdout as evidence; return its presence and bounded CLI stderr. Keep partial logs on failure."""
    deadline = time.monotonic() + timeout
    size, has_content = 0, False
    diagnostics = b""
    with path.open("wb") as output, selectors.DefaultSelector() as ready:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise ToolError(f"{command[0]} is not installed") from None
        with process:
            try:
                ready.register(process.stdout, selectors.EVENT_READ, True)
                ready.register(process.stderr, selectors.EVENT_READ, False)
                while ready.get_map():
                    remaining = deadline - time.monotonic()
                    events = ready.select(max(0, remaining))
                    if remaining <= 0 or not events:
                        raise ToolError(f"log capture timed out after {timeout:g}s")
                    for stream, _ in events:
                        chunk = os.read(stream.fd, 65_536)
                        if not chunk:
                            ready.unregister(stream.fileobj)
                        elif stream.data:
                            output.write(chunk[: MAX_LOG_BYTES - size])
                            size += len(chunk)
                            has_content |= bool(chunk.strip())
                            if size > MAX_LOG_BYTES:
                                raise ToolError(
                                    f"log capture stopped at {MAX_LOG_BYTES} bytes; file is incomplete"
                                )
                        else:
                            diagnostics = (diagnostics + chunk)[-1000:]
                if process.wait(timeout=max(0, deadline - time.monotonic())):
                    raise ToolError(f"log command exited with code {process.returncode}")
            except (ToolError, subprocess.TimeoutExpired) as exc:
                message = (
                    str(exc) if isinstance(exc, ToolError) else f"log capture timed out after {timeout:g}s"
                )
                detail = diagnostics.decode("utf-8", errors="replace").strip()
                raise ToolError(message + (f"; CLI stderr: {detail}" if detail else "")) from None
            finally:
                if process.poll() is None:
                    process.kill()
    return has_content, diagnostics.decode("utf-8", errors="replace").strip()


def selected_calls(calls: list[dict]) -> list[dict]:
    """Keep failures and the next success after each failure, in chronological order."""
    selected, awaiting_recovery = [], False
    for call in calls:
        if call["status"] in FAILURES:
            selected.append(call)
            awaiting_recovery = True
        elif call["status"] == "success" and awaiting_recovery:
            selected.append(call)
            awaiting_recovery = False
    return selected


def capture_order(calls: list[dict]) -> tuple[list[str], list[str]]:
    """Failed and recovery call IDs to capture, each without duplicates. The first and latest failures lead."""
    selected = selected_calls(calls)
    failed = list(dict.fromkeys(call["function_call_id"] for call in selected if call["status"] in FAILURES))
    if len(failed) > 2:
        failed = [failed[0], failed[-1], *failed[1:-1]]
    seen = set(failed)
    recovered = dict.fromkeys(
        call["function_call_id"]
        for call in selected
        if call["status"] not in FAILURES and call["function_call_id"] not in seen
    )
    return failed, list(recovered)


def round_robin(queues: list[list]) -> list:
    """One entry from each queue in turn, until all are empty."""
    return [entry for row in zip_longest(*queues) for entry in row if entry is not None]


def save_call_history(item: dict, calls: list[dict], path: Path) -> None:
    path.write_text("".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8")
    item["history_path"] = str(path)


def list_calls(item: dict, calls: list[dict]) -> None:
    """List the first selected input of each call with log files, and the first and last selected inputs.

    One call can hold many inputs, so this bounds the manifest by the number of log files. Capture planning does
    not use this list, and the history file keeps every input.
    """
    selected = selected_calls(calls)
    logged = {entry.get("call_id") for entry in item["log_files"]}
    listed, seen = [], set()
    for index, call in enumerate(selected):
        call_id = call["function_call_id"]
        if index in (0, len(selected) - 1) or (call_id in logged and call_id not in seen):
            listed.append(call)
            seen.add(call_id)
    item["calls"] = listed
    item["unlisted_calls"] = len(calls) - len(listed)


def collect(config: dict, since: str, until: str, run_dir: Path) -> dict:
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    history_dir = run_dir / "history"
    history_dir.mkdir()
    captured = 0

    def fetch_log(item: dict, call_id: str = "") -> bool:
        nonlocal captured
        path = log_dir / f"{captured:03}.log"
        captured += 1
        command = modal_command(config, "logs", item["function"], since, until)
        if call_id:
            command += ["--function-call", call_id]
        entry = {"call_id": call_id, "path": str(path), "scope": "call" if call_id else "function"}
        item["log_files"].append(entry)
        label = call_id or "function-level fallback"
        try:
            has_content, diagnostics = capture_log(command, path)
        except ToolError as exc:
            item["gaps"].append(f"{label}: {exc}")
            return False
        if not has_content:
            stderr = f"; CLI stderr: {diagnostics}" if diagnostics else ""
            item["gaps"].append(f"{label}: Modal returned no logs{stderr}")
            return True
        if diagnostics:  # a note, not a gap: the log itself was captured
            entry["cli_stderr"] = diagnostics
        return False

    evidence = {
        "since": since,
        "until": until,
        "profile": config["profile"],
        "environment": config["environment"],
        "functions": [],
    }
    histories, failed, recovered = [], [], []
    for index, name in enumerate(config["functions"]):
        item = {"function": name, "calls": [], "counts": {}, "failures": 0, "gaps": [], "log_files": []}
        evidence["functions"].append(item)
        calls = []
        try:
            calls = json.loads(sh(modal_command(config, "calls", name, since, until), timeout=120).stdout)
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
            calls.sort(key=lambda call: datetime.fromisoformat(call["enqueued_at"]))
            save_call_history(item, calls, history_dir / f"{index:03}.jsonl")
            item["counts"] = dict(Counter(call["status"] for call in calls))
            item["failures"] = sum(count for state, count in item["counts"].items() if state in FAILURES)
            unknown = set(item["counts"]) - FAILURES - PENDING - {"success"}
            if unknown:
                item["gaps"].append(f"unrecognized input states: {', '.join(sorted(unknown))}")
            if not calls:
                item["gaps"].append("no inputs in this window; expected schedules are not checked")
            if PENDING & item["counts"].keys():
                item["gaps"].append("some inputs are still pending or running")
        except (ToolError, ValueError, TypeError) as exc:
            item["gaps"].append(f"could not read call history: {str(exc)[-1000:]}")
            calls = []
        histories.append((item, calls))
        if item["failures"]:
            failed_ids, recovered_ids = capture_order(calls)
            failed.append([(item, call_id) for call_id in failed_ids])
            recovered.append([(item, call_id) for call_id in recovered_ids])
    # Jobs take turns, failures first, so one job that fails every run cannot use up the captures.
    skipped, fallback = Counter(), set()
    for item, call_id in round_robin(failed) + round_robin(recovered):
        if captured == MAX_LOG_FILES:
            skipped[item["function"]] += 1
        elif fetch_log(item, call_id):
            fallback.add(item["function"])
    for item, calls in histories:
        if count := skipped[item["function"]]:
            item["gaps"].append(f"log capture limit reached; {count} selected calls not collected")
        if item["function"] in fallback and captured < MAX_LOG_FILES:
            fetch_log(item)
        list_calls(item, calls)
    return evidence


def inspection_prompt(job: Job) -> str:
    return (
        "\n".join(
            [
                "Assess the observed job failures in this inspection. Read and follow these skills:",
                *(str(path) for path in job.skills),
                f"Repository: {job.repo}. Checkout commit: {job.base} (not necessarily the deployed revision).",
                f"Repository checkout: {job.worktree}. Confine repository searches to this path.",
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
        "summary": "No failed runs were reported in the inspected window.",
        "evidence": [],
        "next_step": "No action required for the inspected jobs.",
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


# --- the publish check: issue text may name what the repository and the run metadata already show

EDGE = "\"'`()[]{}<>,;:!?*."  # stripped from both ends of a word
PIECES = re.compile(r"`[^`\n]+`|\S+")  # a backtick span, or a word
LINE_REF = re.compile(r"(.+):\d+(?:-\d+)?")  # a file and line, such as models/orders.sql:12


def words(text: str) -> list[str]:
    return [word for word in (piece.strip(EDGE) for piece in text.split()) if word]


def time_tokens(value: str) -> set[str]:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return {value}
    return {value, moment.date().isoformat(), moment.strftime("%H:%M"), moment.strftime("%H:%M:%S")}


def known_tokens(worktree: Path, evidence: dict, candidates: list[dict]) -> set[str]:
    """The repository's words and file paths, and the run metadata.

    Never the paths of saved logs or history, nor history fields other than the call ID, state and enqueue time:
    those can hold source data.
    """
    known = set()
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            path = Path(root, name)
            if name == ".git" or path.is_symlink():
                continue
            known.add(path.relative_to(worktree).as_posix())
            known.update(words(path.read_text(encoding="utf-8", errors="replace")))
    known |= time_tokens(evidence["since"]) | time_tokens(evidence["until"])
    for item in evidence["functions"]:
        known.add(item["function"])
        known.update(item["counts"])
        known.update(str(count) for count in [*item["counts"].values(), item.get("failures", 0)])
        if item.get("history_path"):
            for line in Path(item["history_path"]).read_text(encoding="utf-8").splitlines():
                call = json.loads(line)
                known.update((call["function_call_id"], call["status"]))
                known |= time_tokens(call["enqueued_at"])
    for issue in candidates:
        known.update((str(issue["number"]), f"#{issue['number']}"))
    return known


def unknown_tokens(text: str, known: set[str]) -> list[str]:
    """Numbers, words containing @ / _ or ., and words in backticks that `known` does not hold, in text order."""
    missing = []
    for match in PIECES.finditer(text):
        piece = match[0]
        code = len(piece) > 1 and piece[0] == piece[-1] == "`"
        for word in words(piece):
            if not (code or any(char.isdigit() or char in "@/_." for char in word)):
                continue
            ref = LINE_REF.fullmatch(word)
            if word not in known and not (ref and ref[1] in known) and word not in missing:
                missing.append(word)
    return missing


def copied_runs(text: str, logs: list[Path], size: int = 8) -> list[str]:
    """Runs of `size` words in `text` that also appear in a log, ignoring case, in text order."""
    mine = [word.lower() for word in words(text)]
    runs = [tuple(mine[index : index + size]) for index in range(len(mine) - size + 1)]
    wanted, found = set(runs), set()
    for path in logs if wanted else []:
        window = deque(maxlen=size)
        try:
            with open(path, encoding="utf-8", errors="replace") as log:
                for line in log:
                    for word in words(line):
                        window.append(word.lower())
                        if tuple(window) in wanted:
                            found.add(tuple(window))
        except OSError:  # a log that was never saved is a coverage gap, reported elsewhere
            continue
    return list(dict.fromkeys(" ".join(run) for run in runs if run in found))


def report(repo: str, evidence: dict, result: dict, *, verbose: bool = False) -> str:
    label = STATUSES[result["status"]] + (" (incomplete coverage)" if result["gaps"] else "")
    lines = [f"{'# ' if verbose else ''}{repo}: {label}", "", result["summary"]]
    if verbose:
        lines += [
            "",
            f"Modal profile `{evidence['profile']}`, environment `{evidence['environment']}`. "
            f"Inputs enqueued from {evidence['since']} to {evidence['until']} (exclusive). "
            "States are those reported at collection time.",
            "",
            markdown_table(
                ["Job", "Input states"],
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
        notes = [
            f"{item['function']} {entry.get('call_id') or 'function-level'}: {entry['cli_stderr']}"
            for item in evidence["functions"]
            for entry in item["log_files"]
            if entry.get("cli_stderr")
        ]
        if notes:
            lines += ["", "CLI stderr from captured logs:", "", *(f"- {note}" for note in notes)]
    if result["gaps"]:
        lines += ["", "Coverage gaps:", "", *(f"- {gap}" for gap in result["gaps"])]
    lines += ["", f"Next step: {result['next_step']}"]
    if verbose:
        lines += [
            "",
            "This checks reported outcomes for the configured jobs, not missed schedules, "
            "data freshness, or failures hidden by application code.",
        ]
    return "\n".join(lines) + "\n"


def inspection_job(
    repo: str, base: str, clone: Path, worktree: Path, run_dir: Path, args: argparse.Namespace
) -> Job:
    handoff = run_dir / "handoff"
    handoff.mkdir(parents=True)
    return Job(
        repo,
        0,
        [SKILLS_DIR / "assess-failure" / "SKILL.md"],
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
    config = None
    if args.config is not None:
        try:
            config_text = args.config.expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolError(f"could not read inspection config {args.config}: {exc}") from None
        config = read_config(config_text)
    require_gh_login()
    home = Path.home() / ".workshop"
    name = args.repo.replace("/", "-") + "-inspect"
    clone, worktree = home / "repos" / args.repo, home / "worktrees" / name
    base = prepare_worktree(args.repo, "workshop/inspect", clone, worktree)
    if config is None:
        config = read_config(github_file(args.repo, base, CONFIG))
    run_dir = home / "logs" / name / timestamp("%Y%m%dT%H%M%S.%fZ")
    job = inspection_job(args.repo, base, clone, worktree, run_dir, args)
    evidence = collect(config, since, until, run_dir)
    result = assess(job, evidence)
    output = report(args.repo, evidence, result, verbose=True)
    (run_dir / "report.md").write_text(output, encoding="utf-8")
    print(output if args.verbose else report(args.repo, evidence, result))
    print(f"Report: {run_dir / 'report.md'}")
    return {"ok": 0, "ok-after-review": 0, "action-required": 2, "unable-to-assess": 1}[result["status"]]
