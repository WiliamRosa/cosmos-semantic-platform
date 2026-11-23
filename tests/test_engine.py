from __future__ import annotations

import json

import pytest
import yaml

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ensure_read_only
from cosmos_semantic.errors import ConnectorError, ModelError, PolicyError, QueryError
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.spec import Project, json_schema

from .conftest import ROOT


def project(text: str) -> Project:
    return Project.model_validate(yaml.safe_load(text))


BASE = """
version: 1
semantic_models:
  - name: a
    table: t_a
    entities: [{name: a, type: primary, expr: id}, {name: b, type: foreign, expr: b_id}]
    measures: [{name: n, agg: count}]
  - name: b
    table: t_b
    entities: [{name: b, type: primary, expr: id}]
    dimensions: [{name: label, type: categorical}]
"""


def test_schema_matches_committed_file():
    committed = json.loads((ROOT / "schema" / "cosmos-semantic.v1.schema.json").read_text("utf-8"))
    assert committed == json.loads(json.dumps(json_schema(), ensure_ascii=False))


@pytest.mark.parametrize("value", ["7 days", "1 day", "12 hours", "2 weeks", " 3 Days "])
def test_health_max_age_accepts_hours_days_and_weeks(value):
    assert Project.model_validate({"version": 1, "health": {"max_age": value}}).health


@pytest.mark.parametrize(
    "value", ["7", "0 days", "1 month", "-1 days", "a week", "1.5 days", "99999999999 weeks"]
)
def test_health_max_age_rejects_other_values(value):
    with pytest.raises(ValueError, match="health.max_age"):
        Project.model_validate({"version": 1, "health": {"max_age": value}})


def test_model_files_must_agree_on_health_settings(tmp_path):
    from cosmos_semantic.loader import load_project

    (tmp_path / "a.yml").write_text("version: 1\nhealth: {max_age: 7 days}\n")
    (tmp_path / "b.yml").write_text("version: 1\n")
    assert load_project(tmp_path).health.max_age == "7 days"
    (tmp_path / "c.yml").write_text("version: 1\nhealth: {max_age: 1 day}\n")
    with pytest.raises(ModelError, match="health"):
        load_project(tmp_path)


def test_version_is_required():
    with pytest.raises(ValueError):
        Project.model_validate({"semantic_models": []})


def test_unknown_field_is_rejected():
    with pytest.raises(ValueError):
        project(BASE + "\nmetricz: []\n")


def test_joins_are_inferred_from_entities():
    graph = compile_project(project(BASE))
    path = graph.join_path("a", "b")
    assert [(e.source, e.target, e.entity) for e in path] == [("a", "b", "b")]
    with pytest.raises(QueryError):
        graph.join_path("b", "a")


def test_ambiguous_join_path_is_an_error():
    text = (
        BASE
        + """
  - name: c
    table: t_c
    entities: [{name: c, type: primary, expr: id}, {name: b, type: foreign, expr: b_id}]
  - name: d
    table: t_d
    entities: [{name: d, type: primary, expr: id}, {name: c, type: foreign}]
"""
    )
    data = yaml.safe_load(text)
    data["semantic_models"][0]["entities"].append({"name": "c", "type": "foreign"})
    # a -> b directly and a -> c -> b have different lengths: the shortest wins, no ambiguity
    graph = compile_project(Project.model_validate(data))
    assert len(graph.join_path("a", "b")) == 1
    # with two paths of the same length, the compiler refuses
    data["semantic_models"].append(
        {
            "name": "e",
            "table": "t_e",
            "entities": [
                {"name": "e", "type": "primary"},
                {"name": "b", "type": "foreign"},
                {"name": "c", "type": "foreign"},
            ],
        }
    )
    data["semantic_models"][2]["entities"].append({"name": "x", "type": "foreign"})
    data["semantic_models"].append(
        {"name": "x", "table": "t_x", "entities": [{"name": "x", "type": "primary"}]}
    )
    data["semantic_models"][1]["entities"].append({"name": "x", "type": "foreign"})
    with pytest.raises(ModelError, match="ambiguous"):
        compile_project(Project.model_validate(data))


