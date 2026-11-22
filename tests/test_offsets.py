"""Inputs of derived metrics read at an earlier period (DEC-18): the exact numbers on a small
table built for the hard cases (a month without facts, December to January, month ends, a
null period), on every database available, and the refusals at validation and query time.
An input with an alias is read both shifted and unshifted in one expression (DEC-44)."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import re
from pathlib import Path

import pyarrow as pa
import pytest
import sqlglot
import yaml
from pydantic import ValidationError

from cosmos_semantic import demo_data
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ensure_read_only
from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.spec import Project, offset_grain_problem, parse_offset

MODEL = """
version: 1
semantic_models:
  - name: sales
    table: public.offset_sales
    entities: [{name: sale, type: primary, expr: id}]
    dimensions:
      - {name: sold_at, type: time}
      - {name: shipped_at, type: time, expr: sold_at}
      - {name: kind, type: categorical}
    measures:
      - {name: amount, agg: sum}
      - {name: lines, agg: count}
metrics:
  - {name: avg_amount, type: ratio, numerator: amount, denominator: lines}
  - name: amount_prev_month
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 1 month}]
  - name: amount_growth
    type: derived
    expr: "({amount} - {amount_prev_month}) / {amount_prev_month}"
  - name: amount_prev_quarter
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 3 months}]
  - name: amount_prev_year
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 12 months}]
  - name: amount_week_before
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 7 days}]
  - name: lines_day_before
    type: derived
    expr: "{lines}"
    inputs: [{metric: lines, offset: 1 day}]
  - name: avg_amount_prev_month
    type: derived
    expr: "{avg_amount}"
    inputs: [{metric: avg_amount, offset: 1 month}]
  - name: amount_running
    type: cumulative
    measure: amount
    grain_to_date: year
