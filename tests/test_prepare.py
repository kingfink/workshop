import argparse
import copy
import json
from pathlib import Path

import pytest

import workshop as w
import workshop_eval
import workshop_prepare as p

REPO = "acme/analytics"


def pr(number=12, **values):
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "labels": [],
        "author_association": "OWNER",
        "head": {
            "ref": f"feature-{number}",
            "sha": "a" * 40,
            "repo": {"full_name": REPO, "default_branch": "main"},
        },
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": REPO}},
        **values,
    }


def args(**values):
    return argparse.Namespace(
        **{
            "ref": REPO,
            "include_drafts": False,
            "max_fix_attempts": 1,
            "agent_timeout": 60,
            "checks_timeout": 1,
            "budget": 5.0,
            "assessment_budget": 2.0,
            **values,
        }
    )


def job(tmp_path):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    return w.Job(
        REPO,
        12,
        [w.SKILLS_DIR / p.SKILL / "SKILL.md"],
        "branch",
        "base",
        tmp_path,
        {},
        tmp_path,
        handoff,
        tmp_path / "agent.log",
        args(),
        github=False,
    )


def test_pagination(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "gh_json", lambda *a: calls.append(a) or [[pr(1)], [pr(2)]])
    assert [item["number"] for item in p.pages("endpoint")] == [1, 2]
    assert calls == [("api", "--paginate", "--slurp", "endpoint")]


def test_batch_selection_and_explicit_drafts(monkeypatch):
    candidates = [
        pr(3),
        pr(2, draft=True),
        pr(4, labels=[{"name": "workshop:ignore"}]),
        pr(5, author_association="NONE", user={"login": "dependabot[bot]", "type": "Bot"}),
        pr(1),
    ]
    monkeypatch.setattr(p, "pages", lambda _: candidates)
    assert [item["number"] for item in p.select_prs(REPO.upper(), None, False)] == [1, 3, 5]
    assert [item["number"] for item in p.select_prs(REPO, None, True)] == [1, 2, 3, 5]
    monkeypatch.setattr(p, "read_pr", lambda *_: candidates[1])
    assert p.select_prs(REPO, 2, False) == [candidates[1]]
    monkeypatch.setattr(p, "read_pr", lambda *_: candidates[2])
    with pytest.raises(w.ToolError, match="workshop:ignore"):
        p.select_prs(REPO, 4, True)


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"state": "closed"}, "not open"),
        ({"head": {"repo": {"full_name": "someone/fork"}}}, "target repository"),
        ({"head": {"repo": None}}, "target repository"),
    ],
)
def test_exclusions(change, reason):
    assert reason in p.excluded(pr(**change), REPO, True)


@pytest.mark.parametrize("side", ["head", "base"])
def test_shared_destination_excludes_even_an_explicit_pr(monkeypatch, side):
    target, other = pr(12), pr(13, draft=True)
    other[side] = target["head"]
    monkeypatch.setattr(p, "open_prs", lambda _: [target, other])
    monkeypatch.setattr(p, "read_pr", lambda *_: target)
    with pytest.raises(w.ToolError, match=f"{side} of open PR #13"):
        p.select_prs(REPO, 12, False)
    monkeypatch.setattr(p, "feedback", lambda *a: [])
    with pytest.raises(w.ToolError, match="eligibility changed"):
        p.guard(REPO, target, False, target["head"]["sha"], [], True)


@pytest.mark.parametrize("ref, reason", [("main", "default branch"), ("workshop/issue-12", "reserved")])
def test_default_and_workshop_branches_excluded_even_explicitly(monkeypatch, ref, reason):
    target = pr()
    target["base"]["ref"] = "release"
    target["head"]["ref"] = ref
    monkeypatch.setattr(p, "read_pr", lambda *_: target)
    with pytest.raises(w.ToolError, match=reason):
        p.select_prs(REPO, 12, False)