def test_duplicate_primary_entity_is_an_error():
    data = yaml.safe_load(BASE)
    data["semantic_models"][1]["entities"].append({"name": "a", "type": "unique"})
    with pytest.raises(ModelError, match="already a key"):
        compile_project(Project.model_validate(data))


def test_undeclared_user_field_is_an_error():
    data = yaml.safe_load(BASE)
    data["semantic_models"][1]["row_filters"] = [{"name": "f", "filter": "{label} = $user.team"}]
    with pytest.raises(ModelError, match="user_context"):
        compile_project(Project.model_validate(data))


def test_request_filter_cannot_read_other_tables(graph):
    planner = Planner(graph)
    for bad in [
        "{orders.status} IN (SELECT status FROM public.orders)",
        "amount > 0",
        "{orders.status} = $user.region",
    ]:
        with pytest.raises(QueryError):
            planner.plan(QueryRequest(metrics=["revenue"], filters=[bad]))


def test_missing_user_context_denies_query(graph):
    request = QueryRequest(metrics=["customer_count"], dimensions=["customers.region"])
    with pytest.raises(PolicyError, match="region"):
        Planner(graph).plan(request, Principal())


def test_agent_cannot_filter_by_personal_data(graph):
    request = QueryRequest(metrics=["customer_count"], filters=["{customers.tax_id} = '123'"])
    with pytest.raises(PolicyError, match="LGPD"):
        Planner(graph).plan(request, Principal(user={"region": "Midwest"}, channel="mcp"))
    # Applications (not agents) may filter by personal data
    Planner(graph).plan(request, Principal(user={"region": "Midwest"}, channel="api"))


def test_unreachable_dimension_is_explained(graph):
    # Nothing links payments to products: no model reaches both.
    request = QueryRequest(metrics=["paid_value"], dimensions=["products.category"])
    with pytest.raises(QueryError, match="no model links them"):
        Planner(graph).plan(request)


def test_one_to_many_dimension_goes_through_a_distinct_bridge(graph):
    compiled = Planner(graph).plan(
        QueryRequest(metrics=["revenue"], dimensions=["products.category"])
    )
    assert "AS bridge" in compiled.sql and "cosmos_bridge_key" in compiled.sql
    [item] = compiled.trace.one_to_many
    assert item == {
        "base": "orders",
        "via": "order_items",
        "dimensions": ["products.category"],
        "filters": [],
        "in": "orders_agg",
    }


def test_filter_mixing_both_sides_is_refused(graph):
    request = QueryRequest(
        metrics=["revenue"],
        filters=["{orders.status} = 'completed' OR {products.category} = 'Home'"],
    )
    with pytest.raises(QueryError, match="split it"):
        Planner(graph).plan(request)


def test_measures_from_different_models_never_join_before_aggregating(graph):
    sql = (
        Planner(graph)
        .plan(QueryRequest(metrics=["revenue", "paid_value"], dimensions=["orders.status"]))
        .sql
    )
    assert "orders_agg AS" in sql and "payments_agg AS" in sql
    assert "IS NOT DISTINCT FROM" in sql


@pytest.mark.parametrize(
    "dialect",
    [
        "postgres",
        "duckdb",
        "databricks",
        "snowflake",
        "tsql",
        "mysql",
        "oracle",
        "bigquery",
        "trino",
    ],
)
def test_sql_is_generated_for_every_dialect(graph, dialect):
    compiled = Planner(graph, dialect).plan(
        QueryRequest(
            metrics=["revenue", "paid_value", "average_order_value"],
            dimensions=["orders.ordered_at__month", "customers.region"],
            limit=10,
        ),
        Principal(user={"region": "Midwest"}),
    )
    assert compiled.dialect == dialect
    ensure_read_only(compiled.sql, dialect)


def test_read_only_guard():
    with pytest.raises(ConnectorError):
        ensure_read_only("DELETE FROM t", "postgres")
    with pytest.raises(ConnectorError):
        ensure_read_only("SELECT 1; DROP TABLE t", "postgres")


