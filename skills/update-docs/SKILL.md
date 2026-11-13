---
name: update-docs
description: Bring a repository's documentation up to date with the pull requests and issues finished in a time window. Use in a workshop job when the issue asks to update the docs from recent work.
---

# Update docs

## Scope

In scope:

- Standing rules stated in a pull request, review or issue thread in the window, added to `AGENTS.md`. Includes answers to questions that are generalizable.
- Documentation about what those pull requests changed that no longer matches them, in Markdown files such as `README.md` and `AGENTS.md`.

Out of scope:

- Any file that is not Markdown.
- Rules nobody stated, even if the code follows them.
- Rewording documentation that is still accurate.

## Before starting

The issue gives the window, such as "since 2026-10-01". If it does not, look at the last 30 days.

## Gathering

With the `github` tools, find the pull requests merged in the window (`is:pr is:merged merged:>=<date>`) and the issues closed in it (`is:issue is:closed closed:>=<date>`). Read each one with its comments and reviews, and each pull request's diff.

## Verification

`git diff --name-only origin/HEAD` lists only Markdown files.

## Pull request

In the body, list each change with the pull request or issue it came from.

If nothing needs changing, ask the owner to close the issue instead, and list what you checked.
