---
name: add-field
description: Add one or more fields that already exist in loaded source tables through to the models and semantic layer. Use in a workshop job when the issue asks to add or expose fields.
---

# Add fields

## Scope

In scope:

- One or more requested fields that exist in loaded source tables and need to reach the models.
- Carrying those fields into the semantic layer or BI tool definitions, if the repository has them.

Out of scope:

- A source or entity (object) that is not loaded yet.
- New business logic, or a new metric definition.
- Any change to existing rows or columns.
- Any change to a model's grain.

## Before starting

- Check that each requested field does not already exist under another name.
- If any field's source values look like they need cleaning before use, such as mixed formats, placeholder values, or strings or integers that should be booleans, ask how to handle them.

## Locating

For each requested field, if the issue names a source table or column, inspect it first. Search for alternatives only if it cannot be found. Otherwise find candidate columns in the loaded source tables and any ingestion schema files in the repository. If more than one fits that field, ask.

Group schema, uniqueness and join checks into as few warehouse calls as possible. Establish the source, any join keys and the destination for every requested field before editing. If any field needs clarification, ask before changing any of them.

## Changing

In this order:

1. Capture the baseline needed to verify row counts, grain and pre-existing columns before editing.
2. For each requested field, update the first model that reads its source table.
3. Update each downstream model between those models and where the requester wants to use each field.
4. Update the semantic layer or BI definitions, if the repository has them.
5. Update each field's column description.

## Verification

- Add not-null or accepted-values tests where each field's meaning implies them, the way the repository tests similar columns.
- Build all changed models and the descendants necessary to validate the change. Repeat a successful check only after a relevant change or a new failure.
- The row count, the grain and every pre-existing column of each touched model must stay the same. In the pull request, give the row counts before and after for each touched model, and say how you confirmed the rest.

## Close-out comment

Give the requester one or more queries they can run to check every added field, and describe what they return, such as each column's type and how many rows have a value. Do not copy values from the data into the comment.
