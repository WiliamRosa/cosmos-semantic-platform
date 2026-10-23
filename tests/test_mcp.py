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
def server(graph, duckdb_connector):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    settings = AuthSettings(
        dev_keys={"agente-sul": DevKey(subject="agente", user={"region": "Sul"})}
    )
    return build_mcp(SemanticService(engine), Authenticator(settings, graph.project.user_context))


def call(server, tool: str, **arguments):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments)
            return result

    result = asyncio.run(run())
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_lists_the_five_tools(server):
    async def run():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(run()) == {"search_concepts", "explain", "list_members", "query", "show_sql"}


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
    assert "'Sul'" in result["sql"]
