#!/usr/bin/env python3
"""workshop: have a coding agent work a labelled GitHub issue into a pull request."""

import argparse
import base64
import functools
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
import urllib.parse
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
SKILLS_DIR = TOOL_ROOT / "skills"
REPO_SKILL_DIRS = (
    ".agents/skills",
    ".claude/skills",
)  # a target repo's own skills, on its default branch; the first wins
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# skills issue runs never use: inspect uses assess-failure, prepare uses prepare-pr, and review-analytics-artifact
# reports findings instead of opening a pull request
NOT_ISSUE_SKILLS = ("assess-failure", "prepare-pr", "review-analytics-artifact")

LABELS = {  # name: (color, description)
    "workshop:queued": ("5319e7", "Ready for a run. Applied by the owner."),
    "workshop:working": ("fbca04", "A run is in progress."),
    "workshop:waiting": (
        "d93f0b",
        "Waiting for the owner. The latest workshop comment says why. Comment to continue.",
    ),
}
PREPARE_LABEL = ("ededed", "Exclude this pull request from workshop prepare.")

TRUSTED_AUTHORS = {"OWNER", "MEMBER", "COLLABORATOR"}  # people with push access
SMALL_MODEL = "haiku"  # grades answers in evals

CHECKS_POLL_S = 10  # seconds between looks at a pull request's checks
NO_CHECKS_GRACE_S = 120  # how long checks may take to appear before we decide there are none
FAILED_LOG_CHARS = 10_000  # kept from the end of each failed workflow run's log
NOTE_CHARS = (
    60_000  # kept of the agent's note: GitHub takes 65,536 characters in a comment, the run record included
)
SH_TIMEOUT_S = (
    600  # for each command the tool runs itself: a stalled one would leave the issue labelled working
)
# For those commands: git and gh fail rather than ask at the terminal, and git ignores replacement refs, which the
# agent can write in the clone.
TOOL_ENV = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


class ToolError(Exception):
    """Stops the tool with exit code 1."""


def status(message: str) -> None:
    print(f"workshop: {message}", file=sys.stderr, flush=True)


def sh(
    cmd: list,
    *,
    cwd: Path | None = None,
    input: str | None = None,
    check: bool = True,
    env: dict | None = None,
    timeout: float = SH_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    cmd = [str(part) for part in cmd]
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**(os.environ if env is None else env), **TOOL_ENV},
            timeout=timeout,
        )
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
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "|" + "---|" * len(headers),
            *("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows),
        ]
    )


# --- arguments and safety


def parse_issue_ref(text: str) -> tuple[str, int]:
    """'owner/repo#12' -> ('owner/repo', 12)"""
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#([1-9][0-9]*)", text, re.ASCII)
    if not m:
        raise ToolError(f"expected owner/repo#number, got {text!r}")
    return m[1], int(m[2])


def parse_run_ref(text: str) -> tuple[str, int | None]:
    """'owner/repo#12' -> ('owner/repo', 12), and 'owner/repo' -> ('owner/repo', None), for all its issues."""
    return parse_issue_ref(text) if "#" in text else (parse_issue_ref(text + "#1")[0], None)


def parse_skills(value: str) -> list[str]:
    names = list(dict.fromkeys(name.strip() for name in value.split(",")))
    for name in names:
        if name == "workshop":
            raise ToolError("the workshop skill is always included; do not list it in --skills")
        if not SKILL_NAME.fullmatch(name):
            raise ToolError(f"bad skill name {name!r}: use lowercase letters, digits and hyphens")
    return names


def find_skills(names: list[str], repo_skills: dict[str, Path]) -> list[Path]:
    """The SKILL.md of the workshop skill, then of each job skill.

    A skill in `repo_skills`, the target repo's own by name, replaces the tool's skill of the same name. The
    workshop skill is always the tool's.
    """
    found = [SKILLS_DIR / "workshop" / "SKILL.md"]
    for name in names:
        path = repo_skills.get(name, SKILLS_DIR / name / "SKILL.md")
        if not path.is_file():
            theirs = " or ".join(f"{directory}/{name}/" for directory in REPO_SKILL_DIRS)
            raise ToolError(
                f"unknown skill {name!r}: neither the target repo's {theirs} nor workshop's skills/{name}/"
            )
        found.append(path)
    return found


def repo_slug(url: str) -> str:
    """The lowercase `owner/repo` of a GitHub URL, such as https://github.com/Owner/Repo.git or
    git@github.com:owner/repo, or "" if it is not one."""
    found = re.fullmatch(
        r"(?:(?:https?|ssh)://(?:[^@/]+@)?github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        url.strip(),
    )
    return found[1].lower() if found else ""


def skill_source(text: str) -> str:
    """The repository a SKILL.md was installed from, as `gh skill` records it in the frontmatter's
    `metadata: github-repo:`, or "" if it records none."""
    frontmatter = text.split("\n---", 1)[0]
    metadata = re.search(r"^metadata:[ \t]*\n((?:[ \t]+.*(?:\n|$))*)", frontmatter, re.M)
    found = metadata and re.search(r"^[ \t]+github-repo:[ \t]*['\"]?([^'\"\s]*)", metadata[1], re.M)
    return repo_slug(found[1]) if found else ""


@functools.cache
def own_repo() -> str:
    """This tool's repository on GitHub, as `skill_source` gives it, or "" if git cannot tell."""
    try:
        result = git("-C", TOOL_ROOT, "remote", "get-url", "origin", check=False)
    except ToolError:  # git is not installed
        return ""
    return repo_slug(result.stdout) if result.returncode == 0 else ""