"""


def _graph(text: str = MODEL, strict: bool = True):
    return compile_project(Project.model_validate(yaml.safe_load(text)), strict=strict)


def _ts(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


# February 2025 has no rows; 31 December 23:30 and 1 January 00:00 sit on each side of the
# year change; one sale has no date.
ROWS = [
    (1, "2024-11-15 10:00", "a", 5.0),
    (2, "2024-12-31 23:30", "a", 10.0),
    (3, "2025-01-01 00:00", "a", 20.0),
    (4, "2025-01-08 08:00", "a", 3.0),
    (5, "2025-01-31 12:00", "b", 7.0),
    (6, "2025-03-31 09:00", "a", 40.0),
    (7, "2025-04-30 18:00", "a", 50.0),
    (8, None, "a", 100.0),
]
TABLE = pa.table(
    {
        "id": pa.array([r[0] for r in ROWS], pa.int64()),
        "sold_at": pa.array([_ts(r[1]) for r in ROWS], pa.timestamp("us")),
        "kind": pa.array([r[2] for r in ROWS], pa.string()),
        "amount": pa.array([r[3] for r in ROWS], pa.float64()),
    }
)
SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
    "bigquery_connector",
    "trino_connector",
]


def _norm(value):
    if isinstance(value, decimal.Decimal):
        value = float(value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _run(connector, metrics, dimensions, text: str = MODEL):
    compiled = Planner(_graph(text), connector.dialect, connector.config.schema_map).plan(
        QueryRequest(metrics=metrics, dimensions=dimensions)
    )
    columns = [c.name for c in compiled.columns]
    rows = [
        tuple(_norm(r[c]) for c in columns) for r in connector.execute(compiled.sql).to_pylist()
    ]
    return sorted(rows, key=lambda t: tuple((v is None, str(v)) for v in t))


@pytest.fixture(params=SOURCES)
def source(request):
    """The session's connector with the offset table, dropped afterwards: other tests list the
    demo tables, and the containers outlive the run."""
    connector = request.getfixturevalue(request.param)
    demo_data.load(connector, {"offset_sales": TABLE})
    yield connector
    schema = connector.config.schema_map.get("public", "public")
    drop = f"DROP TABLE IF EXISTS {schema}.offset_sales"
    for sql in sqlglot.transpile(drop, read="duckdb", write=connector.dialect):
        connector.run_script(sql)


MONTH = "sales.sold_at__month"


def test_month_offset_across_an_empty_month_and_a_year_change(source):
    """Rows are the months with facts: February is not output, and March, whose month before
    has no facts, gets NULL. January reads December across the year change; the null period
    never matches a period, so it gets NULL too."""
    rows = _run(source, ["amount", "amount_prev_month", "amount_growth"], [MONTH])
    assert rows == [
        ("2024-11-01", 5.0, None, None),
        ("2024-12-01", 10.0, 5.0, 1.0),
        ("2025-01-01", 30.0, 10.0, 2.0),
        ("2025-03-01", 40.0, None, None),
        ("2025-04-01", 50.0, 40.0, 0.25),
        (None, 100.0, None, None),
    ]


def test_month_offset_by_another_dimension_and_alone(source):
    """Each kind is shifted on its own; alone in the request, the offset input still gives the
    rows of the periods with facts."""
    rows = _run(source, ["amount_prev_month"], ["sales.kind", MONTH])
    assert rows == [
        ("a", "2024-11-01", None),
        ("a", "2024-12-01", 5.0),
        ("a", "2025-01-01", 10.0),
        ("a", "2025-03-01", None),
        ("a", "2025-04-01", 40.0),
        ("a", None, None),
        ("b", "2025-01-01", None),
    ]


def test_ratio_read_a_month_earlier(source):
    rows = _run(source, ["avg_amount", "avg_amount_prev_month"], [MONTH])
    assert rows == [
        ("2024-11-01", 5.0, None),
        ("2024-12-01", 10.0, 5.0),
        ("2025-01-01", 10.0, 10.0),
        ("2025-03-01", 40.0, None),
        ("2025-04-01", 50.0, 40.0),
        (None, 100.0, None),
    ]


def test_months_by_quarter_and_by_year(source):
    assert _run(source, ["amount", "amount_prev_quarter"], ["sales.sold_at__quarter"]) == [
        ("2024-10-01", 15.0, None),
        ("2025-01-01", 70.0, 15.0),
        ("2025-04-01", 50.0, 70.0),
        (None, 100.0, None),
    ]
    assert _run(source, ["amount", "amount_prev_year"], ["sales.sold_at__year"]) == [
        ("2024-01-01", 15.0, None),
        ("2025-01-01", 120.0, 15.0),
        (None, 100.0, None),
    ]


def test_day_offsets_by_day_and_by_week(source):
    """A count read a day earlier is NULL, not 0, when that day has no facts. By ISO week,
    31 December and 1 January share the week of 30 December."""
    assert _run(source, ["amount_week_before", "lines_day_before"], ["sales.sold_at__day"]) == [
        ("2024-11-15", None, None),
        ("2024-12-31", None, None),
        ("2025-01-01", None, 1),
        ("2025-01-08", 20.0, None),
        ("2025-01-31", None, None),
        ("2025-03-31", None, None),
        ("2025-04-30", None, None),
        (None, None, None),
    ]
    assert _run(source, ["amount", "amount_week_before"], ["sales.sold_at__week"]) == [
        ("2024-11-11", 5.0, None),
        ("2024-12-30", 30.0, None),
        ("2025-01-06", 3.0, 30.0),
        ("2025-01-27", 7.0, None),
        ("2025-03-31", 40.0, None),
        ("2025-04-28", 50.0, None),
        (None, 100.0, None),
    ]


def test_offset_alongside_a_running_total(source):
    rows = _run(source, ["amount_running", "amount_prev_month"], [MONTH])
    assert rows[:5] == [
        ("2024-11-01", 5.0, None),
        ("2024-12-01", 15.0, 5.0),
        ("2025-01-01", 30.0, 10.0),
        ("2025-03-01", 70.0, None),
        ("2025-04-01", 120.0, 40.0),
    ]


@pytest.mark.parametrize(
    ("metric", "dimensions", "message"),
    [
        (
            "amount_prev_month",
            ["sales.kind"],
            r"exactly one time dimension in the request \(none requested\)",
        ),
        ("amount_prev_month", [], r"exactly one time dimension in the request \(none requested\)"),
        (
            "amount_prev_month",
            [MONTH, "sales.shipped_at__month"],
            r"exactly one time dimension in the request \(2 requested\)",
        ),
        ("amount_prev_month", ["sales.sold_at__day"], "cannot be applied by day"),
        ("amount_prev_month", ["sales.sold_at__week"], "cannot be applied by week"),
        ("amount_prev_month", ["sales.sold_at__quarter"], "cannot be applied by quarter"),
        ("amount_prev_quarter", ["sales.sold_at__year"], "cannot be applied by year"),
        ("amount_prev_month", ["sales.sold_at"], "to a time dimension without a grain"),
        ("amount_growth", ["sales.sold_at__day"], "`amount_prev_month` reads `amount` earlier"),
        ("amount_week_before", [MONTH], "cannot be applied by month"),
        ("lines_day_before", ["sales.sold_at__week"], r"by week \(in multiples of 7 days\)"),
        ("lines_day_before", ["sales.sold_at__hour"], "cannot be applied by hour"),
    ],
)
def test_query_refusals(metric, dimensions, message):
    with pytest.raises(QueryError, match=message):
        Planner(_graph(), "duckdb").plan(QueryRequest(metrics=[metric], dimensions=dimensions))


def test_grain_fit():
    assert parse_offset("1 month") == (1, "month")
    assert parse_offset(" 12 Months ") == (12, "month")
    assert parse_offset("1 day") == parse_offset("1 days") == (1, "day")
    # Weeks are 7 days and years 12 months (DEC-46), singular and plural.
    assert parse_offset("1 week") == parse_offset("1 weeks") == (7, "day")
    assert parse_offset(" 2 Weeks ") == parse_offset("14 days") == (14, "day")
    assert parse_offset("1 year") == parse_offset("1 years") == (12, "month")
    assert parse_offset("2 years") == parse_offset("24 months") == (24, "month")
    for text in (
        "0 months",
        "0 weeks",
        "0 years",
        "1 quarter",
        "1 hour",
        "month",
        "year",
        "-1 day",
        "1.5 months",
        "1.5 years",
        "",
    ):
        assert parse_offset(text) is None, text
    # The grains each offset is a whole number of periods of; every other grain is refused.
    fits = {
        "1 month": {"month"},
        "2 months": {"month"},
        "3 months": {"month", "quarter"},
        "6 months": {"month", "quarter"},
        "12 months": {"month", "quarter", "year"},
        "1 day": {"day"},
        "3 days": {"day"},
        "7 days": {"day", "week"},
        "14 days": {"day", "week"},
        "1 week": {"day", "week"},
        "2 weeks": {"day", "week"},
        "1 year": {"month", "quarter", "year"},
        "2 years": {"month", "quarter", "year"},
    }
    for text, grains in fits.items():
        for grain in ("hour", "day", "week", "month", "quarter", "year", None):
            assert (offset_grain_problem(text, grain) is None) == (grain in grains), (text, grain)


INVALID = (
    MODEL
    + """
  - name: in_quarters
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 1 quarter}]
  - name: zero
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 0 months}]
  - name: unreferenced
    type: derived
    expr: "{amount}"
    inputs: [{metric: lines, offset: 1 month}]
  - name: twice
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 1 month}, {metric: amount, offset: 2 months}]
  - name: running_shifted
    type: derived
    expr: "{amount_running}"
    inputs: [{metric: amount_running, offset: 1 month}]
  - name: shifted_twice
    type: derived
    expr: "{amount_growth}"
    inputs: [{metric: amount_growth, offset: 1 month}]
  - name: days_and_months
    type: derived
    expr: "{amount} - {lines}"
    inputs: [{metric: amount, offset: 1 year}, {metric: lines, offset: 2 weeks}]
  - name: mixed_through_metrics
    type: derived
    expr: "{amount_prev_month} - {amount_week_before}"
  - name: above_the_mix
    type: derived
    expr: "{mixed_through_metrics} * 2"
