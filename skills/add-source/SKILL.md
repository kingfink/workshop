---
name: add-source
description: Add a source or resource to an existing ingestion project. Use in a workshop job when the issue asks to ingest data that is not loaded.
---

# Add a source

## Scope

In scope:

- A new source, or a resource in an existing source.
- The configuration and pipeline wiring needed to use it in the target repository.

Out of scope:

- Introducing an ingestion framework, destination or orchestration system.
- Downstream models, metrics and semantic layer definitions.
- Changing what existing resources load.

## Before starting

- Reuse an existing connector or resource if it provides the requested data.
- Identify the requested records and fields, grain, key, history and update behavior from the issue and source documentation.
- For an API, check pagination and whether the requested fields require fetching each record's details.

## Changing

- Preserve source values and identifiers. Leave business transformations to downstream models.
- Load the requested history on the first run. Reruns must pick up new records without duplicates and handle updates as specified.
- Report failed requests; do not treat them as empty results or skip their records.

## Verification

- With fixtures or a development destination, verify that every requested record and field arrives, including later pages and detail responses when the API uses them.
- Repeat the load and retry after a failed request. Verify that records are neither lost nor duplicated, and test updates if supported.

## Pull request

State the grain, key, first-run history and rerun behavior. List required configuration and any release, deployment or backfill steps still needed.

## Close-out comment

Give a command or query to check the loaded fields and key uniqueness. If the issue names a join, include a query to check it.
