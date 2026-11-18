"""Filters on metric values (DEC-29): strict parsing, refusals, injection attempts and the
numbers they keep. The reference cases A39-A42 check the numbers against hand-written SQL."""

from __future__ import annotations

import pytest
import sqlglot
from typer.testing import CliRunner

from cosmos_semantic.cli import app
from cosmos_semantic.errors import QueryError
from cosmos_semantic.evidence import EvidenceBuilder
from cosmos_semantic.planner import (
    MAX_METRIC_FILTERS,
    MetricCondition,
    Planner,
    Principal,
    QueryRequest,
    parse_metric_filter,
)

from .conftest import EXAMPLE

DIALECTS = ["duckdb", "postgres", "tsql", "databricks", "snowflake", "mysql", "oracle"]
NORTHEAST = Principal(user={"region": "Northeast"})


def _rows(connector, graph, request, principal=NORTHEAST):
    compiled = Planner(graph, connector.dialect).plan(request, principal)
    return connector.execute(compiled.sql).to_pylist()


# -- parsing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{revenue} > 1000", [MetricCondition("revenue", ">", "1000")]),
        ("{revenue}>=1000.5", [MetricCondition("revenue", ">=", "1000.5")]),
        ("  {revenue}   <   -2.25  ", [MetricCondition("revenue", "<", "-2.25")]),
        ("{revenue} <= 0", [MetricCondition("revenue", "<=", "0")]),
        ("{revenue} = 3", [MetricCondition("revenue", "=", "3")]),
        ("{revenue} <> 3", [MetricCondition("revenue", "<>", "3")]),
        ("{revenue} is null", [MetricCondition("revenue", "IS NULL", None)]),
        ("{revenue} IS NOT NULL", [MetricCondition("revenue", "IS NOT NULL", None)]),
        (
            "{revenue} > 1 and {order_count} Is Not Null AND {order_count} < 9",
            [
                MetricCondition("revenue", ">", "1"),
                MetricCondition("order_count", "IS NOT NULL", None),
                MetricCondition("order_count", "<", "9"),
            ],
        ),
    ],
)
def test_accepted_forms(text, expected):
    assert parse_metric_filter(text) == expected


ATTACKS = [
    "",
    "   ",
    "{revenue}",
    "revenue > 1",
    "{revenue} > ",
    "{revenue} 1000",
    "{revenue} > 1; DROP TABLE public.orders",
    "{revenue} > 1 OR 1 = 1",
    "{revenue} > 1 OR {revenue} < 1",
    "{revenue} > 1 --",
    "{revenue} > 1 /* comment */",
    "{revenue} > (SELECT MAX(amount) FROM public.orders)",
    "({revenue} > 1)",
    "{revenue}) OR (1=1",
    "NOT {revenue} > 1",
    "{revenue} > '1000'",
    '{revenue} > "1000"',
    "{revenue} > 1e9",
    "{revenue} > 0x10",
    "{revenue} > 1.",
    "{revenue} > .5",
    "{revenue} > 1.2.3",
    "{revenue} > 1_000",
    "{revenue} > - 1",
    "{revenue} > +1",
    "{revenue} > ١٠",  # Arabic-Indic digits
    "{revenue} > 1234567890123456789",  # more than 18 digits
    "{revenue} > NULL",
    "{revenue} = {order_count}",
    "{revenue} + 1 > 2",
    "{revenue} > 1 AND",
    "{revenue} > 1 AND1 = 1",
    "{revenue} > 1 AND {order_count}",
    "{revenue} > 1 {order_count} > 2",
    "{revenue} IS",
    "{revenue} IS NOT",
    "{revenue} IS NOT 1",
    "{revenue} ISNULL",
    "{revenue} != 1",
    "{revenue} == 1",
    "{revenue} BETWEEN 1 AND 2",
    "{revenue} IN (1, 2)",
    "{revenue} LIKE 1",
    "{Revenue} > 1",
    "{orders.revenue} > 1",
    "{rule.active_customer}",
    "{revenue} > $user.region",
    "{$user.region} > 1",
    "{revenue} > 1\nUNION SELECT 1",
    "{revenue} > 1\x00",
    "{revenue} > 1 " + "AND {revenue} > 1 " * 40,  # over the length limit
]


