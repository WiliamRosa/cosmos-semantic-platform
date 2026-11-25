"""Rolling windows over a distinct count (DEC-49).

A rolling window over a `count_distinct` measure is recomputed over each window from the facts,
not added up from daily values: a customer seen on several days of the window counts once.
Reference cases A56 to A58 (tests/test_reference.py) check the numbers against hand-written SQL
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
from cosmos_semantic.interchange import import_model
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
      - {name: buyers, agg: count_distinct, expr: buyer_id}
metrics:
  - {name: buyers_7d, type: cumulative, measure: buyers, window: 7 days}
  - {name: buyers_1w, type: cumulative, measure: buyers, window: 1 week}
  - {name: amount_7d, type: cumulative, measure: amount, window: 7 days}
"""

# (id, sold_at, kind, buyer_id, amount). Buyer 1 buys on 1, 2 and 3 March; buyer 2 on 1 and
# 10 March; one sale has no date and one no kind.
ROWS = [
    (1, "2026-03-01 10:00:00", "a", 1, 10),
    (2, "2026-03-01 15:00:00", "a", 2, 20),
    (3, "2026-03-02 09:00:00", "a", 1, 30),
    (4, "2026-03-02 11:00:00", None, 5, 40),
    (5, "2026-03-03 08:00:00", "a", 1, 50),
    (6, "2026-03-03 18:00:00", "b", 3, 60),
    (7, "2026-03-10 12:00:00", "a", 2, 70),
    (8, None, "a", 4, 80),
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
        "CREATE TABLE t_sales (id INTEGER, sold_at TIMESTAMP, kind VARCHAR, buyer_id INTEGER, "
        "amount INTEGER)"
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


def test_the_window_counts_each_customer_once(graph, con):
    rows = _run(graph, con, ["buyers_7d", "buyers"], [DAY])
    window = {k: v["buyers_7d"] for k, v in rows.items()}
    assert window == {
        _day("2026-03-01"): 2,  # buyers 1 and 2
        _day("2026-03-02"): 3,  # 1, 2 and 5: buyer 1 again on the 2nd counts once
        _day("2026-03-03"): 4,  # 1, 2, 3 and 5
        _day("2026-03-10"): 1,  # 4 to 10 March: buyer 2 only
        None: None,  # a sale without a date: no day is within 7 days of an unknown day
    }
    # Summing the daily distinct counts over the window counts buyer 1 once per day.
    daily = {k: v["buyers"] for k, v in rows.items() if k is not None}
    summed = {
        day: sum(n for d, n in daily.items() if day - dt.timedelta(days=6) <= d <= day)
        for day in daily
    }
    assert summed[_day("2026-03-03")] == 6
    assert summed != {k: v for k, v in window.items() if k is not None}


def test_each_combination_of_the_other_dimensions_has_its_own_window(graph, con):
    rows = _run(graph, con, ["buyers_7d"], ["sales.kind", DAY])
    assert {k: v["buyers_7d"] for k, v in rows.items()} == {
        ("a", _day("2026-03-01")): 2,
        ("a", _day("2026-03-02")): 2,
        ("a", _day("2026-03-03")): 2,
        ("a", _day("2026-03-10")): 1,
        ("a", None): None,
        ("b", _day("2026-03-03")): 1,
        (None, _day("2026-03-02")): 1,  # a null kind is a group of its own
    }


def test_a_date_filter_chooses_the_days_and_does_not_cut_the_window(graph, con):
    """DEC-34: the filter picks the output days; each day's window still reads the 6 days
    before it. A filter shape that cannot be split still restricts the facts first."""
    after = _run(graph, con, ["buyers_7d"], [DAY], ["{sales.sold_at} >= DATE '2026-03-03'"])
    assert {k: v["buyers_7d"] for k, v in after.items()} == {
        _day("2026-03-03"): 4,
        _day("2026-03-10"): 1,
    }
    before = _run(graph, con, ["buyers_7d"], [DAY], ["NOT ({sales.sold_at} < DATE '2026-03-03')"])
    assert before[_day("2026-03-03")]["buyers_7d"] == 2  # buyers 1 and 3 of the 3rd only


def test_other_filters_restrict_the_facts_first(graph, con):
    rows = _run(graph, con, ["buyers_7d"], [DAY], ["{sales.kind} = 'a'"])
    assert rows[_day("2026-03-03")]["buyers_7d"] == 2  # buyers 1 and 2; not 3 (b) nor 5


def test_sum_windows_beside_a_distinct_window_keep_their_numbers(graph, con):
    rows = _run(graph, con, ["amount_7d", "buyers_7d"], [DAY])
    assert rows[_day("2026-03-03")] == {"amount_7d": 210, "buyers_7d": 4}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("filters", "facts"),
    [
        ([], "BUYERS_FACTS"),
        (["{sales.sold_at} >= DATE '2026-01-01'"], "BUYERS_FACTS_LOOKBACK_7_DAYS"),
        (["NOT ({sales.sold_at} < DATE '2026-01-01')"], "BUYERS_FACTS"),
    ],
    ids=["unfiltered", "lookback", "filtered-first"],
)
def test_distinct_window_sql_for_every_dialect(graph, dialect, filters, facts):
    request = QueryRequest(
        metrics=["buyers_7d", "amount_7d", "buyers"],
        dimensions=["sales.kind", DAY],
        filters=filters,
    )
    sql = Planner(graph, dialect).plan(request).sql
    ensure_read_only(sql, dialect)
    upper = sql.upper().replace("`", "").replace('"', "")
    assert "OVER" not in upper  # a range join, not a window frame
    assert "COUNT(DISTINCT PREV.BUYERS)" in upper
    assert "SUM(PREV.AMOUNT)" in upper
    assert re.search(rf"JOIN {facts}( AS)? PREV\b", upper)  # Oracle writes no AS


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "filters",
    [[], ["{sales.sold_at} >= DATE '2026-01-01'"]],
    ids=["unfiltered", "lookback"],
)
def test_a_distinct_window_in_weeks_has_the_sql_of_its_days(dialect, filters):
    """`1 week` compiles to the SQL, and so the `sql_hash`, of `7 days` (DEC-27)."""
    weeks = compile_project(_project())
    days = compile_project(_project(MODEL.replace("window: 1 week", "window: 7 days")))
    request = QueryRequest(metrics=["buyers_1w"], dimensions=[DAY], filters=filters)
    written = Planner(weeks, dialect).plan(request).sql
    same = Planner(days, dialect).plan(request).sql
    assert written == same
    assert sql_hash(written) == sql_hash(same)