def test_other_bot_cannot_supply_a_patch_even_if_it_is_a_member(monkeypatch):
    other = pr(user={"login": "github-actions[bot]", "type": "Bot"}, author_association="MEMBER")
    monkeypatch.setattr(p, "read_pr", lambda *_: other)
    with pytest.raises(w.ToolError, match="author must have push access or be Dependabot"):
        p.select_prs(REPO, 12, False)


def test_only_trusted_feedback_reaches_the_agent(monkeypatch):
    monkeypatch.setattr(
        p,
        "pages",
        lambda _: [
            {"id": 1, "author_association": "NONE", "body": "untrusted"},
            {
                "id": 2,
                "author_association": "OWNER",
                "body": "requested change",
                "state": "CHANGES_REQUESTED",
            },
            {"id": 3, "author_association": "OWNER", "body": p.MARKER},
            {"id": 4, "author_association": "MEMBER", "user": {"type": "Bot"}, "body": "bot prose"},
        ],
    )
    result = p.feedback(REPO, 12)
    assert len(result) == 3
    assert all(item["body"] == "requested change" for item in result)


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "NONE"])
def test_request_only_includes_trusted_authors(tmp_path, monkeypatch, association):
    item = pr(author_association=association, title="Add a feature", body="Keep existing behavior")
    if association == "NONE":
        item["user"] = {"login": "dependabot[bot]", "type": "Bot"}
    monkeypatch.setattr(p, "feedback", lambda *a: [])
    monkeypatch.setattr(w, "pr_checks", lambda *a: (item["head"]["sha"], []))
    monkeypatch.setattr(w, "gh", lambda *a: argparse.Namespace(stdout="patch"))
    evidence = p.collect(REPO, item, tmp_path / "logs")
    expected = {"title": item["title"], "body": item["body"]} if association in w.TRUSTED_AUTHORS else {}
    assert evidence["request"] == expected
    assert evidence["diff"] == "patch"
    assert evidence["dependabot"] == (association == "NONE")


def test_collect_saves_complete_failed_job_logs(tmp_path, monkeypatch):
    task = job(tmp_path)
    item = pr(title="Update widget", body="Update the dependency")
    checks = [
        {"bucket": "fail", "link": f"https://github.com/{REPO}/actions/runs/27/job/{number}"}
        for number in (31, 32)
    ]
    root_error = "FAIL accepted_values_channel_type\n"
    full_log = root_error + "routine progress\n" * 1000 + "RuntimeError: command failed\n"
    assert root_error not in full_log[-w.FAILED_LOG_CHARS :]
    monkeypatch.setattr(p, "feedback", lambda *a: [])
    monkeypatch.setattr(w, "pr_checks", lambda *a: (item["head"]["sha"], checks))
    fetches = []

    def gh_json(*args):
        if args[-1].endswith("/jobs"):
            return [
                {"jobs": [{"id": 30, "name": "lint", "conclusion": "success"}]},
                {"jobs": [{"id": n, "name": "build", "conclusion": "failure"} for n in (31, 32)]},
            ]
        return {"event": "pull_request", "actor": {"login": "dependabot[bot]"}}

    def gh(*args, **kwargs):
        if args[:2] == ("pr", "diff"):
            return argparse.Namespace(stdout="patch")
        assert "--log" in args and "--log-failed" not in args
        fetches.append(args[args.index("--job") + 1])
        return argparse.Namespace(stdout=full_log, returncode=0)

    monkeypatch.setattr(w, "gh_json", gh_json)
    monkeypatch.setattr(w, "gh", gh)
    log_dir = tmp_path / "logs"
    evidence = p.collect(REPO, item, log_dir)
    assert fetches == [31, 32]  # two checks on one run do not fetch its jobs twice
    assert "pull_request, actor: dependabot[bot]" in evidence["failed_logs"]
    assert root_error not in evidence["failed_logs"]  # evidence holds paths, not inline logs
    for number in (31, 32):
        path = log_dir / f"27-{number}.log"
        assert str(path.resolve()) in evidence["failed_logs"]
        assert path.read_text() == full_log

    task.inspection = True
    command = w.claude_command(task)
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert f"Read(/{log_dir}/**)" in allowed and "Bash" not in allowed
    task.inspection = False
    task.log_path = tmp_path / "12" / "agent.log"
    task.evidence_dir = log_dir
    command = w.claude_command(task)
    assert f"Read(/{log_dir}/**)" in command[command.index("--allowedTools") + 1].split(",")


