---
name: other
description: Make a change to a repository that no more specific job skill covers, where a test, build, query or command can show the change is done. Use in a workshop job when the issue asks for such a change.
---

# Other

## Scope

In scope:

- One change to the target repository whose result a test, a build, a query or a command can show.

Out of scope:

- A request for an answer rather than a change to the repository, such as a figure, a report or a survey of the code.

## Pull request

Add a line to the body that names the kind of work:

```
Kind: <kind>
```

The kind is two to four lowercase words, a verb and what it acts on, that would fit other changes of the same type: `rename field`, not `rename amount`. If you have the `github` tools, search the repository's pull requests for `Kind:` and reuse a kind that fits.