"""
)


def test_validation_refuses_what_the_slice_does_not_cover():
    with pytest.raises(ModelError):
        _graph(INVALID)
    graph = _graph(INVALID, strict=False)
    errors = {}
    for issue in graph.issues:
        if issue.severity == "error":
            errors.setdefault(issue.location.split(".")[1], []).append(issue.message)
    assert set(errors) == {
        "in_quarters",
        "days_and_months",
        "mixed_through_metrics",
        "zero",
        "unreferenced",
        "twice",
        "running_shifted",
        "shifted_twice",
    }
    assert "offset `1 quarter` is not supported" in errors["in_quarters"][0]
    # Days (or weeks) and months (or years) under one metric: no grain answers it (DEC-46).
    # The error is on the metric where the mix appears, not on the metrics above it.
    assert errors["days_and_months"] == [
        "`days_and_months` reads inputs `2 weeks` and `1 year` earlier; an offset in days or "
        "weeks runs by day or week and one in months or years by month, quarter or year, so "
        "no grain can answer it"
    ]
    assert "reads inputs `7 days` and `1 month` earlier" in errors["mixed_through_metrics"][0]
    assert "offset `0 months` is not supported" in errors["zero"][0]
    assert errors["unreferenced"] == ["input `lines` is not referenced in `expr`"]
    assert errors["twice"] == [
        "`amount` is listed more than once in `inputs`; to read it at several periods, give "
        "each input of it an `alias` and read it in `expr` by that name"
    ]
    assert "uses the cumulative metric `amount_running`" in errors["running_shifted"][0]
    assert "which has offsets of its own" in errors["shifted_twice"][0]
    # The planner refuses them too, for graphs compiled without `strict`.
    for metric, message in [
        ("in_quarters", "offset `1 quarter` is not supported"),
        ("days_and_months", "an offset of `2 weeks` cannot be applied by month"),
        ("running_shifted", "uses the cumulative metric `amount_running`"),
        ("shifted_twice", "which has offsets of its own"),
    ]:
        with pytest.raises(QueryError, match=message):
            Planner(graph, "duckdb").plan(QueryRequest(metrics=[metric], dimensions=[MONTH]))


@pytest.mark.parametrize(
    ("dialect", "shifted"),
    [
        ("duckdb", "+ INTERVAL 1 MONTH"),
        ("postgres", "+ INTERVAL '1 MONTH'"),
        ("tsql", "DATEADD(MONTH, 1, "),
        ("mysql", "INTERVAL 1 MONTH)"),
        ("oracle", "+ NUMTOYMINTERVAL(1, 'MONTH')"),
        ("databricks", "DATEADD(MONTH, 1, "),
        ("snowflake", "DATEADD(MONTH, 1, "),
        ("trino", "DATE_ADD('MONTH', 1, "),
    ],
)
def test_sql_for_every_dialect(dialect, shifted):
    compiled = Planner(_graph(), dialect).plan(
        QueryRequest(metrics=["amount", "amount_growth"], dimensions=["sales.kind", MONTH])
    )
    ensure_read_only(compiled.sql, dialect)
    assert "sales_agg_offset_1_month" in compiled.sql
    assert shifted in compiled.sql


# -- The same input read shifted and unshifted, through an alias (DEC-44) ----------------------
ALIASED = (
    MODEL
    + """
  - name: amount_growth_inline
    type: derived
    expr: "({amount} - {amount_before}) / {amount_before}"
    inputs: [{metric: amount, offset: 1 month, alias: amount_before}]
  - name: month_minus_quarter_before
    type: derived
    expr: "COALESCE({amount_m1}, 0) - COALESCE({amount_m3}, 0)"
    inputs:
      - {metric: amount, offset: 1 month, alias: amount_m1}
      - {metric: amount, offset: 3 months, alias: amount_m3}
  - name: avg_amount_change
    type: derived
    expr: "{avg_amount} / {avg_amount_before}"
    inputs: [{metric: avg_amount, offset: 1 month, alias: avg_amount_before}]
  - name: lines_vs_week_before
    type: derived
    expr: "{lines} - {lines_week_before}"
    inputs: [{metric: lines, offset: 7 days, alias: lines_week_before}]