ROLLING = """
version: 1
semantic_models:
  - name: sales
    table: t_sales
    entities: [{name: sale, type: primary, expr: id}]
    dimensions: [{name: sold_at, type: time}, {name: kind, type: categorical}]
    measures:
      - {name: amount, agg: sum}
      - {name: lines, agg: count}
      - {name: top_amount, agg: max, expr: amount}
metrics:
  - {name: amount_7d, type: cumulative, measure: amount, window: 7 days}
  - {name: lines_1d, type: cumulative, measure: lines, window: 1 day}
  - {name: top_30d, type: cumulative, measure: top_amount, window: 30 days}
  - {name: amount_2w, type: cumulative, measure: amount, window: 2 weeks}
  - {name: amount_1mo, type: cumulative, measure: amount, window: 1 month}
  - {name: amount_0d, type: cumulative, measure: amount, window: 0 days}
  - {name: amount_0w, type: cumulative, measure: amount, window: 0 weeks}
  - {name: amount_7d_doubled, type: derived, expr: "{amount_7d} * 2"}
"""


@pytest.mark.parametrize(
    ("metric", "dimensions", "message"),
    [
        ("amount_1mo", ["sales.sold_at__day"], "only windows in days or weeks"),
        ("amount_0d", ["sales.sold_at__day"], "only windows in days or weeks"),
        ("amount_0w", ["sales.sold_at__day"], "only windows in days or weeks"),
        ("amount_2w", ["sales.sold_at__week"], "needs the time dimension at day grain"),
        ("amount_2w", ["sales.sold_at__month"], "needs the time dimension at day grain"),
        ("amount_7d", ["sales.sold_at__week"], "needs the time dimension at day grain"),
        ("amount_7d", ["sales.sold_at__month"], "needs the time dimension at day grain"),
        ("amount_7d", ["sales.sold_at"], "needs the time dimension at day grain"),
        ("amount_7d", ["sales.kind"], "exactly one time dimension"),
        ("amount_7d_doubled", ["sales.sold_at__day"], "cannot be part of another metric"),
    ],
)
def test_rolling_window_refusals(metric, dimensions, message):
    graph = compile_project(project(ROLLING))
    with pytest.raises(QueryError, match=message):
        Planner(graph, "duckdb").plan(QueryRequest(metrics=[metric], dimensions=dimensions))


# An average cannot be combined from daily values: the model is refused when it is validated
# (`cosmos validate`, `cosmos check`), not only when the metric is queried. A distinct count is
# recomputed from the facts of each window instead (DEC-49), so its window is valid.
ROLLING_UNCOMBINABLE = (
    ROLLING.replace(
        "      - {name: top_amount, agg: max, expr: amount}\n",
        "      - {name: top_amount, agg: max, expr: amount}\n"
        "      - {name: mean_amount, agg: avg, expr: amount}\n"
        "      - {name: buyers, agg: count_distinct, expr: buyer_id}\n",
    )
    + "  - {name: mean_7d, type: cumulative, measure: mean_amount, window: 7 days}\n"
    + "  - {name: buyers_7d, type: cumulative, measure: buyers, window: 7 days}\n"
)


def test_rolling_window_over_avg_is_refused_by_validation():
    with pytest.raises(ModelError, match="mean_7d") as caught:
        compile_project(project(ROLLING_UNCOMBINABLE))
    assert "buyers_7d" not in str(caught.value)

    graph = compile_project(project(ROLLING_UNCOMBINABLE), strict=False)
    errors = {i.location: i.message for i in graph.issues if i.severity == "error"}
    assert set(errors) == {"metrics.mean_7d"}
    assert "over a `avg` measure is not supported" in errors["metrics.mean_7d"]
    assert "count_distinct" in errors["metrics.mean_7d"]  # named among the aggregations to use
    # The other rolling metrics of the model, the distinct count's included, are valid.
    assert not any(i.location in ("metrics.amount_7d", "metrics.buyers_7d") for i in graph.issues)


