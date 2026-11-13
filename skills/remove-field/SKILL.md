---
name: remove-field
description: Remove a field from the models and semantic layer. Use in a workshop job when the issue asks to remove, drop or stop exposing a field.
---

# Remove a field

## Scope

In scope:

- Removing a field, with its tests and descriptions, from the models the issue names and every model downstream of them. If the issue names no model, from every model.
- Removing it from the semantic layer or BI tool definitions, if the repository has them.

Out of scope:

- Removing a model, source or entity (object).
- Any change to what is loaded, or to the source definitions.
- Any change to other columns, to rows, or to a model's grain.

## Locating

Find every reference to the field, including each name it is given downstream.

## Before changing

- If anything uses the field other than selecting it through, such as another column's expression, a join, a filter or a grouping, ask: removing it would change other columns or rows.
- If a metric or measure is built on the field, ask whether to remove that too.

## Changing

In this order:

1. The semantic layer or BI definitions, if the repository has them.
2. Each model in scope, from the furthest downstream back, with the field's tests and description.

## Verification

- A search of the repository for the field and each of its downstream names finds it only upstream of the models the issue names, or, if it names none, only in the source definitions.
- The row count, the grain and every other column of each touched model must stay the same. In the pull request, give the row counts before and after for each touched model, and say how you confirmed the rest.

## Pull request

List each place in the repository that showed the field to people, such as semantic layer, BI or exposure definitions. Say that uses outside the repository, such as dashboards, exports or other repositories, were not checked.

## Close-out comment

Give the requester a query that lists the columns of each touched model, so they can see the field is gone.
