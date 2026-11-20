import argparse
import copy
import json
from pathlib import Path

import pytest

import workshop as w
import workshop_prepare as p
import workshop_prepare_eval as pe
from workshop_eval import load_cases

REPO = "acme/analytics"


def pr(number=12, **values):
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "labels": [],
        "user": {"login": "dependabot[bot]"},
        "head": {"ref": "dependabot/uv/widget-2", "sha": "a" * 40, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "sha": "b" * 40},
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
        pr(5, user={"login": "owner", "id": 2}),
        pr(1),
    ]
    monkeypatch.setattr(p, "pages", lambda _: candidates)
    assert [item["number"] for item in p.select_prs(REPO, None, False)] == [1, 3, 5]
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
        ],
    )
    result = p.feedback(REPO, 12)
    assert len(result) == 3
    assert all(item["body"] == "requested change" for item in result)


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE"])
def test_request_only_includes_trusted_authors(monkeypatch, association):
    item = pr(author_association=association, title="Add a feature", body="Keep existing behavior")
    monkeypatch.setattr(p, "feedback", lambda *a: [])
    monkeypatch.setattr(w, "pr_checks", lambda *a: (item["head"]["sha"], []))
    monkeypatch.setattr(w, "gh", lambda *a: argparse.Namespace(stdout="patch"))
    evidence = p.collect(REPO, item)
    expected = {"title": item["title"], "body": item["body"]} if association in w.TRUSTED_AUTHORS else {}
    assert evidence["request"] == expected
    assert evidence["diff"] == "patch"


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
        p.guard(REPO, original, False, original["head"]["sha"], p.fingerprint([]))


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
    branch = p.checkout(REPO, item, clone, tree)
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
    assert p.push(task, item, False, "")
    published = w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip()
    assert published == task.pushed
    assert (task.worktree / "unrelated.txt").read_text() == "preserve me\n"
    w.git("-C", task.clone, "merge-base", "--is-ancestor", item["head"]["sha"], task.pushed)
    assert not p.push(task, item, False, "")


def test_atomic_lease_refuses_a_concurrent_remote_commit(git_job, monkeypatch):
    task, item, seed, remote, commit = git_job
    w.git("-C", seed, "checkout", item["head"]["ref"])
    newer = commit(seed, "owners-work.txt", "do not overwrite\n")

    def racing_guard(*_):
        w.git("-C", seed, "push", "origin", item["head"]["ref"])

    monkeypatch.setattr(p, "guard", racing_guard)
    with pytest.raises(w.ToolError, match="stale info"):
        p.push(task, item, False, "")
    assert w.git("--git-dir", remote, "rev-parse", item["head"]["ref"]).stdout.strip() == newer


def test_push_refuses_rewritten_or_uncommitted_work(git_job, monkeypatch):
    task, item, _, _, _ = git_job
    monkeypatch.setattr(p, "guard", lambda *a: None)
    (task.worktree / "unrelated.txt").write_text("uncommitted")
    with pytest.raises(w.ToolError, match="uncommitted"):
        p.push(task, item, False, "")
    w.git("-C", task.worktree, "reset", "--hard", task.base)
    with pytest.raises(w.ToolError):
        p.push(task, item, False, "")


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
    monkeypatch.setattr(p, "guard", lambda *a: None)
    monkeypatch.setattr(p, "push", lambda *a: False)
    monkeypatch.setattr(w, "wait_for_checks", lambda *a: validation)

    def agent(self, prompt):
        (self.handoff / "preparation.json").write_text('{"status":"ready","summary":"Tests passed."}')
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    assert p.prepare_one(task, pr(), {"feedback": []}, "review", False)["status"] == expected


def test_blocked_agent_does_not_push(tmp_path, monkeypatch):
    task = job(tmp_path)
    monkeypatch.setattr(p, "guard", lambda *a: None)
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
    candidates = [pr(12), pr(13, user={"login": "owner"})]
    monkeypatch.setattr(w, "prepare_worktree", lambda *a: "base")
    monkeypatch.setattr(w, "copy_repo_skills", lambda *a: None)
    monkeypatch.setattr(w, "repo_settings", lambda *a: {})
    monkeypatch.setattr(w, "gh_json", lambda *a: {"login": "owner"})
    monkeypatch.setattr(
        p,
        "collect",
        lambda r, item: (
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
    monkeypatch.setattr(p, "guard", lambda *a: None)
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


def test_fix_loop_can_stop_on_an_external_blocker(tmp_path, monkeypatch):
    task = job(tmp_path)
    monkeypatch.setattr(p, "guard", lambda *a: None)
    pushes, prompts = [], []
    monkeypatch.setattr(p, "push", lambda *a: pushes.append("push"))
    monkeypatch.setattr(
        w,
        "wait_for_checks",
        lambda *a: {
            "status": "failed",
            "reason": "checks-failed",
            "checks": [{"bucket": "fail"}],
        },
    )
    monkeypatch.setattr(w, "failing_logs", lambda *a: "Missing CI service credentials")

    def agent(self, prompt):
        prompts.append(prompt)
        state = "ready" if len(prompts) == 1 else "blocked"
        (self.handoff / "preparation.json").write_text(json.dumps({"status": state, "summary": "reviewed"}))
        return 0

    monkeypatch.setattr(w.Job, "run_agent", agent)
    assert p.prepare_one(task, pr(), {"feedback": []}, "review", False)["status"] == "blocked"
    assert len(prompts) == 2 and len(pushes) == 1
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


@pytest.mark.parametrize("change", ["repair", "none", "weaken-tests", "uncommitted"])
def test_feature_eval_checks_committed_behavior_and_preserves_tests(tmp_path, monkeypatch, change):
    case = next(case for case in load_cases("prepare-pr") if case["name"] == "prepare-pr-feature-repair")
    monkeypatch.setattr(pe, "judge", lambda *a: True)

    def agent(task, prompt):
        if change != "none":
            (task.worktree / "app.py").write_text("def total(values, fee=0):\n    return sum(values) + fee\n")
        if change == "weaken-tests":
            (task.worktree / "tests" / "test_fee.py").write_text("")
        if change in ("repair", "weaken-tests"):
            pe.commit(task.worktree, "Repair")
        (task.handoff / "preparation.json").write_text('{"status":"ready","summary":"Verified"}')
        return 0

    monkeypatch.setattr(w, "invoke_agent", agent)
    result = pe.eval_preparation(case, tmp_path, args(), 1)
    assert result["passed"] == (change == "repair")
