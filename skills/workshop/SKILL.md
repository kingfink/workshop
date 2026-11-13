---
name: workshop
description: Rules for every workshop job, which turns one GitHub issue into one pull request in a target repository. Always used together with the job's own skill, such as add-field.
---

# Workshop job

You are working one GitHub issue. The working directory is a checkout of the target repository on the job's branch. The owner queued the issue, answers your questions and reviews your pull request.

You cannot write to GitHub; read it with the read-only `github` tools. When you exit, the workshop tool publishes what you left in the handoff directory named in the prompt:

- If you wrote `question.md`, the tool posts it on the issue and publishes nothing else.
- Otherwise it pushes your commits on the current branch, and opens a pull request from `pull-request.md` or updates the open one.

If you leave neither a question nor commits, the job has failed. Your final message is not read.

If the job uses several skills, follow all of them. Where two conflict, the stricter instruction applies. If that does not settle it, ask.

## Reading the request

Later comments from the owner override earlier ones. Comments that start with `**workshop:` were posted by the tool: run summaries, and questions from earlier runs.

This may be a rerun. Build on any work already on the branch.

## Asking

Ask when the answer is not in the issue, the repository's documentation or `AGENTS.md`. Also ask when the scope rules of any skill in use exclude the request: say which rule excludes it, and offer options such as a narrower request or handing it to a person.

Write one question with concrete options to `question.md`, then stop.

## Working

- Commit on the current branch only, in small commits with descriptive messages.
- If the repository's `README.md` or `AGENTS.md` says how to build and test locally, do so before you finish.
- Change only the target repository. If the request also needs a change in another repository, describe it in the pull request instead of making it.

Commands run in a sandbox. They can write only to the working directory, the handoff directory and temp, and reach only the network hosts the repository allows. Never work around the sandbox. If a local build, test or check cannot run in it, say which in the pull request and rely on the pull request's checks. If anything else cannot, ask.

## Documentation

Put each fact in one place, as close to the code as it can go: first names and tests, then the description fields the repository already uses, then `AGENTS.md` for conventions an agent needs. Do not create separate definition or decision files.

## Decisions

A one-off decision stays in the issue thread. If the owner's answer is a standing rule for future jobs, add it to the repository's `AGENTS.md` in the same pull request, and mention it in the pull request body.

## Pull request

Write `pull-request.md`: the title on the first line, then the body. The title states the change. The body gives a short summary, anything your skills ask you to report, and `Closes #<issue>`.

If you are told that checks failed, fix the cause and commit it on the same branch.

## Close-out comment

Write `comment.md` for the requester: how to check the change once the pull request is merged. Once checks pass, the tool posts it on the issue with a link to the pull request.

## Hard rules

- Never write to production data.
- Never edit CI configuration, or skip, loosen or delete a test or check, to get to green.

If you cannot finish within these rules, ask.