@pytest.mark.parametrize("failure", ["metadata", "jobs", "download", "empty", "no-failed-jobs"])
def test_failed_job_logs_report_unavailable_evidence(tmp_path, monkeypatch, failure):
    def gh_json(*args):
        if args[-1].endswith("/jobs"):
            if failure == "jobs":
                raise w.ToolError("not found")
            return [
                {
                    "jobs": []
                    if failure == "no-failed-jobs"
                    else [{"id": 31, "name": "build", "conclusion": "failure"}]
                }
            ]
        raise w.ToolError("metadata unavailable")

    monkeypatch.setattr(w, "gh_json", gh_json)
    monkeypatch.setattr(
        w,
        "gh",
        lambda *a, **kw: argparse.Namespace(
            stdout="" if failure == "empty" else "log output\n", returncode=int(failure == "download")
        ),
    )
    result = w.failing_logs(REPO, [{"link": f"https://github.com/{REPO}/actions/runs/27"}], True, tmp_path)
    if failure == "metadata":
        assert (tmp_path / "27-31.log").read_text() == "log output\n"
        assert "Full failed-job log:" in result
    else:
        assert "unavailable" in result or "no failed-job logs available" in result
        assert "Full failed-job log:" not in result
        assert not list(tmp_path.glob("*.log"))


def test_push_can_move_a_comment_and_dismiss_an_approval(monkeypatch):
    original = pr()
    current = copy.deepcopy(original)
    current["head"]["sha"] = "after-push"
    notes = [
        {"kind": "inline", "id": 1, "body": "Keep the test", "path": "test.py", "line": 10},
        {"kind": "review", "id": 2, "reviewer": 42, "state": "APPROVED", "body": "Thanks"},
    ]
    fresh = [{**notes[0], "line": None}, {**notes[1], "state": "DISMISSED"}]
    monkeypatch.setattr(p, "read_pr", lambda *_: current)
    monkeypatch.setattr(p, "feedback", lambda *_: fresh)
    monkeypatch.setattr(w, "gh_json", lambda *a: {"reviewDecision": "REVIEW_REQUIRED"})
    guarded = p.guard(REPO, original, False, "after-push", notes)
    result = p.final_result(REPO, {"status": "ready", "reason": "Checks passed."}, guarded, notes)
    assert result["status"] == "ready"
    assert "needs reapproval" in result["reason"]
    fresh[0]["body"] = "New feedback"
    with pytest.raises(w.ToolError, match="review feedback changed"):
        p.guard(REPO, original, False, "after-push", notes)
    fresh[0]["body"] = notes[0]["body"]
    notes[1]["state"] = "CHANGES_REQUESTED"
    with pytest.raises(w.ToolError, match="review feedback changed"):
        p.guard(REPO, original, False, "after-push", notes)


@pytest.mark.parametrize("field", ["head", "base", "feedback", "draft", "ignore", "closed", "body"])
def test_guard_detects_changes(monkeypatch, field):
    original = pr()
    current = copy.deepcopy(original)
    notes = []
    if field in ("head", "base"):
        current[field]["sha"] = "c" * 40
    elif field == "feedback":
        notes.append({"body": "please stop"})
    elif field == "draft":
        current["draft"] = True
    elif field == "ignore":
        current["labels"] = [{"name": "workshop:ignore"}]
    elif field == "body":
        current["body"] = "Changed intent"
    else:
        current["state"] = "closed"
    monkeypatch.setattr(p, "read_pr", lambda *_: current)
    monkeypatch.setattr(p, "feedback", lambda *_: notes)
    with pytest.raises(w.ToolError):
        p.guard(REPO, original, False, original["head"]["sha"], [])


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [{"prs": [12, 12], "status": "prepare", "reason": "review"}],
        [{"prs": [13], "status": "prepare", "reason": "review"}],
        [{"prs": [12], "status": "ready", "reason": "green"}],
    ],
)
def test_assessment_fails_closed(tmp_path, monkeypatch, groups):
    task = job(tmp_path)

    def agent(self, prompt):
        (self.handoff / "assessment.json").write_text(json.dumps({"groups": groups}))
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    with pytest.raises(w.ToolError):
        p.assess(task, [{"number": 12}])


