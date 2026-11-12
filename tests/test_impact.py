"""Impact analysis (ADR 0008)."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import QueryError
from cosmos_semantic.impact import analyze, changed_members
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import SemanticService


def affected(graph, *targets):
    return analyze(graph, list(targets))["affected"]


def test_measure_reaches_every_metric_built_on_it(graph):
    result = affected(graph, "measure:revenue")
    assert {"completed_revenue", "average_order_value", "net_revenue", "collection_gap"} <= set(
        result["metrics"]
    )
    assert "items_per_order" not in result["metrics"]
    assert {"gross_revenue", "net_revenue"} <= set(result["concepts"])


def test_metric_name_also_finds_measures(graph):
    assert affected(graph, "metric:revenue") == affected(graph, "measure:revenue")


def test_dimension_reaches_rules_and_metric_filters(graph):
    assert affected(graph, "dimension:orders.status")["metrics"] == ["completed_revenue"]
    result = affected(graph, "dimension:customers.last_order_at")
    assert result["rules"] == ["active_customer"]
    assert result["concepts"] == ["active_customer"]


def test_region_dimension_reaches_the_row_filter(graph):
    result = affected(graph, "dimension:customers.region")
    assert result["row_filters"] == ["customers.by_region"]
    assert "customer_region" in result["concepts"]


def test_model_lists_joining_models_without_inflating_metrics(graph):
    result = affected(graph, "model:customers")
    assert result["metrics"] == []
    assert result["measures"] == ["customer_count"]
    assert set(result["models"]) == {"customers", "orders", "order_items", "payments"}


def test_unknown_targets_are_explained(graph):
    with pytest.raises(QueryError, match="does not exist"):
        analyze(graph, ["metric:nope"])
    with pytest.raises(QueryError, match="kind:name"):
        analyze(graph, ["revenue"])


def test_consumers_come_from_the_audit_log(graph, duckdb_connector, tmp_path):
    engine = Engine(graph, ConnectionsFile(data_sources={"src": duckdb_connector.config}))
    engine._connectors["src"] = duckdb_connector
    audit = tmp_path / "audit.jsonl"
    service = SemanticService(engine, audit_path=audit)
    ana = Principal(user={"region": "Midwest"}, channel="api", subject="ana")
    bot = Principal(user={"region": "Midwest"}, channel="mcp", subject="agent")
    service.query(QueryRequest(metrics=["customer_count"], filters=["{rule.active_customer}"]), ana)
    service.query(QueryRequest(metrics=["customer_count"], dimensions=["segment"]), ana)
    service.query(QueryRequest(metrics=["revenue"], dimensions=["orders.status"]), bot)

    report = analyze(graph, ["rule:active_customer"], audit_path=audit)
    [consumer] = report["consumers"]
    assert consumer["subject"] == "ana" and consumer["queries"] == 1
    assert consumer["members"] == ["rule.active_customer"]

    report = analyze(graph, ["model:customers"], audit_path=audit)
    assert [(c["subject"], c["queries"]) for c in report["consumers"]] == [("ana", 2)]

    report = analyze(graph, ["dimension:orders.status"], audit_path=audit)
    assert [(c["subject"], c["channel"]) for c in report["consumers"]] == [("agent", "mcp")]


def test_corrupted_audit_lines_are_skipped(graph, duckdb_connector, tmp_path):
    engine = Engine(graph, ConnectionsFile(data_sources={"src": duckdb_connector.config}))
    engine._connectors["src"] = duckdb_connector
    audit = tmp_path / "audit.jsonl"
    service = SemanticService(engine, audit_path=audit)
    ana = Principal(user={"region": "Midwest"}, channel="api", subject="ana")
    request = QueryRequest(metrics=["customer_count"], filters=["{rule.active_customer}"])
    service.query(request, ana)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "query", "outcome": "ok", "princ\n')  # cut off mid-write
        fh.write("not json at all\n")
        fh.write("42\n")
    service.query(request, ana)

    report = analyze(graph, ["rule:active_customer"], audit_path=audit)
    assert [(c["subject"], c["queries"]) for c in report["consumers"]] == [("ana", 2)]


def test_changes_between_model_versions(graph):
    project = graph.project.model_copy(deep=True)
    metric = next(m for m in project.metrics if m.name == "completed_revenue")
    metric.filter = "{status} IN ('completed', 'pending')"
    rule = next(r for r in project.business_rules if r.name == "active_customer")
    rule.logic = "{last_purchase} >= CURRENT_DATE - INTERVAL '60' DAY"
    orders = next(m for m in project.semantic_models if m.name == "orders")
    orders.measures = [m for m in orders.measures if m.name != "discounts"]
    dropped = ("net_revenue", "discount_rate", "collection_gap")
    project.metrics = [m for m in project.metrics if m.name not in dropped]
    concept = next(c for c in project.glossary if c.concept == "net_revenue")
    concept.maps_to = None
    # A process that used a removed metric must be updated too, or validation fails.
    process = next(p for p in project.processes if p.name == "order_to_cash")
    process.uses = [u for u in process.uses if u.partition(":")[2] not in dropped]
    new = compile_project(project)

    changes = changed_members(graph, new)
    assert set(changes) == {
        "metric:completed_revenue",
        "metric:net_revenue",
        "metric:discount_rate",
        "metric:collection_gap",
        "rule:active_customer",
        "measure:discounts",
        "concept:net_revenue",
        "process:order_to_cash",
    }
    removed: list[str] = []
    report = analyze(new, changes, removed=removed)
    assert set(report["removed"]) == {
        "metric:net_revenue",
        "metric:discount_rate",
        "metric:collection_gap",
        "measure:discounts",
    }
    assert "completed_revenue" in report["affected"]["metrics"]
    assert "active_customer" in report["affected"]["concepts"]
