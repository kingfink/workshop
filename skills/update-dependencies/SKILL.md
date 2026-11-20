---
name: update-dependencies
description: Prepare an existing dependency-update pull request by reviewing compatibility, resolving conflicts and checking the result. Used by workshop prepare.
---

# Prepare dependency updates

Preserve each PR's requested versions, existing commits and unrelated work.

## Assess the batch

- `blocked`: missing credentials, inaccessible services or failures unrelated to the update. Group PRs with the same cause; state the action needed and how to verify recovery. Passing lint or parsing does not establish that a failed build passed.
- `needs-info`: changing requested versions, dropping platform support, deciding product behavior or addressing feedback beyond dependency preparation. Give the owner concrete options.
- `prepare`: compatibility review or dependency-related repair can proceed. Inspect uncertain compatibility in the PR checkout; neither a version number nor passing checks alone settles it.

## Prepare one PR

- Check compatibility against the repository's use of the changed dependencies. Use supplied evidence and allowed tools; report any required evidence you cannot access.
- Resolve conflicts while preserving requested updates and base constraints. Regenerate lockfiles with the repository's package manager or documented resolver; never hand-edit generated entries. Ask if the constraints cannot be satisfied together.
- Fix only dependency-related failures. Reproduce application failures before repairing them, then run the same check afterward. Use the repository's documented commands and run required checks even when no edit is needed.
- Commit repairs on the current branch. Never switch branches or discard existing commits. Do not make a commit when no change is needed.

Report the repairs, checks and results, and any owner action. Name checks that could not run and why. Use `ready` only when checks passed or the remaining validation can run in GitHub CI; otherwise use `blocked` or `needs-info`. The tool verifies GitHub checks and publishes commits only for `ready` results.

## Boundaries

- Never merge, close, approve or change a PR's title, description or draft status. The tool handles pushes and comments.
- Never write to production data or change credentials, access controls or secrets.
- Never edit CI configuration, or skip, loosen or delete a test or check, to get to green.
- Never work around sandbox restrictions. Change only the selected PR's repository.
- Public summaries must use repository identifiers and check names, not log excerpts, secrets or data values.

If a boundary prevents completion, stop and report the blocker or decision needed.