def test_shared_blockers_are_assessed_and_reported_once(tmp_path, monkeypatch):
    task = job(tmp_path)
    calls = []

    def agent(self, prompt):
        calls.append(prompt)
        (self.handoff / "assessment.json").write_text(
            json.dumps(
                {
                    "groups": [
                        {"prs": [12, 13], "status": "blocked", "reason": "Missing CI credentials."},
                    ]
                }
            )
        )
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    result = p.assess(task, [{"number": 12}, {"number": 13}])
    assert len(calls) == 1
    assert p.report(REPO, result).count("Missing CI credentials.") == 1
    assert result[12] == result[13]


@pytest.fixture
def git_job(tmp_path):
    seed, remote, clone, tree = [tmp_path / name for name in ("seed", "remote.git", "clone", "tree")]
    w.git("init", "--initial-branch=main", seed)
    w.git("-C", seed, "config", "user.name", "workshop")
    w.git("-C", seed, "config", "user.email", "workshop@localhost")

    def commit(path, name, text):
        (path / name).write_text(text)
        w.git("-C", path, "add", name)
        w.git(
            "-C",
            path,
            "-c",
            "user.name=workshop",
            "-c",
            "user.email=workshop@localhost",
            "commit",
            "-m",
            name,
        )
        return w.git("-C", path, "rev-parse", "HEAD").stdout.strip()

    commit(seed, "dependency.txt", "1\n")
    item = pr()
    w.git("-C", seed, "checkout", "-b", item["head"]["ref"])
    item["head"]["sha"] = commit(seed, "dependency.txt", "2\n")
    w.git("-C", seed, "checkout", "main")
    item["base"]["sha"] = commit(seed, "unrelated.txt", "preserve me\n")
    w.git("clone", "--bare", seed, remote)
    w.git("-C", seed, "remote", "add", "origin", remote)
    w.git("clone", remote, clone)
    branch = p.checkout(item, clone, tree)
    task = job(tmp_path)
    task.clone, task.worktree, task.branch = clone, tree, branch
    task.base, task.pushed = item["base"]["sha"], item["head"]["sha"]
    w.git(
        "-C",
        tree,
        "-c",
        "user.name=workshop",
        "-c",
        "user.email=workshop@localhost",
        "merge",
        "--no-edit",
        task.base,
    )
    return task, item, seed, remote, commit


def test_push_preserves_history_and_rerun_does_not_push(git_job, monkeypatch):
    task, item, _, remote, _ = git_job
    monkeypatch.setattr(p, "guard", lambda *a: None)
    assert p.push(task, item, False, [])
    published = w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip()
    assert published == task.pushed
    assert (task.worktree / "unrelated.txt").read_text() == "preserve me\n"
    w.git("-C", task.clone, "merge-base", "--is-ancestor", item["head"]["sha"], task.pushed)
    assert not p.push(task, item, False, [])


def test_clean_behind_pr_keeps_its_head(git_job, monkeypatch):
    task, item, _, remote, _ = git_job
    w.git("-C", task.worktree, "reset", "--hard", task.pushed)
    monkeypatch.setattr(p, "guard", lambda *a: pytest.fail("an unchanged branch should never be pushed"))
    assert not p.push(task, item, False, [])
    assert w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip() == task.pushed
    current = {**item, "mergeable_state": "behind", "feedback": []}
    result = p.final_result(REPO, {"status": "ready", "reason": "Checks passed."}, current, [])
    assert result["status"] == "blocked" and "Update the branch" in result["reason"]


