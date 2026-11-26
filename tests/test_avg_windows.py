"""Rolling windows over an average (DEC-60).

A rolling window over an `avg` measure is recomputed over each window from the facts, as the
window's sum over its count of non-null values, never as an average of daily averages. NULL
values count in neither, as in SQL `AVG`, and a window without a non-null value is NULL.
Reference cases A70 to A72 (tests/test_reference.py) check the numbers against hand-written SQL
on every database; these tests check the semantics on a small table, the SQL on every dialect,
the evidence, and that the exports and imports keep refusing such a window.
"""

from __future__ import annotations

import datetime as dt
import re

import duckdb
import pytest
import yaml

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ensure_read_only
from cosmos_semantic.errors import MetricLossError
from cosmos_semantic.evidence import EvidenceBuilder, sql_hash
from cosmos_semantic.interchange.cube import export_cube
from cosmos_semantic.interchange.dbt import export_dbt
from cosmos_semantic.planner import Planner, QueryRequest
from cosmos_semantic.spec import Project

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
      - {name: mean_amount, agg: avg, expr: amount}
      - {name: mean_units, agg: avg, expr: units}
metrics:
  - {name: mean_7d, type: cumulative, measure: mean_amount, window: 7 days}
  - {name: mean_1w, type: cumulative, measure: mean_amount, window: 1 week}
  - {name: units_7d, type: cumulative, measure: mean_units, window: 7 days}
  - {name: amount_7d, type: cumulative, measure: amount, window: 7 days}
"""

# (id, sold_at, kind, amount, units). 1 March has one sale of 10, 2 March three sales of 40:
# the 2-day window's average is (10 + 3 * 40) / 4 = 32.5, the average of the daily averages
# (10 + 40) / 2 = 25. On 5 March and 20 March every amount is NULL; the 20th is more than 7
# days after any amount, so its window has no non-null value. Units are integers whose
# averages are not whole numbers. One sale has no date and one no kind.
ROWS = [
    (1, "2026-03-01 10:00:00", "a", 10, 1),
    (2, "2026-03-02 09:00:00", "a", 40, 1),
    (3, "2026-03-02 11:00:00", "a", 40, 2),
    (4, "2026-03-02 15:00:00", None, 40, 2),
    (5, "2026-03-05 08:00:00", "a", None, None),
    (6, "2026-03-05 18:00:00", "b", None, 3),
    (7, "2026-03-20 12:00:00", "a", None, 1),
    (8, None, "a", 80, 1),
]
DAY = "sales.sold_at__day"


def _project(text: str = MODEL) -> Project:
    return Project.model_validate(yaml.safe_load(text))


@pytest.fixture(scope="module")
def graph():
    return compile_project(_project())


@pytest.fixture(scope="module")
def con():
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t_sales (id INTEGER, sold_at TIMESTAMP, kind VARCHAR, amount INTEGER, "
        "units INTEGER)"
    )
    con.executemany("INSERT INTO t_sales VALUES (?, ?, ?, ?, ?)", ROWS)
    yield con
    con.close()


def _run(graph, con, metrics, dimensions, filters=()):
    request = QueryRequest(metrics=metrics, dimensions=dimensions, filters=list(filters))
    sql = Planner(graph, "duckdb").plan(request).sql
    cursor = con.execute(sql)
    names = [c[0] for c in cursor.description]
    out = {}
    for row in cursor.fetchall():
        record = dict(zip(names, row, strict=True))
        day = record.pop("sales__sold_at__day")
        key = (record.pop("sales__kind"), day) if "sales.kind" in dimensions else day
        out[key] = record
    return out


def _day(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def test_the_window_averages_the_facts_not_the_daily_averages(graph, con):
    rows = _run(graph, con, ["mean_7d", "mean_amount"], [DAY])
    window = {k: v["mean_7d"] for k, v in rows.items()}
    assert window == {
        _day("2026-03-01"): 10.0,
        _day("2026-03-02"): 32.5,  # (10 + 40 + 40 + 40) / 4
        _day("2026-03-05"): 32.5,  # its own amounts are NULL; the 1st and 2nd are in its window
        _day("2026-03-20"): None,  # no non-null amount in its window: NULL, not 0
        None: None,  # a sale without a date: no day is within 7 days of an unknown day
    }
    # The average of the daily averages would weigh 1 March's single sale as much as the
    # three sales of the 2nd.
    daily = {k: v["mean_amount"] for k, v in rows.items()}
    assert (daily[_day("2026-03-01")] + daily[_day("2026-03-02")]) / 2 == 25.0
    assert daily[_day("2026-03-05")] is None


def test_null_values_count_in_neither_the_sum_nor_the_count(graph, con):
    """As SQL AVG: a NULL is not a zero, so it does not pull the average down, and the
    window's count only has the non-null values."""
    rows = _run(graph, con, ["mean_7d"], [DAY], ["{sales.sold_at} >= DATE '2026-03-05'"])
    assert {k: v["mean_7d"] for k, v in rows.items()} == {
        _day("2026-03-05"): 32.5,
        _day("2026-03-20"): None,
    }
    (expected,) = con.execute(
        "SELECT AVG(amount) FROM t_sales WHERE sold_at < TIMESTAMP '2026-03-06'"
    ).fetchone()
    assert expected == 32.5


