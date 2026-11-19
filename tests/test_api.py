from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cosmos_semantic.api import create_app
from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.service import SemanticService
from cosmos_semantic.spec import Project

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
        dev_keys={"dev-sul": DevKey(subject="dev@local", user={"region": "Midwest"})},
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
    assert [r["customers__region"] for r in response.json()["rows"]] == ["Midwest"]


def test_oidc_token_claims_feed_row_filters(client, signing_key):
    response = client.post(
        "/v1/query",
        headers={"Authorization": f"Bearer {token(signing_key, region='West')}"},
        json={"metrics": ["customer_count"], "dimensions": ["customers.region"]},
    )
    assert response.status_code == 200, response.text
    assert [r["customers__region"] for r in response.json()["rows"]] == ["West"]
    evidence = response.json()["evidence"]
    assert evidence["policies"]["row_filters"][0]["user_fields"] == {"region": "West"}


def test_token_without_required_claim_is_forbidden(client, signing_key):
    response = client.get("/v1/members", headers={"Authorization": f"Bearer {token(signing_key)}"})
    assert response.status_code == 403
    assert "region" in response.json()["detail"]


def test_wrong_audience_or_expired_token_is_rejected(client, signing_key):
    for bad in (
        token(signing_key, region="Midwest", aud="api://outra"),
        token(signing_key, region="Midwest", exp=int(time.time()) - 10),
        token(rsa.generate_private_key(public_exponent=65537, key_size=2048), region="Midwest"),
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
    assert rule["kind"] == "rule" and "Large" in rule["text"]
    assert rule["health"] == {
        "state": "healthy",
        "reasons": [{"code": "rule_tests_pass", "total": 3}],
        "last_reconciled_at": None,
        "last_tests_run_at": None,
    }


def test_explain_carries_the_console_health(graph, monkeypatch, tmp_path):
    runs = tmp_path / "runs.jsonl"
    case = {"id": "R1", "title": "Legacy", "metrics": ["completed_revenue"], "outcome": "error"}
    run = {"event": "check", "at": "2026-11-15T12:00:00+00:00", "run_id": "run_1"}
    runs.write_text(json.dumps(run | {"reconciliations": [case]}) + "\n")
    monkeypatch.setenv("COSMOS_RUNS_LOG", str(runs))
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    settings = AuthSettings(dev_keys={"analyst": DevKey(subject="analyst@example.com", user={})})
    service = SemanticService(engine, admins=set())
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))
    headers = {"X-API-Key": "analyst"}

    assert client.get("/v1/explain/metric:completed_revenue").status_code == 401
    metric = client.get("/v1/explain/metric:completed_revenue", headers=headers).json()
    console = client.get("/v1/console/members/metric:completed_revenue", headers=headers).json()
    assert metric["health"] == console["health"]
    assert metric["health"]["state"] == "failing"
    assert metric["health"]["last_reconciled_at"] == "2026-11-15T12:00:00+00:00"
    assert metric["health"]["reasons"][0]["at"] == "2026-11-15T12:00:00+00:00"
    # The run recorded no rule tests, so none ran in it.
    assert metric["health"]["last_tests_run_at"] is None
    assert {k: v for k, v in metric.items() if k != "health"} == console["detail"]
    assert client.get("/v1/explain/metric:net_revenue", headers=headers).json()["health"] == {
        "state": "unchecked",
        "reasons": [{"code": "never_reconciled"}],
        "last_reconciled_at": None,
        "last_tests_run_at": None,
    }
    unknown = client.get("/v1/explain/metric:does_not_exist", headers=headers)
    assert unknown.status_code == 400 and "health" not in unknown.text