def test_conflict_only_repair_publishes_the_merge_commit(git_job, monkeypatch):
    task, item, seed, remote, commit = git_job
    task.base = commit(seed, "dependency.txt", "3\n")
    w.git("-C", seed, "push", "origin", "main")
    w.git("-C", task.clone, "fetch", "origin")
    w.git("-C", task.worktree, "reset", "--hard", task.pushed)

    def agent(self, prompt):
        assert '"merge_conflicts": true' in prompt
        conflict = w.git(
            "-C",
            self.worktree,
            "-c",
            "user.name=workshop",
            "-c",
            "user.email=w@localhost",
            "merge",
            "--no-edit",
            self.base,
            check=False,
        )
        assert conflict.returncode and "CONFLICT" in conflict.stdout
        (self.worktree / "dependency.txt").write_text("2\n")
        workshop_eval.commit(self.worktree, "Resolve dependency conflict")
        (self.handoff / "preparation.json").write_text(
            '{"status":"ready","summary":"Resolved the conflict."}'
        )
        return 0

    monkeypatch.setattr(p, "guard", lambda *a: {"mergeable": False})
    monkeypatch.setattr(w.Job, "run_agent", agent)
    monkeypatch.setattr(w, "wait_for_checks", lambda *a: {"status": "passed"})
    assert p.prepare_one(task, item, {"feedback": []}, "Review the PR", False)["status"] == "ready"
    repaired = w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip()
    assert repaired == task.pushed
    parents = w.git("-C", task.clone, "show", "-s", "--format=%P", repaired).stdout.split()
    assert parents == [item["head"]["sha"], task.base]


def test_repair_missing_base_is_blocked_without_push(git_job, monkeypatch):
    task, item, _, remote, commit = git_job
    w.git("-C", task.worktree, "reset", "--hard", task.pushed)
    monkeypatch.setattr(p, "guard", lambda *a: {"mergeable": True})

    def agent(self, prompt):
        assert '"merge_conflicts": false' in prompt
        commit(self.worktree, "dependency.txt", "Repaired\n")
        (self.handoff / "preparation.json").write_text('{"status":"ready","summary":"Repaired."}')
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    monkeypatch.setattr(w, "wait_for_checks", lambda *a: pytest.fail("cannot poll before publishing"))
    result = p.prepare_one(task, item, {"feedback": []}, "Review the PR", False)
    assert result["status"] == "blocked" and f"merge base `{task.base}`" in result["reason"]
    assert w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip() == task.pushed


def test_fresh_checkout_recovers_from_a_detached_worktree(git_job):
    task, item, _, _, _ = git_job
    w.git("-C", task.worktree, "checkout", "--detach")
    fresh = task.worktree.with_name("fresh")
    branch = p.checkout(item, task.clone, fresh)
    assert branch != task.branch
    assert w.git("-C", fresh, "rev-parse", "HEAD").stdout.strip() == task.pushed


def test_atomic_lease_refuses_a_concurrent_remote_commit(git_job, monkeypatch):
    task, item, seed, remote, commit = git_job
    w.git("-C", seed, "checkout", item["head"]["ref"])
    newer = commit(seed, "owners-work.txt", "do not overwrite\n")

    def racing_guard(*_):
        w.git("-C", seed, "push", "origin", item["head"]["ref"])

    monkeypatch.setattr(p, "guard", racing_guard)
    with pytest.raises(w.ToolError, match="stale info"):
        p.push(task, item, False, [])
    assert w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip() == newer


def test_push_refuses_rewritten_or_uncommitted_work(git_job, monkeypatch):
    task, item, _, _, _ = git_job
    monkeypatch.setattr(p, "guard", lambda *a: None)
    (task.worktree / "unrelated.txt").write_text("uncommitted")
    with pytest.raises(w.ToolError, match="uncommitted"):
        p.push(task, item, False, [])
    w.git("-C", task.worktree, "reset", "--hard", task.base)
    with pytest.raises(w.ToolError):
        p.push(task, item, False, [])


