---
name: review-artifact
description: Review an existing analytical dashboard, report or app for correctness, structure, use of authoritative definitions and interactive behavior. Use when asked to verify or audit analytical content, not to build or repair it.
---

# Review artifact

Review the named analytical content and return findings with evidence. Do not repair, redesign or publish it, change shared definitions or alter access settings.

## Establish the review

- Use the request, `AGENTS.md` and repository skills for business definitions and inspection methods. Do not require a semantic layer when the repository establishes authority another way.
- Identify the content type, audience and intended decisions. Distinguish exploration, recurring reporting and operational workflows. When purpose or meaning is unresolved, name the decision needed and continue independent checks.
- Identify the version inspected: published content, draft, code revision or export. Record its data sources, environment, refresh or snapshot time, query identity, viewer role, filters, parameters and time zone. Reconcile version differences before combining evidence.
- Map the pages, figures, claims, controls, navigation and data-changing actions in scope. Prioritize reported problems, primary metrics and important user paths for deeper checks.
- Check that requested metrics, populations and comparisons are present or explicitly identified as unavailable or out of scope.

## Trace definitions and data

- Trace selected figures and claims from their displayed components through queries, transformations and inputs. Include logic after the query: notebook code, table calculations and browser code.
- Identify shared definitions, local overrides, direct queries, imported snapshots, hard-coded values and user assumptions per component. Inspect the effective definition, including inherited or overridden logic; a model name or certification badge is insufficient evidence.
- Check local and duplicated logic against authoritative definitions and repository rules. Shared logic can be wrong; direct SQL and local calculations are not defects by themselves. Treat labeled snapshots and scenario inputs according to their stated purpose. Flag sample or fallback data presented as live results.
- Identify conflicting definitions without choosing one on the owner's behalf. Check that titles and narrative claims match the measures and evidence; a numerical match does not establish a claimed cause or explanation.
- Check whether inputs and dependencies are recorded well enough to reproduce the result. Identify missing ownership when a definition or repair needs a decision.

## Verify selected results and behavior

- Check grain, join cardinality, aggregation, denominators and totals. Account for nulls, missing periods, duplicate rows, row limits, pagination and sampling. Check rates, averages and distinct counts before aggregating them again.
- Compare matching populations, filters, date boundaries, time zones, units and data freshness. Check partial-period comparisons. Require evidence before attributing differences to source changes or rounding.
- For estimates, scenarios and recommendations, check whether plausible changes to material assumptions or population exclusions change the conclusion. Use documented ranges or justified alternatives.
- Independently recalculate selected results from the business definition or check them against controlled expectations. Repeating the artifact's calculation establishes reproducibility, not correctness.
- Follow query results into the display. Check component bindings, units, labels, scales and comparisons. Distinguish zero from missing, loading, stale and failed results.
- Test selected filters, inputs, drilldowns and navigation within the execution boundaries below. Record the starting state, action, expected result and observed result. Check affected components, defaults, reset behavior and preservation of filter context. Set a bounded wait for an observable result or error; record a timeout if neither appears. Verify completed results against the expected filters and calculation.
- For notebooks and apps, check execution order, input changes and cache invalidation; saved outputs may not reflect current code or inputs.
- Test representative viewer roles or tenants using available authorized test contexts. Check whose access supplies shared results. Do not infer viewer behavior from an administrator's view.
- Assess layout, navigation and interaction against the audience's task and documented accessibility requirements. Identify obstacles to finding, interpreting or inspecting results.
- Use source and metadata for structural checks and rendered content for display and interaction checks. A screenshot cannot establish lineage or interactive behavior.
- Use available validators for relevant checks and record their scope and known blind spots. When comparing versions, distinguish new failures from existing ones.

## Execution and evidence boundaries

- Use documented read-only tools and bounded queries within configured limits. Before executing notebook or app code, inspect its reachable actions for writes and external side effects. Do not run paths with unknown effects or changes to live data; test them in a configured isolated fixture or report them as unverified.
- Do not print row-level records. Keep private figures, query results, credentials and data-only identifiers out of public files, issues and pull requests. Use permitted references and describe discrepancies without copying private values.

## Report

Return the requested report with the artifact and version, scope and tested contexts. State coverage gaps: unknown context, unresolved expectations, missing evidence or access, unsupported checks, and anything sampled or excluded. Order findings by demonstrated impact.

Give each check one status:

- **Verified:** Evidence demonstrates the expectation for the recorded version and context.
- **Contradicted:** Evidence shows a definition, requirement or claim is not met.
- **Unable to verify:** The expectation is unresolved or required evidence or access is unavailable.

For each finding, give the component, expectation, observation, evidence reference, impact and next step. Use references that let a reviewer locate the definition, query, code or captured result. For visual findings, identify the element and explain the misleading interpretation or obstacle to the user's task, supported by displayed data or task requirements. Distinguish observed failures from suspected causes. Separate correctness, definition consistency, usability and maintainability; design preferences and optional improvements are recommendations, not failed checks.

Never turn sampled checks or successful query execution into an unqualified pass for the whole artifact.
