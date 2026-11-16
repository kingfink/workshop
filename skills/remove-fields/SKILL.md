---
name: remove-fields
description: Remove one or more fields from the models and semantic layer. Use in a workshop job when the issue asks to remove, drop or stop exposing fields.
---

# Remove fields

## Scope

In scope:

- Removing one or more requested fields, with their tests and descriptions, from the models the issue names for each field and every model downstream of them. If the issue names no model for a field, remove it from every model.
- Removing those fields from the semantic layer or BI tool definitions, if the repository has them.

Out of scope:

- Removing a model, source or entity (object).
- Any change to what is loaded, or to the source definitions.
- Any change to columns outside the requested removals, to rows, or to a model's grain.

## Locating

Find every reference to each requested field, including each name it is given downstream.

## Before changing

Check every requested field before editing. If any field needs clarification, ask before changing any of them.

- If anything uses a requested field other than selecting it through, such as another column's expression, a join, a filter or a grouping, ask: removing it would change other columns or rows.
- If a metric or measure is built on a requested field, ask whether to remove that too.

## Changing

In this order:

1. The semantic layer or BI definitions, if the repository has them.
2. Each model in scope, from the furthest downstream back, with the removed fields' tests and descriptions.

## Verification

- A search of the repository for each requested field and its downstream names finds it only upstream of the models the issue names for that field, or, if it names none, only in the source definitions.
- The row count, the grain and every column outside the requested removals in each touched model must stay the same. In the pull request, give the row counts before and after for each touched model, and say how you confirmed the rest.

## Pull request

List each place in the repository that showed any of the removed fields to people, such as semantic layer, BI or exposure definitions. Say that uses outside the repository, such as dashboards, exports or other repositories, were not checked.

## Close-out comment

Give the requester a query that lists the columns of each touched model, so they can confirm the requested removals.
