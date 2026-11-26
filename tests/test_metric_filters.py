"""Filters on metric values (DEC-29): strict parsing, refusals, injection attempts and the
numbers they keep. The reference cases A40-A43, A73-A75 and A80 check the numbers against
hand-written SQL; DEC-58 lets them apply after running totals, rolling windows and offsets."""

from __future__ import annotations

import datetime as dt

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

DIALECTS = [
    "duckdb",
    "postgres",
    "tsql",
    "databricks",
    "snowflake",
    "mysql",
    "oracle",
    "bigquery",
    "trino",
]
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


# -- after running totals, rolling windows and offsets (DEC-58) ---------------------------


def _kept_and_every(connector, graph, request):
    every = _rows(connector, graph, request.model_copy(update={"metric_filters": []}))
    return _rows(connector, graph, request), every


def _expected(every, keep):
    return sorted(map(str, (r for r in every if keep(r))))


def test_an_offset_growth_filter_keeps_the_months_with_their_unfiltered_growth(
    graph, duckdb_connector
):
    """ "Months where revenue grew more than 10% over the month before": each month kept has
    the growth it has without the filter, and a month removed is still the month before."""
    request = QueryRequest(
        metrics=["revenue", "revenue_prev_month", "revenue_mom_growth"],
        dimensions=["orders.ordered_at__month"],
        metric_filters=["{revenue_mom_growth} > 0.1"],
    )
    kept, every = _kept_and_every(duckdb_connector, graph, request)
    assert 0 < len(kept) < len(every)
    expected = _expected(
        every, lambda r: r["revenue_mom_growth"] is not None and r["revenue_mom_growth"] > 0.1
    )
    assert sorted(map(str, kept)) == expected
    by_month = {r["orders__ordered_at__month"]: r for r in every}
    months = {r["orders__ordered_at__month"] for r in kept}

    def before(month):
        return month.replace(year=month.year - (month.month == 1), month=(month.month - 2) % 12 + 1)

    # Some month kept reads, as its month before, a month the filter removed.
    removed_before = [
        r
        for r in kept
        if (prev := before(r["orders__ordered_at__month"])) not in months
        and prev in by_month
        and r["revenue_prev_month"] == by_month[prev]["revenue"]
    ]
    assert removed_before


def _median_revenue(connector, graph, request):
    """A threshold between the rows' revenues, written as a metric filter number: the demo
    data is generated relative to today, so a fixed number could keep every row or none."""
    every = _rows(connector, graph, request.model_copy(update={"metric_filters": []}))
    values = sorted(r["revenue"] for r in every if r["revenue"] is not None)
    return f"{values[len(values) // 2]:.2f}"


def test_a_date_filter_an_offset_and_a_metric_filter_together(graph, duckdb_connector):
    """The date filter chooses the months, the first month still reads the whole month before
    it outside the range (DEC-54), and the metric filter keeps rows of that result only (the
    reference case A80 checks the numbers against hand-written SQL)."""
    request = QueryRequest(
        metrics=["revenue", "revenue_prev_month", "revenue_mom_growth"],
        dimensions=["orders.ordered_at__month"],
        filters=["{orders.ordered_at} >= CURRENT_DATE - INTERVAL '120' DAY"],
        metric_filters=["{revenue_prev_month} IS NOT NULL"],
    )
    kept, every = _kept_and_every(duckdb_connector, graph, request)
    assert sorted(map(str, kept)) == _expected(every, lambda r: r["revenue_prev_month"] is not None)
    first = min(r["orders__ordered_at__month"] for r in every)
    # The first month of the range reads its month before from outside the range.
    assert first in {r["orders__ordered_at__month"] for r in kept}
    # A growth filter then removes rows without changing the values of the others.
    grew = _rows(
        duckdb_connector,
        graph,
        request.model_copy(update={"metric_filters": ["{revenue_mom_growth} < 0"]}),
    )
    assert sorted(map(str, grew)) == _expected(
        every, lambda r: r["revenue_mom_growth"] is not None and r["revenue_mom_growth"] < 0
    )


def test_a_rolling_window_still_reads_the_days_the_metric_filter_removes(graph, duckdb_connector):
    """A day the metric filter removes is still in the window of the days after it (the
    reference case A74 checks the numbers against hand-written SQL)."""
    request = QueryRequest(
        metrics=["revenue_rolling_7_days", "revenue"],
        dimensions=["orders.ordered_at__day"],
        filters=["{orders.ordered_at} >= CURRENT_DATE - INTERVAL '45' DAY"],
    )
    threshold = _median_revenue(duckdb_connector, graph, request)
    request = request.model_copy(update={"metric_filters": [f"{{revenue}} > {threshold}"]})
    kept, every = _kept_and_every(duckdb_connector, graph, request)
    assert 0 < len(kept) < len(every)
    assert sorted(map(str, kept)) == _expected(every, lambda r: r["revenue"] > float(threshold))
    by_day = {r["orders__ordered_at__day"]: r for r in every}
    for row in kept:
        day = row["orders__ordered_at__day"]
        assert row["revenue_rolling_7_days"] == by_day[day]["revenue_rolling_7_days"]
    # Some kept day's 7-day sum holds more than the revenue of the kept days in its window.
    days = {r["orders__ordered_at__day"]: r["revenue"] for r in kept}
    week = dt.timedelta(days=6)
    assert any(
        r["revenue_rolling_7_days"]
        > sum(
            v
            for d, v in days.items()
            if r["orders__ordered_at__day"] - week <= d <= r["orders__ordered_at__day"]
        )
        + 0.01
        for r in kept
    )