def test_evidence_says_the_window_is_recomputed_from_the_facts(graph):
    builder = EvidenceBuilder(graph)
    request = QueryRequest(metrics=["buyers_7d", "amount_7d"], dimensions=[DAY])
    time = builder.time("buyers_7d", request)
    assert time["kind"] == "window" and time["days"] == 7 and time["from_facts"] is True
    assert time["filters_applied"] == "time_after"
    # A window combined from daily values keeps the envelope it had.
    assert "from_facts" not in builder.time("amount_7d", request)
    assert builder.formula("buyers_7d") == (
        "COUNT(DISTINCT sales.buyer_id) recomputed over the 7 days ending on each day"
    )
    assert builder.formula("buyers_1w").endswith("recomputed over the 1 week ending on each day")


def test_a_distinct_window_cannot_be_part_of_another_metric():
    text = MODEL + '  - {name: buyers_7d_doubled, type: derived, expr: "{buyers_7d} * 2"}\n'
    with pytest.raises(Exception, match="cannot be part of another metric"):
        Planner(compile_project(_project(text)), "duckdb").plan(
            QueryRequest(metrics=["buyers_7d_doubled"], dimensions=[DAY])
        )


# -- Interchange: the exports and imports keep refusing it until checked -------------------


@pytest.mark.parametrize("export", [export_dbt, export_cube], ids=["dbt", "cube"])
def test_exports_refuse_a_distinct_window(export):
    with pytest.raises(MetricLossError, match="buyers_7d") as caught:
        export(_project())
    message = str(caught.value)
    assert "DEC-49" in message
    assert "`count_distinct`" in message
    assert "amount_7d" not in message  # a sum window is still exported
    result = export(_project(), allow_metric_loss=True)
    assert "buyers_7d" not in result.text
    assert any(w.startswith("metrics.buyers_7d: ") and "DEC-49" in w for w in result.warnings)


def test_dbt_import_skips_a_distinct_window(tmp_path):
    doc = {
        "semantic_models": [
            {
                "name": "orders",
                "model": "ref('orders')",
                "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                "dimensions": [{"name": "ordered_at", "type": "time"}],
                "measures": [
                    {"name": "revenue", "agg": "sum", "expr": "amount"},
                    {"name": "buyers", "agg": "count_distinct", "expr": "customer_id"},
                ],
            }
        ],
        "metrics": [
            {
                "name": "revenue_7d",
                "type": "cumulative",
                "type_params": {"measure": "revenue", "window": "7 days"},
            },
            {
                "name": "buyers_7d",
                "type": "cumulative",
                "type_params": {"measure": "buyers", "window": "7 days"},
            },
        ],
    }
    path = tmp_path / "dbt" / "models.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    result = import_model("dbt", path.parent, schema="public")
    assert {m.name for m in result.project.metrics} == {"revenue_7d"}
    skipped = [w for w in result.warnings if "DEC-49" in w]
    assert len(skipped) == 1
    assert skipped[0].startswith("metrics.buyers_7d: over measure `buyers`: ")
    assert "recomputed from the facts of each window in Cosmos (DEC-49)" in skipped[0]
    assert skipped[0].endswith("metric skipped")
