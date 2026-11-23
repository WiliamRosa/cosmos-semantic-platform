"""DEC-45: declared reconciliation cases through the REST API and MCP.

Only cases the operator declared run (``COSMOS_CHECK_CASES``), as the caller, with the same
auth and audit as ``query``; each run is recorded so health updates. Row filters cannot be
applied to a case's hand-written SQL, so a case is refused when a row filter applies to it or
when its SQL may read a table a row filter guards: a caller never sees rows outside their
filter, in the row diff or anywhere else."""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import Client

from cosmos_semantic.api import create_app
from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.checks import append_run, read_runs, run_checks
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.evidence import EvidenceBuilder
from cosmos_semantic.health import Health
from cosmos_semantic.mcp_server import build_mcp
from cosmos_semantic.reconcile import ZooCase, row_filter_conflicts
from cosmos_semantic.service import MAX_RECONCILE_DIFF_ROWS, SemanticService

MIDWEST = {"X-API-Key": "midwest"}

CASES = [
    {
        # Counts canceled orders too: divergent from the governed metric.
        "id": "R1",
        "title": "Finance revenue report",
        "request": {"metrics": ["completed_revenue"], "dimensions": ["orders.status"]},
        "sql": "SELECT status, SUM(amount) FROM public.orders GROUP BY status",
    },
    {
        "id": "R2",
        "title": "Order count dashboard",
        "request": {"metrics": ["order_count"], "dimensions": ["orders.status"]},
        "sql": "WITH o AS (SELECT * FROM public.orders) "
        "SELECT status, COUNT(*) FROM o GROUP BY status",
    },
    {
        # The governed side reads `customers`, which has a row filter (by region).
        "id": "SEG",
        "title": "Customers by segment",
        "request": {"metrics": ["customer_count"], "dimensions": ["customers.segment"]},
        "sql": "SELECT segment, COUNT(DISTINCT customer_id) FROM public.customers GROUP BY 1",
    },
    {
        # The governed side has no row filter, but the hand-written SQL reads `customers`.
        "id": "SNEAK",
        "title": "Orders by status, South only",
        "request": {"metrics": ["order_count"], "dimensions": ["orders.status"]},
        "sql": "SELECT o.status, COUNT(*) FROM public.orders o "
        "JOIN public.customers c ON c.customer_id = o.customer_id "
        "WHERE c.region = 'South' GROUP BY o.status",
    },
    {
        # Fails on the database; the error quotes the hand-written SQL.
        "id": "BROKEN",
        "title": "A report whose column is gone",
        "request": {"metrics": ["order_count"], "dimensions": ["orders.status"]},
        "sql": "SELECT status, SUM(secret_column_xyz) FROM public.orders GROUP BY status",
    },
]
ALL_IDS = ["R1", "R2", "SEG", "SNEAK", "BROKEN"]


def _service(graph, duckdb_connector, tmp_path, interval: float = 0.0) -> SemanticService:
    cases = tmp_path / "cases.yml"
    cases.write_text(yaml.safe_dump({"cases": CASES}), encoding="utf-8")
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    service = SemanticService(
        engine,
        audit_path=tmp_path / "audit.jsonl",
        cases_path=cases,
        runs_path=tmp_path / "runs.jsonl",
        admins=set(),
        reconcile_min_interval=interval,
    )
    return service


@pytest.fixture
def setup(graph, duckdb_connector, tmp_path):
    service = _service(graph, duckdb_connector, tmp_path)
    settings = AuthSettings(
        dev_keys={"midwest": DevKey(subject="analyst@example.com", user={"region": "Midwest"})}
    )
    authenticator = Authenticator(settings, graph.project.user_context)
    client = TestClient(create_app(service, authenticator))
    return client, service, authenticator


def _audit(service) -> list[dict]:
    lines = service.audit_path.read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r["event"] == "reconcile"]


def _runs(service) -> list[dict]:
    return list(read_runs(service.health.runs_path))


def test_reconcile_needs_credentials(setup):
    client, _, _ = setup
    assert client.post("/v1/reconcile/R1").status_code == 401
    assert client.get("/v1/reconcile").status_code == 401


def test_lists_declared_cases_without_their_sql(setup):
    client, _, _ = setup
    body = client.get("/v1/reconcile", headers=MIDWEST).json()
    assert body["available"] is True
    assert [c["id"] for c in body["cases"]] == ALL_IDS
    assert all(c.keys() == {"id", "title"} for c in body["cases"])
    assert "SELECT" not in json.dumps(body)


