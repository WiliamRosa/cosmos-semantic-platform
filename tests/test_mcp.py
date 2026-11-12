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


def test_lists_the_seven_tools(server):
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
