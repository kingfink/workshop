# Modal inspection

[`workshop inspect`](../README.md#inspect) checks jobs defined as Modal functions.

## Setup

Install and authenticate the [Modal CLI](https://modal.com/docs/reference/cli). The reader is tested with Modal 1.6.4 and requires `modal function calls --since --until --json` and `modal function logs`.

Add `.workshop/inspect.toml` to the target repo's default branch:

```toml
[modal]
profile = "acme"
environment = "main"
functions = ["analytics/sync"]
```

Each entry in `functions` is an `app/function` name. The named profile must belong to the intended Modal workspace. Credentials stay in Modal's local configuration or the calling shell, never in the repository.

To try a configuration before merging it, run `workshop.py inspect acme/analytics --config /path/to/inspect.toml`. Repository context still comes from the default branch, which may differ from the deployed revision.

## Collection

The collector reads inputs enqueued within the inspection window for each configured function. States are observed at collection time; the window filters enqueue times, not completion times. Calls and logs use explicit time bounds instead of Modal's default tail of recent entries.

Failures, timeouts, termination, initialization failures, internal failures and memory evictions trigger log collection and assessment. The collector prioritizes failed calls across all jobs, then the next successful call after each failure, deduplicating call IDs. Other successful calls do not require log capture and are not coverage gaps. Empty call logs trigger one function-level fallback, with the coverage gap retained. Successful calls do not cancel earlier failures.

`evidence.json` keeps counts for the whole window, failed and selected recovery calls, log paths, and a `history_path` pointing to the complete call history stored as JSON Lines. The agent can search that history without loading it all at once.

Modal 1.6.4 prints fetched application stdout and stderr to the CLI's stdout; both are saved as evidence. The CLI's own stderr is kept separate and reported as a coverage gap, with at most its last 1,000 bytes included. A CLI warning alone does not count as a nonempty application log.

The assessment agent searches the saved logs and reads relevant excerpts and repository files. It has no shell, network tools, GitHub tools or service credentials. Saved evidence and agent logs may contain sensitive source output. Repeating an inspection collects fresh evidence without changing Modal.

## Limits and coverage

- Each Modal command has a 120-second timeout.
- More than 10,000 inputs per function requires a smaller inspection window.
- Log collection stops at 20 captures per inspection and 2 MB per file. Selected calls skipped because of that limit and incomplete files are reported as coverage gaps; partial files are preserved.

No inputs, unfinished inputs, unknown states, failed reads or truncated logs prevent an OK result. Known work can still produce Action required alongside coverage gaps.

This checks reported inputs for the configured functions. It does not check missed schedules, data freshness, failures hidden by application code or functions from previous deployments with different IDs.