def test_explain_and_console_agree_on_a_stale_reconciliation(graph, monkeypatch, tmp_path):
    data = graph.project.model_dump(mode="json")
    data["health"] = {"max_age": "2 weeks"}
    aged = compile_project(Project.model_validate(data))
    runs = tmp_path / "runs.jsonl"
    at = "2020-01-01T00:00:00+00:00"
    case = {"id": "R2", "title": "Orders", "metrics": ["order_count"], "outcome": "pass"}
    run = {"event": "check", "run_id": "run_1", "at": at, "reconciliations": [case]}
    runs.write_text(json.dumps(run) + "\n")
    monkeypatch.setenv("COSMOS_RUNS_LOG", str(runs))
    engine = Engine(aged, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    settings = AuthSettings(dev_keys={"analyst": DevKey(subject="analyst@example.com", user={})})
    service = SemanticService(engine, admins=set())
    client = TestClient(create_app(service, Authenticator(settings, aged.project.user_context)))
    headers = {"X-API-Key": "analyst"}

    explained = client.get("/v1/explain/order_count", headers=headers).json()["health"]
    member = client.get("/v1/console/members/measure:order_count", headers=headers).json()
    assert explained == member["health"]
    assert explained["state"] == "attention" and explained["last_reconciled_at"] == at
    assert {"code": "reconciliation_stale", "at": at, "max_age": "2 weeks"} in explained["reasons"]


def test_explain_survives_an_unreadable_runs_log(graph, monkeypatch, tmp_path, caplog):
    runs = tmp_path / "runs.jsonl"
    runs.write_text('{"event": "check", "secret-ish": \n', encoding="utf-8")
    monkeypatch.setenv("COSMOS_RUNS_LOG", str(runs))
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    settings = AuthSettings(dev_keys={"analyst": DevKey(subject="analyst@example.com", user={})})
    service = SemanticService(engine, admins=set())
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))

    with caplog.at_level("WARNING", logger="cosmos_semantic.service"):
        response = client.get("/v1/explain/metric:net_revenue", headers={"X-API-Key": "analyst"})
    assert response.status_code == 200, response.text
    assert response.json()["health"] == {
        "state": "unchecked",
        "reasons": [{"code": "runs_log_unreadable"}],
        "last_reconciled_at": None,
        "last_tests_run_at": None,
    }
    assert "cannot read the check runs log" in caplog.text and "secret-ish" not in caplog.text


def test_glossary_as_skos(client):
    response = client.get(
        "/v1/glossary", params={"format": "jsonld"}, headers={"X-API-Key": "dev-sul"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/ld+json")
    ids = {node["@id"] for node in response.json()}
    assert "urn:cosmos-semantic:glossary:concept/active_customer" in ids
    turtle = client.get("/v1/glossary", headers={"X-API-Key": "dev-sul"})
    assert turtle.headers["content-type"].startswith("text/turtle")
    bad = client.get("/v1/glossary", params={"format": "x"}, headers={"X-API-Key": "dev-sul"})
    assert bad.status_code == 400


def test_impact_shows_who_queried_only_to_admins(graph, duckdb_connector, tmp_path):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    settings = AuthSettings(
        dev_keys={
            "analyst": DevKey(subject="analyst@example.com", user={"region": "Midwest"}),
            "lead": DevKey(subject="lead@example.com", user={"region": "Midwest"}),
        }
    )
    service = SemanticService(
        engine, audit_path=tmp_path / "audit.jsonl", admins={"lead@example.com"}
    )
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))
    analyst, lead = {"X-API-Key": "analyst"}, {"X-API-Key": "lead"}
    ran = client.post(
        "/v1/query",
        headers=analyst,
        json={"metrics": ["customer_count"], "filters": ["{rule.active_customer}"]},
    )
    assert ran.status_code == 200, ran.text

    assert client.get("/v1/impact/rule:active_customer").status_code == 401
    response = client.get("/v1/impact/rule:active_customer", headers=analyst)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["affected"]["rules"] == ["active_customer"]
    assert "sales-leadership" in report["owners"]
    assert report["consumers_visible"] is False and "consumers" not in report
    assert "analyst@example.com" not in response.text

    admin = client.get("/v1/impact/rule:active_customer", headers=lead).json()
    assert admin["consumers_visible"] is True
    assert [(c["subject"], c["channel"], c["queries"]) for c in admin["consumers"]] == [
        ("analyst@example.com", "api", 1)
    ]

    unknown = client.get("/v1/impact/metric:does_not_exist", headers=lead)
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "QueryError"
    assert "does not exist in the model" in unknown.json()["detail"]
    assert client.get("/v1/impact/active_customer", headers=lead).status_code == 400


