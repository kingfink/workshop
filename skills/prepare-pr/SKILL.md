---
name: prepare-pr
description: Prepare an existing pull request for review by checking its intended behavior, addressing feedback, resolving conflicts and verifying repairs. Used by workshop prepare.
---

# Prepare a pull request

Preserve each PR's intent, existing commits and unrelated work. Establish the intent from the supplied request, trusted feedback and diff. Ask if these do not establish what the PR should do.

## Assess the batch

- `blocked`: missing credentials, inaccessible services or failures unrelated to the PR. Do not start repairs until the blocker is cleared. Group PRs with the same cause; state the action needed and how to verify recovery. Passing lint or parsing does not establish that a failed build passed.
- `needs-info`: unclear intent, a product decision, dropping platform support or feedback that changes the PR's scope. Give the owner concrete options.
- `prepare`: review or repair within the PR's intent can proceed. Defer questions about changed code that require the PR checkout to the repair phase.

## Prepare one PR

- Check the diff against the request and trusted review feedback. Verify the promised behavior, not only whether CI passes. Report required evidence you cannot access with the allowed tools.
- Resolve conflicts and fix demonstrated problems within the PR's intent. Reproduce failures before repairing them, then run the same check afterward. Use the repository's documented commands and run required checks even when no edit is needed.
- Commit repairs on the current branch.

Report the repairs, checks and results, and any owner action. For repairs, include the check's failing result before the change and passing result afterward. Name checks that could not run and why. Use `ready` only when checks passed or the remaining validation can run in GitHub CI; otherwise use `blocked` or `needs-info`.

## Dependency changes

When the PR changes dependency declarations or lockfiles:

- Check compatibility against the repository's use of the changed dependencies; a version number alone is not evidence of a blocker.
- Preserve requested versions and base constraints. Regenerate lockfiles with the repository's package manager or documented resolver; never hand-edit generated entries. Ask if the constraints cannot be satisfied together or a requested version must change.
- A Dependabot-triggered workflow uses Dependabot secrets in place of Actions secrets. If the run actor and credential configuration explain a missing secret, ask the owner to configure the needed Dependabot secret and verify a Dependabot-triggered run. An empty variable alone does not identify the cause. Never commit just to trigger CI under another actor.

## Boundaries

- Never write to production data or change credentials, access controls or secrets.
- Never edit CI configuration, or skip, loosen or delete a test or check, to get to green.
- Never work around sandbox restrictions. Change only the selected PR's repository.
- Public summaries must use repository identifiers and check names, not log excerpts, secrets or data values.

If a boundary prevents completion, stop and report the blocker or decision needed.
