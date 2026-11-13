#!/usr/bin/env python3
"""workshop: have a coding agent work a labelled GitHub issue into a pull request."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
SKILLS_DIR = TOOL_ROOT / "skills"
REPO_SKILLS = Path(".workshop/skills")  # a target repo's own skills, read from its default branch

LABELS = {  # name: (color, description)
    "workshop:queued": ("5319e7", "Ready for a run. Applied by the owner."),
    "workshop:working": ("fbca04", "A run is in progress."),
    "workshop:waiting": ("d93f0b", "Waiting for the owner. The latest workshop comment says why."),
}

CHECKS_POLL_S = 10  # seconds between looks at a pull request's checks
NO_CHECKS_GRACE_S = 120  # how long checks may take to appear before we decide there are none
FAILED_LOG_CHARS = 10_000  # kept from the end of each failed workflow run's log
NOTE_CHARS = 60_000  # kept of the agent's note: GitHub takes 65,536 characters in a comment, the run record included
SH_TIMEOUT_S = 600  # for each command the tool runs itself: a stalled one would leave the issue labelled working
# For those commands: git and gh fail rather than ask at the terminal, and git ignores replacement refs, which the
# agent can write in the clone.
TOOL_ENV = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


class ToolError(Exception):
    """Stops the tool with exit code 1."""


def status(message: str) -> None:
    print(f"workshop: {message}", file=sys.stderr, flush=True)


def sh(cmd: list, *, cwd: Path | None = None, input: str | None = None, check: bool = True,
       env: dict | None = None, timeout: float = SH_TIMEOUT_S) -> subprocess.CompletedProcess:
    cmd = [str(part) for part in cmd]
    try:
        result = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env={**(os.environ if env is None else env), **TOOL_ENV},
                                timeout=timeout)
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise ToolError(f"`{' '.join(cmd)}` did not finish within {timeout:g}s") from None
    if check and result.returncode != 0:
        raise ToolError(f"`{' '.join(cmd)}` failed: {(result.stderr or result.stdout).strip()}")
    return result


def gh(*args, **kwargs) -> subprocess.CompletedProcess:
    return sh(["gh", *args], **kwargs)


def gh_json(*args):
    return json.loads(gh(*args).stdout or "null")


def git(*args, **kwargs) -> subprocess.CompletedProcess:
    return sh(["git", *args], **kwargs)


def timestamp(fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    """The time now in UTC, by default as records give it."""
    return datetime.now(timezone.utc).strftime(fmt)


def duration(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s"


def markdown_table(headers: list[str], rows: list[list]) -> str:
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers),
                      *("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)])


# --- arguments and safety


def parse_issue_ref(text: str) -> tuple[str, int]:
    """'owner/repo#12' -> ('owner/repo', 12)"""
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#([1-9][0-9]*)", text, re.ASCII)
    if not m:
        raise ToolError(f"expected owner/repo#number, got {text!r}")
    return m[1], int(m[2])


def parse_skills(value: str) -> list[str]:
    names = list(dict.fromkeys(name.strip() for name in value.split(",")))
    for name in names:
        if name == "workshop":
            raise ToolError("the workshop skill is always included; do not list it in --skills")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            raise ToolError(f"bad skill name {name!r}: use lowercase letters, digits and hyphens")
    return names


def find_skills(names: list[str], repo_skills: Path) -> list[Path]:
    """The SKILL.md of the workshop skill, then of each job skill.

    A skill in `repo_skills`, the target repo's own, replaces the tool's skill of the same name. The workshop skill
    is always the tool's.
    """
    found = [SKILLS_DIR / "workshop" / "SKILL.md"]
    for name in names:
        candidates = [repo_skills / name / "SKILL.md", SKILLS_DIR / name / "SKILL.md"]
        path = next((path for path in candidates if path.is_file()), None)
        if path is None:
            raise ToolError(f"unknown skill {name!r}: neither the target repo's {REPO_SKILLS / name / 'SKILL.md'} "
                            f"nor workshop's skills/{name}/SKILL.md exists")
        found.append(path)
    return found


def require_gh_login() -> None:
    if gh("auth", "status", check=False).returncode != 0:
        raise ToolError("gh is not authenticated; run `gh auth login`")


# --- GitHub and git


def view_issue(repo: str, issue: int, fields: str) -> dict:
    return gh_json("issue", "view", issue, "--repo", repo, "--json", fields)


def set_labels(repo: str, issue: int, add: list[str], remove: list[str]) -> None:
    """Add and remove labels on the issue, touching only those that change."""
    current = {label["name"] for label in view_issue(repo, issue, "labels")["labels"]}
    changes = [f"--add-label={name}" for name in add if name not in current]
    changes += [f"--remove-label={name}" for name in remove if name in current]
    if changes:
        gh("issue", "edit", issue, "--repo", repo, *changes)


def default_branch(clone: Path) -> tuple[str, str]:
    """The target repo's default branch and its commit, asked of GitHub.

    Not read from the clone: the agent can write the clone's refs, `origin/HEAD` among them.
    """
    listing = git("-C", clone, "ls-remote", "--symref", "origin", "HEAD").stdout
    name = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", listing, re.M)
    commit = re.search(r"^(\w+)\tHEAD$", listing, re.M)
    if not (name and commit):
        raise ToolError(f"could not find the default branch of the repository {clone} was cloned from")
    return name[1], commit[1]


def github_file(repo: str, commit: str, path: str) -> str | None:
    """A file in the target repo at `commit`, or None if it has none.

    Read from GitHub, never from the clone: the agent can write the clone's objects, and git does not check an
    object's content against its name.
    """
    result = gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={commit}",
                check=False)
    if result.returncode == 0:
        return result.stdout
    if "HTTP 404" in result.stderr:
        return None
    raise ToolError(f"could not read {path} in {repo} from GitHub: {result.stderr.strip()}")


def copy_repo_skills(repo: str, commit: str, names: list[str], into: Path) -> None:
    """Copy the target repo's own SKILL.md for each named skill that has one, as it is on GitHub at `commit`."""
    shutil.rmtree(into, ignore_errors=True)
    for name in names:
        text = github_file(repo, commit, f"{REPO_SKILLS}/{name}/SKILL.md")
        if text is not None:
            (into / name).mkdir(parents=True)
            (into / name / "SKILL.md").write_text(text, encoding="utf-8")