def skill_description(path: Path) -> str:
    """The description in a SKILL.md's frontmatter."""
    frontmatter = path.read_text(encoding="utf-8").split("\n---", 1)[0]
    found = re.search(r"^description: *(.+)$", frontmatter, re.M)
    return found[1].strip() if found else ""


NON_JOB_SKILLS = ("workshop", *NOT_ISSUE_SKILLS)  # every job has workshop


def job_skills() -> dict[str, str]:
    """The tool's job skills by name, with their descriptions."""
    return {
        path.parent.name: skill_description(path)
        for path in sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if path.parent.name not in NON_JOB_SKILLS
    }


def offered_skills(repo_skills: dict[str, Path]) -> list[Path]:
    """The workshop skill's SKILL.md, then every job skill's by name, the target repo's own included, for the agent
    to choose from."""
    return find_skills(sorted((job_skills().keys() | repo_skills.keys()) - set(NON_JOB_SKILLS)), repo_skills)


def require_gh_login() -> None:
    if gh("auth", "status", check=False).returncode != 0:
        raise ToolError("gh is not authenticated; run `gh auth login`")


# --- GitHub and git


def view_issue(repo: str, issue: int, fields: str) -> dict:
    return gh_json("issue", "view", issue, "--repo", repo, "--json", fields)


LEFT_OUT = "(left out: written by someone without push access)"


def trusted_thread(repo: str, issue: int) -> str:
    """The issue and its comments, for an agent without GitHub tools. Like the GitHub server's lockdown mode, it keeps
    only what people with push access wrote."""
    item = gh_json("api", f"repos/{repo}/issues/{issue}")
    path = f"repos/{repo}/issues/{issue}/comments?per_page=100"
    comments = [comment for page in gh_json("api", "--paginate", "--slurp", path) for comment in page]

    def text(entry: dict, key: str = "body") -> str:
        return (entry[key] or "").strip() if entry.get("author_association") in TRUSTED_AUTHORS else LEFT_OUT

    thread = [f"# {text(item, 'title')}", *([] if text(item) == LEFT_OUT else ["", text(item)])]
    for number, comment in enumerate(comments, 1):
        author = (comment.get("user") or {}).get("login", "ghost")
        thread += ["", f"## Comment {number}, by {author}", "", text(comment)]
    return "\n".join(thread)


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
    result = gh(
        "api",
        "-H",
        "Accept: application/vnd.github.raw",
        f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={commit}",
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if "HTTP 404" in result.stderr:
        return None
    raise ToolError(f"could not read {path} in {repo} from GitHub: {result.stderr.strip()}")


def skill_folder(repo: str, commit: str, directory: str) -> list[dict]:
    """The files under `directory`, such as .agents/skills, in the target repo at `commit`, as GitHub's trees API
    lists them with paths relative to it, or [] if it has no such folder.

    Only this folder is listed: the tree of a whole repository can be too large for GitHub to list.
    """
    parent, _, name = directory.rpartition("/")
    result = gh("api", f"repos/{repo}/contents/{urllib.parse.quote(parent)}?ref={commit}", check=False)
    if result.returncode != 0:
        if "HTTP 404" in result.stderr:
            return []
        raise ToolError(f"could not list {parent} in {repo} from GitHub: {result.stderr.strip()}")
    entries = json.loads(result.stdout)
    found = (
        [e["sha"] for e in entries if e["name"] == name and e["type"] == "dir"]
        if isinstance(entries, list)
        else []
    )
    if not found:
        return []
    tree = gh_json("api", f"repos/{repo}/git/trees/{found[0]}?recursive=1")
    if tree.get("truncated"):
        raise ToolError(f"GitHub cannot list all the files in {directory} of {repo} at {commit}")
    return tree["tree"]


def github_blob(repo: str, sha: str) -> bytes:
    """A file's exact bytes from GitHub, by its git object id."""
    return base64.b64decode(gh_json("api", f"repos/{repo}/git/blobs/{sha}")["content"])


def copy_repo_skills(repo: str, commit: str, into: Path) -> dict[str, Path]:
    """Copy the target repo's own skills into `into`, as they are on GitHub at `commit`, and return each one's
    SKILL.md by name.

    A skill is a folder with a SKILL.md in one of REPO_SKILL_DIRS, and the first of those wins. Only plain files
    inside the folder are copied: a link could point anywhere. A copy that `gh skill` installed from this tool's own
    repository is skipped, so workshop's own skills always run as this tool has them.
    """
    shutil.rmtree(into, ignore_errors=True)
    folders: dict[str, dict[str, dict]] = {}  # name: {path in the folder: its tree entry}
    for directory in REPO_SKILL_DIRS:
        files: dict[str, dict[str, dict]] = {}
        for entry in skill_folder(repo, commit, directory):
            name, _, file = entry["path"].partition("/")
            if (
                entry["type"] == "blob"
                and entry["mode"] in ("100644", "100755")
                and SKILL_NAME.fullmatch(name)
                and not {"", ".", ".."} & set(file.split("/"))
            ):
                files.setdefault(name, {})[file] = entry
        for name, found in files.items():
            if "SKILL.md" in found:
                folders.setdefault(name, found)
    skills = {}
    for name, files in sorted(folders.items()):
        text = github_blob(repo, files["SKILL.md"]["sha"])
        if (source := skill_source(text.decode("utf-8", "replace"))) and source == own_repo():
            continue
        for file, entry in files.items():
            path = into / name / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text if file == "SKILL.md" else github_blob(repo, entry["sha"]))
            path.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        skills[name] = into / name / "SKILL.md"
    return skills


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
    git(
        "-C", clone, "remote", "set-head", "origin", default
    )  # origin/HEAD is for the agent: the tool uses `base`
    if (worktree / ".git").exists():
        status(f"reusing {worktree}")
    else:
        status(f"creating {worktree} on {branch}")
        git("-C", clone, "worktree", "prune")
        # Check out the branch if it exists locally or on the remote; otherwise start it from the default branch.
        if git("-C", clone, "worktree", "add", worktree, branch, check=False).returncode != 0:
            git("-C", clone, "worktree", "add", "--no-track", "-b", branch, worktree, base)
    # Start from what is on GitHub: the pushed branch, or the default branch if nothing is pushed yet.
    pushed = (
        git("-C", clone, "rev-parse", "--verify", "--quiet", f"origin/{branch}", check=False).returncode == 0
    )
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
    handoff: (
        Path  # where the agent leaves question.md, pull-request.md and comment.md for the tool to publish
    )
    log_path: Path
    args: argparse.Namespace
    agent_s: float = 0.0  # time spent in the agent this run
    checks_s: float = 0.0  # time spent waiting for checks this run
    pushed: str = ""  # the commit the tool last pushed this run
    kind: str = ""  # the kind of work its pull-request.md names, such as "rename field"
    github: bool = True  # the read-only GitHub server; evals run without it
    inspection: bool = False  # read supplied evidence; write only the assessment
    agent_args: list[str] = field(
        default_factory=list
    )  # more for the agent's command line, such as a spending cap
    evidence_dir: Path | None = None  # additional read-only logs supplied by the tool
    budget: float = 0.0  # API-equivalent dollars for all of this run's agent invocations; 0 for no cap
    choose: bool = False  # `skills` are offered: the agent reads workshop and chooses the job skills that fit
    modal: bool = False  # the agent runs in a Modal Sandbox, without GitHub tools
    validation: dict = field(default_factory=dict)  # the latest wait_for_checks result

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
CLAUDE_LOGIN = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
HIDDEN_FROM_COMMANDS = (*CLAUDE_LOGIN, "OTEL_EXPORTER_OTLP_HEADERS")  # the Claude login and the PostHog token

