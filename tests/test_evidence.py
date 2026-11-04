"""Evidence envelope and replay (ADR 0008), on every available database."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import PolicyError
from cosmos_semantic.evidence import REPRODUCIBILITY_CAVEAT
from cosmos_semantic.expressions import remap_sql
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import SemanticService

SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
]
SUDESTE = Principal(user={"region": "Northeast"}, channel="api", subject="ana@example.com")
REQUEST = QueryRequest(
    metrics=["average_order_value", "customer_count"],
    dimensions=["customers.segment"],
    filters=["{rule.active_customer}"],
)


def make_service(graph, connector, tmp_path) -> SemanticService:
    engine = Engine(graph, ConnectionsFile(data_sources={"src": connector.config}))
    engine._connectors["src"] = connector
    return SemanticService(engine, audit_path=tmp_path / "audit.jsonl")


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


def test_envelope_describes_definitions_rules_and_policies(graph, connector, tmp_path):
    result = make_service(graph, connector, tmp_path).query(REQUEST, SUDESTE)
    ev = result["evidence"]

    assert ev["query_id"].startswith("q_")
    assert ev["as_of"] and ev["timezone"] == "America/New_York"
    assert ev["data_source"]["dialect"] == connector.dialect
    assert ev["principal"] == {
        "subject": "ana@example.com",
        "channel": "api",
        "user_context": {"region": "Northeast"},
    }

    metrics = {m["name"]: m for m in ev["metrics"]}
    assert metrics["average_order_value"]["formula"] == ("(SUM(orders.amount)) / (COUNT(orders.*))")
    assert {m["name"] for m in metrics["average_order_value"]["measures"]} == {
        "revenue",
        "order_count",
    }
    assert "average_order_value" in metrics["average_order_value"]["concepts"]

    [rule] = ev["rules"]
    assert rule["name"] == "active_customer"
    assert rule["used_as"] == ["filter"]
    assert rule["owner"] == "sales-leadership"
    assert rule["tests"] == {"status": "passed", "passed": 2, "total": 2}

    [row_filter] = ev["policies"]["row_filters"]
    assert row_filter["model"] == "customers"
    assert row_filter["user_fields"] == {"region": "Northeast"}
    assert sorted(row_filter["applied_in"]) == ["customers_agg", "orders_agg"]

    assert set(ev["lineage"]["models"]) == {"customers", "orders"}
    assert ev["result"]["row_count"] == result["row_count"] > 0
    assert REPRODUCIBILITY_CAVEAT in ev["caveats"]


def test_replay_of_an_unchanged_query_is_identical(graph, connector, tmp_path):
    service = make_service(graph, connector, tmp_path)
    query_id = service.query(REQUEST, SUDESTE)["evidence"]["query_id"]
    report = service.replay(query_id)
    assert report["verdict"] == "identical", report
    assert all(report["matches"].values())


def test_replay_detects_changed_data(graph, connector, tmp_path):
    service = make_service(graph, connector, tmp_path)
    request = QueryRequest(metrics=["revenue"], dimensions=["orders.status"])
    query_id = service.query(request, SUDESTE)["evidence"]["query_id"]

    def run(sql: str) -> None:
        connector.run_script(remap_sql(sql, connector.dialect, connector.config.schema_map))

    run("UPDATE public.orders SET amount = amount + 1 WHERE order_id = 1")
    try:
        report = service.replay(query_id)
    finally:
        run("UPDATE public.orders SET amount = amount - 1 WHERE order_id = 1")
    assert report["verdict"] == "data_changed", report
    assert report["matches"]["sql_hash"] and not report["matches"]["result_hash"]
    assert service.replay(query_id)["verdict"] == "identical"


def test_replay_detects_changed_model(graph, connector, tmp_path):
    service = make_service(graph, connector, tmp_path)
    query_id = service.query(REQUEST, SUDESTE)["evidence"]["query_id"]

    project = graph.project.model_copy(deep=True)
    project.metrics[0].description = "edited definition text"
    changed = make_service(compile_project(project), connector, tmp_path)
    report = changed.replay(query_id)
    assert report["verdict"] == "model_changed", report
    # A documentation-only change keeps the SQL and the number.
    assert report["matches"]["sql_hash"] and report["matches"]["result_hash"]


def test_only_the_original_user_can_replay(graph, duckdb_connector, tmp_path):
    service = make_service(graph, duckdb_connector, tmp_path)
    query_id = service.query(REQUEST, SUDESTE)["evidence"]["query_id"]
    other = Principal(user={"region": "Midwest"}, channel="api", subject="bob@example.com")
    with pytest.raises(PolicyError):
        service.replay(query_id, requester=other)
    assert service.replay(query_id, requester=SUDESTE)["verdict"] == "identical"


def test_failed_queries_are_audited_without_hashes(graph, duckdb_connector, tmp_path):
    service = make_service(graph, duckdb_connector, tmp_path)
    with pytest.raises(PolicyError):
        service.query(
            QueryRequest(metrics=["customer_count"], dimensions=["customers.tax_id"]),
            Principal(user={"region": "Midwest"}, channel="mcp"),
        )
    record = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert '"outcome": "error"' in record and '"result_hash": null' in record


def test_same_result_hash_on_every_database(graph, request, tmp_path):
    hashes = {}
    for name in SOURCES:
        try:
            connector = request.getfixturevalue(name)
        except pytest.skip.Exception:
            continue
        ev = make_service(graph, connector, tmp_path / name).query(REQUEST, SUDESTE)["evidence"]
        hashes[name] = (ev["result"]["hash"], ev["model_hash"])
    if len(hashes) < 2:
        pytest.skip("needs at least two databases")
    assert len(set(hashes.values())) == 1, hashes