@pytest.mark.parametrize(
    "validation, expected",
    [
        ({"status": "passed", "reason": "reported-checks-passed"}, "ready"),
        ({"status": "unverified", "reason": "no-checks"}, "blocked"),
        ({"status": "unverified", "reason": "all-skipped"}, "blocked"),
        ({"status": "unverified", "reason": "head-changed"}, "blocked"),
    ],
)
def test_ready_requires_observed_ci(tmp_path, monkeypatch, validation, expected):
    task = job(tmp_path)
    monkeypatch.setattr(p, "guard", lambda *a: {"mergeable": None})
    monkeypatch.setattr(p, "push", lambda *a: False)
    monkeypatch.setattr(w, "wait_for_checks", lambda *a: validation)

    def agent(self, prompt):
        assert '"merge_conflicts": null' in prompt
        (self.handoff / "preparation.json").write_text('{"status":"ready","summary":"Tests passed."}')
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    assert p.prepare_one(task, pr(), {"feedback": []}, "review", False)["status"] == expected


def test_blocked_agent_does_not_push(tmp_path, monkeypatch):
    task = job(tmp_path)
    monkeypatch.setattr(p, "guard", lambda *a: {})
    monkeypatch.setattr(p, "push", lambda *a: pytest.fail("must not push"))

    def agent(self, prompt):
        (self.handoff / "preparation.json").write_text(
            '{"status":"blocked","summary":"Missing credentials."}'
        )
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    assert p.prepare_one(task, pr(), {"feedback": []}, "review", False)["status"] == "blocked"


def test_report_updates_only_our_comment(monkeypatch):
    comments = [
        {"id": 1, "user": {"login": "someone"}, "body": p.MARKER},
        {"id": 2, "user": {"login": "owner"}, "body": p.MARKER},
    ]
    monkeypatch.setattr(p, "pages", lambda *_: comments)
    calls = []
    monkeypatch.setattr(w, "gh", lambda *a, **kw: calls.append((a, kw)))
    for _ in range(2):
        p.publish_result(REPO, pr(), {"status": "blocked", "reason": "Missing credentials"}, "a", {}, "owner")
    assert all(call[0][2] == "PATCH" and call[0][3].endswith("issues/comments/2") for call in calls)


def test_batch_assesses_before_repairs_and_continues_after_failure(tmp_path, monkeypatch, capsys):
    events = []
    candidates = [pr(12), pr(13)]
    monkeypatch.setattr(w, "prepare_worktree", lambda *a: "base")
    monkeypatch.setattr(w, "copy_repo_skills", lambda *a: {})
    monkeypatch.setattr(w, "repo_settings", lambda *a: {})
    monkeypatch.setattr(w, "gh_json", lambda *a: {"login": "owner"})
    monkeypatch.setattr(
        p,
        "collect",
        lambda r, item, log_dir: (
            events.append(f"collect {item['number']}")
            or {
                "number": item["number"],
                "feedback": [],
            }
        ),
    )

    def assess(*_):
        events.append("assess")
        return {n: {"status": "prepare", "reason": "review"} for n in (12, 13)}

    monkeypatch.setattr(p, "assess", assess)
    monkeypatch.setattr(p, "guard", lambda *a: {"feedback": []})
    monkeypatch.setattr(w, "git", lambda *a, **kw: argparse.Namespace(returncode=0))
    monkeypatch.setattr(p, "checkout", lambda *a: "branch")

    def prepare(task, *_):
        events.append(f"prepare {task.issue}")
        if task.issue == 12:
            raise w.ToolError("conflict")
        return {"status": "ready", "reason": "verified"}

    monkeypatch.setattr(p, "prepare_one", prepare)
    monkeypatch.setattr(p, "publish_result", lambda *a: None)
    assert p.run_batch(REPO, candidates, args(), tmp_path, False) == 1
    assert events == ["collect 12", "collect 13", "assess", "prepare 12", "prepare 13"]
    assert "conflict" in capsys.readouterr().out


def test_no_eligible_prs_does_not_invoke_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(w, "require_gh_login", lambda: None)
    monkeypatch.setattr(p, "select_prs", lambda *a: [])
    assert p.cmd_prepare(args()) == 0
    assert "No eligible" in capsys.readouterr().out


