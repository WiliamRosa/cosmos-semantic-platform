"""Roadmap gates: the engine returns the same result as hand-written SQL."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import ModelError
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import Planner, QueryRequest
from cosmos_semantic.reference import ReferenceCase, load_cases, run_case
from cosmos_semantic.spec import Project

from .conftest import CASES, EXAMPLE

ALL = load_cases(CASES)
ADVANCED = load_cases(CASES.with_name("queries_advanced.yml"))
SQL_CASES = [c for c in ALL + ADVANCED if c.question is None]


def _check(case, graph, connector):
    result = run_case(case, graph, connector)
    if result.status == "skip":
        pytest.skip(result.detail)
    assert result.status == "pass", f"{case.id}: {result.detail}"


@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_duckdb(case, graph, duckdb_connector):
    _check(case, graph, duckdb_connector)


@pytest.mark.postgres
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_postgres(case, graph, postgres_connector):
    _check(case, graph, postgres_connector)


@pytest.mark.sqlserver
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_sqlserver(case, graph, sqlserver_connector):
    _check(case, graph, sqlserver_connector)


@pytest.mark.databricks
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_databricks(case, graph, databricks_connector):
    _check(case, graph, databricks_connector)


@pytest.mark.snowflake
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_snowflake(case, graph, snowflake_connector):
    _check(case, graph, snowflake_connector)


@pytest.mark.mysql
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_mysql(case, graph, mysql_connector):
    _check(case, graph, mysql_connector)


@pytest.mark.mariadb
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_mariadb(case, graph, mariadb_connector):
    _check(case, graph, mariadb_connector)


@pytest.mark.oracle
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_oracle(case, graph, oracle_connector):
    _check(case, graph, oracle_connector)


@pytest.mark.bigquery
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_bigquery(case, graph, bigquery_connector):
    _check(case, graph, bigquery_connector)


def test_year_to_date_distinct_count_is_refused(duckdb_connector):
    """A year-to-date over a `count_distinct` measure cannot be in a valid model (validation
    refuses it, so it is not in the example and has no case in the YAML files); a model loaded
    despite the error still gets no number from the planner (DEC-21)."""
    data = load_project(EXAMPLE).model_dump(mode="json")
    data["metrics"].append(
        {
            "name": "buyers_year_to_date",
            "type": "cumulative",
            "measure": "buyers",
            "grain_to_date": "year",
        }
    )
    with pytest.raises(ModelError, match="year-to-date over a `count_distinct` measure"):
        compile_project(Project.model_validate(data))
    loaded = compile_project(Project.model_validate(data), strict=False)
    case = ReferenceCase(
        id="DEC21-COUNT-DISTINCT",
        category="tempo",
        phase=3,
        title="Distinct buyers year to date is refused (a buyer would count once per day)",
        request=QueryRequest(
            metrics=["buyers_year_to_date"], dimensions=["orders.ordered_at__month"]
        ),
        expect_error="QueryError",
    )
    result = run_case(case, loaded, duckdb_connector)
    assert result.status == "pass", result.detail
    assert "cannot be accumulated" in result.detail


def test_there_are_twenty_cases():
    assert len(ALL) == 20
    assert len({c.id for c in ALL}) == 20


def test_offset_cases_cross_a_year_change_and_an_empty_month(graph, duckdb_connector):
    """A35 and A36 prove what they claim on any day the suite runs (the data follows the
    clock): A35 has a January that reads the December before, and A36 has a month without
    orders whose next month gets NULL (DEC-18)."""
    by_id = {c.id: c for c in ADVANCED}
    month = "orders__ordered_at__month"

    def rows(case_id):
        compiled = Planner(graph, "duckdb").plan(by_id[case_id].request)
        return duckdb_connector.execute(compiled.sql).to_pylist()

    def index(value):
        return value.year * 12 + value.month

    a35 = {index(r[month]): r for r in rows("A35")}
    januaries = [k for k in a35 if k % 12 == 1 and k - 1 in a35]
    assert januaries
    for k in januaries:
        assert a35[k]["revenue_prev_month"] == pytest.approx(a35[k - 1]["revenue"])

    completed = {index(r[month]): r for r in rows("A36") if r["orders__status"] == "completed"}
    months = sorted(completed)
    gaps = [k for k, before in zip(months[1:], months, strict=False) if k - before > 1]
    assert gaps, "the filter must leave a month without orders"
    for k in gaps:
        assert completed[k]["revenue_prev_month"] is None
        assert completed[k]["revenue_mom_growth"] is None
