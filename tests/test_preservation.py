"""The dbt-duckdb fixture's check: deliberately wrong outputs must fail even when the candidate's dbt build passes."""

import importlib.util
import json
from copy import deepcopy

import pytest

import workshop_eval

spec = importlib.util.spec_from_file_location(
    "dbt", workshop_eval.EVALS_DIR / "fixtures/dbt-duckdb/fixture.py"
)
dbt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dbt)
preservation_error = dbt.preservation_error


@pytest.fixture
def baseline():
    return {
        "tables": {
            "customers": {"columns": {"id": "INTEGER", "country": "VARCHAR"}, "rows": [[1, "US"], [2, None]]},
            "orders": {"columns": {"id": "INTEGER", "amount": "INTEGER"}, "rows": [[1, 10]]},
        },
        "tests": [
            {
                "model": "model.fixture.customers",
                "depends_on": ["model.fixture.customers"],
                "column": "id",
                "definition": {"name": "unique"},
                "config": {"severity": "ERROR"},
            },
            {
                "model": "model.fixture.customers",
                "depends_on": ["model.fixture.customers"],
                "column": "country",
                "definition": {"name": "accepted_values", "kwargs": {"values": ["US", "GB"]}},
                "config": {"severity": "ERROR"},
            },
        ],
    }


def test_add_field_preserves_existing_data(baseline):
    after = deepcopy(baseline)
    after["tables"]["customers"] = {
        "columns": {"tier": "VARCHAR", "country": "VARCHAR", "id": "INTEGER"},
        "rows": [["free", None, 2], ["pro", "US", 1]],  # row and column order need not match
    }
    assert preservation_error(baseline, after, {"add_columns": {"customers": {"tier": "VARCHAR"}}}) == ""


def test_remove_field_permits_only_its_own_tests_to_be_removed(baseline):
    baseline["tests"][-1]["depends_on"].append(
        "model.fixture.countries"
    )  # a relationship can refer to two models
    after = deepcopy(baseline)
    after["tables"]["customers"] = {"columns": {"id": "INTEGER"}, "rows": [[1], [2]]}
    after["tests"].pop()
    case = {"remove_columns": {"customers": ["country"]}}
    assert preservation_error(baseline, after, case) == ""
    after["tests"].clear()
    assert "unrelated test" in preservation_error(baseline, after, case)


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda data: data["tables"]["customers"]["rows"][0].__setitem__(1, "GB"), "values changed"),
        (lambda data: data["tables"]["customers"]["rows"].__setitem__(1, [1, "US"]), "values changed"),
        (lambda data: data["tables"]["orders"]["rows"][0].__setitem__(1, 20), "orders: existing"),
        (lambda data: data["tables"]["customers"]["columns"].__setitem__("id", "VARCHAR"), "schema changed"),
        (
            lambda data: data["tables"]["customers"]["columns"].__setitem__("extra", "VARCHAR"),
            "schema changed",
        ),
        (lambda data: data["tables"].pop("orders"), "removed a model"),
        (lambda data: data["tests"].pop(), "unrelated test"),
        (lambda data: data["tests"][0]["config"].__setitem__("severity", "WARN"), "unrelated test"),
        (lambda data: data["tests"][0]["config"].__setitem__("where", "false"), "unrelated test"),
        (lambda data: data["tests"][1]["definition"]["kwargs"]["values"].append("XX"), "unrelated test"),
    ],
    ids=[
        "old value",
        "grain",
        "unaffected model",
        "type",
        "extra field",
        "missing model",
        "deleted test",
        "warning",
        "filtered test",
        "looser test",
    ],
)
def test_rejects_changes_outside_the_contract(baseline, change, reason):
    after = deepcopy(baseline)
    change(after)
    assert reason in preservation_error(baseline, after, {})


def test_equal_counts_and_distinct_rows_are_not_enough(baseline):
    baseline["tables"]["customers"]["rows"].append([1, "US"])
    after = deepcopy(baseline)
    after["tables"]["customers"]["rows"][-1] = [2, None]
    assert "values changed" in preservation_error(baseline, after, {})


def test_expected_added_field_is_required(baseline):
    assert "schema changed" in preservation_error(
        baseline, baseline, {"add_columns": {"customers": {"tier": "VARCHAR"}}}
    )


def test_snapshot_preserves_sql_tests_without_an_attached_column(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    test = {
        "resource_type": "test",
        "attached_node": None,
        "depends_on": {"nodes": ["model.fixture.customers"]},
        "config": {},
        "raw_code": "select id from customers where id is null",
    }
    manifest = target / "manifest.json"
    manifest.write_text(json.dumps({"nodes": {"test.fixture.ids": test}}))
    monkeypatch.setattr(
        dbt,
        "query",
        lambda cache, project, sql: (
            [["customers", "id", "INTEGER"]] if "information_schema" in sql else [[1]]
        ),
    )
    before = dbt.snapshot(None, tmp_path)
    test["raw_code"] = "select id from customers where false"
    manifest.write_text(json.dumps({"nodes": {"test.fixture.ids": test}}))
    after = dbt.snapshot(None, tmp_path)
    assert "unrelated test" in preservation_error(before, after, {})


def test_check_grades_only_changes_to_models_and_their_results(tmp_path, monkeypatch, baseline):
    """A failed build comes with its log, for the agent to fix. Query results must match row by row."""
    project, cache = tmp_path / "graded", tmp_path / "cache"
    (project / "models").mkdir(parents=True)
    cache.mkdir()
    (cache / "baseline.json").write_text(json.dumps(baseline))
    workshop_eval.git("init", "--quiet", project)

    def change(name):
        (project / name).write_text("select 1\n")
        workshop_eval.commit(project, name)
        return workshop_eval.git("-C", project, "rev-parse", "HEAD").stdout.strip()

    (tmp_path / "start.json").write_text(json.dumps({"main": change("models/start.sql")}))
    case = {"queries": [{"sql": "select tier", "rows": [[1, "pro"], [2, "free"]]}]}
    (tmp_path / "case.json").write_text(json.dumps(case))
    change("models/tiers.sql")
    monkeypatch.setattr(dbt, "build", lambda cache, project: (False, "Compilation Error"))
    assert dbt.check(tmp_path, cache) == ("dbt build failed", "Compilation Error")
    monkeypatch.setattr(dbt, "build", lambda cache, project: (True, ""))
    monkeypatch.setattr(dbt, "snapshot", lambda cache, project: baseline)
    monkeypatch.setattr(dbt, "query", lambda cache, project, sql: [[1, "free"], [2, "pro"]])  # same tiers
    assert dbt.check(tmp_path, cache)[0] == "`select tier` returned [[1, 'free'], [2, 'pro']]"
    monkeypatch.setattr(dbt, "query", lambda cache, project, sql: case["queries"][0]["rows"])
    assert dbt.check(tmp_path, cache) == ("", "")
    change("dbt_project.yml")
    assert dbt.check(tmp_path, cache)[0] == "changed files outside models/: dbt_project.yml"
