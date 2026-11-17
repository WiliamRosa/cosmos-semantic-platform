"""Roadmap gates: the engine returns the same result as hand-written SQL."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import ModelError
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import QueryRequest
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
