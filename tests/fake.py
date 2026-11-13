"""A fake `gh`, `git` and agent, sharing the JSON state file named by FAKE_STATE.

Usage: fake.py gh|git|agent ARGS...
"""

import json
import os
import re
import sys
import time

STATE = os.environ["FAKE_STATE"]
BRANCH = "workshop/issue-12"  # the tests always work acme/analytics#12


def load():
    with open(STATE) as f:
        return json.load(f)


def save(state):
    with open(STATE, "w") as f:
        json.dump(state, f)


def flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def gh(state, args):
    issue = state["issue"]
    command = args[:2]
    for name, working in state.get("failing", {}).items():  # {"issue edit": 1}: all but the first such call fail
        made = sum(call[0] == "gh" and name in (call[1], " ".join(call[1:3])) for call in state["calls"])
        if name in (args[0], " ".join(command)) and made > working:
            return f"gh: {name} failed (HTTP 500)"
    if command == ["auth", "status"]:
        return 0 if state.get("gh_auth_ok", True) else 1
    elif args[0] == "api":  # api -H <accept> repos/<repo>/contents/<path>?ref=<commit>: a file as GitHub has it
        path, commit = re.fullmatch(r"repos/acme/analytics/contents/(.+)\?ref=(.+)", args[-1]).groups()
        content = state.get("default_branch_files", {}).get(path) if commit == state["default_commit"] else None
        if content is None:
            return "gh: Not Found (HTTP 404)"
        sys.stdout.write(content)
    elif command == ["repo", "clone"]:
        os.makedirs(os.path.join(args[3], ".git"))
    elif command == ["label", "create"]:
        if args[2] not in state["labels"]:
            state["labels"].append(args[2])
    elif command == ["issue", "view"]:
        everything = {
            "state": issue["state"],
            "labels": [{"name": name} for name in issue["labels"]],
            "comments": issue["comments"],
        }
        print(json.dumps({field: everything[field] for field in flag(args, "--json").split(",")}))
    elif command == ["issue", "edit"]:
        for arg in args:
            if arg.startswith("--add-label="):
                issue["labels"].append(arg.split("=", 1)[1])
            elif arg.startswith("--remove-label="):
                issue["labels"].remove(arg.split("=", 1)[1])
    elif command == ["issue", "comment"]:
        body = sys.stdin.read() if flag(args, "--body-file") == "-" else flag(args, "--body")
        issue["comments"].append({"id": f"IC_{len(issue['comments'])}", "body": body, "viewerDidAuthor": True})
    elif command == ["issue", "list"]:  # the one issue, if it has the label
        print(json.dumps([{"number": 12, "comments": issue["comments"]}] if flag(args, "--label") in issue["labels"]
                         else []))
    elif command == ["pr", "create"]:
        number = 100 + len(state["prs"])
        state["prs"].append({"number": number, "url": f"https://github.com/acme/analytics/pull/{number}",
                             "head": flag(args, "--head"), "title": flag(args, "--title")})
    elif command == ["pr", "view"]:  # the head is the last commit pushed unless a test says otherwise
        pr = next(pr for pr in state["prs"] if pr["url"] == args[2] or str(pr["number"]) == args[2])
        head = pr.get("head_oid", f"{state['pushed_commits']:040x}")
        rounds = state["checks"]
        checks = rounds[min(max(state["pushes"] - 1, 0), len(rounds) - 1)] if rounds else {}
        conclusions = {"pass": "SUCCESS", "fail": "FAILURE", "cancel": "CANCELLED", "skipping": "SKIPPED"}
        rollup = [{"name": name, "status": "IN_PROGRESS" if bucket == "pending" else "COMPLETED",
                   "conclusion": conclusions.get(bucket),
                   "detailsUrl": "https://github.com/acme/analytics/actions/runs/555/job/1"}
                  for name, bucket in checks.items()]
        info = {"state": pr.get("state", "OPEN"), "headRefOid": head, "statusCheckRollup": rollup}
        print(json.dumps({key: info[key] for key in flag(args, "--json").split(",")}))
    elif command == ["pr", "edit"]:
        next(pr for pr in state["prs"] if pr["number"] == int(args[2]))["title"] = flag(args, "--title")
    elif command == ["pr", "list"]:
        print(json.dumps([{"number": pr["number"], "url": pr["url"]} for pr in state["prs"]
                          if pr["head"] == flag(args, "--head")]))
    elif command == ["run", "view"]:
        print(state["run_logs"].get(args[2], ""))
    else:
        return f"fake gh: unsupported: {args}"
    return 0


