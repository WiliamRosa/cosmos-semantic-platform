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
    "dialect", ["postgres", "duckdb", "databricks", "snowflake", "tsql", "mysql", "oracle"]
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
  - {name: amount_7d_doubled, type: derived, expr: "{amount_7d} * 2"}
"""


@pytest.mark.parametrize(
    ("metric", "dimensions", "message"),
    [
        ("amount_2w", ["sales.sold_at__day"], "only windows in days"),
        ("amount_1mo", ["sales.sold_at__day"], "only windows in days"),
        ("amount_0d", ["sales.sold_at__day"], "only windows in days"),
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


# An average or a distinct count cannot be combined from daily values: the model is refused
# when it is validated (`cosmos validate`, `cosmos check`), not only when the metric is queried.
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


def test_rolling_window_over_avg_or_count_distinct_is_refused_by_validation():
    with pytest.raises(ModelError, match="mean_7d") as caught:
        compile_project(project(ROLLING_UNCOMBINABLE))
    assert "buyers_7d" in str(caught.value)

    graph = compile_project(project(ROLLING_UNCOMBINABLE), strict=False)
    errors = {i.location: i.message for i in graph.issues if i.severity == "error"}
    assert set(errors) == {"metrics.mean_7d", "metrics.buyers_7d"}
    assert "over a `avg` measure is not supported" in errors["metrics.mean_7d"]
    assert "over a `count_distinct` measure is not supported" in errors["metrics.buyers_7d"]
    # The other rolling metrics of the model are still valid.
    assert not any(i.location == "metrics.amount_7d" for i in graph.issues)


def test_cosmos_validate_reports_rolling_windows_over_avg_or_count_distinct(tmp_path):
    from typer.testing import CliRunner

    from cosmos_semantic.cli import app

    (tmp_path / "model.yml").write_text(ROLLING_UNCOMBINABLE, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--model", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "[error] metrics.mean_7d: a rolling window over a `avg` measure" in result.output
    assert "[error] metrics.buyers_7d: a rolling window over a `count_distinct`" in result.output


@pytest.mark.parametrize(
    ("metric", "message"),
    [
        ("mean_7d", "over a `avg` measure is not supported"),
        ("buyers_7d", "over a `count_distinct` measure"),
    ],
)
def test_rolling_window_over_avg_or_count_distinct_is_still_refused_when_queried(metric, message):
    graph = compile_project(project(ROLLING_UNCOMBINABLE), strict=False)
    with pytest.raises(QueryError, match=message):
        Planner(graph, "duckdb").plan(
            QueryRequest(metrics=[metric], dimensions=["sales.sold_at__day"])
        )


def test_rolling_windows_over_sum_count_min_and_max_pass_validation():
    graph = compile_project(project(ROLLING), strict=False)
    assert not [i for i in graph.issues if i.severity == "error"]
    flagged = {i.location for i in graph.issues}
    assert not flagged & {"metrics.amount_7d", "metrics.lines_1d", "metrics.top_30d"}
    # Windows in other units stay a warning: the model is valid, those queries are refused.
    assert {"metrics.amount_2w", "metrics.amount_1mo", "metrics.amount_0d"} <= flagged
    # Validation and the planner accept the same aggregations.
    from cosmos_semantic import planner, spec

    assert set(planner._WINDOW_AGGS) == set(spec.WINDOW_AGGS)


@pytest.mark.parametrize(
    "dialect", ["postgres", "duckdb", "databricks", "snowflake", "tsql", "mysql", "oracle"]
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