def test_divergent_case_returns_diff_diagnosis_and_evidence(setup):
    client, service, _ = setup
    response = client.post("/v1/reconcile/R1", headers=MIDWEST)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["case"] == {"id": "R1", "title": "Finance revenue report"}
    assert body["outcome"] == "fail" and body["status"] == "divergent"
    assert (
        body["diff"] and {"key", "cosmos", "legacy", "difference", "side"} <= body["diff"][0].keys()
    )
    rows = body["rows"]
    assert len(body["diff"]) == rows["differing"] + rows["only_in_cosmos"] + rows["only_in_legacy"]
    assert body["causes"]
    assert body["evidence"]["row_filters"] == []
    assert body["evidence"]["sql"]["legacy_hash"] and body["evidence"]["formula"]
    # The legacy SQL itself never leaves the server.
    assert "public.orders" not in json.dumps(body)

    # Audited like a query, with the caller's identity.
    [record] = _audit(service)
    assert record["outcome"] == "ok" and record["query_id"] == body["evidence"]["query_id"]
    assert record["principal"]["subject"] == "analyst@example.com"
    assert record["principal"]["channel"] == "api"
    assert record["request"]["case"] == "R1" and record["sql"]
    # Not a query: it does not replay.
    replay = client.post(f"/v1/replay/{record['query_id']}", headers=MIDWEST)
    assert replay.status_code == 400

    # Recorded as a partial check run, so health updates.
    [run] = _runs(service)
    assert run["partial"] is True and run["channel"] == "api"
    assert run["run_id"] == body["evidence"]["run_id"]
    assert [c["id"] for c in run["reconciliations"]] == ["R1"]
    assert "subject" not in json.dumps(run)
    assert body["health"]["state"] == "failing"
    assert body["health"]["last_reconciled_at"] == run["at"]
    explained = client.get("/v1/explain/completed_revenue", headers=MIDWEST).json()
    assert explained["health"]["reasons"][0]["case"] == "R1"


def test_diff_is_capped(setup):
    client, _, _ = setup
    body = client.post("/v1/reconcile/R1?max_rows=1", headers=MIDWEST).json()
    assert len(body["diff"]) == 1
    rows = body["rows"]
    assert body["diff_truncated"] is (
        rows["differing"] + rows["only_in_cosmos"] + rows["only_in_legacy"] > 1
    )
    huge = client.post("/v1/reconcile/R1?max_rows=100000", headers=MIDWEST).json()
    assert len(huge["diff"]) <= MAX_RECONCILE_DIFF_ROWS
    assert client.post("/v1/reconcile/R1?max_rows=0", headers=MIDWEST).status_code == 400


def test_unknown_case_is_404_without_reflecting_the_id(setup):
    client, service, _ = setup
    name = "<img src=x onerror=alert(1)>"
    response = client.post(f"/v1/reconcile/{name}", headers=MIDWEST)
    assert response.status_code == 404
    assert response.json()["error"] == "NotFoundError"
    assert "onerror" not in response.text and "img" not in response.text
    [record] = _audit(service)
    assert record["outcome"] == "error" and record["request"] == {"case": "<invalid>"}
    assert _runs(service) == []


def test_without_declared_cases_nothing_runs(graph, duckdb_connector, tmp_path, monkeypatch):
    monkeypatch.delenv("COSMOS_CHECK_CASES", raising=False)
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    client = TestClient(create_app(SemanticService(engine), None, allow_anonymous=True))
    assert client.get("/v1/reconcile").json() == {"available": False, "cases": []}
    assert client.post("/v1/reconcile/R1").status_code == 404


@pytest.mark.parametrize("case", ["SEG", "SNEAK"])
def test_case_under_row_filters_is_refused(setup, case):
    """The safe option: row filters cannot be applied to hand-written SQL, so a case they could
    affect is refused, whoever asks, and no row of it is returned or recorded."""
    client, service, _ = setup
    response = client.post(f"/v1/reconcile/{case}", headers=MIDWEST)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"] == "PolicyError" and "row filter" in body["detail"]
    assert "diff" not in body and "South" not in response.text
    [record] = _audit(service)
    assert record["outcome"] == "error" and "PolicyError" in record["error"]
    assert _runs(service) == []