@pytest.mark.parametrize(
    ("metric", "grain"), [("revenue_year_to_date", "month"), ("revenue_month_to_date", "day")]
)
def test_a_running_total_still_adds_the_periods_the_metric_filter_removes(
    graph, duckdb_connector, metric, grain
):
    request = QueryRequest(metrics=[metric, "revenue"], dimensions=[f"orders.ordered_at__{grain}"])
    threshold = _median_revenue(duckdb_connector, graph, request)
    request = request.model_copy(update={"metric_filters": [f"{{revenue}} < {threshold}"]})
    kept, every = _kept_and_every(duckdb_connector, graph, request)
    assert 0 < len(kept) < len(every)
    assert sorted(map(str, kept)) == _expected(every, lambda r: r["revenue"] < float(threshold))
    # The running total of some kept period holds more than the kept periods up to it.
    column = f"orders__ordered_at__{grain}"

    def period(d):  # the period the running total restarts at
        return (d.year,) if grain == "month" else (d.year, d.month)

    def kept_so_far(row):
        return sum(
            k["revenue"]
            for k in kept
            if period(k[column]) == period(row[column]) and k[column] <= row[column]
        )

    assert any(r[metric] > kept_so_far(r) + 0.01 for r in kept)


def test_order_and_limit_apply_after_the_metric_filter_on_a_window(graph, duckdb_connector):
    base = QueryRequest(
        metrics=["revenue_year_to_date", "revenue"],
        dimensions=["orders.ordered_at__month"],
        order_by=["-revenue_year_to_date"],
    )
    every = _rows(duckdb_connector, graph, base)
    threshold = _median_revenue(duckdb_connector, graph, base)
    kept = _rows(
        duckdb_connector,
        graph,
        base.model_copy(update={"metric_filters": [f"{{revenue}} < {threshold}"], "limit": 2}),
    )
    assert len(kept) == 2
    assert kept == [r for r in every if r["revenue"] < float(threshold)][:2]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_metric_filter_wraps_the_window_and_the_shift(graph, dialect):
    """The window function and the shifted join are inside `cosmos_result`; the outer WHERE
    reads its output columns only."""
    request = QueryRequest(
        metrics=["revenue_year_to_date", "revenue_mom_growth"],
        dimensions=["orders.ordered_at__month"],
        metric_filters=["{revenue_mom_growth} > 0.1", "{revenue_year_to_date} > 0"],
    )
    sql = Planner(graph, dialect).plan(request, NORTHEAST).sql
    (statement,) = sqlglot.parse(sql, read=dialect)
    where = statement.args["where"]
    assert not list(where.find_all(sqlglot.exp.Window))
    assert {c.name for c in where.find_all(sqlglot.exp.Column)} == {
        "revenue_mom_growth",
        "revenue_year_to_date",
    }
    result = next(c for c in statement.find_all(sqlglot.exp.CTE) if c.alias == "cosmos_result")
    assert list(result.find_all(sqlglot.exp.Window))


def test_evidence_says_the_metric_filters_apply_after_the_window_and_shift(graph, duckdb_connector):
    request = QueryRequest(
        metrics=["revenue", "revenue_mom_growth", "revenue_year_to_date"],
        dimensions=["orders.ordered_at__month"],
        metric_filters=["{revenue_mom_growth} > 0.1"],
    )
    compiled = Planner(graph, "duckdb").plan(request, NORTHEAST)
    ev = EvidenceBuilder(graph).build(
        request=request,
        compiled=compiled,
        principal=NORTHEAST,
        data_source="x",
        table=duckdb_connector.execute(compiled.sql),
    )
    times = {m["name"]: m["time"] for m in ev["metrics"]}
    assert times["revenue"] is None
    assert times["revenue_mom_growth"]["metric_filters_applied"] == "after"
    assert times["revenue_year_to_date"]["metric_filters_applied"] == "after"
    assert any(
        "after the running totals" in c and "`revenue_mom_growth`" in c for c in ev["caveats"]
    )

    plain = request.model_copy(update={"metric_filters": []})
    compiled = Planner(graph, "duckdb").plan(plain, NORTHEAST)
    ev = EvidenceBuilder(graph).build(
        request=plain,
        compiled=compiled,
        principal=NORTHEAST,
        data_source="x",
        table=duckdb_connector.execute(compiled.sql),
    )
    assert all("metric_filters_applied" not in (m["time"] or {}) for m in ev["metrics"])


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
