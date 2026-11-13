"""Deliberately wrong outputs must fail even when the candidate's dbt build passes."""

from copy import deepcopy
import json

import pytest

import workshop_eval
from workshop_eval import preservation_error


@pytest.fixture
def baseline():
    return {
        "tables": {
            "customers": {"columns": {"id": "INTEGER", "country": "VARCHAR"},
                          "rows": [[1, "US"], [2, None]]},
            "orders": {"columns": {"id": "INTEGER", "amount": "INTEGER"}, "rows": [[1, 10]]},
        },
        "tests": [
            {"model": "model.fixture.customers", "depends_on": ["model.fixture.customers"], "column": "id",
             "definition": {"name": "unique"}, "config": {"severity": "ERROR"}},
            {"model": "model.fixture.customers", "depends_on": ["model.fixture.customers"], "column": "country",
             "definition": {"name": "accepted_values", "kwargs": {"values": ["US", "GB"]}},
             "config": {"severity": "ERROR"}},
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
    baseline["tests"][-1]["depends_on"].append("model.fixture.countries")  # a relationship can refer to two models
    after = deepcopy(baseline)
    after["tables"]["customers"] = {"columns": {"id": "INTEGER"}, "rows": [[1], [2]]}
    after["tests"].pop()
    case = {"remove_columns": {"customers": ["country"]}}
    assert preservation_error(baseline, after, case) == ""
    after["tests"].clear()
    assert "unrelated test" in preservation_error(baseline, after, case)


@pytest.mark.parametrize("change, reason", [
    (lambda data: data["tables"]["customers"]["rows"][0].__setitem__(1, "GB"), "values changed"),
    (lambda data: data["tables"]["customers"]["rows"].__setitem__(1, [1, "US"]), "values changed"),
    (lambda data: data["tables"]["orders"]["rows"][0].__setitem__(1, 20), "orders: existing"),
    (lambda data: data["tables"]["customers"]["columns"].__setitem__("id", "VARCHAR"), "schema changed"),
    (lambda data: data["tables"]["customers"]["columns"].__setitem__("extra", "VARCHAR"), "schema changed"),
    (lambda data: data["tables"].pop("orders"), "removed a model"),
    (lambda data: data["tests"].pop(), "unrelated test"),
    (lambda data: data["tests"][0]["config"].__setitem__("severity", "WARN"), "unrelated test"),
    (lambda data: data["tests"][0]["config"].__setitem__("where", "false"), "unrelated test"),
    (lambda data: data["tests"][1]["definition"]["kwargs"]["values"].append("XX"), "unrelated test"),
], ids=["old value", "grain", "unaffected model", "type", "extra field", "missing model", "deleted test",
        "warning", "filtered test", "looser test"])
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
        baseline, baseline, {"add_columns": {"customers": {"tier": "VARCHAR"}}})


def test_snapshot_preserves_sql_tests_without_an_attached_column(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    test = {"resource_type": "test", "attached_node": None, "depends_on": {"nodes": ["model.fixture.customers"]},
            "config": {}, "raw_code": "select id from customers where id is null"}
    manifest = target / "manifest.json"
    manifest.write_text(json.dumps({"nodes": {"test.fixture.ids": test}}))
    monkeypatch.setattr(workshop_eval, "query", lambda fixture, project, sql:
                        [["customers", "id", "INTEGER"]] if "information_schema" in sql else [[1]])
    before = workshop_eval.snapshot(None, tmp_path)
    test["raw_code"] = "select id from customers where false"
    manifest.write_text(json.dumps({"nodes": {"test.fixture.ids": test}}))
    after = workshop_eval.snapshot(None, tmp_path)
    assert "unrelated test" in preservation_error(before, after, {})