def test_integer_columns_are_not_divided_as_integers(graph, con):
    rows = _run(graph, con, ["units_7d"], [DAY])
    assert rows[_day("2026-03-02")]["units_7d"] == pytest.approx(6 / 4)
    assert rows[_day("2026-03-05")]["units_7d"] == pytest.approx(9 / 5)
    assert rows[_day("2026-03-20")]["units_7d"] == 1.0


def test_each_combination_of_the_other_dimensions_has_its_own_window(graph, con):
    rows = _run(graph, con, ["mean_7d"], ["sales.kind", DAY])
    assert {k: v["mean_7d"] for k, v in rows.items()} == {
        ("a", _day("2026-03-01")): 10.0,
        ("a", _day("2026-03-02")): 30.0,  # (10 + 40 + 40) / 3: the sale with no kind is apart
        ("a", _day("2026-03-05")): 30.0,
        ("a", _day("2026-03-20")): None,
        ("a", None): None,
        ("b", _day("2026-03-05")): None,
        (None, _day("2026-03-02")): 40.0,  # a null kind is a group of its own
    }


def test_a_date_filter_chooses_the_days_and_does_not_cut_the_window(graph, con):
    """DEC-34: the filter picks the output days; each day's window still reads the 6 days
    before it. A filter shape that cannot be split still restricts the facts first."""
    after = _run(graph, con, ["mean_7d"], [DAY], ["{sales.sold_at} >= DATE '2026-03-02'"])
    assert after[_day("2026-03-02")]["mean_7d"] == 32.5
    before = _run(graph, con, ["mean_7d"], [DAY], ["NOT ({sales.sold_at} < DATE '2026-03-02')"])
    assert before[_day("2026-03-02")]["mean_7d"] == 40.0  # the 2nd's own sales only


def test_other_filters_restrict_the_facts_first(graph, con):
    rows = _run(graph, con, ["mean_7d"], [DAY], ["{sales.kind} = 'a'"])
    assert rows[_day("2026-03-02")]["mean_7d"] == 30.0  # not 32.5: the sale with no kind is out


