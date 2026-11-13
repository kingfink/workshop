---
name: add-field
description: Add a field that already exists in a loaded source table through to the models and semantic layer. Use in a workshop job when the issue asks to add or expose a field.
---

# Add a field

## Scope

In scope:

- A field that exists in a loaded source table and needs to reach the models.
- Carrying that field into the semantic layer or BI tool definitions, if the repository has them.

Out of scope:

- A source or entity (object) that is not loaded yet.
- New business logic, or a new metric definition.
- Any change to existing rows or columns.
- Any change to a model's grain.

## Before starting

- Check that the field does not already exist under another name.
- If the source values look like they need cleaning before use, such as mixed formats, placeholder values, or strings or integers that should be booleans, ask how to handle them.

## Locating

If the issue names a source table or column, inspect it first. Search for alternatives only if it cannot be found. Otherwise find candidate columns in the loaded source tables and any ingestion schema files in the repository. If more than one fits, ask.

Group schema, uniqueness and join checks into as few warehouse calls as possible. Once the source, any join keys and the destination are established, make the change.

## Changing

In this order:

1. Capture the baseline needed to verify row counts, grain and pre-existing columns before editing.
2. Update the first model that reads the source table.
3. Update each downstream model between it and where the requester wants to use the field.
4. Update the semantic layer or BI definitions, if the repository has them.
5. Update the column description.

## Verification

- Add not-null or accepted-values tests where the field's meaning implies them, the way the repository tests similar columns.
- Build all changed models and the descendants necessary to validate the change. Repeat a successful check only after a relevant change or a new failure.
- The row count, the grain and every pre-existing column of each touched model must stay the same. In the pull request, give the row counts before and after for each touched model, and say how you confirmed the rest.

## Close-out comment

Give the requester a query they can run, and describe what it returns, such as the column's type and how many rows have a value. Do not copy values from the data into the comment.
