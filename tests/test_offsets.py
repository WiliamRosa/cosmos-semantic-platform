"""Inputs of derived metrics read at an earlier period (DEC-18): the exact numbers on a small
table built for the hard cases (a month without facts, December to January, month ends, a
null period), on every database available, and the refusals at validation and query time."""

from __future__ import annotations

import datetime as dt
import decimal

import pyarrow as pa
import pytest
import sqlglot
import yaml

from cosmos_semantic import demo_data
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ensure_read_only
from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.interchange import import_model
from cosmos_semantic.interchange.dbt import export_dbt
from cosmos_semantic.planner import Planner, QueryRequest
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


def _run(connector, metrics, dimensions):
    compiled = Planner(_graph(), connector.dialect, connector.config.schema_map).plan(
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
    for text in ("0 months", "2 weeks", "1 year", "month", "-1 day", "1.5 months", ""):
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
    }
    for text, grains in fits.items():
        for grain in ("hour", "day", "week", "month", "quarter", "year", None):
            assert (offset_grain_problem(text, grain) is None) == (grain in grains), (text, grain)


INVALID = (
    MODEL
    + """
  - name: in_weeks
    type: derived
    expr: "{amount}"
    inputs: [{metric: amount, offset: 2 weeks}]
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
        "in_weeks",
        "zero",
        "unreferenced",
        "twice",
        "running_shifted",
        "shifted_twice",
    }
    assert "offset `2 weeks` is not supported" in errors["in_weeks"][0]
    assert "offset `0 months` is not supported" in errors["zero"][0]
    assert errors["unreferenced"] == ["input `lines` is not referenced in `expr`"]
    assert "`amount` is listed more than once in `inputs`" in errors["twice"]
    assert "uses the cumulative metric `amount_running`" in errors["running_shifted"][0]
    assert "which has offsets of its own" in errors["shifted_twice"][0]
    # The planner refuses them too, for graphs compiled without `strict`.
    for metric, message in [
        ("in_weeks", "offset `2 weeks` is not supported"),
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
    ],
)
def test_sql_for_every_dialect(dialect, shifted):
    compiled = Planner(_graph(), dialect).plan(
        QueryRequest(metrics=["amount", "amount_growth"], dimensions=["sales.kind", MONTH])
    )
    ensure_read_only(compiled.sql, dialect)
    assert "sales_agg_offset_1_month" in compiled.sql
    assert shifted in compiled.sql


# -- dbt export and import as `offset_window` (DEC-36) ------------------------------------------

SHIFTED = [
    "amount_prev_month",
    "amount_growth",
    "amount_prev_quarter",
    "amount_prev_year",
    "amount_week_before",
    "lines_day_before",
    "avg_amount_prev_month",
]


def _dbt_round_trip(tmp_path):
    exported = export_dbt(_graph().project, allow_metric_loss=True)
    path = tmp_path / "dbt" / "semantic_models.yml"
    path.parent.mkdir()
    path.write_text(exported.text, encoding="utf-8")
    return exported, import_model("dbt", path, schema="public")


def test_dbt_export_writes_offset_window(tmp_path):
    """Months are written in the largest unit they are a whole number of, so MetricFlow
    shifts the aggregated period at every grain the engine accepts; the import reads them
    back as the same offsets."""
    exported, back = _dbt_round_trip(tmp_path)
    metrics = {m["name"]: m for m in yaml.safe_load(exported.text)["metrics"]}
    windows = {
        name: metrics[name]["type_params"]["metrics"] for name in SHIFTED if name != "amount_growth"
    }
    assert windows == {
        "amount_prev_month": [{"name": "amount", "offset_window": "1 month"}],
        "amount_prev_quarter": [{"name": "amount", "offset_window": "1 quarter"}],
        "amount_prev_year": [{"name": "amount", "offset_window": "1 year"}],
        "amount_week_before": [{"name": "amount", "offset_window": "7 days"}],
        "lines_day_before": [{"name": "lines", "offset_window": "1 day"}],
        "avg_amount_prev_month": [{"name": "avg_amount", "offset_window": "1 month"}],
    }
    assert "amount_running" not in metrics  # running totals wait for a later slice
    native = {m.name: m for m in _graph().project.metrics}
    for metric in back.project.metrics:
        if metric.name in SHIFTED:
            assert metric.offsets() == native[metric.name].offsets(), metric.name
    warning = next(w for w in exported.warnings if w.startswith("metrics.amount_prev_quarter:"))
    assert warning.startswith(
        "metrics.amount_prev_quarter: `amount` read 3 months earlier is `offset_window: 1 "
        "quarter` over MetricFlow's `metric_time`, which is `sales.sold_at`; by that dimension "
        "(by month, by quarter in multiples of 3 months) the numbers are the engine's"
    )
    assert warning.endswith(
        "; queried by `sales.shipped_at`, the engine shifts that dimension instead, which "
        "MetricFlow does not"
    )
    # Read back from `1 quarter` and `1 year`, the import has nothing to add about grains.
    assert not any("shifts each day before grouping" in w for w in back.warnings)


def test_dbt_export_offsets_same_numbers(source, tmp_path):
    """Exported to dbt and imported back, every metric that reads an input at an earlier
    period returns the native numbers at every grain the engine accepts, with another
    dimension and on the second time dimension, and both refuse the same queries."""
    native = _graph()
    _, back = _dbt_round_trip(tmp_path)
    other = compile_project(back.project)

    def run(graph, metrics, dimensions):
        compiled = Planner(graph, source.dialect, source.config.schema_map).plan(
            QueryRequest(metrics=metrics, dimensions=dimensions)
        )
        columns = [c.name for c in compiled.columns]
        out = source.execute(compiled.sql).to_pylist()
        rows = [tuple(_norm(r[c]) for c in columns) for r in out]
        return sorted(rows, key=lambda t: tuple((v is None, str(v)) for v in t))

    compared = refused = 0
    for name in SHIFTED:
        for grain in ("day", "week", "month", "quarter", "year"):
            for dims in (
                [f"sales.sold_at__{grain}"],
                ["sales.kind", f"sales.sold_at__{grain}"],
                [f"sales.shipped_at__{grain}"],
            ):
                metrics = ["amount", name]
                try:
                    want = run(native, metrics, dims)
                except QueryError as exc:
                    with pytest.raises(QueryError) as again:
                        run(other, metrics, dims)
                    assert str(again.value) == str(exc)
                    refused += 1
                    continue
                assert want
                assert run(other, metrics, dims) == want, (name, dims)
                compared += 1
    # Per dimension set: 1 month by month (3 metrics); 3 months by month and quarter;
    # 12 months by month, quarter and year; 7 days by day and week; 1 day by day.
    assert compared == 3 * (3 + 2 + 3 + 2 + 1)
    assert compared + refused == 7 * 5 * 3
