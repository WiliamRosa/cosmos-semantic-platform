"""Remote MCP as an OAuth protected resource (MCP authorization spec, RFC 9728)."""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cosmos_semantic.api import create_app
from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.mcp_server import build_mcp
from cosmos_semantic.service import SemanticService

ISSUER = "https://login.microsoftonline.com/tenant-test/v2.0"
AUDIENCE = "api://cosmos-semantic"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class _Key:
    def __init__(self, key):
        self.key = key


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_client(graph, connector, signing_key, public_url=None, audit_path=None):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = connector
    settings = AuthSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://example.invalid/keys",
        dev_keys={"dev-sul": DevKey(subject="dev", user={"region": "Midwest"})},
    )
    authenticator = Authenticator(settings, graph.project.user_context)

    class FakeJWKS:
        def get_signing_key_from_jwt(self, _token):
            return _Key(signing_key.public_key())

    authenticator.__dict__["_jwks"] = FakeJWKS()
    service = SemanticService(engine, audit_path=audit_path)
    mcp_app = build_mcp(service, authenticator).streamable_http_app(
        streamable_http_path="/", host="testserver"
    )
    app = create_app(service, authenticator, mcp_app=mcp_app, public_url=public_url)
    return TestClient(app)


def token(key, **claims) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u1", "iat": now, "exp": now + 600}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256")


def call_tool(client, headers: dict[str, str], tool: str, **arguments):
    """Calls one MCP tool over streamable HTTP, in its own session, as the given credential."""
    headers = MCP_HEADERS | headers
    init = client.post("/mcp/", json=INITIALIZE, headers=headers)
    assert init.status_code == 200, init.text
    session = headers | {"mcp-session-id": init.headers["mcp-session-id"]}
    client.post(
        "/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session
    )
    message = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    response = client.post("/mcp/", json=message, headers=session)
    assert response.status_code == 200, response.text
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    result = json.loads(data[-1] if data else response.text)["result"]
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def test_metadata_names_the_identity_provider(graph, duckdb_connector, signing_key):
    with make_client(graph, duckdb_connector, signing_key) as client:
        meta = client.get("/.well-known/oauth-protected-resource").json()
        assert meta["resource"] == "http://testserver/mcp"
        assert meta["authorization_servers"] == [ISSUER]
        assert meta["bearer_methods_supported"] == ["header"]
        assert client.get("/.well-known/oauth-protected-resource/mcp").json() == meta


def test_public_url_is_used_behind_a_proxy(graph, duckdb_connector, signing_key):
    with make_client(graph, duckdb_connector, signing_key, "https://cosmos.example.com") as client:
        assert client.get("/.well-known/oauth-protected-resource").json()["resource"] == (
            "https://cosmos.example.com/mcp"
        )
        response = client.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
        assert (
            'resource_metadata="https://cosmos.example.com/.well-known'
            in (response.headers["www-authenticate"])
        )


def test_missing_token_gets_a_401_challenge(graph, duckdb_connector, signing_key):
    with make_client(graph, duckdb_connector, signing_key) as client:
        response = client.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource"' in (
            challenge
        )


def test_invalid_token_and_missing_claims(graph, duckdb_connector, signing_key):
    with make_client(graph, duckdb_connector, signing_key) as client:
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad = client.post(
            "/mcp/",
            json=INITIALIZE,
            headers=MCP_HEADERS | {"authorization": f"Bearer {token(other_key, region='Midwest')}"},
        )
        assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]
        no_claim = client.post(
            "/mcp/",
            json=INITIALIZE,
            headers=MCP_HEADERS | {"authorization": f"Bearer {token(signing_key)}"},
        )
        assert no_claim.status_code == 403
        assert 'error="insufficient_scope"' in no_claim.headers["www-authenticate"]


def test_valid_token_reaches_the_mcp_server(graph, duckdb_connector, signing_key):
    with make_client(graph, duckdb_connector, signing_key) as client:
        for headers in (
            {"authorization": f"Bearer {token(signing_key, region='Midwest')}"},
            {"x-api-key": "dev-sul"},
        ):
            response = client.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS | headers)
            assert response.status_code == 200, response.text
            assert "cosmos-semantic" in response.text


def test_a_user_cannot_replay_another_users_query(graph, duckdb_connector, signing_key, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    alice = {"authorization": f"Bearer {token(signing_key, sub='alice', region='Midwest')}"}
    dev = {"x-api-key": "dev-sul"}
    with make_client(graph, duckdb_connector, signing_key, audit_path=audit_path) as client:
        result = call_tool(client, dev, "query", metrics=["customer_count"])
        query_id = result["evidence"]["query_id"]
        foreign = call_tool(client, alice, "replay", query_id=query_id)
        unknown = call_tool(client, alice, "replay", query_id="does-not-exist")
        # Another user's query looks exactly like an id that does not exist.
        assert foreign["error"] == unknown["error"] == "QueryError"
        assert foreign["detail"] == unknown["detail"].replace("does-not-exist", query_id)
        assert "verdict" not in foreign
        assert call_tool(client, dev, "replay", query_id=query_id)["verdict"] == "identical"
