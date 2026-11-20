"""Date filters do not cut rolling windows short (DEC-34).

A request filter on a rolling window's time dimension chooses the output days; each day's window
still reads the N - 1 days before it. Other filters apply to the facts first. Reference cases
A30 and A44-A47 check the numbers against hand-written SQL on every database; these tests check
the filter shapes, the SQL of requests without such a filter, and the dialects.
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
WINDOWS = ["revenue_rolling_7_days", "smallest_order_rolling_7_days"]

# Digest of the SQL of each request on every dialect, from the planner before DEC-34. A request
# without a filter on a rolling window's time dimension keeps its SQL, so its `sql_hash` too.
# A SQLGlot upgrade that changes how SQL is printed changes them all; regenerate them then.
UNCHANGED = {
    "c3d2ce741961792c": {"metrics": ["revenue_rolling_7_days"], "dimensions": [DAY]},
    "b04c2c9266de2087": {
        "metrics": ["revenue_rolling_7_days", "revenue"],
        "dimensions": ["orders.status", DAY],
        "filters": ["{orders.status} <> 'cancelled'"],
    },
    "9fef5d0f3b84cfc4": {
        "metrics": ["smallest_order_rolling_7_days", "smallest_order"],
        "dimensions": ["orders.status", DAY],
    },
    "45c332556d81be0a": {
        "metrics": ["customers_rolling_30_days"],
        "dimensions": ["customers.segment", "customers.last_order_at__day"],
    },
    # A date filter without a rolling window, and under a running total: unchanged too.
    "85b87c827c245efd": {
        "metrics": ["revenue", "order_count"],
        "dimensions": [DAY],
        "filters": ["{orders.ordered_at} >= CURRENT_DATE - INTERVAL '45' DAY"],
    },
    "e4e93947c0b92440": {
        "metrics": ["revenue_year_to_date"],
        "dimensions": ["orders.ordered_at__month"],
        "filters": ["{orders.ordered_at} >= CURRENT_DATE - INTERVAL '400' DAY"],
    },
}


@pytest.mark.parametrize("digest", list(UNCHANGED))
def test_requests_without_a_date_filter_on_a_window_keep_their_sql_hash(graph, digest):
    request = QueryRequest(**UNCHANGED[digest], as_of=dt.date(2026, 11, 20))
    principal = Principal(user={"region": "Northeast"})
    hashes = [sql_hash(Planner(graph, d).plan(request, principal).sql) for d in DIALECTS]
    assert hashlib.sha256("\n".join(hashes).encode()).hexdigest()[:16] == digest


TODAY = dt.date.today()
X = TODAY - dt.timedelta(days=60)
Y = TODAY - dt.timedelta(days=20)
Z = TODAY - dt.timedelta(days=75)
MONTH = (TODAY - dt.timedelta(days=90)).replace(day=1)
WEEK = TODAY - dt.timedelta(days=100 + (TODAY - dt.timedelta(days=100)).weekday())


def _d(day: dt.date) -> str:
    return f"DATE '{day.isoformat()}'"


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


# Filter shapes the planner splits, each with the days it keeps. The demo orders are placed
# between 07:00 and 22:59, so a bound at midnight keeps whole days.
SPLIT = [
    (f"{{orders.ordered_at}} >= {_d(X)}", lambda d: d >= X),
    (f"{{orders.ordered_at}} > {_d(X)}", lambda d: d >= X),
    (f"{_d(X)} <= {{orders.ordered_at}}", lambda d: d >= X),
    (f"{{orders.ordered_at}} < {_d(Y)}", lambda d: d < Y),
    (f"{{orders.ordered_at}} BETWEEN {_d(X)} AND {_d(Y)}", lambda d: X <= d < Y),
    (f"{{{DAY}}} = {_d(X)}", lambda d: d == X),
    (f"{{{DAY}}} IN ({_d(X)}, {_d(Y)}, {_d(Z)})", lambda d: d in (X, Y, Z)),
    (f"{{{DAY}}} >= {_d(X)} AND {{{DAY}}} <= {_d(Y)}", lambda d: X <= d <= Y),
    (f"{{orders.ordered_at__month}} = {_d(MONTH)}", lambda d: d.replace(day=1) == MONTH),
    (f"{{orders.ordered_at__week}} >= {_d(WEEK)}", lambda d: _monday(d) >= WEEK),
    (
        f"{{orders.ordered_at}} < {_d(Z)} OR {{orders.ordered_at}} >= {_d(Y)}",
        lambda d: d < Z or d >= Y,
    ),
]


def _by_day(service_rows) -> dict[dt.date, tuple]:
    out = {}
    for row in service_rows:
        day = row["orders__ordered_at__day"]
        out[None if day is None else day.date()] = tuple(row[m] for m in WINDOWS)
    return out


def _query(graph, connector, filters):
    request = QueryRequest(metrics=WINDOWS, dimensions=[DAY], filters=filters)
    compiled = Planner(graph, connector.dialect).plan(request)
    return compiled, _by_day(connector.execute(compiled.sql).to_pylist())


@pytest.mark.parametrize(("text", "keep"), SPLIT, ids=[t for t, _ in SPLIT])
def test_a_date_filter_keeps_full_windows(graph, duckdb_connector, text, keep):
    """The rows are the unfiltered rows of the days the filter keeps, with the same numbers."""
    _, everything = _query(graph, duckdb_connector, [])
    compiled, rows = _query(graph, duckdb_connector, [text])
    assert compiled.trace.window_filters == {
        name: {"after": [text], "before": []} for name in WINDOWS
    }
    expected = {d: v for d, v in everything.items() if d is not None and keep(d)}
    assert expected and rows.keys() == expected.keys()
    for day, values in rows.items():
        assert values == pytest.approx(expected[day]), day


def test_other_filters_still_apply_to_the_facts_first(graph, duckdb_connector):
    status = "{orders.status} = 'completed'"
    _, completed = _query(graph, duckdb_connector, [status])
    for filters in (
        [f"{{orders.ordered_at}} >= {_d(X)}", status],
        [f"{{orders.ordered_at}} >= {_d(X)} AND {status}"],
    ):
        compiled, rows = _query(graph, duckdb_connector, filters)
        assert "orders_agg_lookback_7_days" in compiled.sql
        expected = {d: v for d, v in completed.items() if d is not None and d >= X}
        assert expected and rows.keys() == expected.keys()
        for day, values in rows.items():
            assert values == pytest.approx(expected[day]), day


# Shapes the planner cannot split: they still restrict the facts before the window, as before
# DEC-34 (reference case A47 checks the numbers of one).
UNSPLIT = [
    f"NOT ({{orders.ordered_at}} < {_d(X)})",
    f"{{orders.ordered_at}} <> {_d(X)}",
    f"{{{DAY}}} NOT IN ({_d(X)}, {_d(Y)})",
    "EXTRACT(DOW FROM {orders.ordered_at}) = 1",
    f"{{orders.ordered_at__hour}} >= TIMESTAMP '{X.isoformat()} 12:00:00'",
    f"{{orders.ordered_at}} >= {_d(X)} OR {{orders.status}} = 'pending'",
    "{orders.ordered_at} >= {orders.ordered_at} - INTERVAL '1' DAY",
]


@pytest.mark.parametrize("text", UNSPLIT)
def test_other_shapes_keep_cutting_the_window(graph, duckdb_connector, text):
    compiled, rows = _query(graph, duckdb_connector, [text])
    assert "lookback" not in compiled.sql
    assert compiled.trace.window_filters == {
        name: {"after": [], "before": [text]} for name in WINDOWS
    }
    assert rows


def test_a_filter_on_another_time_dimension_applies_first(graph):
    request = QueryRequest(
        metrics=["revenue_rolling_7_days"],
        dimensions=[DAY],
        filters=[f"{{customers.created_at}} >= {_d(X)}"],
    )
    compiled = Planner(graph, "duckdb").plan(request, Principal(user={"region": "South"}))
    assert "lookback" not in compiled.sql and compiled.trace.window_filters == {}


def test_lower_bounds_read_back_n_days_on_the_column_and_n_minus_one_at_a_grain(graph):
    def sql(text: str) -> str:
        request = QueryRequest(metrics=["revenue_rolling_7_days"], dimensions=[DAY], filters=[text])
        return Planner(graph, "postgres").plan(request).sql

    # The column may hold a time of day that the bound cuts: one day more.
    assert "orders.ordered_at + INTERVAL '7 DAY' >= CAST('2026-01-10' AS DATE)" in sql(
        "{orders.ordered_at} >= DATE '2026-01-10'"
    )
    # At day grain or coarser, every moment of a day has the same value: N - 1 days.
    assert (
        "DATE_TRUNC('DAY', orders.ordered_at + INTERVAL '6 DAY') >= CAST('2026-01-10' AS DATE)"
        in sql(f"{{{DAY}}} >= DATE '2026-01-10'")
    )
    # Upper bounds are kept as written; a 1-day window at a grain reads nothing earlier.
    upper = sql("{orders.ordered_at} < DATE '2026-01-10'")
    assert "orders.ordered_at < CAST('2026-01-10' AS DATE)" in upper
    assert "INTERVAL '7 DAY'" not in upper.split("orders_agg_lookback_7_days AS")[1]


def test_windows_of_the_same_length_share_their_facts(graph):
    request = QueryRequest(
        metrics=[*WINDOWS, "revenue"],
        dimensions=["orders.status", DAY],
        filters=[f"{{orders.ordered_at}} >= {_d(X)}"],
    )
    sql = Planner(graph, "duckdb").plan(request).sql
    assert sql.count("orders_agg_lookback_7_days AS (") == 1
    assert "FROM orders_agg AS cur" in sql and "JOIN orders_agg_lookback_7_days AS prev" in sql


@pytest.mark.parametrize("dialect", DIALECTS)
def test_date_filtered_window_sql_for_every_dialect(graph, dialect):
    request = QueryRequest(
        metrics=[*WINDOWS, "revenue"],
        dimensions=["orders.status", DAY],
        filters=[
            "{orders.ordered_at} >= CURRENT_DATE - INTERVAL '45' DAY",
            "{orders.ordered_at__month} IN (CURRENT_DATE - INTERVAL '30' DAY, CURRENT_DATE)",
            "{orders.status} <> 'cancelled'",
        ],
    )
    compiled = Planner(graph, dialect).plan(request)
    ensure_read_only(compiled.sql, dialect)
    assert "orders_agg_lookback_7_days" in compiled.sql.replace('"', "").replace("`", "")
    if dialect == "tsql":
        assert "INTERVAL" not in compiled.sql.upper()
