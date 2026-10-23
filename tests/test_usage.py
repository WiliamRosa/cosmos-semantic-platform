"""Usage and cost report from the audit log (ADR 0009), on every available database."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from cosmos_semantic.connectors import ConnectionsFile, CostRates
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import PolicyError, QueryError
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import SemanticService
from cosmos_semantic.usage import usage_report

SOURCES = ["duckdb_connector", "postgres_connector", "sqlserver_connector"]
ANA = Principal(user={"region": "Midwest"}, channel="api", subject="ana@example.com")
BOT = Principal(user={"region": "Midwest"}, channel="mcp", subject="sales-agent")
RATES = CostRates(per_query=0.01, per_second=0.5, per_1k_rows=0.2, currency="USD")


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def audited(graph, connector, tmp_path):
    config = connector.config.model_copy(update={"cost": RATES})
    connections = ConnectionsFile(data_sources={"src": config})
    engine = Engine(graph, connections)
    engine._connectors["src"] = connector
    service = SemanticService(engine, audit_path=tmp_path / "audit.jsonl")
    service.query(QueryRequest(metrics=["revenue", "order_count"]), ANA)
    service.query(QueryRequest(metrics=["revenue"], dimensions=["orders.status"]), ANA)
    service.query(QueryRequest(metrics=["customer_count"], filters=["{rule.active_customer}"]), BOT)
    with pytest.raises(PolicyError):
        service.query(QueryRequest(metrics=["revenue"], dimensions=["customers.tax_id"]), BOT)
    with pytest.raises(QueryError):
        service.query(QueryRequest(metrics=["no_such_metric"]), ANA)
    return service.audit_path, connections


def test_report_counts_queries_by_metric_user_and_channel(graph, audited):
    path, connections = audited
    report = usage_report(path, connections, graph)

    total = report["total"]
    assert (total["queries"], total["errors"], total["denied"]) == (5, 1, 1)
    metrics = {m["metric"]: m for m in report["by_metric"]}
    assert metrics["revenue"]["queries"] == 3 and metrics["revenue"]["denied"] == 1
    assert metrics["revenue"]["principals"] == 2
    assert metrics["customer_count"]["agent_queries"] == 1
    assert {(p["subject"], p["channel"]) for p in report["by_principal"]} == {
        ("ana@example.com", "api"),
        ("sales-agent", "mcp"),
    }
    channels = {c["channel"]: c["queries"] for c in report["by_channel"]}
    assert channels == {"api": 3, "mcp": 2}
    assert [r["rule"] for r in report["by_rule"]] == ["active_customer"]
    assert "net_revenue" in report["unused_metrics"]
    assert report["unknown_metrics"] == ["no_such_metric"]
    assert len(report["slowest"]) == 3


def test_cost_follows_the_rates_and_splits_across_metrics(graph, audited):
    path, connections = audited
    report = usage_report(path, connections, graph)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def cost(r):
        if r["data_source"] is None:  # denied or invalid before reaching the database
            return 0.0
        return 0.01 + 0.5 * (r["duration_ms"] or 0) / 1000 + 0.2 * (r["row_count"] or 0) / 1000

    assert report["currency"] == "USD"
    assert report["total"]["cost"] == pytest.approx(sum(cost(r) for r in records), abs=1e-5)
    first = cost(records[0])
    order_count = next(m for m in report["by_metric"] if m["metric"] == "order_count")
    assert order_count["cost"] == pytest.approx(first / 2, abs=1e-5)
    # Splitting never double counts: metric costs add up to the total of queries with metrics.
    assert sum(m["cost"] for m in report["by_metric"]) == pytest.approx(
        report["total"]["cost"], abs=1e-4
    )


def test_without_rates_the_cost_is_zero(graph, audited):
    path, _ = audited
    report = usage_report(path, None, graph)
    assert report["currency"] is None and report["total"]["cost"] == 0


def test_period_filter(graph, audited):
    path, connections = audited
    tomorrow = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)
    assert usage_report(path, connections, since=tomorrow)["total"]["queries"] == 0
    assert usage_report(path, connections, until=tomorrow)["total"]["queries"] == 5


def test_mixed_currencies_are_rejected(audited, tmp_path):
    path, connections = audited
    other = connections.data_sources["src"].model_copy(
        update={"cost": CostRates(per_query=1, currency="BRL")}
    )
    mixed = ConnectionsFile(data_sources={**connections.data_sources, "other": other})
    with pytest.raises(QueryError, match="different currencies"):
        usage_report(path, mixed)
