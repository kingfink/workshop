# AGENTS.md

How to work on this repository. [README.md](README.md) says what the tool is and why.

## Keep it small

Build the smallest thing that meets the need. Anything else is easy to add later.

## Design

- Keep the state between runs on GitHub: issue labels, issue comments and the pull request. Files under `~/.workshop/` are only clones, worktrees, logs, eval runs, and the handoff and the copy of the target repo's skills from the run in progress.
- Give the agent no credentials it does not need. The tool does every write to GitHub.
- Keep every command safe to rerun. After a crash or an answered question, a rerun picks up where the last run stopped.
- Reach the agent only through `invoke_agent`.

## This repository is public

Put no project IDs, dataset names, example records, or client or personal names anywhere in it. Use placeholders such as `acme/analytics`.

## Writing skills

A skill must stand on its own: the agent working a job can read the skills, but not the rest of this repository.

- Include only what changes what the agent does. Leave out what the prompt, another skill or common practice covers, such as how to push.
- Keep the `workshop` skill general: task detail goes in the job's skill, and detail about one target repo in that repo, such as its `AGENTS.md` or its own skills.
- Tie each condition to something the agent can check: "if `AGENTS.md` says how", not "where practical".
- Word each rule to cover only what it means to: "Never edit CI configuration to get to green", not "Never edit CI configuration".
- Describe how things are, not the roadmap. Leave out "yet" and "for now".

## Evals

When you change a skill or the agent's prompt, run that skill's evals with `./workshop.py eval --skill <name>` and paste the table into the pull request.

Write each case in `evals/cases/` from a rule in the skill, not from its wording, so a reworded skill still has to pass. When a real job goes wrong, add a case that would have caught it. The test projects in `evals/fixtures/` hold made-up data only.