def test_cosmos_validate_reports_rolling_windows_over_avg_only(tmp_path):
    from typer.testing import CliRunner

    from cosmos_semantic.cli import app

    (tmp_path / "model.yml").write_text(ROLLING_UNCOMBINABLE, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--model", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "[error] metrics.mean_7d: a rolling window over a `avg` measure" in result.output
    assert "metrics.buyers_7d" not in result.output


def test_rolling_window_over_avg_is_still_refused_when_queried():
    graph = compile_project(project(ROLLING_UNCOMBINABLE), strict=False)
    with pytest.raises(QueryError, match="over a `avg` measure is not supported"):
        Planner(graph, "duckdb").plan(
            QueryRequest(metrics=["mean_7d"], dimensions=["sales.sold_at__day"])
        )
    # The distinct count's window plans (DEC-49); tests/test_distinct_windows.py runs it.
    Planner(graph, "duckdb").plan(
        QueryRequest(metrics=["buyers_7d"], dimensions=["sales.sold_at__day"])
    )


def test_rolling_windows_over_sum_count_min_and_max_pass_validation():
    graph = compile_project(project(ROLLING), strict=False)
    assert not [i for i in graph.issues if i.severity == "error"]
    flagged = {i.location for i in graph.issues}
    assert not flagged & {
        "metrics.amount_7d",
        "metrics.lines_1d",
        "metrics.top_30d",
        "metrics.amount_2w",
    }
    # Windows in other units stay a warning: the model is valid, those queries are refused.
    assert {"metrics.amount_1mo", "metrics.amount_0d", "metrics.amount_0w"} <= flagged
    # Validation and the planner accept the same aggregations.
    from cosmos_semantic import planner, spec

    assert set(planner._COMBINE_AGGS) == set(spec.WINDOW_AGGS)


@pytest.mark.parametrize(
    "dialect",
    [
        "postgres",
        "duckdb",
        "databricks",
        "snowflake",
        "tsql",
        "mysql",
        "oracle",
        "bigquery",
        "trino",
    ],
)
def test_rolling_window_sql_for_every_dialect(dialect):
    graph = compile_project(project(ROLLING))
    compiled = Planner(graph, dialect).plan(
        QueryRequest(
            metrics=["amount_7d", "lines_1d", "top_30d", "amount"],
            dimensions=["sales.kind", "sales.sold_at__day"],
        )
    )
    ensure_read_only(compiled.sql, dialect)
    sql = compiled.sql.upper().replace("`", "").replace('"', "")
    assert "OVER" not in sql  # a self-join, not a window frame (SQL Server has no RANGE)
    assert "MAX(PREV.TOP_AMOUNT)" in sql and "SUM(PREV.LINES)" in sql


def test_parse_window():
    from cosmos_semantic.spec import parse_window, window_parts

    assert parse_window("1 day") == 1 and parse_window("7 days") == 7
    assert parse_window("1 week") == parse_window("1 weeks") == 7
    assert parse_window(" 2 Weeks ") == 14 and window_parts("2 Weeks") == (2, "week")
    for text in ("0 weeks", "0 days", "1 month", "1.5 weeks", "-1 week", "week", "", None):
        assert parse_window(text) is None, text


@pytest.mark.parametrize(
    "dialect",
    ["postgres", "duckdb", "databricks", "snowflake", "tsql", "mysql", "oracle", "bigquery"],
)
@pytest.mark.parametrize(
    "filters",
    [[], ["{sales.sold_at} >= DATE '2026-01-01'"], ["NOT ({sales.sold_at} < DATE '2026-01-01')"]],
    ids=["unfiltered", "lookback", "filtered-first"],
)
def test_window_in_weeks_has_the_sql_of_its_days(dialect, filters):
    """`window: 2 weeks` compiles to exactly the SQL, and so the `sql_hash`, of `14 days`
    (DEC-27), with and without a date filter the window reads back before (DEC-34)."""
    from cosmos_semantic.evidence import sql_hash

    weeks = compile_project(project(ROLLING))
    days = compile_project(project(ROLLING.replace("window: 2 weeks", "window: 14 days")))
    assert weeks.metrics["amount_2w"].window == "2 weeks"
    assert days.metrics["amount_2w"].window == "14 days"
    request = QueryRequest(
        metrics=["amount_2w", "amount"],
        dimensions=["sales.kind", "sales.sold_at__day"],
        filters=filters,
    )
    written = Planner(weeks, dialect).plan(request).sql
    same = Planner(days, dialect).plan(request).sql
    assert written == same
    assert sql_hash(written) == sql_hash(same)


# -- Cumulative (to-date) metrics over each aggregation, and their grains (DEC-21) ----------

TO_DATE = """
version: 1
semantic_models:
  - name: sales
    table: t_sales
    entities: [{name: sale, type: primary, expr: id}]
    dimensions: [{name: sold_at, type: time}, {name: kind, type: categorical}]
    measures:
      - {name: amount, agg: sum}
      - {name: lines, agg: count}
      - {name: top_amount, agg: max, expr: amount}
      - {name: low_amount, agg: min, expr: amount}
      - {name: mean_amount, agg: avg, expr: amount}
      - {name: buyers, agg: count_distinct, expr: buyer_id}
metrics:
  - {name: amount_ytd, type: cumulative, measure: amount, grain_to_date: year}
  - {name: lines_mtd, type: cumulative, measure: lines, grain_to_date: month}
  - {name: top_ytd, type: cumulative, measure: top_amount, grain_to_date: year}
  - {name: low_qtd, type: cumulative, measure: low_amount, grain_to_date: quarter}
  - {name: amount_wtd, type: cumulative, measure: amount, grain_to_date: week}
  - {name: amount_running, type: cumulative, measure: amount}
"""
UNACCUMULABLE = """
  - {name: mean_ytd, type: cumulative, measure: mean_amount, grain_to_date: year}
  - {name: buyers_ytd, type: cumulative, measure: buyers, grain_to_date: year}
  - {name: buyers_running, type: cumulative, measure: buyers}
"""


def test_to_date_over_avg_or_count_distinct_is_a_validation_error():
    with pytest.raises(ModelError, match="year-to-date over a `avg` measure"):
        compile_project(project(TO_DATE + UNACCUMULABLE))
    graph = compile_project(project(TO_DATE + UNACCUMULABLE), strict=False)
    errors = {i.location: i.message for i in graph.issues if i.severity == "error"}
    assert set(errors) == {"metrics.mean_ytd", "metrics.buyers_ytd", "metrics.buyers_running"}
    assert errors["metrics.buyers_ytd"] == (
        "a year-to-date over a `count_distinct` measure is not supported; it cannot be "
        "accumulated from per-period values (use a sum, count, min or max measure)"
    )
    assert errors["metrics.buyers_running"].startswith("a running total over a `count_distinct`")
    # sum, count, min and max accumulate: no issue at all.
    assert not compile_project(project(TO_DATE)).issues


def test_cli_validate_refuses_a_year_to_date_distinct_count(tmp_path):
    from typer.testing import CliRunner

    from cosmos_semantic.cli import app

    (tmp_path / "model.yml").write_text(TO_DATE + UNACCUMULABLE, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "-m", str(tmp_path)])
    assert result.exit_code == 1
    assert "[error] metrics.buyers_ytd: a year-to-date over a `count_distinct`" in result.stdout


