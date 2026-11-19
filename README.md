# workshop

> This project is experimental.

`workshop` is a command-line tool that has a coding agent turn a GitHub issue into a pull request. It stops when the pull request's checks pass or a person is needed, and never merges.

It also inspects configured jobs and reports whether their failures need action. Inspections publish nothing and make no repairs.

It is for routine changes with obvious, verifiable solutions, such as adding a field or bringing docs up to date, so people can spend their time on harder work. Inspired by [Gumloop's Software Factory](https://www.gumloop.com/engineering/software-factory).

## Vocabulary

| Term | Meaning |
|---|---|
| Skill | Instructions for one kind of agent work, defined by a `SKILL.md` file, such as `add-fields` or `assess-failure`. |
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
./workshop.py run '<owner>/<repo>#<issue>' --skills add-fields
```

```bash
./workshop.py score <owner>/<repo>
```

```bash
./workshop.py inspect <owner>/<repo>
```

```bash
./workshop.py eval --skill add-fields
```

`init` creates the labels below in a target repo. It is safe to rerun.

### run

`run` works one issue, which must be open and labelled `workshop:queued` or `workshop:waiting`. It clones the issue's repo under `~/.workshop/` and works there, so it can be run from any directory. Run it again to continue after answering a question.

The agent runs at `medium` effort. Each invocation's prompt includes its deadline, set by `--agent-timeout`.

| Option | Default | |
|---|---|---|
| `--skills` | required | Comma-separated. The `workshop` skill is always included. A target repo's own `.workshop/skills/<name>/SKILL.md`, on its default branch, replaces workshop's skill of that name, except `workshop`. |
| `--max-fix-attempts` | `2` | How many times the agent is sent back to fix failing checks. |
| `--agent-timeout` | `900` | Seconds per agent run. |
| `--checks-timeout` | `300` | Seconds to wait for checks, each round. |

### score

`score` reads the run records in the tool's own comments on a target repo's issues, and shows for each skill how its jobs ended: merged as-is (at the commit the tool last pushed), merged after changes, closed unmerged, open or escalated, with the questions, fix attempts, denied tool calls and cost. It also counts each job's latest recorded CI validation: passed, failed, unverified or unknown for older records. These describe the recorded commit when checked, not the PR's current checks. `--since 2026-10-01` counts only runs from that date on.

### inspect

`inspect` checks configured jobs and reports whether action is needed. It reads `.workshop/inspect.toml` and repository context from the target repo's default branch. [Modal setup and collection details](docs/modal-inspection.md) describe the supported source.

```bash
./workshop.py inspect <owner>/<repo>
```

`--since 24h` is optional: the default is the last 24 hours. Positive durations in minutes, hours or days are accepted, up to `7d`. Modal keeps call history longer than logs, so failures older than its log retention are reported as coverage gaps; see [Limits and coverage](docs/modal-inspection.md#limits-and-coverage).

Reported states determine whether failures occurred. When they did, `assess-failure` uses the logs and repository context to decide whether work remains. A later successful run alone does not establish recovery.

| Result | Exit code | Meaning |
|---|---|---|
| OK | `0` | No failures or coverage gaps were reported. No agent was needed. |
| OK after review | `0` | The agent found recovery with no outstanding action. |
| Action required | `2` | The report gives the next step and how to verify it. |
| Unable to assess | `1` | Evidence is missing or inconclusive, or the inspection could not finish. |

Missing or incomplete evidence prevents an OK result. This checks observed runs; missed schedules and data freshness are outside its scope.

The terminal shows the outcome, a short summary and the next step. The full report, evidence and agent log are saved under `~/.workshop/logs/<repo>-inspect/<run>/`; `--verbose` also prints the full report. Inspections make no changes to services or GitHub and do not appear in `score`.

Use `--config PATH` to try a local configuration; repository context still comes from the default branch. `--agent-timeout` sets the assessment limit in seconds and defaults to `300`.

`--issue` also proposes an issue when the result is Action required. The tool gives the agent the repo's open issues, and those closed during the window, written by people with push access. The agent matches one or proposes a new one. The terminal shows the matched issue's link, or a link to GitHub's new-issue form filled in, for you to review and submit. Before offering a new issue, the tool checks that every number, every word containing `@`, `/`, `_` or `.`, and every word in backticks appears in the repository or the run metadata, or is joined from such words by `-`, `_`, `/`, `=` or `,`, as dbt's test names are, and that no eight words in a row come from a log. Words that fail the check are listed above the link, for you to look at before you submit. Inspect still writes nothing to GitHub.

### eval

`eval` runs the agent on each case in `evals/cases/` against a small test project in `evals/fixtures/`, without GitHub, and grades what it did. A case that should end in a pull request passes if the agent's committed change builds, preserves the baseline outside the case's permitted column changes, and returns the expected query results. The grader compares complete model outputs, including duplicate counts, column names and types, and existing test definitions. New fields are checked by key. A case that should end in a question passes if a small model judges that the question does what the case says. It publishes nothing, and prints a table of results.

Inspection cases provide made-up job histories, logs and repository context. They check the assessment's outcome, that repository files stay unchanged, and whether a small model judges its explanation and next step to meet the case's criterion. Run them with `./workshop.py eval --skill assess-failure`; they make no calls to external services for evidence.

| Option | Default | |
|---|---|---|
| `--skill` | all | Only the cases for this skill. |
| `--case` | all | Only the named cases, by case file name without `.toml`, such as `add-fields-plan-tier`. Repeat it or separate names with commas. |
| `--changed` | off | Only the cases whose inputs differ from where this branch left the default branch, `origin/HEAD`, counting uncommitted and new files. A case's inputs are its case file, its fixture and the `SKILL.md` of each skill it uses, the `workshop` skill included except in inspection cases. A change to `workshop.py`, `workshop_eval.py` or `workshop_inspect.py`, which hold the prompts and grading, selects every case. |
| `--runs` | `1` | Runs of each case. |
| `--budget` | `5` | API-equivalent dollars for all the runs. Each run may spend an equal share of what is left, and stops at the end of the turn that goes over it, so the total can go slightly over. |
| `--max-fix-attempts` | `1` | How many times the agent is sent back to fix a failing build. |
| `--agent-timeout` | `900` | Seconds per agent run. |

`--skill`, `--case` and `--changed` combine: a case runs if it meets each one given.

Each run's record goes in `~/.workshop/logs/evals.jsonl`, and its files in `~/.workshop/evals/runs/`.

## Development

Install the development tools with `uv sync --locked`. Their versions are recorded in `uv.lock`. Ruff formats Python code, sorts imports and checks for common mistakes.

```bash
uv run ruff check --fix .
uv run ruff format .
uv run python -m pytest -q
```

CI runs the tests and checks lint and formatting without changing files. The eval fixture repositories have their own configuration and are excluded from Ruff.

With Claude Code installed and logged in, run `WORKSHOP_TEST_CLAUDE=1 uv run pytest tests/test_inspect_permissions.py` to check actual `Read`, `Grep` and `Glob` permissions against harmless files inside and outside the allowed paths. This opt-in test uses the hosted agent and is skipped by default and when `claude` is unavailable; it is separate from the skill evals.

## Security

- The agent holds no GitHub credentials. It reads GitHub through GitHub's MCP server, read-only and in lockdown mode, which in a public repo hides what people without push access wrote. The tool does every write: it posts the agent's question, or pushes the job branch and opens the pull request.
- That server runs with your `gh` token, so the agent can read the issues and pull requests of every repository the token can, not only the target repo's.
- The tool posts what the agent wrote as you: its question, the pull request's title and body, and its closing comment. Anything the agent can read could end up there.
- In `run`, the agent's commands run without prompts, always in Claude Code's sandbox: they can write only to the job's worktree, read nothing else in your home directory, and reach no network host. In `inspect`, the agent has file-reading tools and can write only its assessment in the handoff directory; it has no shell. With `--issue`, the tool fetches the target repo's issues for it and keeps only those written by people with push access.
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

### Generic job skill

One `other` skill for issues that no job skill covers. It adds one rule to the `workshop` skill: before editing, state what done is and the check that will prove it, such as an existing test, a build or a query, and ask if there is none. The pull request names the kind of work, so `score` shows which kinds recur and how they end. Start with jobs that end in a pull request; questions need the answer outcome.

### Answer outcome

Let a job end in a comment on the issue with no pull request, beside a question and a pull request. The agent leaves the answer in the handoff directory and the tool posts it. Reports, surveys and questions need this.

### Triage and follow-up issues

Review issues, tag them with the applicable job skills, and flag missing information, prerequisites or work to split into separate issues. Let `run` use those labels to select skills, combining compatible skills for one change. The owner still chooses which issues to queue.

At the end of a job, let the agent hand off follow-up work for workshop to create as linked issues in the same or another relevant repository. Include the reason, a way to verify completion, suggested skills and any dependency on the original change. Reuse existing issues on reruns, and send follow-ups through triage before the owner queues them. Follow-ups that depend on the original change wait until that prerequisite is satisfied.

Test skill selection with a routing eval: show a model only each skill's name and description, and check that each eval case's issue goes to its skill and that issues no skill covers go to `other`. When a description changes, compare the results with the base branch's.

### Tool skills

A skill that says how to use one tool, such as a BI tool's command line, used together with a job skill. The target repo supplies read-only credentials and the network host in its `.claude/settings.json`. Start with lookups: check which dashboards and semantic-layer definitions use a field before `remove-fields` removes it, and query an added field through the semantic layer. Writes to a tool that is not changed through the repository need a way to publish other than the pull request.

### Skills from past jobs

A job on this repository that writes a new skill. The issue cites past `other` jobs of one kind, and the agent writes the skill and its eval cases from those jobs and from what the owner changed before merging. The owner runs the new evals.

### Update dependencies

One `update-dependencies` skill for requested versions, bot-generated PRs and dependency surveys. Start with existing Dependabot PRs: review compatibility, resolve conflicts and dependency-related failures, run the repository's required checks, and report ready to merge, blocked, or needing an owner decision. This requires support for jobs on existing PRs.

Add a batch mode that works through open Dependabot PRs and returns one consolidated report. PR preparation is a separate task from `inspect`; merging remains a separate owner action. Dependency surveys need the answer outcome.

### Jobs from telemetry

`inspect --issue` proposes an issue, or matches an existing one, for the owner to review and create. Next, publish it without review once the publish check and its evals have earned trust, and name a repair skill in a form the queue can use, so a schedule can run queued issues. Inconclusive assessments identify the missing evidence; a known collection problem that needs action can become its own issue.

The owner queues repair issues for the existing issue-to-PR flow. Inspect again after deployment to verify recovery. Extend collection to sources such as dbt or PostHog as needed, matching their findings against the same issues.

### More job skills

Try each kind of work with the generic job skill first. Write a skill only for the kinds that fail or keep being merged after changes.

- Fix a failing test or pipeline: reproduce the failure, repair the cause and verify the fix.
- Add data tests: turn stated expectations into checks for uniqueness, nulls, relationships or accepted values.
- Rename a field or model: update references, tests and documentation while preserving behavior.
- Add a metric: implement a supplied business definition in the existing models or semantic layer.
- Retire an unused model or source: check for consumers, remove it and clean up references.
- Optimize a slow query: preserve results and demonstrate improvement under comparable conditions.
- Fix a wrong answer from the semantic layer's AI: add the missing synonym or context and show that the question is now answered correctly.

### Verify content and analysis

Check a report, dashboard, query or specified documents against named sources and explicit definitions. Identify conflicting statements, recalculate selected figures and flag unsupported claims. Mark each check as verified, contradicted or unable to verify, with evidence. Start with report numbers; these jobs need the answer outcome.

### Evals

- Baseline: an `eval` option that runs a skill's cases with only the `workshop` skill, to show what the job skill adds.
- Skill file checks in the tests: the name matches the directory, the frontmatter parses and every skill has an eval case.
- Holdout fixture: a second test project that is never used while tuning skills, to show that a skill works beyond `dbt-duckdb`.

### Publishing issues without review

Before `inspect` posts issues itself, block any issue that fails the publish check, and add a second check: a separate model that sees only the proposed issue and the rules for public text, and blocks the issue if it finds a data value. Keep data out of what the inspect agent reads, too, such as row values and failing-row samples in logs, so it has none to repeat.
