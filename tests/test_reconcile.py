"""Reconciliation (ADR 0008): the divergence zoo must be diagnosed on every database."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import QueryError
from cosmos_semantic.interchange import import_model
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.reconcile import check_zoo_case, load_zoo, reconcile_models, reconcile_sql

from .conftest import ROOT

ZOO = load_zoo(ROOT / "examples" / "ecommerce" / "divergence_zoo.yml")
SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
]


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("case", ZOO, ids=lambda c: c.id)
def test_divergence_zoo(case, graph, connector):
    ok, report = check_zoo_case(case, graph, connector)
    found = [c["code"] for c in report["causes"]]
    assert ok, f"{case.id}: status={report['status']} causes={found} rows={report['rows']}"


def test_report_shape(graph, duckdb_connector):
    case = next(c for c in ZOO if c.id == "Z03")
    report = reconcile_sql(graph, duckdb_connector, case.request, case.sql)
    assert report["status"] == "divergent"
    assert report["totals"]["legacy"] > report["totals"]["cosmos"]
    assert report["totals"]["difference_pct"] > 0
    assert report["cosmos"]["formula"] == "SUM(orders.amount)"
    assert report["cosmos"]["as_of"]
    assert report["legacy"]["kind"] == "sql" and len(report["legacy"]["sql_hash"]) == 64
    [cause] = report["causes"]
    assert cause["detail"] == {"joined_model": "order_items", "measure": "revenue"}
    assert report["examples"] and {"key", "cosmos", "legacy"} <= report["examples"][0].keys()


def test_one_metric_at_a_time(graph, duckdb_connector):
    with pytest.raises(QueryError, match="one metric"):
        reconcile_sql(
            graph,
            duckdb_connector,
            QueryRequest(metrics=["revenue", "order_count"]),
            "SELECT 1",
        )


def test_legacy_sql_with_wrong_shape_is_explained(graph, duckdb_connector):
    with pytest.raises(QueryError, match="dimension columns"):
        reconcile_sql(
            graph,
            duckdb_connector,
            QueryRequest(metrics=["revenue"], dimensions=["orders.status"]),
            "SELECT status, SUM(amount), COUNT(*) FROM public.orders GROUP BY status",
        )


def test_imported_cube_model_reconciles_with_native(graph, connector):
    cube = compile_project(import_model("cube", ROOT / "tests" / "fixtures" / "cube").project)
    for metric in ("revenue", "order_count", "average_order_value"):
        report = reconcile_models(
            graph,
            cube,
            connector,
            QueryRequest(metrics=[metric], dimensions=["orders.status"]),
        )
        assert report["status"] == "match", (metric, report["rows"], report["causes"])
        assert report["legacy"]["kind"] == "model"


def test_changed_definition_is_diagnosed_between_models(graph, connector):
    project = graph.project.model_copy(deep=True)
    metric = next(m for m in project.metrics if m.name == "completed_revenue")
    metric.filter = None
    other = compile_project(project)
    report = reconcile_models(
        graph,
        other,
        connector,
        QueryRequest(metrics=["completed_revenue"], dimensions=["orders.ordered_at__month"]),
        Principal(),
    )
    assert report["status"] == "divergent"
    assert report["definitions_differ"] is True
    assert report["causes"][0]["code"] == "filter_difference"