@pytest.mark.parametrize("text", ATTACKS)
def test_anything_outside_the_grammar_is_refused(text, graph):
    with pytest.raises(QueryError):
        parse_metric_filter(text)
    request = QueryRequest(metrics=["revenue", "order_count"], metric_filters=[text])
    with pytest.raises(QueryError):
        Planner(graph, "duckdb").plan(request, NORTHEAST)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_only_the_parsed_parts_reach_the_sql(graph, dialect):
    """The conditions are rebuilt from the parsed metric, operator and digits: the generated
    SQL parses back as one statement whose outer WHERE holds exactly those comparisons."""
    request = QueryRequest(
        metrics=["revenue", "average_order_value"],
        dimensions=["orders.status"],
        metric_filters=["{revenue} > -1000.50 AND {average_order_value} IS NOT NULL"],
    )
    sql = Planner(graph, dialect).plan(request, NORTHEAST).sql
    statements = sqlglot.parse(sql, read=dialect)
    assert len(statements) == 1
    where = statements[0].args["where"].this
    assert {type(c).__name__ for c in where.flatten()} == {"GT", "Not"}
    assert "-1000.50" in where.sql(dialect=dialect)


# -- refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_args", "message"),
    [
        (
            {"metrics": ["revenue"], "metric_filters": ["{order_count} > 1"]},
            "not among the requested metrics",
        ),
        (
            {
                "metrics": ["revenue_month_to_date", "revenue"],
                "dimensions": ["orders.ordered_at__day"],
                "metric_filters": ["{revenue} > 1"],
            },
            "cumulative",
        ),
        (
            {
                "metrics": ["revenue_rolling_7_days"],
                "dimensions": ["orders.ordered_at__day"],
                "metric_filters": ["{revenue_rolling_7_days} > 1"],
            },
            "cumulative",
        ),
        (
            {
                "metrics": ["revenue_prev_month"],
                "dimensions": ["orders.ordered_at__month"],
                "metric_filters": ["{revenue_prev_month} > 1"],
            },
            "earlier period",
        ),
        (
            {
                "metrics": ["revenue_mom_growth", "revenue"],
                "dimensions": ["orders.ordered_at__month"],
                "metric_filters": ["{revenue} > 1"],
            },
            "earlier period",
        ),
        (
            {
                "metrics": ["revenue"],
                "metric_filters": ["{revenue} > 1"] * (MAX_METRIC_FILTERS + 1),
            },
            "at most",
        ),
    ],
)
def test_refusals(graph, request_args, message):
    with pytest.raises(QueryError, match=message):
        Planner(graph, "duckdb").plan(QueryRequest(**request_args), NORTHEAST)


def test_requests_without_metric_filters_keep_their_sql(graph):
    request = QueryRequest(metrics=["revenue"], dimensions=["orders.status"])
    for dialect in DIALECTS:
        same = request.model_copy(update={"metric_filters": []})
        sql = Planner(graph, dialect).plan(request, NORTHEAST).sql
        assert sql == Planner(graph, dialect).plan(same, NORTHEAST).sql
        assert "cosmos_result" not in sql


# -- numbers ------------------------------------------------------------------------------


def test_the_rows_kept_are_the_unfiltered_rows_that_meet_the_conditions(graph, duckdb_connector):
    """Derived and ratio metrics are allowed; the row filters apply to the facts first."""
    base = QueryRequest(
        metrics=["revenue", "net_revenue", "average_order_value"],
        dimensions=["customers.state", "orders.status"],
    )
    every = _rows(duckdb_connector, graph, base)
    assert {r["customers__state"] for r in every} <= {"MA", "NY", "NJ", "PA"}
    kept = _rows(
        duckdb_connector,
        graph,
        base.model_copy(
            update={"metric_filters": ["{net_revenue} >= 20000", "{average_order_value} < 2700"]}
        ),
    )
    expected = [r for r in every if r["net_revenue"] >= 20000 and r["average_order_value"] < 2700]
    assert 0 < len(kept) < len(every)
    assert sorted(map(str, kept)) == sorted(map(str, expected))


