# workshop

> This project is experimental.

`workshop` is a command-line tool that has a coding agent turn a GitHub issue into a pull request. It stops when the pull request's checks pass or a person is needed, and never merges.

It is for routine changes with obvious, verifiable solutions, such as adding a field or bringing docs up to date, so people can spend their time on harder work. Inspired by [Gumloop's Software Factory](https://www.gumloop.com/engineering/software-factory).

## Vocabulary

| Term | Meaning |
|---|---|
| Skill | One kind of work the tool handles end to end, defined by a `SKILL.md` file, such as `add-field`. |
| Owner | The person who queues issues, answers questions and reviews pull requests. |
| Target repo | The repository being changed. |

## Requirements

- Python 3.11 or later.
- `git` and the GitHub CLI, authenticated (`gh auth login`, then `gh auth setup-git`).
- Claude Code, logged in, where its [sandbox](https://code.claude.com/docs/en/sandboxing) can run. That page lists the platforms it supports and what each needs.
- [GitHub's MCP server](https://github.com/github/github-mcp-server) as `github-mcp-server` on the `PATH`.
- For `eval` and development, [uv](https://docs.astral.sh/uv/).

## Commands

```bash
./workshop.py init <owner>/<repo>
```

```bash
./workshop.py run '<owner>/<repo>#<issue>' --skills add-field
```

```bash
./workshop.py score <owner>/<repo>
```

```bash
./workshop.py eval --skill add-field
```

`init` creates the labels below in a target repo. It is safe to rerun.

### run

`run` works one issue, which must be open and labelled `workshop:queued` or `workshop:waiting`. It clones the issue's repo under `~/.workshop/` and works there, so it can be run from any directory. Run it again to continue after answering a question.

| Option | Default | |
|---|---|---|
| `--skills` | required | Comma-separated. The `workshop` skill is always included. A target repo's own `.workshop/skills/<name>/SKILL.md`, on its default branch, replaces workshop's skill of that name, except `workshop`. |
| `--max-fix-attempts` | `2` | How many times the agent is sent back to fix failing checks. |
| `--agent-timeout` | `900` | Seconds per agent run. |
| `--checks-timeout` | `300` | Seconds to wait for checks, each round. |

### score

`score` reads the run records in the tool's own comments on a target repo's issues, and shows for each skill how its jobs ended: merged as-is (at the commit the tool last pushed), merged after changes, closed unmerged, open or escalated, with the questions, fix attempts, denied tool calls and cost. It also counts each job's latest recorded CI validation: passed, failed, unverified or unknown for older records. These describe the recorded commit when checked, not the PR's current checks. `--since 2026-10-01` counts only runs from that date on.

### eval

`eval` runs the agent on each case in `evals/cases/` against a small test project in `evals/fixtures/`, without GitHub, and grades what it did. A case that should end in a pull request passes if the agent's committed change builds, preserves the baseline outside the case's permitted column changes, and returns the expected query results. The grader compares complete model outputs, including duplicate counts, column names and types, and existing test definitions. New fields are checked by key. A case that should end in a question passes if a small model judges that the question does what the case says. It publishes nothing, and prints a table of results.

| Option | Default | |
|---|---|---|
| `--skill` | all | Only the cases for this skill. |
| `--runs` | `1` | Runs of each case. |
| `--budget` | `2` | API-equivalent dollars for all the runs. Each run may spend an equal share of what is left, and stops at the end of the turn that goes over it, so the total can go slightly over. |
| `--max-fix-attempts` | `1` | How many times the agent is sent back to fix a failing build. |
| `--agent-timeout` | `900` | Seconds per agent run. |

Each run's record goes in `~/.workshop/logs/evals.jsonl`, and its files in `~/.workshop/evals/runs/`.

## Development

Install the development tools with `uv sync --locked`. Their versions are recorded in `uv.lock`. Ruff formats Python code, sorts imports and checks for common mistakes.

```bash
uv run ruff check --fix .
uv run ruff format .
uv run python -m pytest -q
```

CI runs the tests and checks lint and formatting without changing files. The eval fixture repositories have their own configuration and are excluded from Ruff.

## Security

- The agent holds no GitHub credentials. It reads GitHub through GitHub's MCP server, read-only and in lockdown mode, which in a public repo hides what people without push access wrote. The tool does every write: it posts the agent's question, or pushes the job branch and opens the pull request.
- That server runs with your `gh` token, so the agent can read the issues and pull requests of every repository the token can, not only the target repo's.
- The tool posts what the agent wrote as you: its question, the pull request's title and body, and its closing comment. Anything the agent can read could end up there.
- The agent's commands run without prompts, always in Claude Code's sandbox: they can write only to the job's worktree, read nothing else in your home directory, and reach no network host.
- A target repo lets its tools reach more with `sandbox.network.allowedDomains`, `sandbox.filesystem.allowRead` and `sandbox.filesystem.allowWrite` in its `.claude/settings.json` on the default branch. Its `permissions.deny` and `permissions.ask` rules apply too.
- The agent gets only a few variables from the shell `run` starts in, such as `PATH` and `HOME`, so none of that shell's secrets. Variables the target repo's tools need go in the `env` key of the same file, and reach only the agent's commands.
- Those tools use whatever credentials they can read, such as a warehouse login. Give those only the access jobs need, such as no writes to production.
- The pull request's checks run the agent's code outside the sandbox, with the secrets and network access the target repo's CI gives a branch of the same repo. The tool sends the logs of failed runs back to the agent.

## Run records

The agent's log streams to `~/.workshop/logs/` and stays there: it can hold anything a command printed.

Each run also records its outcome, the skills it used with a hash of each, the pull request and the commit the tool last pushed, its time in the agent and waiting for checks, and the agent's model, turns, tokens, cost and tool calls, including the ones it was denied. The record goes in `~/.workshop/logs/runs.jsonl` and, hidden, in the run's comment on the issue, which shows the agent time and cost.

Validation is separate from the outcome: an open PR can be unverified. The `validation` record names its source (`github-checks`), status, reason, commit, observation time and reported checks with their states and links. `passed` means the reported checks finished without a failure and at least one succeeded; it does not establish that every intended check ran. Failed or cancelled checks give `failed`. No checks, only skipped or neutral checks, an unfinished wait without a known failure, or no observation give `unverified`. If the PR moves to another commit while waiting, the tool escalates instead of attributing its checks to the pushed commit. Local tests described by the agent are not tool-observed validation.

Costs are API-equivalent: what the tokens would cost at API prices, whether or not Claude Code is logged in with a subscription.

## Labels

An issue has one of these at a time.

| Label | Meaning |
|---|---|
| `workshop:queued` | Ready for a run. Applied by the owner. |
| `workshop:working` | A run is in progress. |
| `workshop:waiting` | Waiting for the owner. The latest workshop comment says why. |

A run that is killed before it can clean up, such as by a power cut, leaves the issue labelled `workshop:working`. Replace that label with `workshop:queued` to run it again.

## Roadmap ideas

### Update dependencies

One `update-dependencies` skill for requested versions, bot-generated PRs and dependency surveys. Start with requested versions; the other scenarios need support for existing PRs and successful runs without changes.

### Jobs from telemetry

Investigate failures from sources such as dbt, Modal or PostHog and propose deduplicated GitHub issues with evidence and a way to verify the fix. The owner queues them for the existing issue-to-PR flow. Start with one source and one reproducible failure class; confirm the result after deployment.

### More job skills

- Fix a failing test or pipeline: reproduce the failure, repair the cause and verify the fix.
- Add data tests: turn stated expectations into checks for uniqueness, nulls, relationships or accepted values.
- Rename a field or model: update references, tests and documentation while preserving behavior.
- Add a metric: implement a supplied business definition in the existing models or semantic layer.
- Retire an unused model or source: check for consumers, remove it and clean up references.
- Optimize a slow query: preserve results and demonstrate improvement under comparable conditions.
