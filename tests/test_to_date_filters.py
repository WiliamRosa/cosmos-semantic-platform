"""Date filters do not cut grain-to-date metrics short (DEC-48).

A request filter on a year-, quarter-, month-, week- or day-to-date metric's time dimension
chooses the output rows; each row still reads its period from the start. Other filters apply
to the facts first. Reference cases A09 and A59-A62 check the numbers against hand-written SQL
on every database; these tests check the filter shapes, the SQL of requests without such a
filter, and the dialects. The shapes split are those of rolling windows (DEC-34,
tests/test_window_filters.py).
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from cosmos_semantic.connectors import ensure_read_only
from cosmos_semantic.evidence import sql_hash
from cosmos_semantic.planner import Planner, Principal, QueryRequest

DIALECTS = ["duckdb", "postgres", "tsql", "databricks", "snowflake", "mysql", "oracle"]
DAY = "orders.ordered_at__day"
MONTH = "orders.ordered_at__month"
TO_DATE = ["revenue_year_to_date", "revenue_month_to_date", "max_order_value_year_to_date"]
YEARLY = ["revenue_year_to_date", "max_order_value_year_to_date"]

# Digest of the SQL of each request on every dialect, from the planner before DEC-48. A request
# without a filter on a grain-to-date metric's time dimension, or with one the planner cannot
# split, keeps its SQL, so its `sql_hash` too. A SQLGlot upgrade that changes how SQL is
# printed changes them all; regenerate them then.
UNCHANGED = {
    "3a09050181b4d490": {"metrics": ["revenue_year_to_date"], "dimensions": [MONTH]},
    "169d2bac97a5284d": {
        "metrics": ["revenue_month_to_date", "revenue"],
        "dimensions": ["orders.status", DAY],
        "filters": ["{orders.status} <> 'cancelled'"],
    },
    # A filter on another time dimension applies to the facts first, as before.
    "dd83eb01d6243f0f": {
        "metrics": ["max_order_value_year_to_date", "revenue_year_to_date"],
        "dimensions": ["orders.status", MONTH],
        "filters": ["{customers.created_at} >= CURRENT_DATE - INTERVAL '400' DAY"],
    },
    # A shape the planner cannot split (NOT) keeps cutting the running total, as before.
    "3966150ed74f8869": {
        "metrics": ["revenue_year_to_date", "revenue_rolling_7_days"],
        "dimensions": [DAY],
        "filters": ["NOT ({orders.ordered_at} < CURRENT_DATE - INTERVAL '45' DAY)"],
    },
}


@pytest.mark.parametrize("digest", list(UNCHANGED))
def test_requests_without_a_date_filter_on_a_running_total_keep_their_sql_hash(graph, digest):
    request = QueryRequest(**UNCHANGED[digest], as_of=dt.date(2026, 11, 20))
    principal = Principal(user={"region": "Northeast"})
    hashes = [sql_hash(Planner(graph, d).plan(request, principal).sql) for d in DIALECTS]
    assert hashlib.sha256("\n".join(hashes).encode()).hexdigest()[:16] == digest


TODAY = dt.date.today()
X = TODAY - dt.timedelta(days=60)
Y = TODAY - dt.timedelta(days=20)
Z = TODAY - dt.timedelta(days=75)
FIRST = (TODAY - dt.timedelta(days=90)).replace(day=1)
WEEK = TODAY - dt.timedelta(days=100 + (TODAY - dt.timedelta(days=100)).weekday())


def _d(day: dt.date) -> str:
    return f"DATE '{day.isoformat()}'"


# Filter shapes the planner splits, as for rolling windows. X, Y and Z fall inside months, so
# the first month of each range starts before the filter.
SPLIT = [
    f"{{orders.ordered_at}} >= {_d(X)}",
    f"{{orders.ordered_at}} > {_d(X)}",
    f"{_d(X)} <= {{orders.ordered_at}}",
    f"{{orders.ordered_at}} < {_d(Y)}",
    f"{{orders.ordered_at}} BETWEEN {_d(X)} AND {_d(Y)}",
    f"{{{DAY}}} = {_d(X)}",
    f"{{{DAY}}} IN ({_d(X)}, {_d(Y)}, {_d(Z)})",
    f"{{{DAY}}} >= {_d(X)} AND {{{DAY}}} <= {_d(Y)}",
    f"{{{MONTH}}} = {_d(FIRST)}",
    f"{{orders.ordered_at__week}} >= {_d(WEEK)}",
    f"{{orders.ordered_at}} < {_d(Z)} OR {{orders.ordered_at}} >= {_d(Y)}",
]


def _rows(graph, connector, metrics, dimension, filters) -> tuple:
    request = QueryRequest(metrics=metrics, dimensions=[dimension], filters=filters)
    compiled = Planner(graph, connector.dialect).plan(request)
    key = dimension.replace(".", "__")
    out = {}
    for row in connector.execute(compiled.sql).to_pylist():
        out[row[key]] = tuple(row[m] for m in metrics)
    return compiled, out


def _check(graph, connector, metrics, dimension, filters, unfiltered_filters=()):
    """The rows are those a plain metric keeps under ``filters``, with the numbers of the
    request without its filters on the time dimension."""
    _, everything = _rows(graph, connector, metrics, dimension, list(unfiltered_filters))
    _, kept = _rows(graph, connector, ["revenue"], dimension, filters)
    compiled, rows = _rows(graph, connector, metrics, dimension, filters)
    expected = {k: v for k, v in everything.items() if k in kept}
    assert expected and rows.keys() == expected.keys()
    for key, values in rows.items():
        assert values == pytest.approx(expected[key]), key
    return compiled


@pytest.mark.parametrize("text", SPLIT)
@pytest.mark.parametrize("dimension", [DAY, MONTH])
def test_a_date_filter_reads_each_period_from_the_start(graph, duckdb_connector, text, dimension):
    metrics = TO_DATE if dimension == DAY else YEARLY  # month-to-date by month is each total
    compiled = _check(graph, duckdb_connector, metrics, dimension, [text])
    assert compiled.trace.window_filters == {m: {"after": [text], "before": []} for m in metrics}


def test_the_first_period_starts_before_the_filter(graph, duckdb_connector):
    """Before DEC-48 (as an unsplit `NOT` still does) the first days after `X` totalled only
    from `X`; now they count the days of their month before `X` too."""
    split = [f"{{orders.ordered_at}} >= {_d(X)}"]
    cut = [f"NOT ({{orders.ordered_at}} < {_d(X)})"]
    _, rows = _rows(graph, duckdb_connector, ["revenue_month_to_date"], DAY, split)
    _, before = _rows(graph, duckdb_connector, ["revenue_month_to_date"], DAY, cut)
    assert rows.keys() == before.keys()
    first = min(rows)
    month = [d for d in rows if (d.year, d.month) == (first.year, first.month)]
    assert X.day > 1 and month
    for day in month:  # the month has days with orders before X (demo data: every few days)
        assert rows[day][0] > before[day][0] + 0.005, day
    later = [d for d in rows if (d.year, d.month) != (first.year, first.month)]
    for day in later:
        assert rows[day][0] == pytest.approx(before[day][0]), day


def test_other_filters_still_apply_to_the_facts_first(graph, duckdb_connector):
    status = "{orders.status} = 'completed'"
    for filters in (
        [f"{{orders.ordered_at}} >= {_d(X)}", status],
        [f"{{orders.ordered_at}} >= {_d(X)} AND {status}"],
    ):
        compiled = _check(graph, duckdb_connector, YEARLY, MONTH, filters, [status])
        assert "orders_agg_from_year_start" in compiled.sql


def test_an_upper_bound_inside_a_month_keeps_that_months_whole_total(graph, duckdb_connector):
    """By month, `ordered_at < Y` keeps Y's month; its year-to-date runs to the month's end."""
    filters = [f"{{orders.ordered_at}} >= {_d(X)} AND {{orders.ordered_at}} < {_d(Y)}"]
    _, everything = _rows(graph, duckdb_connector, YEARLY, MONTH, [])
    _, rows = _rows(graph, duckdb_connector, YEARLY, MONTH, filters)
    last = dt.datetime.combine(Y.replace(day=1), dt.time())
    assert rows[last] == pytest.approx(everything[last])


# Shapes the planner cannot split: they still restrict the facts before the running total, as
# before DEC-48 (reference case A62 checks the numbers of one).
UNSPLIT = [
    f"NOT ({{orders.ordered_at}} < {_d(X)})",
    f"{{orders.ordered_at}} <> {_d(X)}",
    f"{{{DAY}}} NOT IN ({_d(X)}, {_d(Y)})",
    "EXTRACT(DOW FROM {orders.ordered_at}) = 1",
    f"{{orders.ordered_at__hour}} >= TIMESTAMP '{X.isoformat()} 12:00:00'",
    f"{{orders.ordered_at}} >= {_d(X)} OR {{orders.status}} = 'pending'",
]


@pytest.mark.parametrize("text", UNSPLIT)
def test_other_shapes_keep_cutting_the_running_total(graph, duckdb_connector, text):
    compiled, rows = _rows(graph, duckdb_connector, TO_DATE, DAY, [text])
    assert "_start" not in compiled.sql
    assert compiled.trace.window_filters == {m: {"after": [], "before": [text]} for m in TO_DATE}
    assert rows


def test_a_filter_on_another_time_dimension_applies_first(graph):
    request = QueryRequest(
        metrics=["revenue_year_to_date"],
        dimensions=[MONTH],
        filters=[f"{{customers.created_at}} >= {_d(X)}"],
    )
    compiled = Planner(graph, "duckdb").plan(request, Principal(user={"region": "South"}))
    assert "_start" not in compiled.sql and compiled.trace.window_filters == {}


def _sql(graph, metric: str, dimension: str, text: str) -> str:
    request = QueryRequest(metrics=[metric], dimensions=[dimension], filters=[text])
    sql = Planner(graph, "postgres").plan(request).sql
    return sql.split("_start AS (")[1].split("), ")[0]


def test_lower_bounds_read_the_start_of_the_next_period(graph):
    start = _sql(graph, "revenue_year_to_date", MONTH, "{orders.ordered_at} >= DATE '2026-03-10'")
    assert (
        "DATE_TRUNC('YEAR', orders.ordered_at) + INTERVAL '12 MONTH' >= CAST('2026-03-10' AS DATE)"
        in start
    )
    start = _sql(graph, "revenue_month_to_date", DAY, f"{{{DAY}}} > DATE '2026-03-10'")
    assert (
        "DATE_TRUNC('DAY', DATE_TRUNC('MONTH', orders.ordered_at) + INTERVAL '1 MONTH') > "
        "CAST('2026-03-10' AS DATE)"
    ) in start


def test_upper_bounds_read_the_start_of_the_row(graph):
    # On the column, the row's start: a bound inside the row keeps all of the row's facts.
    start = _sql(graph, "revenue_year_to_date", MONTH, "{orders.ordered_at} < DATE '2026-03-10'")
    assert "DATE_TRUNC('MONTH', orders.ordered_at) < CAST('2026-03-10' AS DATE)" in start
    # At a grain whose periods hold whole rows, the bound stays as written.
    start = _sql(graph, "revenue_year_to_date", MONTH, f"{{{MONTH}}} <= DATE '2026-03-01'")
    assert "DATE_TRUNC('MONTH', orders.ordered_at) <= CAST('2026-03-01' AS DATE)" in start
    start = _sql(graph, "revenue_month_to_date", DAY, f"{{{DAY}}} <= DATE '2026-03-10'")
    assert "DATE_TRUNC('DAY', orders.ordered_at) <= CAST('2026-03-10' AS DATE)" in start
    # A day grain under a month row: read at the month's start, so the month stays whole.
    start = _sql(graph, "revenue_year_to_date", MONTH, f"{{{DAY}}} <= DATE '2026-03-10'")
    assert (
        "DATE_TRUNC('DAY', DATE_TRUNC('MONTH', orders.ordered_at)) <= CAST('2026-03-10' AS DATE)"
        in start
    )


def test_metrics_of_the_same_period_share_their_facts(graph):
    request = QueryRequest(
        metrics=[*YEARLY, "revenue_month_to_date", "revenue"],
        dimensions=["orders.status", DAY],
        filters=[f"{{orders.ordered_at}} >= {_d(X)}"],
    )
    sql = Planner(graph, "duckdb").plan(request).sql
    assert sql.count("orders_agg_from_year_start AS (") == 1
    assert sql.count("orders_agg_from_month_start AS (") == 1
    assert "FROM orders_agg AS cur" in sql


def test_a_running_total_over_all_time_is_not_split(graph, duckdb_connector):
    """Without `grain_to_date` there is no period start to read back to; the filter applies to
    the facts first, as before."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.spec import CumulativeMetric

    project = graph.project.model_copy(deep=True)
    project.metrics.append(
        CumulativeMetric(type="cumulative", name="revenue_running", measure="revenue")
    )
    other = compile_project(project)
    request = QueryRequest(
        metrics=["revenue_running"], dimensions=[MONTH], filters=[f"{{{MONTH}}} >= {_d(FIRST)}"]
    )
    compiled = Planner(other, "duckdb").plan(request)
    assert "_start" not in compiled.sql and compiled.trace.window_filters == {}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_date_filtered_to_date_sql_for_every_dialect(graph, dialect):
    request = QueryRequest(
        metrics=[*YEARLY, "revenue_month_to_date", "revenue"],
        dimensions=["orders.status", DAY],
        filters=[
            "{orders.ordered_at} >= CURRENT_DATE - INTERVAL '45' DAY",
            "{orders.ordered_at__month} IN (CURRENT_DATE - INTERVAL '30' DAY, CURRENT_DATE)",
            "{orders.status} <> 'cancelled'",
        ],
    )
    compiled = Planner(graph, dialect).plan(request)
    ensure_read_only(compiled.sql, dialect)
    plain = compiled.sql.replace('"', "").replace("`", "")
    assert "orders_agg_from_year_start" in plain and "orders_agg_from_month_start" in plain
    if dialect == "tsql":
        assert "INTERVAL" not in compiled.sql.upper()
