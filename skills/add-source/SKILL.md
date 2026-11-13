---
name: add-source
description: Add a source or resource through extraction code or managed connector configuration. Use in a workshop job when the issue asks to ingest new data in an existing ingestion project.
---

# Add a source

## Scope

In scope:

- A new source or resource, implemented in code or configured through an existing managed ingestion service.
- The configuration and pipeline wiring stored in the target repository.

Out of scope:

- Introducing an ingestion framework, managed ingestion service, destination or orchestration system.
- Downstream models, metrics and semantic layer definitions.
- Changing what existing resources load.

## Before starting

- Reuse an existing connector or resource if it provides the requested data.
- Identify the requested records and fields, grain, key, history and update behavior from the issue and source documentation.
- When writing API extraction code, check pagination and whether the requested fields require fetching each record's details.

## Changing

- Preserve source values and identifiers. Leave business transformations to downstream models.
- Load the requested history on the first run. Reruns must pick up new records without duplicates and handle updates as specified.
- In custom extraction code, report failed requests; do not treat them as empty results or skip their records.

## Verification

- For custom extraction, use fixtures or a development destination to check completeness, pagination and detail responses. Test reruns and recovery after failed requests for missing or duplicated records, and updates if supported.
- For managed connectors, validate the repository configuration and confirm that documented field coverage, history, keys and update behavior meet the request.
- If a development destination is configured and accessible, verify loaded fields, key uniqueness and any requested joins there. Otherwise, state which data checks remain for after deployment.

## Pull request

State the grain, key, first-run history and rerun behavior. List required configuration and any release, deployment or backfill steps still needed.

## Close-out comment

Give a command or query to check the loaded fields and key uniqueness. If the issue names a join, include a query to check it.
