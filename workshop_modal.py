"""Unattended runs: the agent in a Modal Sandbox, and a scheduled Modal function that runs workshop.

Deploy the schedule with `uv run modal deploy workshop_modal.py`, and run it once now with
`uv run modal run workshop_modal.py::work_queue`. `./workshop.py run --modal` uses only `run_agent`.
Checked against the modal client 1.6.4. Claude Code's sandbox needs the VM runtime: under gVisor it cannot make
network namespaces.
"""

import os
import shlex
import shutil
import tarfile
import tempfile
import urllib.parse
from pathlib import Path

import modal

import workshop as w

APP = "workshop"
CLAUDE_CODE = "2.1.332"  # the version checked in a Modal Sandbox
SETUP = ".workshop/setup.sh"  # on the target repo's default branch
INSIDE = "/tmp/workshop"  # in the Sandbox: the bundles and tars that carry the job in and out
LOGIN = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
MACHINE = (
    "PATH",
    "SHELL",
    "TMPDIR",
    "USER",
    "CLAUDE_CONFIG_DIR",
)  # what describes the tool's machine, not the Sandbox

AGENT_IMAGE = (
    modal.Image.from_registry("node:22-bookworm-slim", add_python="3.12")
    .apt_install("bubblewrap", "socat", "git", "ripgrep", "curl", "ca-certificates", "procps", "util-linux")
    .run_commands(f"npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE}")
)

# In the Sandbox: unpack what `pack` made at the same paths as on the tool's machine, then make the worktree a clone
# of the job branch. Files setup.sh left that git ignores stay; other untracked files go.
ENTER = r"""set -euo pipefail
packed=$1 root=$2 worktree=$3 branch=$4 default=$5 repo=$6
tar -xpf "$packed" -C "$root"
mkdir -p "$worktree" && cd "$worktree"
git init -q
git fetch -q "$root/tmp/workshop/in.bundle" "+refs/heads/$branch:refs/heads/$branch" \
  "+refs/remotes/origin/*:refs/remotes/origin/*"
git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/$default"
git remote add origin "https://github.com/$repo.git"
git checkout -q -f "$branch"
git clean -q -fd
"""

LEAVE = r"""set -euo pipefail
worktree=$1 branch=$2 handoff=$3 out=$4
git -C "$worktree" bundle create -q "$out/work.bundle" "refs/heads/$branch"
tar -cf "$out/handoff.tar" -C "$handoff" .
"""


def arcname(path: Path) -> str:
    return str(path).lstrip("/")