def test_admin_depends_on_the_anonymous_marker_not_the_name(graph, duckdb_connector, tmp_path):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    audit = tmp_path / "audit.jsonl"
    service = SemanticService(engine, audit_path=audit, admins={"lead@example.com"})
    # A credential whose subject is literally "anonymous" is an ordinary user.
    settings = AuthSettings(
        dev_keys={"anon": DevKey(subject="anonymous", user={"region": "Midwest"})}
    )
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))
    headers = {"X-API-Key": "anon"}
    ran = client.post(
        "/v1/query",
        headers=headers,
        json={"metrics": ["customer_count"], "filters": ["{rule.active_customer}"]},
    )
    assert ran.status_code == 200, ran.text
    report = client.get("/v1/impact/rule:active_customer", headers=headers).json()
    assert report["consumers_visible"] is False and "consumers" not in report

    # The user that --allow-anonymous creates (local development) still sees everything.
    local = TestClient(create_app(service, None, allow_anonymous=True))
    admin = local.get("/v1/impact/rule:active_customer").json()
    assert admin["consumers_visible"] is True
    assert [c["subject"] for c in admin["consumers"]] == ["anonymous"]


def test_dimension_values_endpoint_is_audited(graph, duckdb_connector, tmp_path):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    settings = AuthSettings(
        dev_keys={"dev-sul": DevKey(subject="dev@local", user={"region": "Midwest"})}
    )
    audit_path = tmp_path / "audit.jsonl"
    service = SemanticService(engine, audit_path=audit_path)
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))
    headers = {"X-API-Key": "dev-sul"}

    response = client.get("/v1/dimensions/orders.status/values", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    grouped = client.post(
        "/v1/query",
        headers=headers,
        json={"metrics": ["order_count"], "dimensions": ["orders.status"]},
    ).json()
    expected = {r["orders__status"]: r["order_count"] for r in grouped["rows"]}
    assert {v["value"]: v["count"] for v in body["values"]} == expected
    region = client.get("/v1/dimensions/customers.region/values", headers=headers).json()
    assert [v["value"] for v in region["values"]] == ["Midwest"]
    limited = client.get(
        "/v1/dimensions/orders.status/values", params={"limit": 1}, headers=headers
    ).json()
    assert len(limited["values"]) == 1 and limited["truncated"] is True

    personal = client.get("/v1/dimensions/customers.tax_id/values", headers=headers)
    assert personal.status_code == 403
    assert client.get("/v1/dimensions/orders.ordered_at/values", headers=headers).status_code == 400
    assert client.get("/v1/dimensions/nope.nope/values", headers=headers).status_code == 400
    zero = client.get("/v1/dimensions/orders.status/values", params={"limit": 0}, headers=headers)
    assert zero.status_code == 400

    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    values = [r for r in records if r["event"] == "dimension_values"]
    first = values[0]
    assert first["query_id"] == body["evidence"]["query_id"] and first["outcome"] == "ok"
    assert first["principal"]["subject"] == "dev@local"
    assert first["request"] == {"dimensions": ["orders.status"], "limit": 100}
    assert "GROUP BY" in first["sql"] and first["row_count"] == len(body["values"])
    assert [r["outcome"] for r in values[-4:]] == ["error"] * 4
    assert "PolicyError" in values[-4]["error"]
    assert values[-1]["request"]["limit"] == 0 and "at least 1" in values[-1]["error"]
    # A dimension_values record is not a query: it does not replay.
    replay = client.post(f"/v1/replay/{first['query_id']}", headers=headers)
    assert replay.status_code == 400