def prepare_worktree(repo: str, branch: str, clone: Path, worktree: Path) -> str:
    """Clone or fetch the target repo, create or reuse the job's worktree on `branch`, and reset it to GitHub.

    Return the default branch's commit on GitHub.
    """
    if not (clone / ".git").exists():
        status(f"cloning {repo} into {clone}")
        gh("repo", "clone", repo, clone, "--", "--no-checkout")  # files are checked out only in the worktrees
    default, base = default_branch(clone)
    status(f"fetching {repo}")
    git("-C", clone, "fetch", "--prune", "origin")  # after asking for `base`, so the clone has it
    git("-C", clone, "remote", "set-head", "origin", default)  # origin/HEAD is for the agent: the tool uses `base`
    if (worktree / ".git").exists():
        status(f"reusing {worktree}")
    else:
        status(f"creating {worktree} on {branch}")
        git("-C", clone, "worktree", "prune")
        # Check out the branch if it exists locally or on the remote; otherwise start it from the default branch.
        if git("-C", clone, "worktree", "add", worktree, branch, check=False).returncode != 0:
            git("-C", clone, "worktree", "add", "--no-track", "-b", branch, worktree, base)
    # Start from what is on GitHub: the pushed branch, or the default branch if nothing is pushed yet.
    pushed = git("-C", clone, "rev-parse", "--verify", "--quiet", f"origin/{branch}", check=False).returncode == 0
    start = f"origin/{branch}" if pushed else base
    status(f"resetting {worktree} to {start}")
    git("-C", worktree, "reset", "--hard", start)
    git("-C", worktree, "clean", "-fd")
    return base


# --- the job


@dataclass
class Outcome:
    name: str  # pr-open, needs-info or escalated
    reason: str
    pr_url: str = ""
    fix_attempts: int = 0
    note: str = ""  # from the agent: its question, or for the requester once checks pass


@dataclass
class Job:
    repo: str
    issue: int
    skills: list[Path]  # each skill's SKILL.md, the workshop skill first
    branch: str
    base: str  # the default branch's commit on GitHub when the run started
    clone: Path
    settings: dict  # the target repo's .claude/settings.json on its default branch, read once per run
    worktree: Path
    handoff: Path  # where the agent leaves question.md, pull-request.md and comment.md for the tool to publish
    log_path: Path
    args: argparse.Namespace
    agent_s: float = 0.0  # time spent in the agent this run
    checks_s: float = 0.0  # time spent waiting for checks this run
    pushed: str = ""  # the commit the tool last pushed this run
    github: bool = True  # the read-only GitHub server; evals run without it
    agent_args: list[str] = field(default_factory=list)  # more for the agent's command line, such as a spending cap
    validation: dict = field(default_factory=lambda: {
        "source": "github-checks", "status": "unverified", "reason": "not-checked",
        "head_sha": "", "observed_at": "", "checks": [],
    })

    def run_agent(self, prompt: str) -> int:
        status(f"running the agent (log: {self.log_path})")
        started = time.monotonic()
        code = invoke_agent(self, prompt)
        self.agent_s += time.monotonic() - started
        status(f"agent exited with code {code}")
        return code


