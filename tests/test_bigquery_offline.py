"""BigQuery behavior that needs no project (DEC-40); the comparative suites cover the rest."""

from __future__ import annotations

import datetime as dt
import re

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


def test_date_minus_date_is_a_day_count():
    sql = _adapted("SELECT CAST(o.ordered_at AS DATE) - DATE '2000-01-01' FROM o")
    assert sql.startswith("SELECT DATE_DIFF(CAST(o.ordered_at AS DATE), CAST('2000-01-01' AS DATE)")
    assert sql.endswith(", DAY) FROM o")


def test_a_doubled_quote_in_a_model_expression_is_read_as_standard_sql():
    from cosmos_semantic.expressions import parse_template

    template = parse_template("{segment} = 'O''Brien'", "bigquery")
    from sqlglot import exp

    literal = template.tree.find(exp.Literal)
    assert literal is not None and literal.this == "O'Brien"


def test_reference_ctes_never_share_a_name_with_a_column():
    """BigQuery reads `m` as the CTE's whole row when a CTE and a column are both `m` (A08)."""
    case = next(c for c in load_cases(CASES.with_name("queries_advanced.yml")) if c.id == "A08")
    sql = case.sql_for("bigquery")
    assert "WITH m_cte AS" in sql and "FROM m_cte" in sql


# -- TIMESTAMP time columns (DEC-57) ---------------------------------------------------------

TIME_COLUMNS = ("ordered_at", "paid_at", "created_at", "last_order_at")
OTHER_DIALECTS = ("duckdb", "postgres", "tsql", "mysql", "oracle", "databricks", "snowflake")


@pytest.fixture(scope="module")
def timestamp_graph():
    """The example model with every time dimension on a BigQuery TIMESTAMP column."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.loader import load_project

    from .conftest import EXAMPLE

    project = load_project(EXAMPLE)
    for model in project.semantic_models:
        for dim in model.dimensions:
            if dim.type == "time":
                dim.column_type = "timestamp"
    return compile_project(project)


def _answered_cases():
    cases = load_cases(CASES) + load_cases(CASES.with_name("queries_advanced.yml"))
    return [c for c in cases if c.request is not None and not c.expect_error]


def _sql(graph, case, dialect: str) -> str:
    request = case.request.model_copy(update={"as_of": dt.date(2026, 11, 20)})
    planner = Planner(graph, dialect, SCHEMA_MAP)
    return planner.plan(request, Principal(user=case.user, channel=case.channel)).sql


def test_a_timestamp_column_is_read_as_its_utc_civil_time(timestamp_graph):
    """Every reference query plans, parses as BigQuery, and never touches a TIMESTAMP column
    without `DATETIME(column, 'UTC')` first: a bare TIMESTAMP compared with a DATE or DATETIME
    has no matching signature on BigQuery."""
    planned = 0
    for case in _answered_cases():
        sql = _sql(timestamp_graph, case, "bigquery")
        sqlglot.parse_one(sql, read="bigquery")
        for column in TIME_COLUMNS:
            for match in re.finditer(rf"(.{{0,9}})\b\w+\.{column}\b(.{{0,8}})", sql):
                assert match.group(1).endswith("DATETIME("), (case.id, match.group(0))
                assert match.group(2).startswith(", 'UTC')"), (case.id, match.group(0))
        planned += 1
    assert planned >= 50


def test_timestamp_truncation_filters_and_windows(timestamp_graph):
    weeks = _plan(
        timestamp_graph,
        QueryRequest(
            metrics=["order_count"],
            dimensions=["orders.ordered_at__week"],
            filters=["{orders.ordered_at} >= DATE '2026-10-01'"],
        ),
    )
    assert "TIMESTAMP_TRUNC(DATETIME(orders.ordered_at, 'UTC'), ISOWEEK)" in weeks
    assert "DATETIME(orders.ordered_at, 'UTC') >= CAST('2026-10-01' AS DATE)" in weeks

    case = next(c for c in _answered_cases() if c.id == "A48")  # rolling window up to noon
    window = _sql(timestamp_graph, case, "bigquery")
    # Date arithmetic on the civil time needs no further cast.
    assert "DATETIME_ADD(DATETIME(orders.ordered_at, 'UTC'), INTERVAL 7 DAY)" in window
    assert "CAST(DATETIME(" not in window


def test_a_rule_input_bound_to_a_timestamp_column_is_read_in_utc(timestamp_graph):
    case = next(c for c in _answered_cases() if c.id == "R16")  # {rule.active_customer}
    sql = _sql(timestamp_graph, case, "bigquery")
    assert "DATETIME(customers.last_order_at, 'UTC')" in sql
    assert re.search(r"\(\s*DATETIME\(customers\.last_order_at, 'UTC'\)\s*\) >= DATE_ADD", sql)


def test_the_time_range_of_a_timestamp_column(timestamp_graph):
    sql = Planner(timestamp_graph, "bigquery", SCHEMA_MAP).plan_time_range("orders.ordered_at").sql
    assert "TIMESTAMP_TRUNC(DATETIME(orders.ordered_at, 'UTC'), DAY)" in sql


@pytest.mark.parametrize("dialect", OTHER_DIALECTS)
def test_column_type_changes_no_sql_on_other_dialects(graph, timestamp_graph, dialect):
    for case in _answered_cases():
        assert _sql(timestamp_graph, case, dialect) == _sql(graph, case, dialect), case.id


def test_without_column_type_the_bigquery_sql_and_model_hash_stay(graph):
    """A DATETIME or DATE column (no `column_type`) keeps the SQL it had before DEC-57, and
    the model dump (so the model hash in the evidence) has no new key."""
    for case in _answered_cases():
        assert "'UTC')" not in _sql(graph, case, "bigquery"), case.id
    dumped = graph.project.model_dump(mode="json")
    for model in dumped["semantic_models"]:
        for dim in model["dimensions"]:
            assert "column_type" not in dim


def test_column_type_is_only_for_time_dimensions():
    from pydantic import ValidationError

    from cosmos_semantic.spec import Dimension

    assert Dimension(name="t", type="time", column_type="timestamp").column_type == "timestamp"
    with pytest.raises(ValidationError, match="time dimensions only"):
        Dimension(name="status", type="categorical", column_type="timestamp")
    with pytest.raises(ValidationError):
        Dimension(name="t", type="time", column_type="timestamptz")