# Where Claude Code sends each kind of telemetry on the PostHog host: the one place to change after a test session.
# The AI endpoint keeps only spans named or attributed gen_ai., llm., ai. or traceloop.; /i/v1/traces keeps every span.
POSTHOG_OTLP = {
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "/i/v1/metrics",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "/i/v1/logs",  # Claude Code's events
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "/i/v0/ai/otel",
}


def telemetry_env() -> dict[str, str]:
    """Claude Code's OpenTelemetry settings, if the owner's environment names a PostHog project token and host.

    Checked against code.claude.com/docs/en/monitoring-usage and posthog.com/docs on 2026-11-25. It sets none of
    the flags that add content (OTEL_LOG_*, detailed beta tracing), and the agent gets none from the shell, so PostHog
    gets tokens, cost, timing and tool names but no prompts, responses, command lines or their output.
    """
    token, host = os.environ.get("WORKSHOP_POSTHOG_TOKEN"), os.environ.get("WORKSHOP_POSTHOG_HOST")
    if not (token and host):
        return {}
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",  # traces, in beta
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",  # PostHog takes no gRPC
        **{name: host.rstrip("/") + path for name, path in POSTHOG_OTLP.items()},
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {token}",
    }


def agent_env(job: Job) -> dict[str, str]:
    """The agent's environment. The target repo's `env` setting reaches only its commands, not Claude Code itself:
    Claude Code runs the CLAUDE_ENV_FILE script before each command."""
    env_file = job.log_path.parent / "env.sh"
    env_file.write_text(
        "".join(
            f"export {name}={shlex.quote(str(value))}\n"
            for name, value in job.settings.get("env", {}).items()
        )
    )
    return {**kept_env(), **telemetry_env(), "CLAUDE_ENV_FILE": str(env_file)}


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
            "credentials": {"envVars": [{"name": name, "mode": "deny"} for name in HIDDEN_FROM_COMMANDS]},
            "network": {"allowedDomains": network.get("allowedDomains", [])},
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [
                    str(job.worktree),
                    *dict.fromkeys(str(path.parent) for path in job.skills),  # skills that ship scripts
                    str(job.clone / ".git"),
                    str(job.handoff),
                    "~/.gitconfig",
                    *filesystem.get("allowRead", []),
                ],
                "allowWrite": [str(job.handoff), *filesystem.get("allowWrite", [])],
            },
        },
    }