def remote_head(job: w.Job) -> str:
    """The default branch as the clone names it, such as origin/main."""
    return w.git("-C", job.clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()


def pack(job: w.Job, into: Path) -> Path:
    """A tar of what the agent needs, at the same paths as here: the job branch and the default branch as a bundle,
    the handoff directory, the skill folders, the commands' env.sh, and a .gitconfig with only the commit author.
    A bundle holds commits and refs, not the clone's config, so the token stays here."""
    into.mkdir(parents=True)
    pushed = w.git(
        "-C", job.clone, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{job.branch}", check=False
    )
    refs = [f"refs/heads/{job.branch}", f"refs/remotes/{remote_head(job)}"]
    refs += [f"refs/remotes/origin/{job.branch}"] if pushed.returncode == 0 else []
    w.git("-C", job.clone, "bundle", "create", "-q", into / "in.bundle", *refs)
    name, email = (
        w.git("-C", job.clone, "config", key, check=False).stdout.strip() or fallback
        for key, fallback in (("user.name", "workshop"), ("user.email", "workshop@users.noreply.github.com"))
    )
    (into / "gitconfig").write_text(f"[user]\n\tname = {name}\n\temail = {email}\n")
    packed = into / "in.tar"
    with tarfile.open(packed, "w") as tar:
        tar.add(into / "in.bundle", arcname(Path(INSIDE) / "in.bundle"))
        tar.add(into / "gitconfig", arcname(Path.home() / ".gitconfig"))
        tar.add(job.log_path.parent / "env.sh", arcname(job.log_path.parent / "env.sh"))
        tar.add(job.handoff, arcname(job.handoff))
        for folder in dict.fromkeys(path.parent for path in job.skills):
            tar.add(folder, arcname(folder))
    return packed


def bring_back(job: w.Job, out: Path) -> None:
    """Take the job branch and the handoff directory from what LEAVE made. The worktree takes the branch's commit,
    as if the agent had committed here. The handoff is unpacked as data: no links out, no absolute paths."""
    w.git("-C", job.worktree, "fetch", "-q", out / "work.bundle", f"refs/heads/{job.branch}")
    w.git("-C", job.worktree, "reset", "-q", "--hard", "FETCH_HEAD")
    shutil.rmtree(job.handoff)
    job.handoff.mkdir()
    try:
        with tarfile.open(out / "handoff.tar") as tar:
            tar.extractall(job.handoff, filter="data")
    except tarfile.TarError as exc:
        shutil.rmtree(job.handoff)
        job.handoff.mkdir()
        raise w.ToolError(f"the agent's handoff directory could not be unpacked: {exc}") from None


def sandbox_env(env: dict[str, str]) -> dict[str, str]:
    """The claude process's environment in the Sandbox: `agent_env`'s, less what describes the tool's machine.
    Only this process gets the Claude login, and Claude Code's sandbox hides it from commands."""
    if not any(env.get(name) for name in LOGIN):
        raise w.ToolError("an agent on Modal needs CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY")
    return {**{name: value for name, value in env.items() if name not in MACHINE}, "DISABLE_AUTOUPDATER": "1"}


def allowed_hosts(job: w.Job) -> list[str]:
    """What the Sandbox may reach: Anthropic's API, the PostHog host if traces go there, and the target repo's
    allowed domains, which Claude Code's sandbox also allows commands."""
    posthog = (
        urllib.parse.urlparse(os.environ["WORKSHOP_POSTHOG_HOST"]).hostname if w.telemetry_env() else None
    )
    domains = job.settings.get("sandbox", {}).get("network", {}).get("allowedDomains", [])
    return list(dict.fromkeys(["api.anthropic.com", *([posthog] if posthog else []), *domains]))


def repo_secrets(job: w.Job) -> list[modal.Secret]:
    """The target repo's own Modal secret, workshop-<owner>-<repo>, if the owner made one. Its values reach commands,
    as the repo's own credentials do on the owner's machine."""
    secret = modal.Secret.from_name(f"workshop-{job.repo.replace('/', '-').lower()}")
    try:
        secret.hydrate()
    except modal.exception.NotFoundError:
        return []
    return [secret]


def agent_image(job: w.Job, tmp: Path) -> modal.Image:
    """AGENT_IMAGE, then, if the default branch has .workshop/setup.sh, that commit's files at the worktree's path
    with setup.sh run there, with the network open. Modal reuses the build while the commit and path are the same."""
    if w.git("-C", job.clone, "cat-file", "-e", f"{job.base}:{SETUP}", check=False).returncode:
        return AGENT_IMAGE
    w.git("-C", job.clone, "archive", "--output", tmp / "default.tar", job.base)
    with tarfile.open(tmp / "default.tar") as tar:
        tar.extractall(tmp / "default", filter="tar")
    worktree = shlex.quote(str(job.worktree))
    return AGENT_IMAGE.add_local_dir(tmp / "default", str(job.worktree), copy=True).run_commands(
        f"cd {worktree} && bash {SETUP}"
    )


def sandbox_script(sandbox, script: str, *args) -> None:
    proc = sandbox.exec("bash", "-c", script, "workshop", *map(str, args))
    output = "".join([*proc.stdout, *proc.stderr])
    if code := proc.wait():
        raise w.ToolError(f"the Modal Sandbox's script exited with code {code}: {output.strip()[-2000:]}")


def run_agent(job: w.Job, cmd: list[str], env: dict[str, str], prompt: str, log) -> int:
    """Run `cmd` in a Modal Sandbox with the job's working copy, writing its output to `log`, and bring its commits
    and handoff back. Return its exit code, 124 on timeout, as `invoke_agent` does locally."""
    with tempfile.TemporaryDirectory(prefix="workshop-modal-") as tmp:
        try:
            return in_sandbox(job, cmd, sandbox_env(env), prompt, log, Path(tmp))
        except modal.exception.Error as exc:  # as when setup.sh fails: the issue is queued again
            raise w.ToolError(f"Modal: {exc}") from None


def in_sandbox(job: w.Job, cmd: list[str], env: dict[str, str], prompt: str, log, tmp: Path) -> int:
    timeout_s, packed = job.args.agent_timeout, pack(job, tmp / "in")
    w.status("starting the agent's Modal Sandbox")
    sandbox = modal.Sandbox.create(
        app=modal.App.lookup(APP, create_if_missing=True),
        image=agent_image(job, tmp),
        runtime="vm",
        timeout=timeout_s + 600,
        cpu=2,
        memory=4096,
        outbound_domain_allowlist=allowed_hosts(job),
        secrets=repo_secrets(job),
    )
    try:
        sandbox.filesystem.make_directory(INSIDE)
        sandbox.filesystem.copy_from_local(packed, f"{INSIDE}/in.tar")
        where = (job.worktree, job.branch)
        sandbox_script(sandbox, ENTER, f"{INSIDE}/in.tar", "/", *where, remote_head(job), job.repo)
        agent = sandbox.exec(
            "bash", "-c", 'exec timeout -k 30 "$@" 2>&1', "timeout", str(timeout_s), *cmd,
            workdir=str(job.worktree), env=env,
        )  # fmt: skip
        agent.stdin.write(prompt)
        agent.stdin.write_eof()
        agent.stdin.drain()
        for line in agent.stdout:
            log.write(line)
            log.flush()
        code = agent.wait()
        sandbox_script(sandbox, LEAVE, *where, job.handoff, INSIDE)
        (tmp / "out").mkdir()
        for name in ("work.bundle", "handoff.tar"):
            sandbox.filesystem.copy_to_local(f"{INSIDE}/{name}", tmp / "out" / name)
        bring_back(job, tmp / "out")
        return code
    finally:
        sandbox.terminate()


# --- the schedule: the tool in a Modal function, with the GitHub token. The agent runs in its own Sandbox.

app = modal.App(APP)
logs = modal.Volume.from_name("workshop-logs", create_if_missing=True)  # transcripts and runs.jsonl
GH_APT = "https://cli.github.com/packages"
TOOL_IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "curl", "ca-certificates")
    .run_commands(
        f"curl -fsSL {GH_APT}/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg",
        "echo 'deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
        f"{GH_APT} stable main' > /etc/apt/sources.list.d/github-cli.list",
        "apt-get update && apt-get install -y gh",
    )
    .add_local_dir(
        w.TOOL_ROOT,
        "/workshop",
        copy=True,
        ignore=[".git", ".claude", ".venv", ".superpowers", "tests", "evals", "docs", "**/__pycache__"],
    )
    # A remote with no history, so own_repo() still knows this repository.
    .run_commands(
        f"git init -q /workshop && git -C /workshop remote add origin https://github.com/{w.own_repo()}"
    )
    .env({"PYTHONPATH": "/workshop"})
)


@app.function(
    image=TOOL_IMAGE,
    schedule=modal.Cron("0 * * * *"),
    secrets=[modal.Secret.from_name("workshop")],  # GH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, WORKSHOP_REPOS
    volumes={"/root/.workshop/logs": logs},
    timeout=6 * 3600,
    max_containers=1,  # one run at a time: a late run waits for the one before
)
def work_queue() -> None:
    """Work each configured repo's queued issues, as `./workshop.py run <owner>/<repo> --modal` does."""
    w.sh("gh", "auth", "setup-git")  # git pushes with GH_TOKEN, through gh
    try:
        for repo in filter(None, (name.strip() for name in os.environ["WORKSHOP_REPOS"].split(","))):
            w.main(["run", repo, "--modal"])
    finally:
        logs.commit()