@pytest.mark.parametrize(
    ("metric", "message"),
    [
        ("mean_ytd", "`mean_ytd`: a year-to-date over a `avg` measure is not supported"),
        ("buyers_ytd", "`buyers_ytd`: a year-to-date over a `count_distinct` measure"),
        ("buyers_running", "`buyers_running`: a running total over a `count_distinct`"),
    ],
)
def test_to_date_over_avg_or_count_distinct_is_refused_at_query_time(metric, message):
    """Even when the model was loaded despite the validation error."""
    graph = compile_project(project(TO_DATE + UNACCUMULABLE), strict=False)
    with pytest.raises(QueryError, match=message):
        Planner(graph, "duckdb").plan(
            QueryRequest(metrics=[metric], dimensions=["sales.sold_at__month"])
        )


@pytest.mark.parametrize(
    ("metric", "grain", "refused"),
    [
        ("lines_mtd", "quarter", True),
        ("lines_mtd", "year", True),
        ("low_qtd", "year", True),
        ("amount_wtd", "month", True),
        # A week does not nest in a month, quarter or year (DEC-28).
        ("lines_mtd", "week", True),
        ("low_qtd", "week", True),
        ("amount_ytd", "week", True),
        ("top_ytd", "week", True),
        ("lines_mtd", "month", False),
        ("lines_mtd", "day", False),
        ("lines_mtd", "hour", False),
        ("lines_mtd", None, False),
        ("amount_ytd", "year", False),
        ("amount_ytd", "quarter", False),
        ("amount_ytd", "month", False),
        ("amount_ytd", "day", False),
        ("low_qtd", "quarter", False),
        ("low_qtd", "month", False),
        ("amount_wtd", "week", False),
        ("amount_wtd", "day", False),
        ("amount_running", "year", False),  # no to-date grain: a running total at any grain
        ("amount_running", "week", False),
    ],
)
def test_to_date_grain_coarser_than_the_period_is_refused(metric, grain, refused):
    graph = compile_project(project(TO_DATE))
    dim = f"sales.sold_at__{grain}" if grain else "sales.sold_at"
    for dims in (["sales.kind", dim], [dim]):
        request = QueryRequest(metrics=[metric], dimensions=dims)
        if refused:
            with pytest.raises(QueryError, match=f"cannot be grouped by {grain}"):
                Planner(graph, "duckdb").plan(request)
        else:
            Planner(graph, "duckdb").plan(request)