# GitHub's MCP server, read-only. It runs outside the sandbox and gets the token from gh itself,
# so the token is on no command line and not in the agent's environment.
# Lockdown mode hides what people without push access wrote in a public repo, so a stranger cannot instruct the agent.
GITHUB_MCP = (
    'GITHUB_PERSONAL_ACCESS_TOKEN="$(gh auth token)" '
    "exec github-mcp-server stdio --read-only --lockdown-mode --toolsets issues,pull_requests"
)


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
    allowed = [
        "Bash",
        *(f"Read(/{path.parent}/**)" for path in job.skills),
        *([f"Read(/{job.evidence_dir}/**)"] if job.evidence_dir else []),
        f"Edit(/{job.worktree}/**)",
        f"Edit(/{job.handoff}/**)",
        *(["mcp__github"] if job.github else []),
    ]
    if job.inspection:
        servers = {}
        allowed = [
            f"Read(/{job.worktree}/**)",
            f"Read(/{SKILLS_DIR}/**)",
            *(f"Read(/{path.parent}/**)" for path in job.skills),
            f"Read(/{job.log_path.parent / 'evidence.json'})",
            f"Read(/{job.log_path.parent / 'logs'}/**)",
            f"Read(/{job.log_path.parent / 'history'}/**)",
            f"Read(/{job.log_path.parent / 'issues.json'})",
            f"Edit(/{job.handoff / 'assessment.json'})",
        ]
    return [
        "claude",
        "-p",
        "--effort",
        "medium",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ",".join(allowed),
        "--disallowedTools",
        "Monitor",
        "--settings",
        json.dumps(claude_settings(job)),
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps({"mcpServers": servers}),
        "--output-format",
        "stream-json",
        "--verbose",
        *(["--tools", "Read,Glob,Grep,Write"] if job.inspection else []),
        *(["--max-budget-usd", f"{max(budget_left(job), 0.01):.2f}"] if job.budget else []),
        *job.agent_args,
    ]


def budget_left(job: Job) -> float:
    """What the agent may still spend this run. A timed-out invocation reports no cost, so it is not counted."""
    return job.budget - agent_usage(job.log_path)["cost_usd"]


def invoke_agent(job: Job, prompt: str) -> int:
    """Run the agent non-interactively in the job's worktree. Return its exit code (124 on timeout)."""
    worktree, timeout_s, log_path = job.worktree, job.args.agent_timeout, job.log_path
    cmd = claude_command(job)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = agent_env(job)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_s)
    prompt += f"\nThis invocation stops at {deadline:%Y-%m-%dT%H:%M:%SZ} ({timeout_s}s from now). " + (
        "Allow time to write assessment.json before that deadline.\n"
        if job.inspection
        else "Allow time for the required checks, commits and handoff files before that deadline.\n"
    )
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"=== {timestamp()} prompt:\n{prompt}\n=== output:\n")
        log.flush()
        if job.modal:
            import workshop_modal  # cmd_run checked that it imports

            code = workshop_modal.run_agent(job, cmd, env, prompt, log)
            log.write(
                f"\n=== timed out after {timeout_s}s\n" if code == 124 else f"\n=== exited with code {code}\n"
            )
            return code
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=worktree,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
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


def last_result(log_path: Path) -> str:
    """The final message of the agent's latest invocation in its log."""
    results = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            results.append(event.get("result", ""))
    return results[-1] if results else ""


TOKENS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}


def log_events(log_path: Path) -> Iterator[dict]:
    """Claude Code's stream-json events in the output sections of the run's log."""
    in_output = False
    for line in (
        log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else []
    ):
        if line.startswith("=== "):  # invoke_agent's markers: only the output sections hold events
            in_output = line == "=== output:"
            continue
        try:
            event = json.loads(line) if in_output else None
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def skills_read(job: Job) -> list[Path]:
    """The skills of `job.skills` whose SKILL.md the agent read with its Read tool, as the run's log shows."""
    read = {
        Path(block["input"]["file_path"])
        for event in log_events(job.log_path)
        if event.get("type") == "assistant"
        for block in event.get("message", {}).get("content", [])
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "Read"
        and isinstance(block.get("input", {}).get("file_path"), str)
    }
    return [path for path in job.skills if path in read]


def agent_usage(log_path: Path) -> dict:
    """What the agent did in this run's log, from Claude Code's stream-json events. A run that timed out has no cost."""
    usage = {
        "agent": "claude",
        "agent_version": "",
        "model": "",
        "turns": 0,
        "cost_usd": 0.0,
        "tokens": dict.fromkeys(TOKENS, 0),
        "tool_calls": Counter(),
    }
    for event in log_events(log_path):
        if event.get("type") == "system" and event.get("subtype") == "init":
            usage["agent_version"], usage["model"] = (
                event.get("claude_code_version", ""),
                event.get("model", ""),
            )
        elif event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    usage["tool_calls"][block["name"]] += 1
        elif event.get("type") == "result":
            usage["turns"] += event.get("num_turns", 0)
            usage["cost_usd"] += event.get("total_cost_usd", 0.0)
            for name, key in TOKENS.items():
                usage["tokens"][name] += event.get("usage", {}).get(key, 0)
    usage["cost_usd"] = round(usage["cost_usd"], 4)
    return usage


HANDOFF_RULES = """You cannot write to GitHub. Read it with the read-only github tools when you have them. When you \
exit, the workshop tool publishes what you left in the handoff directory:

- To ask, write the question to question.md. The tool posts it on the issue and publishes nothing else.
- Otherwise the tool pushes your commits on the current branch, and opens a pull request from pull-request.md or \
updates the open one. Write the title on the first line, then the body.
- Write the close-out comment to comment.md. Once checks pass, the tool posts it on the issue with a link to the \
pull request.

If you leave neither a question nor commits, the job has failed. Your final message is not read.

Commands run in a sandbox. They can write only to the working directory, the handoff directory and temp, and reach \
only the network hosts the repository allows."""


CHOOSE_RULES = """Next, choose the job skill for the kind of work the issue asks for from the list below, read its \
SKILL.md and follow it exactly. Also read and follow each other job skill that fits. Choose by the kind of work, even \
when that skill's scope rules exclude this request; then follow those rules, which means asking. Choose other only if \
no job skill is for that kind of work."""


def job_lines(job: Job, skills: list[Path]) -> list[str]:
    return [
        *(f"  {path}" for path in skills),
        f"Target repository: {job.repo}. Issue: #{job.issue}. Branch: {job.branch}.",
        f"Handoff directory: {job.handoff}",
    ]


