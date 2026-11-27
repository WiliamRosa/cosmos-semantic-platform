"""Rolling windows in months at month grain (DEC-59).

A `window: N months` over a sum, count, min or max measure, queried by month: each month gets
the measure over the N months ending on it, a range of calendar months, so a month without
facts does not shift the window. Reference cases A76 to A79 (tests/test_reference.py) check
the numbers against hand-written SQL on every database; these tests check the semantics on a
small table, the SQL on every dialect, the refusals and the evidence.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest
import sqlglot
import yaml

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ensure_read_only
from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.evidence import EvidenceBuilder
from cosmos_semantic.planner import Planner, QueryRequest
from cosmos_semantic.spec import Project, parse_month_window, parse_window

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

MODEL = """
version: 1
semantic_models:
  - name: sales
    table: t_sales
    entities: [{name: sale, type: primary, expr: id}]
    dimensions: [{name: sold_at, type: time}, {name: kind, type: categorical}]
    measures:
      - {name: amount, agg: sum}
      - {name: lines, agg: count}
      - {name: top, agg: max, expr: amount}
      - {name: low, agg: min, expr: amount}
metrics:
  - {name: amount_3m, type: cumulative, measure: amount, window: 3 months}
  - {name: lines_12m, type: cumulative, measure: lines, window: 12 months}
  - {name: top_3m, type: cumulative, measure: top, window: 3 months}
  - {name: low_1m, type: cumulative, measure: low, window: 1 month}
  - {name: amount_7d, type: cumulative, measure: amount, window: 7 days}