@pytest.mark.parametrize(
    ("metric", "message"),
    [
        (
            "lines_mtd",
            "`lines_mtd` (`sales.sold_at__week` requested): a month-to-date metric cannot be "
            "grouped by week, since a week can cross into a new month and would be counted in "
            "the old one; use the day or month grain",
        ),
        (
            "amount_ytd",
            "`amount_ytd` (`sales.sold_at__week` requested): a year-to-date metric cannot be "
            "grouped by week, since a week can cross into a new year and would be counted in "
            "the old one; use the day, month, quarter or year grain",
        ),
    ],
)
def test_week_under_a_longer_to_date_period_says_why(metric, message):
    graph = compile_project(project(TO_DATE))
    request = QueryRequest(metrics=[metric], dimensions=["sales.sold_at__week"])
    with pytest.raises(QueryError) as refusal:
        Planner(graph, "duckdb").plan(request)
    assert str(refusal.value) == message


WEEK_UNDER_YEAR = """
  - {name: amount_ytd_doubled, type: derived, expr: "{amount_ytd} * 2"}
  - name: amount_ytd_prev_month
    type: derived
    expr: "{amount_ytd}"
    inputs: [{metric: amount_ytd, offset: 1 month}]
"""


@pytest.mark.parametrize("metric", ["amount_ytd_doubled", "amount_ytd_prev_month"])
def test_week_under_a_year_to_date_reaches_no_number_through_another_metric(metric):
    """A derived metric or an offset cannot wrap a cumulative metric, so no request reaches a
    year-to-date by week through one (DEC-28)."""
    graph = compile_project(project(TO_DATE + WEEK_UNDER_YEAR), strict=False)
    for grain in ("week", "month"):
        request = QueryRequest(metrics=[metric], dimensions=[f"sales.sold_at__{grain}"])
        with pytest.raises(QueryError, match="cumulative"):
            Planner(graph, "duckdb").plan(request)


@pytest.mark.parametrize(
    "dialect",
    [
        "postgres",
        "duckdb",
        "databricks",
        "snowflake",
        "tsql",
        "mysql",
        "oracle",
        "bigquery",
        "trino",
    ],
)
def test_to_date_combines_min_and_max_with_min_and_max(dialect):
    graph = compile_project(project(TO_DATE))
    compiled = Planner(graph, dialect).plan(
        QueryRequest(
            metrics=["amount_ytd", "lines_mtd", "top_ytd", "low_qtd"],
            dimensions=["sales.kind", "sales.sold_at__day"],
        )
    )
    ensure_read_only(compiled.sql, dialect)
    sql = compiled.sql.upper().replace("`", "").replace('"', "")
    assert "SUM(SALES_AGG.AMOUNT) OVER" in sql and "SUM(SALES_AGG.LINES) OVER" in sql
    assert "MAX(SALES_AGG.TOP_AMOUNT) OVER" in sql
    assert "MIN(SALES_AGG.LOW_AMOUNT) OVER" in sql
