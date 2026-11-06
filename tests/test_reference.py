"""Roadmap gates: the engine returns the same result as hand-written SQL."""

from __future__ import annotations

import pytest

from cosmos_semantic.reference import load_cases, run_case

from .conftest import CASES

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


def test_there_are_twenty_cases():
    assert len(ALL) == 20
    assert len({c.id for c in ALL}) == 20