def initial_prompt(job: Job, issue: str = "") -> str:
    """The first prompt of a run. Evals have no GitHub tools, so they give the issue thread as `issue`."""
    read = "Then read the repository's AGENTS.md if present"
    return (
        "\n".join(
            [
                "You are working a job in a workshop. Read "
                + (
                    "this skill first and follow it exactly:"
                    if job.choose
                    else "these files first and follow them exactly:"
                ),
                *job_lines(job, job.skills[:1] if job.choose else job.skills),
                f"{read}. This job has no github tools: the issue with all its comments is below."
                if issue
                else f"{read}, and the issue with all its comments.",
                *(
                    [
                        "",
                        CHOOSE_RULES,
                        *(
                            f"  - {path.parent.name}: {skill_description(path)}\n    {path}"
                            for path in job.skills[1:]
                        ),
                    ]
                    if job.choose
                    else []
                ),
                "",
                HANDOFF_RULES,
                *(["", "The issue with all its comments:", "", issue] if issue else []),
            ]
        )
        + "\n"
    )


def fix_prompt(job: Job, pr: int | None, failing: list[str], logs: str) -> str:
    read = skills_read(job) if job.choose else []
    return (
        "\n".join(
            [
                f"{f'Pull request #{pr} has' if pr else 'Your change has'} failing checks. Read the same skill files again:",
                *job_lines(job, read or job.skills),
                f"Failing checks: {', '.join(failing)}",
                "Logs:",
                logs,
                "Fix the cause and commit it on the same branch.",
                "",
                HANDOFF_RULES,
            ]
        )
        + "\n"
    )


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


KIND = re.compile(r"[ \t>*_-]*kind[*_]*:[*_]*(.*)", re.I)  # also as a list item or in bold: **Kind:**


def pull_request_kind(text: str) -> str:
    """The kind of work on the last `Kind:` line of a pull request outside code blocks, such as "rename field", or "".

    Only lowercase letters, digits, spaces and hyphens are kept: the run record that holds it goes in an HTML comment.
    """
    kinds, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (found := KIND.fullmatch(line)):
            kinds.append(found[1])
    return " ".join(re.sub(r"[^a-z0-9 -]", " ", kinds[-1].lower()).split())[:40].strip() if kinds else ""


def open_pr(job: Job) -> dict | None:
    prs = gh_json(
        "pr", "list", "--repo", job.repo, "--head", job.branch, "--state", "open", "--json", "number,url"
    )
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
    job.kind = pull_request_kind(body) if title else ""
    if title and pr:
        gh(
            "pr",
            "edit",
            pr["number"],
            "--repo",
            job.repo,
            "--title",
            title,
            "--body-file",
            "-",
            input=body.strip(),
        )
    elif title and ahead:
        status("opening a pull request")
        gh(
            "pr",
            "create",
            "--repo",
            job.repo,
            "--head",
            job.branch,
            "--title",
            title,
            "--body-file",
            "-",
            input=body.strip(),
            cwd=job.clone,
        )
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
        state = (
            check.get("conclusion")
            if check.get("status") == "COMPLETED"
            else check.get("status") or check.get("state")
        ) or "PENDING"
        if state == "SUCCESS":
            bucket = "pass"
        elif state in ("SKIPPED", "NEUTRAL"):
            bucket = "skipping"
        elif state in (
            "FAILURE",
            "ERROR",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STALE",
            "STARTUP_FAILURE",
        ):
            bucket = "fail"
        else:
            bucket = "pending"
        checks.append(
            {
                "name": check.get("name") or check.get("context"),
                "bucket": bucket,
                "state": state,
                "link": check.get("detailsUrl") or check.get("targetUrl") or "",
            }
        )
    return info["headRefOid"], checks


def wait_for_checks(repo: str, pr: int, timeout_s: int, head: str = "", previous_head: str = "") -> dict:
    """The checks of one commit, once they finish. No checks or only skipped checks do not establish a pass."""
    start = time.monotonic()
    head_seen = False
    while True:
        observed_head, checks = pr_checks(repo, pr)
        head = head or observed_head
        result = {"status": "unverified", "reason": "", "head_sha": head, "checks": checks}
        if observed_head != head:
            elapsed = time.monotonic() - start
            grace = min(2 * CHECKS_POLL_S, timeout_s)
            if not head_seen and previous_head and observed_head == previous_head and elapsed < grace:
                time.sleep(min(CHECKS_POLL_S, grace - elapsed))
                continue
            return {**result, "reason": "head-changed", "checks": []}
        head_seen = True
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