"""

# (id, sold_at, kind, amount): November 2025 to March 2026, with no sale in January 2026 (a
# month without facts inside the windows, after a year boundary) and one sale without a date.
ROWS = [
    (1, "2025-11-03 10:00:00", "a", 10),
    (2, "2025-11-20 15:00:00", "b", 20),
    (3, "2025-12-31 23:30:00", "a", 30),
    (4, "2026-02-01 00:10:00", "a", 40),
    (5, "2026-02-27 18:00:00", None, 50),
    (6, "2026-03-15 12:00:00", "a", 60),
    (7, None, "a", 70),
]
MONTH = "sales.sold_at__month"


def _project(text: str = MODEL) -> Project:
    return Project.model_validate(yaml.safe_load(text))


@pytest.fixture(scope="module")
def graph():
    return compile_project(_project())


@pytest.fixture(scope="module")
def con():
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t_sales (id INTEGER, sold_at TIMESTAMP, kind VARCHAR, amount INTEGER)"
    )
    con.executemany("INSERT INTO t_sales VALUES (?, ?, ?, ?)", ROWS)
    yield con
    con.close()


def _run(graph, con, metrics, dimensions=(MONTH,), filters=()):
    request = QueryRequest(metrics=metrics, dimensions=list(dimensions), filters=list(filters))
    cursor = con.execute(Planner(graph, "duckdb").plan(request).sql)
    names = [c[0] for c in cursor.description]
    out = {}
    for row in cursor.fetchall():
        record = dict(zip(names, row, strict=True))
        month = record.pop("sales__sold_at__month")
        key = (record.pop("sales__kind"), month) if "sales.kind" in dimensions else month
        out[key] = record
    return out


def _month(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{text}-01")


def test_parse_month_window():
    assert parse_month_window("1 month") == 1 and parse_month_window(" 12 Months ") == 12
    assert parse_month_window("0 months") is None and parse_month_window("7 days") is None
    assert parse_window("3 months") is None  # the day path does not read months


def test_each_month_combines_the_months_of_its_range(graph, con):
    rows = _run(graph, con, ["amount_3m", "lines_12m", "top_3m", "low_1m"])
    assert rows == {
        _month("2025-11"): {"amount_3m": 30, "lines_12m": 2, "top_3m": 20, "low_1m": 10},
        _month("2025-12"): {"amount_3m": 60, "lines_12m": 3, "top_3m": 30, "low_1m": 30},
        # December to February: January has no sales, November is out of the window. A frame
        # of 3 rows would reach back to November (and give 120).
        _month("2026-02"): {"amount_3m": 120, "lines_12m": 5, "top_3m": 50, "low_1m": 40},
        _month("2026-03"): {"amount_3m": 150, "lines_12m": 6, "top_3m": 60, "low_1m": 60},
        None: {"amount_3m": None, "lines_12m": None, "top_3m": None, "low_1m": None},
    }


def test_each_combination_of_the_other_dimensions_has_its_own_window(graph, con):
    rows = _run(graph, con, ["amount_3m"], ["sales.kind", MONTH])
    assert {k: v["amount_3m"] for k, v in rows.items()} == {
        ("a", _month("2025-11")): 10,
        ("a", _month("2025-12")): 40,
        ("a", _month("2026-02")): 70,
        ("a", _month("2026-03")): 100,
        ("a", None): None,
        ("b", _month("2025-11")): 20,
        (None, _month("2026-02")): 50,  # a null kind is a group of its own
    }


def test_a_date_filter_chooses_the_months_and_does_not_cut_the_window(graph, con):
    """As DEC-34 for days: the filter picks the output months; each month's window still
    reads its N whole months, the months before the range and the parts of the first and last
    months the bounds cut included. A shape that cannot be split restricts the facts first."""
    filters = ["{sales.sold_at} >= '2026-02-15'", "{sales.sold_at} < '2026-03-20'"]
    rows = _run(graph, con, ["amount_3m", "lines_12m"], filters=filters)
    assert rows == {
        _month("2026-02"): {"amount_3m": 120, "lines_12m": 5},
        _month("2026-03"): {"amount_3m": 150, "lines_12m": 6},
    }
    quarter = _run(graph, con, ["amount_3m"], filters=["{sales.sold_at__quarter} >= '2026-01-01'"])
    assert {k: v["amount_3m"] for k, v in quarter.items()} == {
        _month("2026-02"): 120,
        _month("2026-03"): 150,
    }
    cut = _run(graph, con, ["amount_3m"], filters=["NOT ({sales.sold_at} < '2026-02-15')"])
    assert {k: v["amount_3m"] for k, v in cut.items()} == {
        _month("2026-02"): 50,
        _month("2026-03"): 110,
    }


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "filters",
    [[], ["{sales.sold_at} >= '2026-01-01'", "{sales.sold_at__month} <= '2026-03-01'"]],
    ids=["unfiltered", "lookback"],
)
def test_month_window_sql_for_every_dialect(graph, dialect, filters):
    request = QueryRequest(
        metrics=["amount_3m", "lines_12m", "top_3m"],
        dimensions=["sales.kind", MONTH],
        filters=filters,
    )
    sql = Planner(graph, dialect).plan(request).sql
    ensure_read_only(sql, dialect)
    sqlglot.parse_one(sql, read=dialect)
    upper = sql.upper().replace("`", "").replace('"', "")
    assert "OVER" not in upper  # a range join, not a window frame
    assert "SUM(PREV.AMOUNT)" in upper and "MAX(PREV.TOP)" in upper
    if filters:
        assert "SALES_AGG_LOOKBACK_3_MONTHS" in upper and "SALES_AGG_LOOKBACK_12_MONTHS" in upper


@pytest.mark.parametrize(
    ("metric", "dimension"),
    [
        ("amount_3m", "sales.sold_at__day"),
        ("amount_3m", "sales.sold_at__week"),
        ("amount_3m", "sales.sold_at__quarter"),
        ("amount_3m", "sales.sold_at"),
        ("amount_7d", MONTH),
    ],
)
def test_other_grains_are_refused(graph, metric, dimension):
    with pytest.raises(QueryError, match="needs the time dimension at"):
        Planner(graph, "duckdb").plan(QueryRequest(metrics=[metric], dimensions=[dimension]))


@pytest.mark.parametrize("agg", ["count_distinct", "avg"])
def test_a_month_window_over_a_distinct_count_or_an_average_is_refused(agg):
    text = (
        MODEL.replace(
            "      - {name: low, agg: min, expr: amount}\n",
            f"      - {{name: low, agg: min, expr: amount}}\n      - {{name: odd, agg: {agg}, expr: amount}}\n",
        )
        + "  - {name: odd_3m, type: cumulative, measure: odd, window: 3 months}\n"
    )
    with pytest.raises(ModelError, match="odd_3m"):
        compile_project(_project(text))
    loose = compile_project(_project(text), strict=False)
    errors = {i.location: i.message for i in loose.issues if i.severity == "error"}
    assert set(errors) == {"metrics.odd_3m"}
    assert (
        f"a rolling window in months over a `{agg}` measure is not supported"
        in errors["metrics.odd_3m"]
    )
    with pytest.raises(QueryError, match="in months over a"):
        Planner(loose, "duckdb").plan(QueryRequest(metrics=["odd_3m"], dimensions=[MONTH]))


def test_evidence_records_the_months(graph):
    builder = EvidenceBuilder(graph)
    request = QueryRequest(metrics=["amount_3m", "amount_7d"], dimensions=[MONTH])
    time = builder.time("amount_3m", request)
    assert time["kind"] == "window" and time["window"] == "3 months"
    assert time["days"] is None and time["months"] == 3
    assert time["query_grain"] == "month" and time["filters_applied"] == "time_after"
    assert "months" not in builder.time("amount_7d", request)  # day windows keep their envelope
    assert (
        builder.formula("amount_3m") == "SUM(sales.amount) over the 3 months ending on each month"
    )