def git(state, args):
    if args[0] == "-C":
        args = args[2:]
    if args[:1] == ["ls-remote"]:  # ls-remote --symref origin HEAD: the default branch as GitHub has it
        print(f"ref: refs/heads/main\tHEAD\n{state['default_commit']}\tHEAD")
    elif args[:1] == ["rev-parse"]:
        if args[-1].startswith("refs/heads/"):  # the commit the tool pushes
            print(f"{state['commits']:040x}")
            return 0
        return 0 if args[-1].removeprefix("origin/") in state["remote_branches"] else 1
    elif args[:2] == ["worktree", "add"]:
        if "-b" in args:
            branch, path = args[args.index("-b") + 1], args[args.index("-b") + 2]
        else:
            path, branch = args[2], args[3]
            if branch not in state["local_branches"] + state["remote_branches"]:
                return f"fatal: invalid reference: {branch}"
        os.makedirs(path)
        open(os.path.join(path, ".git"), "w").close()
        state["local_branches"].append(branch)
    elif args[:1] == ["rev-list"]:  # rev-list --count <default commit>..refs/heads/<branch>
        print(state["commits"])
    elif args[:1] == ["push"]:  # a push with no new commits starts no checks
        if state["commits"] > state["pushed_commits"]:
            state["pushes"] += 1
            state["pushed_commits"] = state["commits"]
        state["remote_branches"].append(args[-1].removeprefix("refs/heads/"))
    return 0


def agent(state):
    """Record the prompt, then commit or leave handoff files as state["agent_mode"] says."""
    prompt = sys.stdin.read()
    with open(os.environ["CLAUDE_ENV_FILE"]) as f:
        env_file = f.read()
    state["agent_runs"].append({"prompt": prompt, "cwd": os.getcwd(), "env": sorted(os.environ), "env_file": env_file})
    handoff = re.search(r"^Handoff directory: (.+)$", prompt, re.M)[1]

    def leave(name, text):
        with open(os.path.join(handoff, name), "w") as f:
            f.write(text)

    mode = state["agent_mode"]
    print(f"fake agent: {mode}", flush=True)
    for event in ({"type": "system", "subtype": "init", "model": "fake-model", "claude_code_version": "0.0.1"},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
                  {"type": "result", "num_turns": 3, "total_cost_usd": 0.25, "usage": {"output_tokens": 100},
                   "permission_denials": [{"tool_name": "WebFetch"}]}):
        print(json.dumps(event), flush=True)
    if mode == "pr":
        state["commits"] += 1
        if "has failing checks" not in prompt:
            leave("pull-request.md", "Add new_field\n\nCloses #12\n")
            leave("comment.md", "Run `select new_field from orders`.\n")
    elif mode == "question":
        leave("question.md", state.get("question", "Which one?\n"))
    elif mode == "link":  # point the handoff at a file the agent could not read
        os.symlink(STATE, os.path.join(handoff, "question.md"))
    elif mode == "silent":
        for i in range(60):
            print(f"thinking line {i}")
    elif mode == "sleep":
        time.sleep(60)
    save(state)
    return 0


def main(tool, args):
    state = load()
    if tool == "agent":
        return agent(state)
    state["calls"].append([tool, *args])
    status = {"gh": gh, "git": git}[tool](state, args)
    save(state)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