def test_sum_windows_beside_an_avg_window_keep_their_numbers(graph, con):
    rows = _run(graph, con, ["amount_7d", "mean_7d"], [DAY])
    assert rows[_day("2026-03-02")] == {"amount_7d": 130, "mean_7d": 32.5}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("filters", "facts"),
    [
        ([], "MEAN_AMOUNT_FACTS"),
        (["{sales.sold_at} >= DATE '2026-01-01'"], "MEAN_AMOUNT_FACTS_LOOKBACK_7_DAYS"),
        (["NOT ({sales.sold_at} < DATE '2026-01-01')"], "MEAN_AMOUNT_FACTS"),
    ],
    ids=["unfiltered", "lookback", "filtered-first"],
)
def test_avg_window_sql_for_every_dialect(graph, dialect, filters, facts):
    request = QueryRequest(
        metrics=["mean_7d", "amount_7d", "mean_amount"],
        dimensions=["sales.kind", DAY],
        filters=filters,
    )
    sql = Planner(graph, dialect).plan(request).sql
    ensure_read_only(sql, dialect)
    upper = sql.upper().replace("`", "").replace('"', "")
    assert "OVER" not in upper  # a range join, not a window frame
    assert "COUNT(SALES.AMOUNT) AS MEAN_AMOUNT_COUNT" in upper
    assert "SUM(PREV.MEAN_AMOUNT_SUM)" in upper
    assert "NULLIF(SUM(PREV.MEAN_AMOUNT_COUNT), 0)" in upper
    assert "AVG(PREV" not in upper  # never an average of daily averages
    assert "SUM(PREV.AMOUNT)" in upper
    assert re.search(rf"JOIN {facts}( AS)? PREV\b", upper)  # Oracle writes no AS


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "filters",
    [[], ["{sales.sold_at} >= DATE '2026-01-01'"]],
    ids=["unfiltered", "lookback"],
)
def test_an_avg_window_in_weeks_has_the_sql_of_its_days(dialect, filters):
    """`1 week` compiles to the SQL, and so the `sql_hash`, of `7 days` (DEC-27)."""
    weeks = compile_project(_project())
    days = compile_project(_project(MODEL.replace("window: 1 week", "window: 7 days")))
    request = QueryRequest(metrics=["mean_1w"], dimensions=[DAY], filters=filters)
    written = Planner(weeks, dialect).plan(request).sql
    same = Planner(days, dialect).plan(request).sql
    assert written == same
    assert sql_hash(written) == sql_hash(same)


def test_evidence_says_the_average_is_recomputed_from_the_facts(graph):
    builder = EvidenceBuilder(graph)
    request = QueryRequest(metrics=["mean_7d", "amount_7d"], dimensions=[DAY])
    time = builder.time("mean_7d", request)
    assert time["kind"] == "window" and time["days"] == 7 and time["from_facts"] is True
    assert time["filters_applied"] == "time_after"
    assert "from_facts" not in builder.time("amount_7d", request)
    assert builder.formula("mean_7d") == (
        "SUM(sales.amount) / COUNT(sales.amount) recomputed over the 7 days ending on each day"
    )
    assert builder.formula("mean_1w").endswith("recomputed over the 1 week ending on each day")
    # The measure itself keeps its formula.
    assert builder.formula("mean_amount") == "AVG(sales.amount)"


def test_an_avg_window_cannot_be_part_of_another_metric():
    text = MODEL + '  - {name: mean_7d_doubled, type: derived, expr: "{mean_7d} * 2"}\n'
    with pytest.raises(Exception, match="cannot be part of another metric"):
        Planner(compile_project(_project(text)), "duckdb").plan(
            QueryRequest(metrics=["mean_7d_doubled"], dimensions=[DAY])
        )


@pytest.mark.parametrize("export", [export_dbt, export_cube], ids=["dbt", "cube"])
def test_exports_refuse_an_avg_window(export):
    """The interchange formats keep refusing it until their numbers are checked."""
    with pytest.raises(MetricLossError, match="mean_7d") as caught:
        export(_project())
    message = str(caught.value)
    assert "DEC-60" in message
    assert "`avg`" in message
    assert "amount_7d" not in message  # a sum window is still exported
    result = export(_project(), allow_metric_loss=True)
    assert "mean_7d" not in result.text
    assert any(w.startswith("metrics.mean_7d: ") and "DEC-60" in w for w in result.warnings)