def test_conflicts_are_an_allowlist(graph):
    def conflicts(sql: str) -> list[str]:
        case = ZooCase(id="X", title="x", request={"metrics": ["order_count"]}, sql=sql)
        return row_filter_conflicts(case, graph, "duckdb")

    assert conflicts("SELECT COUNT(*) FROM public.orders") == []
    assert conflicts("SELECT COUNT(*) FROM orders") == []
    assert conflicts("WITH x AS (SELECT * FROM public.orders) SELECT COUNT(*) FROM x") == []
    assert conflicts("SELECT COUNT(*) FROM public.customers")
    assert conflicts("SELECT COUNT(*) FROM customers")  # unqualified still matches
    assert conflicts("SELECT COUNT(*) FROM (SELECT * FROM public.customers) c")
    assert conflicts(
        "SELECT COUNT(*) FROM public.orders WHERE customer_id IN "
        "(SELECT customer_id FROM public.customers)"
    )
    assert conflicts("SELECT COUNT(*) FROM public.orders_view")  # not a model's table
    assert conflicts("SELECT COUNT(*) FROM read_csv('x.csv')")  # a table function
    assert conflicts("SELEC nonsense FROM")


@pytest.mark.parametrize(
    "sql",
    [
        # A CTE shadowing the table it reads.
        "WITH customers AS (SELECT * FROM customers) SELECT segment, COUNT(*) FROM customers "
        "GROUP BY 1",
        # A CTE declared in another scope, under the name of a table read elsewhere.
        "SELECT segment, COUNT(*) FROM customers GROUP BY 1 UNION ALL SELECT c, 1 FROM "
        "(WITH customers AS (SELECT 1 c) SELECT * FROM customers) x",
        # A subquery reading the real table while another scope declares a CTE of that name.
        "SELECT status, COUNT(*) FROM orders WHERE customer_id IN "
        "(SELECT customer_id FROM customers) GROUP BY 1 UNION ALL SELECT 'x', 1 FROM "
        "(WITH customers AS (SELECT 1 customer_id) SELECT * FROM customers) y",
        # The same shapes with a table outside the model.
        "WITH secrets AS (SELECT * FROM secrets) SELECT COUNT(*) FROM secrets",
        "SELECT COUNT(*) FROM secrets UNION ALL SELECT 1 FROM "
        "(WITH secrets AS (SELECT 1 c) SELECT * FROM secrets) x",
        # A CTE named like a model's table, even one without row filters.
        "WITH orders AS (SELECT 1 AS status) SELECT status, COUNT(*) FROM orders GROUP BY 1",
    ],
)
def test_cte_shadowing_cannot_hide_a_table(graph, sql):
    case = ZooCase(id="X", title="x", request={"metrics": ["order_count"]}, sql=sql)
    assert row_filter_conflicts(case, graph, "duckdb")


def test_table_names_match_strictly_for_the_allowlist(graph):
    def conflicts(sql: str, dialect: str = "duckdb") -> list[str]:
        case = ZooCase(id="X", title="x", request={"metrics": ["order_count"]}, sql=sql)
        return row_filter_conflicts(case, graph, dialect)

    assert conflicts("SELECT COUNT(*) FROM PUBLIC.ORDERS") == []  # unquoted: any case
    assert conflicts('SELECT COUNT(*) FROM "Orders"')  # quoted: another table
    assert conflicts('SELECT COUNT(*) FROM public."orders"')  # quoted on one side only
    assert conflicts("SELECT COUNT(*) FROM other.orders")  # another schema
    assert conflicts("SELECT COUNT(*) FROM other_db.public.orders") == []  # model has no catalog
    # Against models with row filters, matching stays lenient.
    assert conflicts('SELECT COUNT(*) FROM "Customers"')
    assert conflicts("SELECT COUNT(*) FROM other.customers")
    assert conflicts('SELECT COUNT(*) FROM "PUBLIC"."CUSTOMERS"', "snowflake")


def test_a_failing_case_records_no_sql(setup):
    client, service, _ = setup
    response = client.post("/v1/reconcile/BROKEN", headers=MIDWEST)
    assert response.status_code == 400, response.text
    assert "secret_column_xyz" not in response.text
    [run] = _runs(service)
    [item] = run["reconciliations"]
    assert item["outcome"] == "error"
    assert item["detail"].endswith(": case could not run")
    log = service.health.runs_path.read_text(encoding="utf-8")
    assert "secret_column_xyz" not in log and "SUM(" not in log
    assert "secret_column_xyz" not in client.get("/v1/console/runs", headers=MIDWEST).text