def failing_logs(
    repo: str, failing: list[dict], include_trigger: bool = False, log_dir: Path | None = None
) -> str:
    """Save complete failed-job logs when a directory is supplied; otherwise return failed-step tails."""
    run_ids = dict.fromkeys(re.findall(r"/actions/runs/(\d+)", " ".join(check["link"] for check in failing)))
    logs = []
    for run_id in run_ids:
        title = f"--- run {run_id}"
        if include_trigger:
            try:
                run = gh_json("api", f"repos/{repo}/actions/runs/{run_id}")
                actor = (run.get("actor") or {}).get("login")
                if actor:
                    title += f" ({run.get('event', 'unknown event')}, actor: {actor})"
            except ToolError:
                pass  # The failed log may be available even if run metadata is not.
        if log_dir is not None:
            try:
                pages = gh_json("api", "--paginate", "--slurp", f"repos/{repo}/actions/runs/{run_id}/jobs")
                jobs = [job for page in pages for job in page["jobs"]]
            except ToolError:
                logs.append(f"{title}\n(failed-job list unavailable)")
                continue
            failed = [
                job
                for job in jobs
                if job.get("conclusion")
                in ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale")
            ]
            if not failed:
                logs.append(f"{title}\n(no failed-job logs available)")
            for job in failed:
                log = gh("run", "view", run_id, "--repo", repo, "--job", job["id"], "--log", check=False)
                label = f"{title}, job {job['id']} ({job['name']})"
                if log.returncode or not log.stdout.strip():
                    logs.append(f"{label}\n(failed-job log unavailable)")
                    continue
                log_dir.mkdir(parents=True, exist_ok=True)
                path = (log_dir / f"{run_id}-{job['id']}.log").resolve()
                path.write_text(log.stdout, encoding="utf-8")
                logs.append(f"{label}\nFull failed-job log: {path}")
            continue
        log = gh("run", "view", run_id, "--repo", repo, "--log-failed", check=False).stdout.strip()[
            -FAILED_LOG_CHARS:
        ]
        logs.append(f"{title}\n{log}")
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
    if code == 124:
        ended = f"timed out after {job.args.agent_timeout}s"
    elif job.budget and budget_left(job) <= 0:
        ended = f"spent the run's budget of ${job.budget:.2f}"
    else:
        ended = f"exited with code {code}"
    return Outcome(
        "escalated", f"the agent {ended} without leaving a question or a pull request; its log is {log}"
    )


def prove(job: Job, pr: dict) -> Outcome:
    """Wait for the pull request's checks, sending the agent back to fix failures."""
    attempts = 0
    while True:
        status(f"waiting for checks on {pr['url']}")
        started = time.monotonic()
        job.validation = wait_for_checks(job.repo, pr["number"], job.args.checks_timeout, job.pushed)
        job.checks_s += time.monotonic() - started
        state, reason = job.validation["status"], job.validation["reason"]
        if state == "passed" or reason in ("no-checks", "all-skipped"):
            reason = {
                "reported-checks-passed": "reported checks passed",
                "no-checks": "no checks ran",
                "all-skipped": "all reported checks were skipped or neutral",
            }[reason]
            return Outcome("pr-open", reason, pr["url"], attempts, handoff_text(job, "comment.md"))
        if reason == "head-changed":
            return Outcome(
                "escalated",
                "the pull request head changed while checking the pushed commit",
                pr["url"],
                attempts,
            )
        if reason == "timed-out":
            return Outcome(
                "escalated", f"checks did not finish within {job.args.checks_timeout}s", pr["url"], attempts
            )
        failing = [check for check in job.validation["checks"] if check["bucket"] == "fail"]
        names = sorted({check["name"] for check in failing})
        if attempts >= job.args.max_fix_attempts:
            return Outcome(
                "escalated",
                f"checks still failing after {attempts} fix attempts: {', '.join(names)}",
                pr["url"],
                attempts,
            )
        if job.budget and budget_left(job) <= 0:
            return Outcome(
                "escalated",
                f"the run's budget of ${job.budget:.2f} is spent, and checks are still failing: {', '.join(names)}",
                pr["url"],
                attempts,
            )
        attempts += 1
        status(f"checks failed ({', '.join(names)}); fix attempt {attempts} of {job.args.max_fix_attempts}")
        job.run_agent(fix_prompt(job, pr["number"], names, failing_logs(job.repo, failing)))
        if question := handoff_text(job, "question.md"):
            return Outcome("needs-info", "the agent has a question", pr["url"], attempts, question)
        publish_commits(job)


def summary(outcome: Outcome, job: Job, record: dict) -> str:
    lines = [f"**workshop: {outcome.name}**: {outcome.reason}.", ""]
    lines.append(f"- Skills: {skill_label(record['skills'])}.")
    if outcome.pr_url:
        lines += [
            f"- Pull request: {outcome.pr_url}",
            f"- GitHub checks: {job.validation['status']} at `{job.validation['head_sha'][:12]}`.",
            f"- Fix attempts: {outcome.fix_attempts} of {job.args.max_fix_attempts}",
        ]
    lines.append(f"- Agent time: {duration(job.agent_s)}. API-equivalent cost: ${record['cost_usd']:.2f}.")
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


def skill_label(skills: list[dict]) -> str:
    """The job skills a run record lists, without workshop, which every job has: "add-fields", or "none"."""
    return ", ".join(skill["name"] for skill in skills if skill["name"] != "workshop") or "none"


def skill_records(paths: list[Path]) -> list[dict]:
    return [
        {
            "name": path.parent.name,
            "from": "workshop" if path.is_relative_to(SKILLS_DIR) else "target repo",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:12],
        }
        for path in paths
    ]


def run_record(job: Job, outcome: Outcome | None, error: str, total_s: float) -> dict:
    return {
        "finished": timestamp(),
        "repo": job.repo,
        "issue": job.issue,
        "skills": skill_records(skills_read(job)),
        "outcome": outcome.name if outcome else "error",
        "reason": outcome.reason if outcome else error,
        "fix_attempts": outcome.fix_attempts if outcome else 0,
        "pr_url": outcome.pr_url if outcome else "",
        "pushed": job.pushed,
        "kind": job.kind,
        "seconds": {"total": round(total_s), "agent": round(job.agent_s), "checks": round(job.checks_s)},
        **agent_usage(job.log_path),
    }


