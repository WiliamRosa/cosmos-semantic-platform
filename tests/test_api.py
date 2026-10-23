from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cosmos_semantic.api import create_app
from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.service import SemanticService

ISSUER = "https://login.microsoftonline.com/tenant-teste/v2.0"
AUDIENCE = "api://cosmos-semantic"


class _Key:
    def __init__(self, key):
        self.key = key


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def client(graph, duckdb_connector, signing_key):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    settings = AuthSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://example.invalid/keys",
        dev_keys={"dev-sul": DevKey(subject="dev@local", user={"region": "Sul"})},
    )
    authenticator = Authenticator(settings, graph.project.user_context)

    class FakeJWKS:
        def get_signing_key_from_jwt(self, _token):
            return _Key(signing_key.public_key())

    authenticator.__dict__["_jwks"] = FakeJWKS()
    return TestClient(create_app(SemanticService(engine), authenticator))


def token(key, **claims) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u1", "iat": now, "exp": now + 600}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256")


def test_requires_credentials(client):
    response = client.get("/v1/members")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_dev_key(client):
    response = client.post(
        "/v1/query",
        headers={"X-API-Key": "dev-sul"},
        json={"metrics": ["customer_count"], "dimensions": ["customers.region"]},
    )
    assert response.status_code == 200, response.text
    assert [r["customers__region"] for r in response.json()["rows"]] == ["Sul"]


def test_oidc_token_claims_feed_row_filters(client, signing_key):
    response = client.post(
        "/v1/query",
        headers={"Authorization": f"Bearer {token(signing_key, region='Norte')}"},
        json={"metrics": ["customer_count"], "dimensions": ["customers.region"]},
    )
    assert response.status_code == 200, response.text
    assert [r["customers__region"] for r in response.json()["rows"]] == ["Norte"]
    evidence = response.json()["evidence"]
    assert evidence["policies"]["row_filters"][0]["user_fields"] == {"region": "Norte"}


def test_token_without_required_claim_is_forbidden(client, signing_key):
    response = client.get("/v1/members", headers={"Authorization": f"Bearer {token(signing_key)}"})
    assert response.status_code == 403
    assert "region" in response.json()["detail"]


def test_wrong_audience_or_expired_token_is_rejected(client, signing_key):
    for bad in (
        token(signing_key, region="Sul", aud="api://outra"),
        token(signing_key, region="Sul", exp=int(time.time()) - 10),
        token(rsa.generate_private_key(public_exponent=65537, key_size=2048), region="Sul"),
    ):
        response = client.get("/v1/members", headers={"Authorization": f"Bearer {bad}"})
        assert response.status_code == 401


def test_errors_map_to_http_status(client):
    headers = {"X-API-Key": "dev-sul"}
    bad = client.post("/v1/query", headers=headers, json={"metrics": ["nao_existe"]})
    assert bad.status_code == 400
    sql = client.post("/v1/sql", headers=headers, json={"metrics": ["revenue"]})
    assert sql.status_code == 200 and "SUM" in sql.json()["sql"]


def test_concepts_and_explain(client):
    headers = {"X-API-Key": "dev-sul"}
    hits = client.get("/v1/concepts", params={"q": "faturamento líquido"}, headers=headers).json()
    assert hits[0]["maps_to"] == "metric:net_revenue"
    rule = client.get("/v1/explain/rule:order_size_band", headers=headers).json()
    assert rule["kind"] == "rule" and "Grande" in rule["text"]