def test_limit_and_order_apply_to_the_rows_kept(graph, duckdb_connector):
    base = QueryRequest(metrics=["revenue"], dimensions=["customers.state"], order_by=["revenue"])
    every = _rows(duckdb_connector, graph, base)
    threshold = every[1]["revenue"]  # the smallest state is removed
    kept = _rows(
        duckdb_connector,
        graph,
        base.model_copy(update={"metric_filters": [f"{{revenue}} >= {threshold:.2f}"], "limit": 1}),
    )
    assert [r["customers__state"] for r in kept] == [every[1]["customers__state"]]


def test_null_tests_split_the_rows(graph, duckdb_connector):
    """Revenue and payments by day: days with orders but no payment have a null paid value."""
    base = QueryRequest(metrics=["revenue", "paid_value"], dimensions=["orders.ordered_at__day"])
    every = _rows(duckdb_connector, graph, base)
    nulls = _rows(
        duckdb_connector,
        graph,
        base.model_copy(update={"metric_filters": ["{paid_value} IS NULL"]}),
    )
    present = _rows(
        duckdb_connector,
        graph,
        base.model_copy(update={"metric_filters": ["{paid_value} IS NOT NULL"]}),
    )
    assert len(nulls) + len(present) == len(every)
    assert all(r["paid_value"] is None for r in nulls)
    # A comparison with a null value keeps no row.
    compared = _rows(
        duckdb_connector,
        graph,
        base.model_copy(update={"metric_filters": ["{paid_value} > -1000000000"]}),
    )
    assert len(compared) == len(present)


def test_evidence_lists_the_metric_filters_next_to_the_row_filters(graph, duckdb_connector):
    request = QueryRequest(
        metrics=["revenue"], dimensions=["customers.state"], metric_filters=["{revenue} > 1000"]
    )
    compiled = Planner(graph, "duckdb").plan(request, NORTHEAST)
    table = duckdb_connector.execute(compiled.sql)
    ev = EvidenceBuilder(graph).build(
        request=request, compiled=compiled, principal=NORTHEAST, data_source="x", table=table
    )
    assert ev["metric_filters"] == [
        {"metric": "revenue", "op": ">", "value": "1000", "text": "{revenue} > 1000"}
    ]
    assert ev["policies"]["row_filters"]
    assert any("{revenue} > 1000" in c for c in ev["caveats"])
    assert ev["request"]["metric_filters"] == ["{revenue} > 1000"]

    plain = QueryRequest(metrics=["revenue"], dimensions=["orders.status"])
    compiled = Planner(graph, "duckdb").plan(plain, NORTHEAST)
    ev = EvidenceBuilder(graph).build(
        request=plain,
        compiled=compiled,
        principal=NORTHEAST,
        data_source="x",
        table=duckdb_connector.execute(compiled.sql),
    )
    assert ev["metric_filters"] == []
    assert "metric_filters" not in ev["request"]
    assert not any("metric_filters" in c for c in ev["caveats"])


def test_cli_sql_takes_repeated_metric_filters():
    result = CliRunner().invoke(
        app,
        [
            "sql",
            "revenue",
            "order_count",
            "--by",
            "orders.status",
            "--metric-filter",
            "{revenue} > 1000",
            "--metric-filter",
            "{order_count} >= 2",
            "-u",
            "region=Northeast",
            "-m",
            str(EXAMPLE),
            "-c",
            str(EXAMPLE.parent / "connections.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "revenue > 1000 AND order_count >= 2" in " ".join(result.output.split())


def test_cli_refuses_an_injection_attempt():
    result = CliRunner().invoke(
        app,
        [
            "sql",
            "revenue",
            "--metric-filter",
            "{revenue} > 1; DROP TABLE public.orders",
            "-m",
            str(EXAMPLE),
            "-c",
            str(EXAMPLE.parent / "connections.yml"),
        ],
    )
    assert result.exit_code != 0
    assert "DROP" not in result.output.replace("{revenue} > 1; DROP TABLE public.orders", "")
