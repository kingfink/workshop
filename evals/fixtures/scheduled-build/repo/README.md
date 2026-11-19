# Example scheduled build

The `pipeline/sync` function runs a dbt build, retries failed nodes once, then publishes the resulting tables. A failed retry ends the call before publication. The entry point is in `app.py`.