# --- the agent: Claude Code, with the prompt on stdin.
# It holds no GitHub credentials: it reads GitHub through a read-only server, and the tool does every write.


def repo_settings(repo: str, commit: str) -> dict:
    """The target repo's .claude/settings.json on its default branch, where the agent's commits cannot change it."""
    text = github_file(repo, commit, ".claude/settings.json")
    try:
        return {} if text is None else json.loads(text)
    except json.JSONDecodeError:
        raise ToolError("the target repo's .claude/settings.json is not valid JSON") from None


# The only variables the agent gets from the shell that started the run, so its commands see none of its secrets.
AGENT_ENV = ("PATH", "HOME", "USER", "LANG", "SHELL", "TMPDIR")
CLAUDE_LOGIN = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")  # hidden from commands


def agent_env(job: Job) -> dict[str, str]:
    """The agent's environment. The target repo's `env` setting reaches only its commands, not Claude Code itself:
    Claude Code runs the CLAUDE_ENV_FILE script before each command."""
    env_file = job.log_path.parent / "env.sh"
    env_file.write_text("".join(f"export {name}={shlex.quote(str(value))}\n"
                                for name, value in job.settings.get("env", {}).items()))
    return {**kept_env(), "CLAUDE_ENV_FILE": str(env_file)}


def kept_env() -> dict[str, str]:
    return {name: os.environ[name] for name in (*AGENT_ENV, *CLAUDE_LOGIN) if name in os.environ}


def claude_settings(job: Job) -> dict:
    """Run every Bash command in Claude Code's sandbox (code.claude.com/docs/en/sandboxing).

    Commands write only to the worktree, the handoff directory and temp, read nothing else in the home directory,
    and reach no network host. Claude Code keeps commands and its edit tools out of `.git` in the worktree and out of
    git's hooks and config, so the tool can run git in the worktree after the agent has.
    The target repo adds what its tools need, and its deny and ask rules, with the same keys in its settings.
    Only those keys are taken: settings on the command line are trusted more than a repository's own.
    """
    sandbox, permissions = job.settings.get("sandbox", {}), job.settings.get("permissions", {})
    network, filesystem = sandbox.get("network", {}), sandbox.get("filesystem", {})
    return {
        "permissions": {"deny": permissions.get("deny", []), "ask": permissions.get("ask", [])},
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "credentials": {"envVars": [{"name": name, "mode": "deny"} for name in CLAUDE_LOGIN]},
            "network": {"allowedDomains": network.get("allowedDomains", [])},
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [str(job.worktree), str(job.clone / ".git"), str(job.handoff), "~/.gitconfig",
                              *filesystem.get("allowRead", [])],
                "allowWrite": [str(job.handoff), *filesystem.get("allowWrite", [])],
            },
        },
    }


# GitHub's MCP server, read-only. It runs outside the sandbox and gets the token from gh itself,
# so the token is on no command line and not in the agent's environment.
# Lockdown mode hides what people without push access wrote in a public repo, so a stranger cannot instruct the agent.
GITHUB_MCP = ('GITHUB_PERSONAL_ACCESS_TOKEN="$(gh auth token)" '
              "exec github-mcp-server stdio --read-only --lockdown-mode --toolsets issues,pull_requests")


def claude_command(job: Job) -> list[str]:
    """Claude Code in headless print mode.

    Checked against Claude Code 2.1 and code.claude.com/docs/en/headless and /permissions.
    With --permission-mode dontAsk, every tool call that no rule allows is denied.
    Bash is allowed outright: the sandbox confines every command. Left to the sandbox to allow, a command with "=" in
    an argument, as in most SQL, is denied. The target repo's deny and ask rules still win over the allow.
    Monitor is disallowed: Bash allow rules would approve its commands, and it is not known to run them in the sandbox.
    In a permission rule, "//" starts an absolute path.
    stream-json writes each step to the log as it happens; plain text only writes the final message.
    The agent loads no settings files and no MCP server but GitHub's, and in evals none at all.
    """
    servers = {"github": {"command": "sh", "args": ["-c", GITHUB_MCP]}} if job.github else {}
    allowed = ["Bash", *(f"Read(/{path.parent}/**)" for path in job.skills),
               f"Edit(/{job.worktree}/**)", f"Edit(/{job.handoff}/**)", *(["mcp__github"] if job.github else [])]
    return ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools", ",".join(allowed),
            "--disallowedTools", "Monitor",
            "--settings", json.dumps(claude_settings(job)), "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers}),
            "--output-format", "stream-json", "--verbose", *job.agent_args]