@pytest.mark.parametrize("second_status", ["blocked", "ready"])
def test_fix_loop_stops_on_an_external_blocker_or_no_commit(tmp_path, monkeypatch, second_status):
    task = job(tmp_path)
    monkeypatch.setattr(p, "guard", lambda *a: {})
    pushes, prompts = [], []

    def push(*a):
        pushes.append("push")
        return len(pushes) == 1

    polls = []
    monkeypatch.setattr(p, "push", push)
    monkeypatch.setattr(
        w,
        "wait_for_checks",
        lambda *a: (
            polls.append(a)
            or {
                "status": "failed",
                "reason": "checks-failed",
                "checks": [{"bucket": "fail"}],
            }
        ),
    )
    monkeypatch.setattr(w, "failing_logs", lambda *a: "Missing CI service credentials")

    def agent(self, prompt):
        prompts.append(prompt)
        state = "ready" if len(prompts) == 1 else second_status
        (self.handoff / "preparation.json").write_text(json.dumps({"status": state, "summary": "reviewed"}))
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    assert p.prepare_one(task, pr(), {"feedback": []}, "review", False)["status"] == "blocked"
    assert len(prompts) == 2 and len(polls) == 1
    assert len(pushes) == (2 if second_status == "ready" else 1)
    assert "Missing CI service credentials" in prompts[1]


def test_budget_exhaustion_stops_before_another_agent(tmp_path, monkeypatch):
    task = job(tmp_path)
    task.budget = 1
    monkeypatch.setattr(p, "guard", lambda *a: None)
    monkeypatch.setattr(w, "budget_left", lambda *a: 0)
    monkeypatch.setattr(w.Job, "run_agent", lambda *a: pytest.fail("budget spent"))
    assert "budget" in p.prepare_one(task, pr(), {"feedback": []}, "review", False)["reason"]


def test_same_repository_cannot_run_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(w, "require_gh_login", lambda: None)
    lock = tmp_path / ".workshop" / "logs" / "acme-analytics-prepare.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        p.fcntl.flock(handle, p.fcntl.LOCK_EX | p.fcntl.LOCK_NB)
        with pytest.raises(w.ToolError, match="already running"):
            p.cmd_prepare(args())


def test_prepare_skill_is_not_an_issue_skill(fake):
    assert "prepare-pr" not in w.job_skills()
    assert fake.run("run", "acme/analytics#12", "--skills", "prepare-pr") == 1
    assert not fake.calls("gh")


FIXES = {  # each fixture's repaired app.py
    "prepare-pr-feature-repair": "def total(values, fee=0):\n    return sum(values) + fee\n",
    "prepare-pr-dependencies-compatibility": (
        'from widget import render\n\n\ndef message():\n    return render("hello", upper=True)\n'
    ),
}


@pytest.mark.parametrize("name", FIXES)
@pytest.mark.parametrize("change", ["repair", "none", "weaken-tests", "uncommitted"])
def test_preparation_eval_checks_committed_behavior_and_preserves_tests(tmp_path, monkeypatch, name, change):
    """Each fixture's own setup and check run: the PR is committed for the agent, and only its commits are graded."""
    [case] = [case for case in workshop_eval.load_cases("prepare-pr") if case["name"] == name]
    monkeypatch.setattr(workshop_eval, "judge", lambda *a: True)

    def agent(task, prompt):
        assert "Prepare the existing PR #1" in prompt and "diff --git" in prompt  # the PR the setup committed
        if change != "none":
            (task.worktree / "app.py").write_text(FIXES[name])
        if change == "weaken-tests":
            (task.worktree / "tests" / "test_fee.py").write_text("")
        if change in ("repair", "weaken-tests"):
            workshop_eval.commit(task.worktree, "Repair")
        (task.handoff / "preparation.json").write_text('{"status":"ready","summary":"Verified"}')
        return 0

    monkeypatch.setattr(w, "invoke_agent", agent)
    result = workshop_eval.eval_run(case, tmp_path / "run", tmp_path / "cache", args(), 1)
    assert result["passed"] == (change == "repair"), result["why"]
