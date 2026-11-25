import argparse
import io
import json
import os
import subprocess
import tarfile
import urllib.parse
from pathlib import Path

import pytest

import workshop
import workshop_modal

BRANCH = "workshop/issue-12"


def real_git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def job(tmp_path, monkeypatch):
    """A job with a real clone and worktree, as `work` makes them. The clone's remote carries a token."""
    home = tmp_path / "home"
    (home / ".workshop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    real_git("config", "--global", "user.name", "Owner")
    real_git("config", "--global", "user.email", "owner@example.com")
    origin = tmp_path / "origin"
    real_git("init", "-q", "-b", "main", origin)
    (origin / ".gitignore").write_text(".venv/\n")
    (origin / "README.md").write_text("# acme/analytics\n")
    real_git("add", ".", cwd=origin)
    real_git("commit", "-q", "-m", "Initial commit", cwd=origin)
    clone, worktree = (
        home / ".workshop" / "repos" / "acme" / "analytics",
        home / ".workshop" / "worktrees" / "w",
    )
    real_git("clone", "-q", "--no-checkout", origin, clone)
    real_git(
        "-C", clone, "remote", "set-url", "origin", "https://x-access-token:SECRET@github.com/acme/a.git"
    )
    real_git("-C", clone, "remote", "set-head", "origin", "main")
    real_git("-C", clone, "worktree", "add", "-q", "--no-track", "-b", BRANCH, worktree, "origin/main")
    handoff, logs = home / ".workshop" / "handoff" / "w", home / ".workshop" / "logs" / "w"
    handoff.mkdir(parents=True)
    (handoff / "pull-request.md").write_text("Earlier title\n")
    skill = home / ".workshop" / "logs" / "w" / "skills" / "local" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: local\n---\n")
    (logs / "env.sh").write_text("export DBT_TARGET=dev\n")
    args = argparse.Namespace(agent_timeout=900)
    base = real_git("-C", clone, "rev-parse", "origin/main")
    settings = {"sandbox": {"network": {"allowedDomains": ["pypi.org", "api.anthropic.com"]}}}
    return workshop.Job(
        "acme/analytics", 12, [skill], BRANCH, base, clone, settings, worktree, handoff, logs / "x.log", args
    )


def test_the_working_copy_goes_in_and_comes_back_without_the_token(job, tmp_path):
    packed = workshop_modal.pack(job, tmp_path / "packed")
    root = tmp_path / "sandbox"  # stands for the Sandbox's /, where the same paths are made
    inside = root / str(job.worktree).lstrip("/")
    (inside / ".venv").mkdir(parents=True)  # what setup.sh left, ignored by git: it stays
    (inside / ".venv" / "marker").write_text("built")
    (inside / "stray.txt").write_text("from the default branch, deleted on the job branch")
    enter = [
        "bash",
        "-c",
        workshop_modal.ENTER,
        "workshop",
        packed,
        root,
        inside,
        BRANCH,
        "origin/main",
        job.repo,
    ]
    subprocess.run(enter, check=True, capture_output=True)

    assert (inside / ".venv" / "marker").exists() and not (inside / "stray.txt").exists()
    assert real_git("-C", inside, "rev-parse", "origin/HEAD") == job.base
    assert real_git("-C", inside, "symbolic-ref", "--short", "HEAD") == BRANCH
    assert not [path for path in root.rglob("*") if path.is_file() and b"SECRET" in path.read_bytes()]
    assert not any((root / str(job.clone / ".git").lstrip("/")).iterdir())
    sandbox_home = root / str(Path.home()).lstrip("/")
    assert (root / str(job.skills[0]).lstrip("/")).read_text() == job.skills[0].read_text()
    assert (root / str(job.log_path.parent / "env.sh").lstrip("/")).read_text() == "export DBT_TARGET=dev\n"

    (inside / "orders.sql").write_text("select 1\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(sandbox_home / ".gitconfig")}
    subprocess.run(["git", "-C", inside, "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", inside, "commit", "-q", "-m", "Add orders"], check=True, env=env)
    assert real_git("-C", inside, "log", "-1", "--format=%an <%ae>") == "Owner <owner@example.com>"
    handoff = root / str(job.handoff).lstrip("/")
    assert (
        handoff / "pull-request.md"
    ).read_text() == "Earlier title\n"  # the agent sees its earlier handoff
    (handoff / "pull-request.md").write_text("Add orders\n\nKind: add fields\n")
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(["bash", "-c", workshop_modal.LEAVE, "workshop", inside, BRANCH, handoff, out], check=True)

    workshop_modal.bring_back(job, out)
    assert real_git("-C", job.clone, "rev-parse", f"refs/heads/{BRANCH}") == real_git(
        "-C", inside, "rev-parse", "HEAD"
    )
    assert real_git("-C", job.clone, "show", f"{BRANCH}:orders.sql") == "select 1"
    assert (job.handoff / "pull-request.md").read_text() == "Add orders\n\nKind: add fields\n"


@pytest.mark.parametrize(
    "name, kind",
    [("question.md", tarfile.SYMTYPE), ("../question.md", tarfile.REGTYPE), ("/question.md", tarfile.REGTYPE),
     ("notes/question.md", tarfile.REGTYPE), ("question.md", tarfile.LNKTYPE)],
)  # fmt: skip
def test_a_handoff_of_anything_but_plain_files_is_refused(job, tmp_path, name, kind):
    out = tmp_path / "out"
    out.mkdir()
    real_git("-C", job.clone, "bundle", "create", "-q", out / "work.bundle", f"refs/heads/{BRANCH}")
    with tarfile.open(out / "handoff.tar", "w") as tar:
        tar.addfile(tarfile.TarInfo("."))  # what `tar -C handoff .` writes first
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, "/etc/passwd"
        tar.addfile(member, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
    with pytest.raises(workshop.ToolError, match="handoff"):
        workshop_modal.bring_back(job, out)
    assert not list(job.handoff.iterdir()) and not (tmp_path / "question.md").exists()


def test_only_the_claude_process_gets_the_login(monkeypatch):
    env = {
        "PATH": "/opt/homebrew/bin",
        "HOME": "/Users/owner",
        "SHELL": "/bin/zsh",
        "TMPDIR": "/var/folders/x",
        "USER": "owner",
        "LANG": "en_US.UTF-8",
        "CLAUDE_CONFIG_DIR": "/Users/owner/.claude",
        "CLAUDE_CODE_OAUTH_TOKEN": "token",
        "CLAUDE_ENV_FILE": "/Users/owner/.workshop/logs/w/env.sh",
    }
    assert workshop_modal.sandbox_env(env) == {
        "HOME": "/Users/owner",
        "LANG": "en_US.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": "token",
        "CLAUDE_ENV_FILE": "/Users/owner/.workshop/logs/w/env.sh",
        "DISABLE_AUTOUPDATER": "1",
    }
    with pytest.raises(workshop.ToolError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        workshop_modal.sandbox_env({"HOME": "/Users/owner", "CLAUDE_CONFIG_DIR": "/Users/owner/.claude"})


def test_the_sandbox_reaches_anthropic_posthog_and_the_repos_hosts(job, monkeypatch):
    assert workshop_modal.allowed_hosts(job) == ["api.anthropic.com", "pypi.org"]
    monkeypatch.setenv("WORKSHOP_POSTHOG_TOKEN", "phc_x")
    monkeypatch.setenv("WORKSHOP_POSTHOG_HOST", "https://eu.i.posthog.com/")
    assert workshop_modal.allowed_hosts(job) == ["api.anthropic.com", "eu.i.posthog.com", "pypi.org"]


class FakeProcess:
    def __init__(self, lines, code):
        self.stdout, self.stderr, self.code, self.stdin = iter(lines), iter([]), code, io.StringIO()
        self.stdin.write_eof, self.stdin.drain = (lambda: None), (lambda: None)

    def wait(self):
        return self.code


class FakeSandbox:
    def __init__(self, fail_at=""):
        self.execs, self.copied, self.terminated, self.fail_at = [], [], False, fail_at
        self.filesystem = self

    def make_directory(self, path, create_parents=True):
        pass

    def copy_from_local(self, local, remote):
        self.copied.append((str(local), remote))

    def copy_to_local(self, remote, local):
        Path(local).write_bytes(b"")

    def exec(self, *args, env=None, workdir=None, **kwargs):
        self.execs.append({"args": args, "env": env, "workdir": workdir})
        if self.fail_at and self.fail_at in args[2]:
            raise RuntimeError("the Sandbox went away")
        if "timeout" in args[2]:  # the agent
            return FakeProcess(['{"type": "result"}\n'], 124)
        return FakeProcess(["ok\n"], 0)

    def terminate(self):
        self.terminated = True


@pytest.mark.parametrize("fail_at", ["", "timeout"])
def test_the_agent_runs_in_a_vm_sandbox_that_is_always_terminated(job, tmp_path, monkeypatch, fail_at):
    sandbox, created, brought = FakeSandbox(fail_at), {}, []
    monkeypatch.setattr(workshop_modal.modal.App, "lookup", lambda *args, **kwargs: "app")
    monkeypatch.setattr(
        workshop_modal.modal.Sandbox, "create", lambda **kwargs: created.update(kwargs) or sandbox
    )
    monkeypatch.setattr(workshop_modal, "agent_image", lambda job, tmp: "image")
    monkeypatch.setattr(workshop_modal, "repo_secrets", lambda job: ["repo secret"])
    monkeypatch.setattr(workshop_modal, "bring_back", lambda job, out: brought.append(out))
    env = {"HOME": str(Path.home()), "CLAUDE_CODE_OAUTH_TOKEN": "token", "CLAUDE_ENV_FILE": "/x/env.sh"}
    log = io.StringIO()
    if fail_at:
        with pytest.raises(RuntimeError):
            workshop_modal.run_agent(job, ["claude", "-p"], env, "the prompt", log)
    else:
        assert workshop_modal.run_agent(job, ["claude", "-p"], env, "the prompt", log) == 124
        assert log.getvalue() == '{"type": "result"}\n' and len(brought) == 1
    assert sandbox.terminated
    assert created["runtime"] == "vm" and created["timeout"] == 900 + 600
    assert created["outbound_domain_allowlist"] == ["api.anthropic.com", "pypi.org"]
    assert created["secrets"] == ["repo secret"] and created["image"] == "image"
    agent = [run for run in sandbox.execs if "timeout" in run["args"][2]]
    assert [run["env"] for run in agent] == [workshop_modal.sandbox_env(env)]
    assert agent[0]["workdir"] == str(job.worktree) and agent[0]["args"][-2:] == ("claude", "-p")
    assert all(run["env"] is None for run in sandbox.execs if run not in agent)


@pytest.mark.filterwarnings("ignore:The work_queue function is executing locally")
@pytest.mark.parametrize("first", [0, 1, RuntimeError("a Modal stream broke"), 130])
def test_the_schedule_runs_each_configured_repo_on_modal(monkeypatch, tmp_path, first):
    runs, gh, commits = [], [], []
    pings = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(workshop_modal, "heartbeat", lambda signal, batch_id: pings.append(signal))

    def main(argv):
        runs.append(argv)
        if isinstance(first, Exception) and len(runs) == 1:
            raise first
        return first if len(runs) == 1 else 0

    monkeypatch.setenv("WORKSHOP_REPOS", "acme/analytics, acme/web,")
    monkeypatch.setattr(workshop, "main", main)
    monkeypatch.setattr(workshop, "gh", lambda *args, **kwargs: gh.append(args))
    monkeypatch.setattr(
        workshop_modal, "logs", type("Volume", (), {"commit": lambda self: commits.append(1)})()
    )
    if first == 0:
        workshop_modal.work_queue.local()
    else:
        with pytest.raises(Exception, match="acme/analytics"):
            workshop_modal.work_queue.local()
    assert gh == [("auth", "setup-git")] and commits == [1]
    web = [["run", "acme/web", "--modal"]]
    assert runs == [["run", "acme/analytics", "--modal"], *([] if first == 130 else web)]
    assert pings == ["/start", "" if first == 0 else "/fail"]
    events = [
        json.loads(line) for line in (tmp_path / ".workshop/logs/batches.jsonl").read_text().splitlines()
    ]
    assert events[-1]["status"] == ("succeeded" if first == 0 else "failed")
    assert events[-1]["failed_repos"] == ([] if first == 0 else ["acme/analytics"])


@pytest.mark.filterwarnings("ignore:The work_queue function is executing locally")
def test_batch_summary_counts_only_new_jobs_and_keeps_handoffs_separate(monkeypatch, tmp_path, capsys):
    records = tmp_path / ".workshop/logs/runs.jsonl"
    workshop.save_record({"outcome": "error", "reason": "an old run"}, records)
    outcomes = [
        {"outcome": "needs-info", "checks_status": "", "pr_url": ""},
        {
            "outcome": "escalated",
            "checks_status": "unverified",
            "pr_url": "https://github.com/acme/web/pull/2",
        },
        {"outcome": "pr-open", "checks_status": "passed", "pr_url": "https://github.com/acme/web/pull/3"},
    ]

    def main(argv):
        for issue, record in enumerate(outcomes, 1):
            workshop.save_record(
                {**record, "repo": argv[1], "issue": issue, "reason": "private note"}, records
            )
        return 0

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_REPOS", "acme/web")
    monkeypatch.setenv("WORKSHOP_DEPLOYMENT_SHA", "d" * 40)
    monkeypatch.setattr(workshop_modal.modal, "current_function_call_id", lambda: "fc-example")
    monkeypatch.setattr(workshop, "main", main)
    monkeypatch.setattr(workshop, "gh", lambda *args: None)
    monkeypatch.setattr(workshop_modal, "logs", type("Volume", (), {"commit": lambda self: None})())
    pings = []
    monkeypatch.setattr(
        workshop_modal, "heartbeat", lambda signal, batch_id: pings.append((signal, batch_id))
    )
    workshop_modal.work_queue.local()
    events = [json.loads(line) for line in (records.parent / "batches.jsonl").read_text().splitlines()]
    start, end = events
    assert start["event"] == "batch_start" and start["batch_id"] == end["batch_id"]
    assert end["status"] == "succeeded" and end["event"] == "batch_end"
    assert end["modal_call_id"] == "fc-example" and end["deployment_sha"] == "d" * 40
    assert end["issues_processed"] == 3 and end["pull_requests"] == 2 and end["ci_unverified"] == 1
    assert end["outcomes"] == {"needs-info": 1, "escalated": 1, "pr-open": 1}
    assert [job["issue"] for job in end["jobs"]] == [1, 2, 3]
    assert pings == [("/start", end["batch_id"]), ("", end["batch_id"])]
    assert "private note" not in capsys.readouterr().err
    assert "private note" not in (records.parent / "batches.jsonl").read_text()


@pytest.mark.filterwarnings("ignore:The work_queue function is executing locally")
@pytest.mark.parametrize("failure", ["auth", "commit", "interrupt", "empty-repos"])
def test_early_failures_and_log_failures_never_send_success(monkeypatch, tmp_path, capsys, failure):
    pings = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_REPOS", " , " if failure == "empty-repos" else "acme/analytics")
    monkeypatch.setattr(workshop_modal, "heartbeat", lambda signal, batch_id: pings.append(signal))

    def fail():
        raise RuntimeError("service unavailable")

    def main(argv):
        if failure == "interrupt":
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(workshop, "main", main)
    monkeypatch.setattr(workshop, "gh", lambda *args: fail() if failure == "auth" else None)
    monkeypatch.setattr(
        workshop_modal,
        "logs",
        type(
            "Volume",
            (),
            {
                "commit": lambda self: fail() if failure == "commit" else None,
            },
        )(),
    )
    expected = (
        KeyboardInterrupt
        if failure == "interrupt"
        else (workshop.ToolError if failure == "empty-repos" else RuntimeError)
    )
    with pytest.raises(expected):
        workshop_modal.work_queue.local()
    assert pings == ["/start", "/fail"]
    end = json.loads(capsys.readouterr().err.splitlines()[-1].removeprefix("workshop: "))
    assert end["status"] == "failed" and end["error_type"] == expected.__name__
    if failure == "commit":
        assert end["logs_saved"] is False


def test_healthchecks_pings_are_bounded_correlated_and_contain_no_job_data(monkeypatch):
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(b"OK")

    monkeypatch.setattr(workshop_modal.urllib.request, "urlopen", opener)
    monkeypatch.delenv("HEALTHCHECKS_PING_URL", raising=False)
    workshop_modal.heartbeat("/start", "run-id")
    assert requests == []
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/fake-token/?custom=1")
    for signal in ("/start", "", "/fail"):
        workshop_modal.heartbeat(signal, "run-id")
    assert [urllib.parse.urlsplit(request.full_url).path for request, _ in requests] == [
        "/fake-token/start",
        "/fake-token",
        "/fake-token/fail",
    ]
    for request, timeout in requests:
        assert request.method == "POST" and request.data is None and timeout == 5
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) == {
            "custom": ["1"],
            "rid": ["run-id"],
        }


def test_monitoring_outage_does_not_fail_work_or_expose_the_ping_token(monkeypatch, capsys):
    attempts = []

    def opener(request, timeout):
        attempts.append(request)
        raise OSError(f"failed to connect to {request.full_url}")

    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/fake-secret-token")
    monkeypatch.setattr(workshop_modal.urllib.request, "urlopen", opener)
    workshop_modal.heartbeat("/fail", "run-id")
    assert len(attempts) == 3
    output = capsys.readouterr().err
    assert "ping failed after 3 attempts" in output
    assert "fake-secret-token" not in output and "failed to connect" not in output


def test_repo_secret_names_do_not_collide():
    assert workshop_modal.repo_secret("Acme/Analytics") == "workshop-acme--analytics"
    assert workshop_modal.repo_secret("acme-foo/bar") != workshop_modal.repo_secret("acme/foo-bar")


def test_a_modal_error_is_a_tool_error_so_the_issue_is_queued_again(job, monkeypatch):
    def create(**kwargs):
        raise workshop_modal.modal.exception.InvalidError("Image build failed: .workshop/setup.sh exited 1")

    monkeypatch.setattr(workshop_modal.modal.App, "lookup", lambda *args, **kwargs: "app")
    monkeypatch.setattr(workshop_modal.modal.Sandbox, "create", create)
    monkeypatch.setattr(workshop_modal, "agent_image", lambda job, tmp: "image")
    monkeypatch.setattr(workshop_modal, "repo_secrets", lambda job: [])
    env = {"HOME": str(Path.home()), "CLAUDE_CODE_OAUTH_TOKEN": "token"}
    with pytest.raises(workshop.ToolError, match="setup.sh exited 1"):
        workshop_modal.run_agent(job, ["claude"], env, "the prompt", io.StringIO())


def test_setup_sh_runs_in_the_default_branchs_files_at_the_worktrees_path(job, tmp_path):
    assert workshop_modal.agent_image(job, tmp_path) is workshop_modal.AGENT_IMAGE  # no setup.sh
    origin = tmp_path / "origin"
    (origin / ".workshop").mkdir()
    (origin / ".workshop" / "setup.sh").write_text("uv sync --frozen\n")
    (origin / "modal.toml").symlink_to(Path.home() / ".modal.toml")  # on the tool's machine, a secret
    real_git("add", ".", cwd=origin)
    real_git("commit", "-q", "-m", "Add setup.sh", cwd=origin)
    real_git("-C", job.clone, "fetch", "-q", origin, "main")
    job.base = real_git("-C", job.clone, "rev-parse", "FETCH_HEAD")
    (tmp_path / "build").mkdir()  # run_agent's temporary directory
    image = workshop_modal.agent_image(job, tmp_path / "build")
    assert image is not workshop_modal.AGENT_IMAGE
    assert (tmp_path / "build" / "default" / ".workshop" / "setup.sh").read_text() == "uv sync --frozen\n"
    assert not (tmp_path / "build" / "default" / "modal.toml").is_symlink()
