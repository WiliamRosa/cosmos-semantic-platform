from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.mcp_server import build_mcp
from cosmos_semantic.service import SemanticService


@pytest.fixture(scope="module")
def server(graph, duckdb_connector, tmp_path_factory):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    settings = AuthSettings(
        dev_keys={
            "agente-sul": DevKey(subject="agente", user={"region": "Midwest"}),
            "lead-key": DevKey(subject="lead@example.com", user={"region": "Midwest"}),
        }
    )
    audit_path = tmp_path_factory.mktemp("mcp") / "audit.jsonl"
    service = SemanticService(engine, audit_path=audit_path, admins={"lead@example.com"})
    return build_mcp(service, Authenticator(settings, graph.project.user_context))


def call(server, tool: str, **arguments):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments)
            return result

    result = asyncio.run(run())
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_lists_the_eight_tools(server):
    async def run():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(run()) == {
        "search_concepts",
        "explain",
        "list_members",
        "query",
        "show_sql",
        "replay",
        "impact",
        "dimension_values",
    }


def test_without_credentials_the_agent_gets_an_error(server, monkeypatch):
    monkeypatch.delenv("COSMOS_API_KEY", raising=False)
    result = call(server, "list_members")
    assert result["error"] == "AuthError"


def test_agent_answers_with_synonyms_only(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    concept = call(server, "search_concepts", text="compradores ativos")["concepts"][0]
    assert concept["maps_to"] == "rule:active_customer"
    result = call(
        server,
        "query",
        metrics=["customer_count"],
        filters=["{rule.active_customer}"],
    )
    assert result["row_count"] == 1 and result["rows"][0]["customer_count"] >= 0
    assert [r["name"] for r in result["evidence"]["rules"]] == ["active_customer"]
    assert result["evidence"]["principal"]["channel"] == "mcp"


def test_agent_is_blocked_from_sensitive_data(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "query", metrics=["customer_count"], dimensions=["customers.health_plan"])
    assert result["error"] == "PolicyError"


def test_show_sql_applies_the_users_row_filter(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "show_sql", metrics=["customer_count"], dimensions=["customers.region"])
    assert "'Midwest'" in result["sql"]


def test_agent_replays_its_own_query(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "query", metrics=["customer_count"], dimensions=["customers.region"])
    query_id = result["evidence"]["query_id"]
    report = call(server, "replay", query_id=query_id)
    assert report["query_id"] == query_id
    assert report["verdict"] == "identical", report
    assert all(report["matches"].values())


def test_replay_of_an_unknown_query_is_an_error(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "replay", query_id="does-not-exist")
    assert result["error"] == "QueryError"
    assert "not in the audit log" in result["detail"]


def test_impact_shows_who_queried_only_to_admins(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    call(server, "query", metrics=["customer_count"], filters=["{rule.active_customer}"])
    report = call(server, "impact", name="rule:active_customer")
    assert report["targets"] == ["rule:active_customer"]
    assert report["affected"]["rules"] == ["active_customer"]
    assert report["affected"]["processes"] == ["customer_retention"]
    assert report["owners"] == {
        "sales-leadership": ["process:customer_retention", "rule:active_customer"]
    }
    # Who queried a member is personal data: not for a non-admin, not even their own queries.
    assert report["consumers_visible"] is False
    assert "consumers" not in report
    assert "agente" not in json.dumps(report)

    monkeypatch.setenv("COSMOS_API_KEY", "lead-key")
    admin = call(server, "impact", name="rule:active_customer")
    assert admin["consumers_visible"] is True
    assert admin["affected"] == report["affected"] and admin["owners"] == report["owners"]
    agent = next(c for c in admin["consumers"] if c["subject"] == "agente")
    assert agent["channel"] == "mcp" and agent["queries"] >= 1
    assert "rule.active_customer" in agent["members"]


def test_impact_of_an_unknown_member_is_an_error(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "impact", name="metric:does_not_exist")
    assert result["error"] == "QueryError"
    assert "does not exist in the model" in result["detail"]
    bare = call(server, "impact", name="active_customer")
    assert bare["error"] == "QueryError" and "kind:name" in bare["detail"]


def test_explain_tells_the_agent_whether_the_number_is_checked(graph, monkeypatch, tmp_path):
    runs = tmp_path / "runs.jsonl"
    reconciliations = [
        {
            "id": "R1",
            "title": "Legacy revenue",
            "metrics": ["completed_revenue"],
            "outcome": "fail",
        },
        {"id": "R2", "title": "Orders", "metrics": ["order_count"], "outcome": "pass"},
    ]
    runs.write_text(json.dumps({"event": "check", "reconciliations": reconciliations}) + "\n")
    monkeypatch.setenv("COSMOS_RUNS_LOG", str(runs))
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    settings = AuthSettings(dev_keys={"agente-sul": DevKey(subject="agente", user={})})
    server = build_mcp(SemanticService(engine), Authenticator(settings, graph.project.user_context))
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")

    failing = call(server, "explain", name="metric:completed_revenue")
    assert failing["kind"] == "metric" and failing["definition"]
    assert failing["health"] == {
        "state": "failing",
        "reasons": [{"code": "reconciliation_failed", "case": "R1", "title": "Legacy revenue"}],
    }
    order_count = call(server, "explain", name="order_count")
    assert order_count["health"] == {
        "state": "healthy",
        "reasons": [{"code": "reconciled", "count": 1}],
    }
    assert call(server, "explain", name="discount_rate")["health"]["state"] == "unchecked"
    rule = call(server, "explain", name="rule:active_customer")
    assert rule["health"] == {
        "state": "healthy",
        "reasons": [{"code": "rule_tests_pass", "total": 4}],
    }
    concept = call(server, "explain", name="active_customer")
    assert "health" not in concept and concept["defined_by"]["health"] == rule["health"]
    assert "health" not in call(server, "explain", name="customers.region")

    monkeypatch.delenv("COSMOS_API_KEY")
    denied = call(server, "explain", name="metric:completed_revenue")
    assert denied["error"] == "AuthError" and "health" not in denied


def test_dimension_values_match_query_grouped_by_the_dimension(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "dimension_values", name="customers.segment")
    grouped = call(server, "query", metrics=["customer_count"], dimensions=["customers.segment"])
    expected = {r["customers__segment"]: r["customer_count"] for r in grouped["rows"]}
    assert {v["value"]: v["count"] for v in result["values"]} == expected
    assert None in expected  # nulls come back as a value with their count
    keys = [(-v["count"], v["value"] is None, v["value"] or "") for v in result["values"]]
    assert keys == sorted(keys)
    assert result["dimension"] == "customers.segment" and result["truncated"] is False
    assert result["evidence"]["count_of"] == "rows of `customers`"


def test_dimension_values_apply_the_users_row_filters(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "dimension_values", name="customers.region")
    assert [v["value"] for v in result["values"]] == ["Midwest"]
    applied = result["evidence"]["row_filters"]
    assert applied[0]["name"] == "by_region" and applied[0]["user_fields"] == {"region": "Midwest"}


def test_dimension_values_of_booleans_and_rules(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "dimension_values", name="orders.has_discount")
    grouped = call(server, "query", metrics=["order_count"], dimensions=["orders.has_discount"])
    expected = {r["orders__has_discount"]: r["order_count"] for r in grouped["rows"]}
    assert {v["value"]: v["count"] for v in result["values"]} == expected
    rule = call(server, "dimension_values", name="rule.active_customer")
    assert {v["value"] for v in rule["values"]} <= {True, False, None}
    assert rule["evidence"]["rules"] == [
        {"name": "active_customer", "effective_from": "2026-07-01"}
    ]


def test_dimension_values_refuse_personal_and_non_categorical(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    for name in ("customers.email", "customers.health_plan"):
        result = call(server, "dimension_values", name=name)
        assert result["error"] == "PolicyError" and "never listed" in result["detail"]
    result = call(server, "dimension_values", name="payments.installments")
    assert result["error"] == "QueryError" and "categorical and boolean" in result["detail"]
    grained = call(server, "dimension_values", name="customers.created_at__month")
    assert grained["error"] == "QueryError" and "always in days" in grained["detail"]
    unknown = call(server, "dimension_values", name="customers.planet")
    assert unknown["error"] == "QueryError" and "does not exist" in unknown["detail"]


def test_dimension_values_are_capped(server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    full = call(server, "dimension_values", name="products.category")
    two = call(server, "dimension_values", name="products.category", limit=2)
    assert two["values"] == full["values"][:2] and two["truncated"] is True
    assert len(full["values"]) > 2 and full["limit"] == 100
    assert call(server, "dimension_values", name="products.category", limit=10_000)["limit"] == 100
    assert call(server, "dimension_values", name="orders.status", limit=0)["error"] == "QueryError"


def test_time_range_is_the_days_the_user_can_see(server, duckdb_connector, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "dimension_values", name="customers.created_at", limit=1)
    grouped = call(
        server, "query", metrics=["customer_count"], dimensions=["customers.created_at__day"]
    )
    days = sorted(r["customers__created_at__day"][:10] for r in grouped["rows"])
    assert result["dimension"] == "customers.created_at" and result["type"] == "time"
    assert (result["min"], result["max"], result["distinct_days"]) == (
        days[0],
        days[-1],
        len(days),
    )
    applied = result["evidence"]["row_filters"]
    assert applied[0]["name"] == "by_region" and applied[0]["user_fields"] == {"region": "Midwest"}
    assert result["evidence"]["timezone"] == "America/New_York"
    # The row filter narrows the range: other regions' days are not revealed.
    every = duckdb_connector.execute(
        "SELECT COUNT(DISTINCT created_at) AS n FROM public.customers"
    ).to_pylist()[0]["n"]
    assert result["distinct_days"] < every


def test_time_range_of_a_timestamp_column(server, duckdb_connector, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "agente-sul")
    result = call(server, "dimension_values", name="orders.ordered_at")
    day = "CAST(ordered_at AS DATE)"
    want = duckdb_connector.execute(
        f"SELECT MIN({day}) AS a, MAX({day}) AS b, COUNT(DISTINCT {day}) AS n FROM public.orders"
    ).to_pylist()[0]
    assert (result["min"], result["max"], result["distinct_days"]) == (
        want["a"].isoformat(),
        want["b"].isoformat(),
        want["n"],
    )
    assert len(result["min"]) == 10  # an ISO date, not a timestamp