"""
)


def test_alias_reads_the_same_input_shifted_and_unshifted(source):
    """One metric gives the growth that needed a helper metric before: the same numbers as
    `amount_growth`, over `amount_prev_month`, across the empty February and the year change,
    and per kind."""
    rows = _run(source, ["amount", "amount_growth", "amount_growth_inline"], [MONTH], ALIASED)
    assert rows == [
        ("2024-11-01", 5.0, None, None),
        ("2024-12-01", 10.0, 1.0, 1.0),
        ("2025-01-01", 30.0, 2.0, 2.0),
        ("2025-03-01", 40.0, None, None),
        ("2025-04-01", 50.0, 0.25, 0.25),
        (None, 100.0, None, None),
    ]
    assert _run(source, ["amount_growth_inline"], ["sales.kind", MONTH], ALIASED) == [
        ("a", "2024-11-01", None),
        ("a", "2024-12-01", 1.0),
        ("a", "2025-01-01", 1.3),
        ("a", "2025-03-01", None),
        ("a", "2025-04-01", 0.25),
        ("a", None, None),
        ("b", "2025-01-01", None),
    ]


def test_two_aliases_of_one_input_at_two_offsets(source):
    """Two aliases of `amount`, 1 and 3 months earlier, in one expression: each is the value
    of the helper metric with the same offset."""
    metrics = ["amount_prev_month", "amount_prev_quarter", "month_minus_quarter_before"]
    assert _run(source, metrics, [MONTH], ALIASED) == [
        ("2024-11-01", None, None, 0),
        ("2024-12-01", 5.0, None, 5.0),
        ("2025-01-01", 10.0, None, 10.0),
        ("2025-03-01", None, 10.0, -10.0),
        ("2025-04-01", 40.0, 30.0, 10.0),
        (None, None, None, 0),
    ]


def test_aliases_of_a_ratio_and_of_a_count_by_day(source):
    assert _run(source, ["avg_amount", "avg_amount_change"], [MONTH], ALIASED) == [
        ("2024-11-01", 5.0, None),
        ("2024-12-01", 10.0, 2.0),
        ("2025-01-01", 10.0, 1.0),
        ("2025-03-01", 40.0, None),
        ("2025-04-01", 50.0, 1.25),
        (None, 100.0, None),
    ]
    # A day whose day a week before has no facts gets NULL, not the day's own count.
    assert _run(source, ["lines", "lines_vs_week_before"], ["sales.sold_at__day"], ALIASED) == [
        ("2024-11-15", 1, None),
        ("2024-12-31", 1, None),
        ("2025-01-01", 1, None),
        ("2025-01-08", 1, 0),
        ("2025-01-31", 1, None),
        ("2025-03-31", 1, None),
        ("2025-04-30", 1, None),
        (None, 1, None),
    ]


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "tsql", "mysql", "oracle"])
def test_alias_sql_reads_one_cte_per_offset(dialect):
    """The unshifted and the shifted read of `amount` come from the model's CTE and from its
    CTE at that offset, as with a helper metric; two offsets give two CTEs."""
    graph = _graph(ALIASED)
    inline = Planner(graph, dialect).plan(
        QueryRequest(metrics=["amount_growth_inline"], dimensions=[MONTH])
    )
    helper = Planner(graph, dialect).plan(
        QueryRequest(metrics=["amount_growth"], dimensions=[MONTH])
    )
    ctes = re.compile(r"\b(sales_agg\w*) AS \(")
    assert ctes.findall(inline.sql) == ctes.findall(helper.sql)
    assert ctes.findall(inline.sql) == ["sales_agg", "sales_agg_offset_1_month"]
    both = Planner(graph, dialect).plan(
        QueryRequest(metrics=["month_minus_quarter_before"], dimensions=[MONTH])
    )
    assert ctes.findall(both.sql) == ["sales_agg_offset_1_month", "sales_agg_offset_3_month"]


def test_alias_query_refusals():
    """The request checks name the metric an alias reads."""
    graph = _graph(ALIASED)
    with pytest.raises(QueryError, match="`amount_growth_inline` reads `amount` earlier"):
        Planner(graph, "duckdb").plan(
            QueryRequest(metrics=["amount_growth_inline"], dimensions=["sales.sold_at__day"])
        )
    with pytest.raises(QueryError, match=r"`amount_growth_inline` reads `amount` 1 month"):
        Planner(graph, "duckdb").plan(
            QueryRequest(metrics=["amount_growth_inline"], dimensions=["sales.kind"])
        )


def test_alias_in_evidence_formula_and_catalog():
    """The evidence names the alias of each shifted read (DEC-37, DEC-44); the formula and the
    catalog expand it to the metric it reads."""
    from cosmos_semantic.catalog import _metric_text
    from cosmos_semantic.evidence import EvidenceBuilder

    graph = _graph(ALIASED)
    builder = EvidenceBuilder(graph)
    request = QueryRequest(metrics=["amount_growth_inline"], dimensions=[MONTH])
    assert builder.time("amount_growth_inline", request)["shifts"] == [
        {
            "metric": "amount_growth_inline",
            "input": "amount",
            "offset": "1 month",
            "alias": "amount_before",
        }
    ]
    shifts = builder.time("month_minus_quarter_before", request)["shifts"]
    assert [(s["alias"], s["offset"]) for s in shifts] == [
        ("amount_m1", "1 month"),
        ("amount_m3", "3 months"),
    ]
    # Without an alias the entry is as it was.
    assert builder.time("amount_prev_month", request)["shifts"] == [
        {"metric": "amount_prev_month", "input": "amount", "offset": "1 month"}
    ]
    assert builder.formula("amount_growth_inline") == (
        "((SUM(sales.amount)) - (SUM(sales.amount)) 1 month earlier) / "
        "(SUM(sales.amount)) 1 month earlier"
    )
    assert _metric_text(graph.metrics["amount_growth_inline"]).endswith(
        ", with `amount` read 1 month earlier as `amount_before`"
    )


BAD_ALIASES = (
    MODEL
    + """
  - name: alias_is_a_metric
    type: derived
    expr: "{amount} - {avg_amount}"
    inputs: [{metric: amount, offset: 1 month, alias: avg_amount}]
  - name: alias_is_its_input
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 1 month, alias: amount}]
  - name: alias_twice
    type: derived
    expr: "{a1}"
    inputs:
      - {metric: amount, offset: 1 month, alias: a1}
      - {metric: lines, offset: 1 month, alias: a1}
  - name: alias_unused
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 1 month, alias: amount_before}]
  - name: alias_of_nothing
    type: derived
    expr: "{missing_before}"
    inputs: [{metric: missing, offset: 1 month, alias: missing_before}]
  - name: alias_bad_offset
    type: derived
    expr: "{amount} - {amount_before}"
    inputs: [{metric: amount, offset: 1 quarter, alias: amount_before}]
  - name: alias_of_a_running_total
    type: derived
    expr: "{running_before}"
    inputs: [{metric: amount_running, offset: 1 month, alias: running_before}]