def test_a_case_runs_at_most_once_per_interval(graph, duckdb_connector, tmp_path):
    service = _service(graph, duckdb_connector, tmp_path, interval=60)
    settings = AuthSettings(
        dev_keys={"midwest": DevKey(subject="analyst@example.com", user={"region": "Midwest"})}
    )
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))
    assert client.post("/v1/reconcile/R2", headers=MIDWEST).status_code == 200
    again = client.post("/v1/reconcile/R2", headers=MIDWEST)
    assert again.status_code == 429 and again.json()["error"] == "RateLimitError"
    # Another case has its own interval.
    assert client.post("/v1/reconcile/R1", headers=MIDWEST).status_code == 200
    assert [r["reconciliations"][0]["id"] for r in _runs(service)] == ["R2", "R1"]
    assert [r["outcome"] for r in _audit(service)] == ["ok", "error", "ok"]


def test_queries_run_between_the_statements_of_a_reconciliation(setup, monkeypatch):
    """The case holds the connection lock one statement at a time, never for the whole run."""
    client, service, _ = setup
    connector = service.engine._connectors["local"]
    original = connector.execute
    held = []

    def execute(*args, **kwargs):
        held.append(service._lock.locked())
        held.append(service._reconcile_lock.locked())
        return original(*args, **kwargs)

    monkeypatch.setattr(connector, "execute", execute)
    assert client.post("/v1/reconcile/R1", headers=MIDWEST).status_code == 200
    assert held and all(held)  # each statement under both locks...
    assert not service._lock.locked()  # ...and the connection lock free in between
    # A query between statements does not wait for the reconciliation lock.
    with service._reconcile_lock:
        assert (
            client.post("/v1/query", headers=MIDWEST, json={"metrics": ["order_count"]}).status_code
            == 200
        )


def test_partial_runs_keep_the_other_cases_of_the_full_run(graph, duckdb_connector, tmp_path):
    runs = tmp_path / "runs.jsonl"
    cases = [ZooCase.model_validate(c) for c in CASES[:2]]

    def record(selected, partial):
        run = run_checks(
            graph,
            model_hash=EvidenceBuilder(graph).model_hash,
            connector=duckdb_connector,
            source="local",
            cases=selected,
        )
        if partial:
            run["partial"] = True
        append_run(runs, run)
        return run

    full = record(cases, partial=False)
    partial = record(cases[1:], partial=True)
    states = Health(graph, runs_path=runs).states()
    # R1 still counts from the full run; R2 from the newer partial run.
    assert states["metric:completed_revenue"]["state"] == "failing"
    assert states["metric:completed_revenue"]["last_reconciled_at"] == full["at"]
    assert states["measure:order_count"]["state"] == "healthy"
    assert states["measure:order_count"]["last_reconciled_at"] == partial["at"]
    # A full run supersedes everything before it.
    record(cases[1:], partial=False)
    states = Health(graph, runs_path=runs).states()
    assert states["metric:completed_revenue"]["state"] == "unchecked"


def _call(server, tool: str, **arguments):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    result = asyncio.run(run())
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_mcp_reconcile(setup, monkeypatch):
    _, service, authenticator = setup
    server = build_mcp(service, authenticator)
    monkeypatch.setenv("COSMOS_API_KEY", "midwest")

    listed = _call(server, "reconcile")
    assert [c["id"] for c in listed["cases"]] == ALL_IDS
    assert all(c.keys() == {"id", "title"} for c in listed["cases"])

    report = _call(server, "reconcile", case="R2")
    assert report["outcome"] == "pass" and report["status"] == "match"
    assert report["diff"] == [] and report["health"]["state"] == "healthy"
    [record] = _audit(service)
    assert record["principal"]["channel"] == "mcp" and record["outcome"] == "ok"
    [run] = _runs(service)
    assert run["channel"] == "mcp" and run["partial"] is True

    refused = _call(server, "reconcile", case="SNEAK")
    assert refused["error"] == "PolicyError" and "diff" not in refused
    missing = _call(server, "reconcile", case="nope")
    assert missing["error"] == "NotFoundError" and "nope" not in missing["detail"]

    monkeypatch.delenv("COSMOS_API_KEY")
    assert _call(server, "reconcile", case="R2")["error"] == "AuthError"