def save_record(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as records:
        records.write(json.dumps(record) + "\n")


def tool_comment(comment: dict) -> bool:
    """A run summary: posted by the account gh runs as, and ending in a run record."""
    return bool(comment.get("viewerDidAuthor") and RECORD.search(comment["body"]))


def answered(comments: list[dict]) -> bool:
    """Whether someone with push access has commented since the tool's latest run summary."""
    latest = max((index for index, comment in enumerate(comments) if tool_comment(comment)), default=-1)
    return any(
        comment.get("authorAssociation") in TRUSTED_AUTHORS and not tool_comment(comment)
        for comment in comments[latest + 1 :]
    )


def runnable_issues(repo: str) -> list[int]:
    """The open issues a run of the whole repo works, oldest first: each one queued, and each one waiting that
    someone with push access has commented on since the tool's latest run summary."""

    def labelled(label: str, fields: str) -> list[dict]:
        return gh_json(
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            label,
            "--limit",
            "1000",
            "--json",
            fields,
        )

    numbers = {issue["number"] for issue in labelled("workshop:queued", "number")}
    numbers |= {
        issue["number"]
        for issue in labelled("workshop:waiting", "number,comments")
        if answered(issue["comments"])
    }
    return sorted(numbers)


def cmd_run(args: argparse.Namespace) -> int:
    repo, issue = parse_run_ref(args.ref)
    names = parse_skills(args.skills) if args.skills else None
    for name in names or []:
        if name in NOT_ISSUE_SKILLS:
            raise ToolError(f"{name} is not used by issue runs")
    if names and issue is None:
        raise ToolError("--skills needs one issue: owner/repo#number")
    if args.modal:  # before any issue is labelled
        try:
            import workshop_modal  # only runs on Modal need the modal client
        except ImportError as exc:
            raise ToolError(f"--modal needs the modal client: {exc}") from None
        workshop_modal.sandbox_env(dict(os.environ))  # the Claude login
    require_gh_login()
    if issue:
        work(repo, issue, names, args)
        return 0
    issues = runnable_issues(repo)
    if not issues:
        status(f"no issue in {repo} is queued, or waiting with a new comment")
    failed = False
    for issue in issues:  # one failure leaves the issue queued, and the others still run
        try:
            work(repo, issue, names, args)
        except ToolError as exc:
            status(f"error: {repo}#{issue}: {exc}")
            failed = True
    return 1 if failed else 0


def work(repo: str, issue: int, names: list[str] | None, args: argparse.Namespace) -> None:
    """Run one job on the issue, with the named job skills or, if there are none, the one chosen for it."""
    status(f"reading {repo}#{issue}")
    info = view_issue(repo, issue, "state,labels,comments")
    if info["state"] != "OPEN":
        raise ToolError(f"{repo}#{issue} is not open")
    if not {label["name"] for label in info["labels"]} & {"workshop:queued", "workshop:waiting"}:
        raise ToolError(f"{repo}#{issue} is not labelled workshop:queued or workshop:waiting")

    branch = f"workshop/issue-{issue}"
    home, name = Path.home() / ".workshop", f"{repo.replace('/', '-')}-{issue}"
    clone, worktree, handoff = home / "repos" / repo, home / "worktrees" / name, home / "handoff" / name
    base = prepare_worktree(repo, branch, clone, worktree)
    shutil.rmtree(handoff, ignore_errors=True)  # each run starts with an empty handoff
    handoff.mkdir(parents=True)
    log_path = home / "logs" / name / f"{timestamp('%Y%m%dT%H%M%S.%fZ')}.log"  # one per run
    job = Job(
        repo, issue, [], branch, base, clone, repo_settings(repo, base), worktree, handoff, log_path, args
    )
    job.budget, job.modal, job.github = args.budget, args.modal, not args.modal
    repo_skills = copy_repo_skills(repo, base, home / "logs" / name / "skills")  # this run's copy
    job.choose = names is None
    job.skills = offered_skills(repo_skills) if job.choose else find_skills(names, repo_skills)

    outcome, record, posted, started = None, None, False, time.monotonic()
    try:
        status("labelling the issue workshop:working")
        set_labels(repo, issue, add=["workshop:working"], remove=["workshop:queued", "workshop:waiting"])
        code = job.run_agent(initial_prompt(job, "" if job.github else trusted_thread(repo, issue)))
        outcome = determine_outcome(job, code)
        status(f"{outcome.name}: {outcome.reason}")
        record = run_record(job, outcome, "", time.monotonic() - started)
        gh("issue", "comment", issue, "--repo", repo, "--body-file", "-", input=summary(outcome, job, record))
        posted = True
    except BaseException as exc:
        record = record or run_record(
            job, outcome, str(exc) or type(exc).__name__, time.monotonic() - started
        )
        raise
    finally:  # the record first: it needs no network
        save_record(record, home / "logs" / "runs.jsonl")
        # Always take workshop:working off. Waiting means the run's comment says why: without one, as after an error
        # or Ctrl-C, queue the issue again.
        set_labels(
            repo,
            issue,
            add=["workshop:waiting" if posted else "workshop:queued"],
            remove=["workshop:working"],
        )


# --- workshop score: how each job ended, from the run records the tool hid in its own issue comments

# The tool writes the record last in its comment. The agent's note comes before it, so a record there is not counted.
RECORD = re.compile(r"<!-- workshop-run: ([^<>]*) -->\s*\Z")
ENDINGS = ("merged as-is", "merged after changes", "closed unmerged", "open", "escalated")
COUNTED = ("jobs", *ENDINGS, "runs", "questions", "fix")


def job_records(repo: str, since: str) -> list[list[dict]]:
    """Each job's run records, oldest first. Only comments by the account gh runs as count, so none can be forged."""
    issues = {}
    for label in LABELS:
        for issue in gh_json(
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--label",
            label,
            "--limit",
            "1000",
            "--json",
            "number,comments",
        ):
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
        skills = skill_label(runs[-1]["skills"])
        if kind := next((run["kind"] for run in reversed(runs) if run.get("kind")), ""):
            skills += f": {kind}"
        row = rows.setdefault(skills, Counter())
        row["jobs"] += 1
        row[job_ending(runs)] += 1
        row["runs"] += len(runs)
        row["questions"] += sum(run["outcome"] == "needs-info" for run in runs)
        row["fix"] += sum(run["fix_attempts"] for run in runs)
        row["cost"] += sum(run["cost_usd"] for run in runs)
    if not rows:
        status(f"no run records in {args.repo}{f' since {args.since}' if args.since else ''}")
        return 0
    headers = [
        "Skills",
        "Jobs",
        *(ending.capitalize() for ending in ENDINGS),
        "Runs",
        "Questions",
        "Fix attempts",
        "API-equivalent cost",
        "Per merged PR",
    ]
    table = []
    for skills, row in sorted(rows.items()):
        merged = row["merged as-is"] + row["merged after changes"]
        table.append(
            [
                skills,
                *(row[key] for key in COUNTED),
                f"${row['cost']:.2f}",
                f"${row['cost'] / merged:.2f}" if merged else "-",
            ]
        )
    print(markdown_table(headers, table))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    import workshop_eval  # only evals need it

    return workshop_eval.cmd_eval(args)


def cmd_inspect(args: argparse.Namespace) -> int:
    import workshop_inspect

    return workshop_inspect.cmd_inspect(args)


def cmd_init(args: argparse.Namespace) -> int:
    require_gh_login()
    for name, (color, description) in {**LABELS, "workshop:ignore": PREPARE_LABEL}.items():
        gh(
            "label",
            "create",
            name,
            "--repo",
            args.repo,
            "--color",
            color,
            "--description",
            description,
            "--force",
        )
        status(f"label {name} is ready")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    import workshop_prepare

    return workshop_prepare.cmd_prepare(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workshop.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="work one issue, or each one ready in a repo")
    run.add_argument("ref", metavar="OWNER/REPO[#ISSUE]")
    run.add_argument(
        "--skills", help="comma-separated, e.g. add-fields; by default the agent chooses from every job skill"
    )
    run.add_argument("--max-fix-attempts", type=int, default=2)
    run.add_argument("--agent-timeout", type=int, default=900, help="seconds per agent run")
    run.add_argument("--checks-timeout", type=int, default=300, help="seconds to wait for checks each round")
    run.add_argument("--budget", type=float, default=10.0, help="API-equivalent dollars for this run's agent")
    run.add_argument("--modal", action="store_true", help="run the agent in a Modal Sandbox")
    prepare = sub.add_parser("prepare", help="prepare one PR, or a repo's eligible PRs")
    prepare.add_argument("ref", metavar="OWNER/REPO[#PR]")
    prepare.add_argument("--include-drafts", action="store_true", help="include drafts in a batch")
    prepare.add_argument("--max-fix-attempts", type=int, default=2)
    prepare.add_argument("--agent-timeout", type=int, default=900)
    prepare.add_argument("--checks-timeout", type=int, default=300)
    prepare.add_argument("--budget", type=float, default=10.0, help="agent dollars per PR")
    prepare.add_argument(
        "--assessment-budget", type=float, default=5.0, help="agent dollars for batch assessment"
    )
    init = sub.add_parser("init", help="create the workshop labels in a target repo")
    init.add_argument("repo", metavar="OWNER/REPO")
    score = sub.add_parser("score", help="how a target repo's jobs ended, by skill")
    score.add_argument("repo", metavar="OWNER/REPO")
    score.add_argument("--since", default="", help="only runs from this date on, e.g. 2026-10-01")
    inspect = sub.add_parser("inspect", help="inspect configured jobs and report any action needed")
    inspect.add_argument("repo", metavar="OWNER/REPO")
    inspect.add_argument("--config", type=Path, metavar="PATH", help="use a local inspection config")
    inspect.add_argument("--verbose", action="store_true", help="print the full inspection report")
    inspect.add_argument("--since", default="24h", help="look back this long (default: 24h); e.g. 30m or 7d")
    inspect.add_argument("--agent-timeout", type=int, default=300, help="seconds for failure assessment")
    inspect.add_argument(
        "--issue",
        action="store_true",
        help="propose an issue for action required, or the one it matches; publishes nothing",
    )
    evals = sub.add_parser("eval", help="run the agent on the eval cases and grade it; publishes nothing")
    evals.add_argument("--skill", help="only the cases for this skill")
    evals.add_argument(
        "--case",
        action="append",
        default=[],
        help="only this case, by file name without .toml; comma-separated or repeated",
    )
    evals.add_argument(
        "--changed", action="store_true", help="only the cases whose inputs differ from the default branch"
    )
    evals.add_argument("--runs", type=int, default=1, help="runs of each case")
    evals.add_argument("--budget", type=float, default=5.0, help="API-equivalent dollars for all the runs")
    evals.add_argument("--max-fix-attempts", type=int, default=1)
    evals.add_argument("--agent-timeout", type=int, default=900, help="seconds per agent run")
    commands = {
        "run": cmd_run,
        "prepare": cmd_prepare,
        "init": cmd_init,
        "score": cmd_score,
        "eval": cmd_eval,
        "inspect": cmd_inspect,
    }
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
