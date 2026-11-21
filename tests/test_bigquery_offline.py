"""BigQuery behavior that needs no project (DEC-40); the comparative suites cover the rest."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlglot

from cosmos_semantic.connectors import DataSourceConfig, connect
from cosmos_semantic.errors import ModelError
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.reference import load_cases

from .conftest import CASES

SCHEMA_MAP = {"public": "cosmos_demo"}


def _plan(graph, request: QueryRequest, principal: Principal | None = None) -> str:
    planner = Planner(graph, "bigquery", SCHEMA_MAP)
    request = request.model_copy(update={"as_of": dt.date(2026, 11, 20)})
    return planner.plan(request, principal or Principal()).sql


def test_the_source_type_uses_the_bigquery_dialect():
    assert DataSourceConfig(type="bigquery").dialect == "bigquery"


def test_a_source_without_project_is_rejected():
    pytest.importorskip("google.cloud.bigquery")
    with pytest.raises(ModelError, match="options.project"):
        connect(DataSourceConfig(type="bigquery"))


def test_unknown_options_are_rejected():
    pytest.importorskip("google.cloud.bigquery")
    config = DataSourceConfig(type="bigquery", options={"project": "p", "dataset": "x"})
    with pytest.raises(ModelError, match="unknown bigquery options: dataset"):
        connect(config)


def test_weeks_start_on_monday(graph):
    sql = _plan(
        graph, QueryRequest(metrics=["order_count"], dimensions=["orders.ordered_at__week"])
    )
    assert "TIMESTAMP_TRUNC(orders.ordered_at, ISOWEEK)" in sql
    assert ", WEEK)" not in sql


def test_reference_sql_weeks_start_on_monday():
    case = next(c for c in load_cases(CASES.with_name("queries_advanced.yml")) if c.id == "A06")
    sql = case.sql_for("bigquery")
    assert "ISOWEEK" in sql
    assert ", WEEK)" not in sql


def test_tables_live_in_the_mapped_dataset(graph):
    request = QueryRequest(metrics=["revenue"], dimensions=["customers.region"])
    sql = _plan(graph, request, Principal(user={"region": "Midwest"}))
    assert "cosmos_demo.orders" in sql
    assert "public." not in sql


def test_every_reference_query_plans_and_parses_as_bigquery(graph):
    """Every case the engine answers prints SQL that BigQuery's parser in SQLGlot reads back;
    the emulator and a live project check that it runs and returns the same numbers."""
    cases = load_cases(CASES) + load_cases(CASES.with_name("queries_advanced.yml"))
    planned = 0
    for case in cases:
        if case.request is None or case.expect_error:
            continue
        sql = _plan(graph, case.request, Principal(user=case.user, channel=case.channel))
        sqlglot.parse_one(sql, read="bigquery")
        if case.sql_for("bigquery") is not None:
            sqlglot.parse_one(case.sql_for("bigquery"), read="bigquery")
        planned += 1
    assert planned >= 50


def _adapted(sql: str) -> str:
    """Hand-written PostgreSQL, as in the reference queries."""
    from cosmos_semantic import dialects

    return dialects.adapt(sqlglot.parse_one(sql, read="postgres"), "bigquery").sql("bigquery")


def test_date_arithmetic_on_a_time_uses_datetime_add():
    sql = _adapted("SELECT o.ordered_at + INTERVAL '3' HOUR FROM o")
    assert "DATETIME_ADD(CAST(o.ordered_at AS DATETIME), INTERVAL" in sql
    assert "DATE_ADD(" not in sql.replace("DATETIME_ADD(", "")


def test_date_arithmetic_on_a_date_keeps_date_add():
    sql = _adapted("SELECT CURRENT_DATE - INTERVAL '45' DAY")
    assert sql == "SELECT DATE_ADD(CURRENT_DATE, INTERVAL -45 DAY)"
    sql = _adapted("SELECT CAST('2026-11-20' AS DATE) + INTERVAL '1' MONTH")
    assert "DATE_ADD(CAST('2026-11-20' AS DATE), INTERVAL" in sql


def test_a_date_read_as_a_time_uses_the_datetime_function():
    sql = _adapted("SELECT CAST(CAST('2026-11-20' AS DATE) AS TIMESTAMP) + INTERVAL '12' HOUR")
    assert "DATETIME(CAST('2026-11-20' AS DATE))" in sql
    assert "AS DATETIME)" not in sql