"""
)


def test_alias_validation():
    graph = _graph(BAD_ALIASES, strict=False)
    errors = {}
    for issue in graph.issues:
        if issue.severity == "error":
            errors.setdefault(issue.location.split(".")[1], []).append(issue.message)
    assert errors == {
        "alias_is_a_metric": [
            "alias `avg_amount` is already the name of a measure or metric; choose another, so "
            "`{avg_amount}` in `expr` reads one thing"
        ],
        "alias_is_its_input": [
            "alias `amount` is already the name of a measure or metric; choose another, so "
            "`{amount}` in `expr` reads one thing"
        ],
        "alias_twice": ["`a1` names more than one input; use another `alias`"],
        "alias_unused": ["alias `amount_before` (input `amount`) is not referenced in `expr`"],
        "alias_of_nothing": ["reference `missing` is neither a measure nor a metric"],
        "alias_bad_offset": [
            "offset `1 quarter` is not supported; use a whole number of days, weeks, months or "
            "years, such as `7 days`, `2 weeks`, `1 month` or `1 year`"
        ],
        "alias_of_a_running_total": [
            "`amount_running` is read with an offset, and it uses the cumulative metric "
            "`amount_running`; offsets of cumulative metrics are not supported"
        ],
    }
    with pytest.raises(ModelError, match="alias `avg_amount` is already the name"):
        _graph(BAD_ALIASES)
    # The planner refuses what it cannot shift, for graphs compiled without `strict`.
    with pytest.raises(QueryError, match="uses the cumulative metric `amount_running`"):
        Planner(graph, "duckdb").plan(
            QueryRequest(metrics=["alias_of_a_running_total"], dimensions=[MONTH])
        )


def test_aliases_in_weeks_and_years():
    """Aliases take the DEC-46 units: an aliased `1 week` and an aliased `1 year` of the same
    metric mix days and months, which no grain answers, and are refused; an aliased `1 year`
    alone validates and reads `amount` 12 months earlier."""
    mixed = (
        MODEL
        + """
  - name: week_and_year_before
    type: derived
    expr: "{amount} - {amount_week_before_a} - {amount_year_before_a}"
    inputs:
      - {metric: amount, offset: 1 week, alias: amount_week_before_a}
      - {metric: amount, offset: 1 year, alias: amount_year_before_a}
