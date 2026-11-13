# AGENTS.md

A dbt project on DuckDB. The raw data is already loaded into the `raw` schema of `fixture.duckdb`, and `models/sources.yml` describes it.

## Build and test

`.venv/bin/dbt build` builds every model and runs every test. Run it before you finish.

## Conventions

- A staging model selects from one source table, renames columns and casts types. A mart selects from staging models.
- Every model and every column has a description in the YAML file next to it.
- Every key has `unique` and `not_null` tests.