def invoke_agent(job: Job, prompt: str) -> int:
    """Run the agent non-interactively in the job's worktree. Return its exit code (124 on timeout)."""
    worktree, timeout_s, log_path = job.worktree, job.args.agent_timeout, job.log_path
    cmd = claude_command(job)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = agent_env(job)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"=== {timestamp()} prompt:\n{prompt}\n=== output:\n")
        log.flush()
        try:
            proc = subprocess.Popen(cmd, cwd=worktree, env=env, stdin=subprocess.PIPE, stdout=log,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except FileNotFoundError:
            raise ToolError(f"agent executable not found: {cmd[0]}") from None
        try:
            proc.communicate(prompt, timeout=timeout_s)
        except BaseException as exc:  # timeout, Ctrl-C or SIGTERM: stop the agent and everything it started
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            log.write(f"\n=== timed out after {timeout_s}s\n")
            return 124
        log.write(f"\n=== exited with code {proc.returncode}\n")
        return proc.returncode


TOKENS = {"input": "input_tokens", "output": "output_tokens",
          "cache_read": "cache_read_input_tokens", "cache_creation": "cache_creation_input_tokens"}


def agent_usage(log_path: Path) -> dict:
    """What the agent did in this run's log, from Claude Code's stream-json events. A run that timed out has no cost."""
    usage = {"agent": "claude", "agent_version": "", "model": "", "turns": 0, "cost_usd": 0.0,
             "tokens": dict.fromkeys(TOKENS, 0), "tool_calls": Counter(), "denied_tool_calls": Counter()}
    in_output = False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else []:
        if line.startswith("=== "):  # invoke_agent's markers: only the output sections hold events
            in_output = line == "=== output:"
            continue
        try:
            event = json.loads(line) if in_output else None
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            usage["agent_version"], usage["model"] = event.get("claude_code_version", ""), event.get("model", "")
        elif event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    usage["tool_calls"][block["name"]] += 1
        elif event.get("type") == "result":
            usage["turns"] += event.get("num_turns", 0)
            usage["cost_usd"] += event.get("total_cost_usd", 0.0)
            for name, key in TOKENS.items():
                usage["tokens"][name] += event.get("usage", {}).get(key, 0)
            for denial in event.get("permission_denials", []):
                usage["denied_tool_calls"][denial.get("tool_name", "")] += 1
    usage["cost_usd"] = round(usage["cost_usd"], 4)
    return usage


def job_lines(job: Job) -> list[str]:
    return [*(f"  {path}" for path in job.skills),
            f"Target repository: {job.repo}. Issue: #{job.issue}. Branch: {job.branch}.",
            f"Handoff directory: {job.handoff}"]


def initial_prompt(job: Job, issue: str = "") -> str:
    """The first prompt of a run. Evals have no GitHub tools, so they give the issue thread as `issue`."""
    read = "Then read the repository's AGENTS.md if present"
    return "\n".join([
        "You are working a job in a workshop. Read these files first and follow them exactly:",
        *job_lines(job),
        f"{read}. This job has no github tools: here is the issue with all its comments.\n\n{issue}" if issue
        else f"{read}, and the issue with all its comments.",
    ]) + "\n"


def fix_prompt(job: Job, pr: int | None, failing: list[str], logs: str) -> str:
    return "\n".join([
        f"{f'Pull request #{pr} has' if pr else 'Your change has'} failing checks. Read the same skill files again:",
        *job_lines(job),
        f"Failing checks: {', '.join(failing)}",
        "Logs:",
        logs,
        "Fix the cause and commit it on the same branch.",
    ]) + "\n"


# --- handoff: the tool publishes what the agent left


def handoff_text(job: Job, name: str) -> str:
    """A file the agent left in the handoff directory, or "" if there is none.

    The tool reads it outside the sandbox, so links are refused: one could point at a file the agent cannot read.
    """
    path = job.handoff / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    except OSError:  # a symbolic link
        raise ToolError(f"{path} is not a plain file") from None
    with open(fd, encoding="utf-8", errors="replace") as file:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError(f"{path} is not a plain file")
        return file.read().strip()


def open_pr(job: Job) -> dict | None:
    prs = gh_json("pr", "list", "--repo", job.repo, "--head", job.branch, "--state", "open", "--json", "number,url")
    return prs[0] if prs else None


def publish_commits(job: Job) -> dict | None:
    """Push the job branch if it has commits the default branch lacks, then open or update its pull request from
    pull-request.md. Return the open pull request, if there is one.

    The tool pushes the job branch by name, so no other branch is ever pushed.
    """
    ahead = int(git("-C", job.clone, "rev-list", "--count", f"{job.base}..refs/heads/{job.branch}").stdout)
    if ahead:
        status(f"pushing {job.branch}")
        git("-C", job.clone, "push", "origin", f"refs/heads/{job.branch}")
        job.pushed = git("-C", job.clone, "rev-parse", f"refs/heads/{job.branch}").stdout.strip()
    pr = open_pr(job)
    title, _, body = handoff_text(job, "pull-request.md").partition("\n")
    if title and pr:
        gh("pr", "edit", pr["number"], "--repo", job.repo, "--title", title, "--body-file", "-", input=body.strip())
    elif title and ahead:
        status("opening a pull request")
        gh("pr", "create", "--repo", job.repo, "--head", job.branch, "--title", title, "--body-file", "-",
           input=body.strip(), cwd=job.clone)
        pr = open_pr(job)
    return pr


# --- checks


def pr_checks(repo: str, pr: int) -> tuple[str, list[dict]]:
    """The head and its reported checks. Includes legacy commit statuses and gh's paginated check results."""
    info = gh_json("pr", "view", pr, "--repo", repo, "--json", "headRefOid,statusCheckRollup")
    reported = info.get("statusCheckRollup") or []
    if len(reported) >= 100:  # gh may have fetched additional pages while the head changed
        head = gh_json("pr", "view", pr, "--repo", repo, "--json", "headRefOid")["headRefOid"]
        if head != info["headRefOid"]:
            return head, []
    checks = []
    for check in reported:
        state = (check.get("conclusion") if check.get("status") == "COMPLETED"
                 else check.get("status") or check.get("state")) or "PENDING"
        if state == "SUCCESS":
            bucket = "pass"
        elif state in ("SKIPPED", "NEUTRAL"):
            bucket = "skipping"
        elif state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"):
            bucket = "fail"
        else:
            bucket = "pending"
        checks.append({"name": check.get("name") or check.get("context"), "bucket": bucket, "state": state,
                       "link": check.get("detailsUrl") or check.get("targetUrl") or ""})
    return info["headRefOid"], checks


def wait_for_checks(repo: str, pr: int, timeout_s: int, head: str = "") -> dict:
    """Record validation of one commit. No checks or only skipped checks do not establish a pass."""
    start = time.monotonic()
    while True:
        observed_head, checks = pr_checks(repo, pr)
        head = head or observed_head
        result = {"source": "github-checks", "status": "unverified", "reason": "",
                  "head_sha": head, "observed_at": timestamp(), "checks": checks}
        if observed_head != head:
            return {**result, "reason": "head-changed", "observed_head_sha": observed_head, "checks": []}
        elapsed = time.monotonic() - start
        failing = any(check["bucket"] == "fail" for check in checks)
        if checks and all(check["bucket"] != "pending" for check in checks):
            if failing:
                return {**result, "status": "failed", "reason": "checks-failed"}
            if any(check["bucket"] == "pass" for check in checks):
                return {**result, "status": "passed", "reason": "reported-checks-passed"}
            return {**result, "reason": "all-skipped"}
        if not checks and elapsed >= NO_CHECKS_GRACE_S:
            return {**result, "reason": "no-checks"}
        if elapsed >= timeout_s:
            return {**result, "status": "failed" if failing else "unverified", "reason": "timed-out"}
        time.sleep(CHECKS_POLL_S)


def failing_logs(repo: str, failing: list[dict]) -> str:
    """The end of each failed GitHub Actions run's log."""
    run_ids = dict.fromkeys(re.findall(r"/actions/runs/(\d+)", " ".join(check["link"] for check in failing)))
    logs = [f"--- run {run_id}\n" + gh("run", "view", run_id, "--repo", repo, "--log-failed",
                                       check=False).stdout.strip()[-FAILED_LOG_CHARS:]
            for run_id in run_ids]
    return "\n".join(logs) or "(no GitHub Actions logs found; see the checks on the pull request)"


# --- outcomes


def determine_outcome(job: Job, code: int) -> Outcome:
    """Publish what the agent left, then follow the pull request's checks. A question wins over a pull request."""
    if question := handoff_text(job, "question.md"):
        return Outcome("needs-info", "the agent has a question", note=question)
    pr = publish_commits(job)
    if pr:
        return prove(job, pr)
    # The log stays on this machine: it can hold anything a command printed, and the issue may be public.
    log = Path("~") / job.log_path.relative_to(Path.home())
    ended = f"timed out after {job.args.agent_timeout}s" if code == 124 else f"exited with code {code}"
    return Outcome("escalated", f"the agent {ended} without leaving a question or a pull request; its log is {log}")


def prove(job: Job, pr: dict) -> Outcome:
    """Wait for the pull request's checks, sending the agent back to fix failures."""
    attempts = 0
    while True:
        status(f"waiting for checks on {pr['url']}")
        started = time.monotonic()
        # Clear the previous round's result before observing a new commit, even if the GitHub request fails.
        job.validation = {"source": "github-checks", "status": "unverified", "reason": "not-checked",
                          "head_sha": job.pushed, "observed_at": "", "checks": []}
        job.validation = wait_for_checks(job.repo, pr["number"], job.args.checks_timeout, job.pushed)
        job.checks_s += time.monotonic() - started
        state, reason = job.validation["status"], job.validation["reason"]
        if state == "passed" or reason in ("no-checks", "all-skipped"):
            reason = {"reported-checks-passed": "reported checks passed", "no-checks": "no checks ran",
                      "all-skipped": "all reported checks were skipped or neutral"}[reason]
            return Outcome("pr-open", reason, pr["url"], attempts, handoff_text(job, "comment.md"))
        if reason == "head-changed":
            return Outcome("escalated", "the pull request head changed while checking the pushed commit",
                           pr["url"], attempts)
        if reason == "timed-out":
            return Outcome("escalated", f"checks did not finish within {job.args.checks_timeout}s", pr["url"],
                           attempts)
        failing = [check for check in job.validation["checks"] if check["bucket"] == "fail"]
        names = sorted({check["name"] for check in failing})
        if attempts >= job.args.max_fix_attempts:
            return Outcome("escalated", f"checks still failing after {attempts} fix attempts: {', '.join(names)}",
                           pr["url"], attempts)
        attempts += 1
        status(f"checks failed ({', '.join(names)}); fix attempt {attempts} of {job.args.max_fix_attempts}")
        job.run_agent(fix_prompt(job, pr["number"], names, failing_logs(job.repo, failing)))
        if question := handoff_text(job, "question.md"):
            return Outcome("needs-info", "the agent has a question", pr["url"], attempts, question)
        publish_commits(job)


def summary(outcome: Outcome, job: Job, record: dict) -> str:
    lines = [f"**workshop: {outcome.name}**: {outcome.reason}.", ""]
    if outcome.pr_url:
        lines += [f"- Pull request: {outcome.pr_url}",
                  f"- Fix attempts: {outcome.fix_attempts} of {job.args.max_fix_attempts}"]
    lines.append(f"- Agent time: {duration(job.agent_s)}. API-equivalent cost: ${record['cost_usd']:.2f}.")
    validation = record["validation"]
    revision = f" at `{validation['head_sha'][:12]}`" if validation["head_sha"] else ""
    lines.append(f"- GitHub checks: {validation['status']}{revision} ({validation['reason'].replace('-', ' ')}).")
    # The run record for `workshop score`, not shown. With no "<" or ">", nothing in it can end the comment early.
    hidden = json.dumps(record).replace("<", "\\u003c").replace(">", "\\u003e")
    footer = f"\n\n<!-- workshop-run: {hidden} -->\n"
    body = "\n".join(lines)
    if len(body) + len(footer) > 65_536:
        raise ToolError("the run record is too large for a GitHub comment")
    if outcome.note:
        cut = "\n\n(cut here: too long for a comment)"
        available = min(NOTE_CHARS, 65_536 - len(body) - len(footer) - len(cut) - 2)
        if available < 0:
            raise ToolError("the run record is too large for a GitHub comment")
        body += "\n\n" + outcome.note[:available] + (cut if len(outcome.note) > available else "")
    return body + footer


# --- telemetry: one record per run, in ~/.workshop/logs/runs.jsonl and hidden in the run's issue comment


def skill_records(paths: list[Path]) -> list[dict]:
    return [{"name": path.parent.name, "from": "workshop" if path.is_relative_to(SKILLS_DIR) else "target repo",
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:12]} for path in paths]