"""
    )
    graph = _graph(mixed, strict=False)
    errors = [i.message for i in graph.issues if i.location == "metrics.week_and_year_before"]
    assert errors == [
        "`week_and_year_before` reads inputs `1 week` and `1 year` earlier; an offset in days "
        "or weeks runs by day or week and one in months or years by month, quarter or year, "
        "so no grain can answer it"
    ]
    with pytest.raises(ModelError, match="no grain can answer it"):
        _graph(mixed)

    yearly = (
        MODEL
        + """
  - name: amount_yoy
    type: derived
    expr: "{amount} - {amount_year_before_a}"
    inputs: [{metric: amount, offset: 1 year, alias: amount_year_before_a}]
"""
    )
    graph = _graph(yearly)
    assert not [i for i in graph.issues if i.severity == "error"]
    compiled = Planner(graph, "duckdb").plan(
        QueryRequest(metrics=["amount_yoy"], dimensions=["sales.sold_at__year"])
    )
    assert "sales_agg_offset_12_month" in compiled.sql


@pytest.mark.parametrize("alias", ["Revenue_Prev", "revenue-prev", "1st", "a" * 65, ""])
def test_alias_must_be_a_name(alias):
    data = yaml.safe_load(MODEL)
    data["metrics"].append(
        {
            "name": "bad",
            "type": "derived",
            "expr": "{amount}",
            "inputs": [{"metric": "amount", "offset": "1 month", "alias": alias}],
        }
    )
    with pytest.raises(ValidationError, match="alias"):
        Project.model_validate(data)


def test_no_alias_keeps_the_model_hash():
    """An input without an alias dumps as before DEC-44, so the hash of an existing model, and
    the evidence and replays that record it, do not change."""
    dumped = _graph().project.model_dump(mode="json")
    inputs = [i for m in dumped["metrics"] for i in m.get("inputs", [])]
    assert inputs and all(set(i) == {"metric", "offset"} for i in inputs)
    aliased = _graph(ALIASED).project.model_dump(mode="json")
    growth = next(m for m in aliased["metrics"] if m["name"] == "amount_growth_inline")
    assert growth["inputs"] == [{"metric": "amount", "offset": "1 month", "alias": "amount_before"}]


# -- The SQL of existing offset requests does not change (DEC-44) ------------------------------
# sha256 of the SQL each dialect gets for the requests below (or the refusal), as recorded on
# main before DEC-44. A planner change that alters them on purpose rewrites the file with
# `COSMOS_UPDATE_PINNED_SQL=1 uv run pytest tests/test_offsets.py -k pinned`.
PINNED = Path(__file__).parent / "fixtures" / "offset_sql_hashes.json"
PIN_DIALECTS = (
    "duckdb",
    "postgres",
    "tsql",
    "mysql",
    "oracle",
    "databricks",
    "snowflake",
    "bigquery",
)
PIN_AS_OF = dt.date(2026, 11, 21)


def _pinned_requests() -> dict[str, tuple]:
    from cosmos_semantic.loader import load_project
    from cosmos_semantic.reference import load_cases

    from .conftest import EXAMPLE, ROOT

    example = compile_project(load_project(EXAMPLE))
    out: dict[str, tuple] = {}
    for case in load_cases(ROOT / "reference" / "queries_advanced.yml"):
        if case.id in ("A35", "A36", "A37", "A38"):
            request = case.request.model_copy(update={"as_of": PIN_AS_OF})
            principal = Principal(user=case.user, channel=case.channel)
            out[f"reference/queries_advanced.yml:{case.id}"] = (example, request, principal)
    shifted = {
        "ecommerce": (
            example,
            ["revenue_prev_month", "revenue_mom_growth", "order_count_7_days_earlier"],
            ["orders.ordered_at"],
        ),
        "offsets": (
            _graph(),
            [
                "amount_prev_month",
                "amount_growth",
                "amount_prev_quarter",
                "amount_prev_year",
                "amount_week_before",
                "lines_day_before",
                "avg_amount_prev_month",
            ],
            ["sales.sold_at", "sales.shipped_at"],
        ),
    }
    for name, (graph, metrics, times) in shifted.items():
        for metric in metrics:
            for time in times:
                for grain in ("day", "week", "month", "quarter", "year"):
                    dim = f"{time}__{grain}"
                    request = QueryRequest(metrics=[metric], dimensions=[dim], as_of=PIN_AS_OF)
                    out[f"{name}:{metric}:{dim}"] = (graph, request, Principal())
    return out


def _pinned_hashes() -> dict[str, str]:
    from cosmos_semantic.errors import CosmosError
    from cosmos_semantic.evidence import sql_hash

    hashes = {}
    for key, (graph, request, principal) in _pinned_requests().items():
        for dialect in PIN_DIALECTS:
            try:
                sql = Planner(graph, dialect).plan(request, principal).sql
            except CosmosError as exc:
                hashes[f"{key}|{dialect}"] = f"refused: {exc}"
            else:
                hashes[f"{key}|{dialect}"] = sql_hash(sql)
    return hashes


def test_pinned_sql_of_existing_offset_requests():
    hashes = _pinned_hashes()
    if os.environ.get("COSMOS_UPDATE_PINNED_SQL"):
        PINNED.write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))
    assert sum(not v.startswith("refused") for v in pinned.values()) >= 100
    changed = sorted(k for k in pinned if hashes.get(k) != pinned[k])
    assert not changed, f"{len(changed)} request-dialect pairs changed, such as {changed[:3]}"