def run_record(job: Job, outcome: Outcome | None, error: str, total_s: float) -> dict:
    return {
        "finished": timestamp(),
        "repo": job.repo,
        "issue": job.issue,
        "skills": skill_records(job.skills),
        "outcome": outcome.name if outcome else "error",
        "reason": outcome.reason if outcome else error,
        "fix_attempts": outcome.fix_attempts if outcome else 0,
        "pr_url": outcome.pr_url if outcome else "",
        "pushed": job.pushed,
        "validation": job.validation,
        "seconds": {"total": round(total_s), "agent": round(job.agent_s), "checks": round(job.checks_s)},
        **agent_usage(job.log_path),
    }


def save_record(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as records:
        records.write(json.dumps(record) + "\n")


def cmd_run(args: argparse.Namespace) -> int:
    repo, issue = parse_issue_ref(args.ref)
    names = parse_skills(args.skills)
    require_gh_login()
    status(f"reading {repo}#{issue}")
    info = view_issue(repo, issue, "state,labels")
    if info["state"] != "OPEN":
        raise ToolError(f"{repo}#{issue} is not open")
    if not {label["name"] for label in info["labels"]} & {"workshop:queued", "workshop:waiting"}:
        raise ToolError(f"{repo}#{issue} is not labelled workshop:queued or workshop:waiting")

    branch = f"workshop/issue-{issue}"
    home, name = Path.home() / ".workshop", f"{repo.replace('/', '-')}-{issue}"
    clone, worktree, handoff = home / "repos" / repo, home / "worktrees" / name, home / "handoff" / name
    base = prepare_worktree(repo, branch, clone, worktree)
    repo_skills = home / "logs" / name / "skills"  # this run's copy of the target repo's own
    copy_repo_skills(repo, base, names, repo_skills)
    skills = find_skills(names, repo_skills)
    shutil.rmtree(handoff, ignore_errors=True)  # each run starts with an empty handoff
    handoff.mkdir(parents=True)
    log_path = home / "logs" / name / f"{timestamp('%Y%m%dT%H%M%S.%fZ')}.log"  # one per run
    job = Job(repo, issue, skills, branch, base, clone, repo_settings(repo, base), worktree, handoff, log_path, args)

    outcome, record, posted, started = None, None, False, time.monotonic()
    try:
        status("labelling the issue workshop:working")
        set_labels(repo, issue, add=["workshop:working"], remove=["workshop:queued", "workshop:waiting"])
        code = job.run_agent(initial_prompt(job))
        outcome = determine_outcome(job, code)
        status(f"{outcome.name}: {outcome.reason}")
        record = run_record(job, outcome, "", time.monotonic() - started)
        gh("issue", "comment", issue, "--repo", repo, "--body-file", "-", input=summary(outcome, job, record))
        posted = True
    except BaseException as exc:
        record = record or run_record(job, outcome, str(exc) or type(exc).__name__, time.monotonic() - started)
        raise
    finally:  # the record first: it needs no network
        save_record(record, home / "logs" / "runs.jsonl")
        # Always take workshop:working off. Waiting means the run's comment says why: without one, as after an error
        # or Ctrl-C, queue the issue again.
        set_labels(repo, issue, add=["workshop:waiting" if posted else "workshop:queued"], remove=["workshop:working"])
    return 0


# --- workshop score: how each job ended, from the run records the tool hid in its own issue comments

# The tool writes the record last in its comment. The agent's note comes before it, so a record there is not counted.
RECORD = re.compile(r"<!-- workshop-run: ([^<>]*) -->\s*\Z")
ENDINGS = ("merged as-is", "merged after changes", "closed unmerged", "open", "escalated")
COUNTED = ("jobs", *ENDINGS, "runs", "questions", "fix", "denied")


def job_records(repo: str, since: str) -> list[list[dict]]:
    """Each job's run records, oldest first. Only comments by the account gh runs as count, so none can be forged."""
    issues = {}
    for label in LABELS:
        for issue in gh_json("issue", "list", "--repo", repo, "--state", "all", "--label", label, "--limit", "1000",
                             "--json", "number,comments"):
            issues[issue["number"]] = issue
    jobs = []
    for issue in issues.values():
        runs = []
        for comment in issue["comments"]:
            found = RECORD.search(comment["body"]) if comment.get("viewerDidAuthor") else None
            try:
                runs += [json.loads(found[1])] if found else []
            except json.JSONDecodeError:  # a comment someone has edited
                pass
        if runs := sorted((run for run in runs if run["finished"] >= since), key=lambda run: run["finished"]):
            jobs.append(runs)
    return jobs


def job_ending(runs: list[dict]) -> str:
    """Merged as-is means the pull request merged at the commit the tool last pushed: nobody changed it after."""
    pr_url = next((run["pr_url"] for run in reversed(runs) if run.get("pr_url")), "")
    if pr_url:
        pr = gh_json("pr", "view", pr_url, "--json", "state,headRefOid")
        pushed = next((run["pushed"] for run in reversed(runs) if run.get("pushed")), "")
        if pr["state"] == "MERGED":
            return "merged as-is" if pr["headRefOid"] == pushed else "merged after changes"
        if pr["state"] == "CLOSED":
            return "closed unmerged"
    return "escalated" if runs[-1]["outcome"] == "escalated" else "open"


def cmd_score(args: argparse.Namespace) -> int:
    require_gh_login()
    rows: dict[str, Counter] = {}
    for runs in job_records(args.repo, args.since):
        skills = ", ".join(skill["name"] for skill in runs[-1]["skills"][1:])  # the first is always workshop
        row = rows.setdefault(skills, Counter())
        row["jobs"] += 1
        row[job_ending(runs)] += 1
        row["ci_" + runs[-1].get("validation", {}).get("status", "unknown")] += 1
        row["runs"] += len(runs)
        row["questions"] += sum(run["outcome"] == "needs-info" for run in runs)
        row["fix"] += sum(run["fix_attempts"] for run in runs)
        row["denied"] += sum(sum(run["denied_tool_calls"].values()) for run in runs)
        row["cost"] += sum(run["cost_usd"] for run in runs)
    if not rows:
        status(f"no run records in {args.repo}{f' since {args.since}' if args.since else ''}")
        return 0
    validations = ("passed", "failed", "unverified", "unknown")
    headers = ["Skills", "Jobs", *(ending.capitalize() for ending in ENDINGS), "Runs", "Questions", "Fix attempts",
               "Denied calls", "API-equivalent cost", "Per merged PR"]
    headers += [f"CI {state}" for state in validations]
    table = []
    for skills, row in sorted(rows.items()):
        merged = row["merged as-is"] + row["merged after changes"]
        table.append([skills, *(row[key] for key in COUNTED),
                      f"${row['cost']:.2f}", f"${row['cost'] / merged:.2f}" if merged else "-",
                      *(row["ci_" + state] for state in validations)])
    print(markdown_table(headers, table))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    import workshop_eval  # only evals need it
    return workshop_eval.cmd_eval(args)


def cmd_init(args: argparse.Namespace) -> int:
    require_gh_login()
    for name, (color, description) in LABELS.items():
        gh("label", "create", name, "--repo", args.repo, "--color", color, "--description", description, "--force")
        status(f"label {name} is ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workshop.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="work one issue")
    run.add_argument("ref", metavar="OWNER/REPO#ISSUE")
    run.add_argument("--skills", required=True, help="comma-separated, e.g. add-field")
    run.add_argument("--max-fix-attempts", type=int, default=2)
    run.add_argument("--agent-timeout", type=int, default=900, help="seconds per agent run")
    run.add_argument("--checks-timeout", type=int, default=300, help="seconds to wait for checks each round")
    init = sub.add_parser("init", help="create the workshop labels in a target repo")
    init.add_argument("repo", metavar="OWNER/REPO")
    score = sub.add_parser("score", help="how a target repo's jobs ended, by skill")
    score.add_argument("repo", metavar="OWNER/REPO")
    score.add_argument("--since", default="", help="only runs from this date on, e.g. 2026-10-01")
    evals = sub.add_parser("eval", help="run the agent on the eval cases and grade it; publishes nothing")
    evals.add_argument("--skill", help="only the cases for this skill")
    evals.add_argument("--runs", type=int, default=1, help="runs of each case")
    evals.add_argument("--budget", type=float, default=2.0, help="API-equivalent dollars for all the runs")
    evals.add_argument("--max-fix-attempts", type=int, default=1)
    evals.add_argument("--agent-timeout", type=int, default=900, help="seconds per agent run")
    commands = {"run": cmd_run, "init": cmd_init, "score": cmd_score, "eval": cmd_eval}
    try:
        args = parser.parse_args(argv)
        return commands[args.command](args)
    except SystemExit as exc:  # argparse exits 2 on bad arguments; ours is 1
        return 1 if exc.code else 0
    except ToolError as exc:
        status(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        status("interrupted")
        return 130


if __name__ == "__main__":
    sys.modules["workshop"] = sys.modules[__name__]  # so workshop_eval imports this module, not a second copy
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # clean up on SIGTERM as on Ctrl-C
    sys.exit(main())
