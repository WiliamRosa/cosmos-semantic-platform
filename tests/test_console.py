"""Console views (ADR 0012): read-only, built from the model, the audit log and check runs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cosmos_semantic.api import create_app
from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.checks import append_run, read_runs, run_checks
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.console import Console
from cosmos_semantic.engine import Engine
from cosmos_semantic.evidence import EvidenceBuilder
from cosmos_semantic.planner import Principal
from cosmos_semantic.reconcile import ZooCase
from cosmos_semantic.service import SemanticService
from cosmos_semantic.spec import Project

ADMIN = {"X-API-Key": "lead"}
ANALYST = {"X-API-Key": "analyst"}

# A legacy report that disagrees with the governed metric (canceled orders included), with no
# expectation: outside the zoo, a case passes only when the numbers match.
LEGACY_REVENUE = ZooCase(
    id="R1",
    title="Finance revenue report",
    request={"metrics": ["completed_revenue"], "dimensions": ["orders.status"]},
    sql="SELECT status, SUM(amount) FROM public.orders GROUP BY status",
)
MATCHING = ZooCase(
    id="R2",
    title="Order count dashboard",
    request={"metrics": ["order_count"], "dimensions": ["orders.status"]},
    sql="SELECT status, COUNT(*) FROM public.orders GROUP BY status",
)


@pytest.fixture
def setup(graph, duckdb_connector, tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    monkeypatch.setenv("COSMOS_RUNS_LOG", str(runs))
    monkeypatch.setenv("COSMOS_CONSOLE_ADMINS", "lead@example.com")
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    service = SemanticService(engine, audit_path=tmp_path / "audit.jsonl")
    settings = AuthSettings(
        dev_keys={
            "lead": DevKey(subject="lead@example.com", user={"region": "Midwest"}),
            "analyst": DevKey(subject="analyst@example.com", user={"region": "South"}),
        }
    )
    client = TestClient(create_app(service, Authenticator(settings, graph.project.user_context)))
    return client, service, runs


def _check(graph, duckdb_connector, runs, cases):
    run = run_checks(
        graph,
        model_hash=EvidenceBuilder(graph).model_hash,
        connector=duckdb_connector,
        source="local",
        cases=cases,
    )
    append_run(runs, run)
    return run


def _ask(client, headers, metrics):
    response = client.post("/v1/query", headers=headers, json={"metrics": metrics})
    assert response.status_code == 200, response.text
    return response.json()["evidence"]["query_id"]


def test_console_needs_credentials(setup):
    client, _, _ = setup
    assert client.get("/v1/console/overview").status_code == 401


def test_overview_counts_governance_and_usage(setup, graph):
    client, _, _ = setup
    _ask(client, ANALYST, ["revenue"])
    _ask(client, ADMIN, ["revenue", "order_count"])
    body = client.get("/v1/console/overview", headers=ANALYST).json()
    assert body["counts"]["metrics"] == len(graph.metrics)
    assert body["governance"]["rule_tests_failed"] == 0
    assert body["governance"]["rule_tests_total"] > 0
    assert body["usage"]["queries_30d"] == 2
    assert body["usage"]["top_metrics"][0] == {"metric": "revenue", "queries": 2}
    assert body["latest_check"] is None
    assert body["viewer"] == {"subject": "analyst@example.com", "admin": False}


def test_answers_are_private_unless_admin(setup):
    client, _, _ = setup
    mine = _ask(client, ANALYST, ["revenue"])
    theirs = _ask(client, ADMIN, ["order_count"])
    own = client.get("/v1/console/answers", headers=ANALYST).json()
    assert own["scope"] == "yours"
    assert [a["query_id"] for a in own["answers"]] == [mine]
    everyone = client.get("/v1/console/answers", headers=ADMIN).json()
    assert {a["query_id"] for a in everyone["answers"]} == {mine, theirs}
    assert client.get(f"/v1/console/answers/{theirs}", headers=ANALYST).status_code == 403
    detail = client.get(f"/v1/console/answers/{mine}", headers=ANALYST).json()
    assert detail["formulas"]["revenue"] and detail["sql"] and not detail["model_changed"]
    assert detail["user_context"] == {"region": "South"}
    assert client.get("/v1/console/usage", headers=ANALYST).status_code == 403
    assert client.get("/v1/console/usage", headers=ADMIN).json()["available"] is True


def test_check_runs_drive_health(setup, graph, duckdb_connector):
    client, _, runs = setup
    run = _check(graph, duckdb_connector, runs, [LEGACY_REVENUE, MATCHING])
    assert [c["outcome"] for c in run["reconciliations"]] == ["fail", "pass"]
    assert run["reconciliations"][0]["causes"]

    body = client.get("/v1/console/overview", headers=ANALYST).json()
    assert body["latest_check"]["ok"] is False
    assert body["latest_check"]["reconciliations"]["fail"] == 1
    catalog = {
        i["id"]: i for i in client.get("/v1/console/catalog", headers=ANALYST).json()["items"]
    }
    assert catalog["metric:completed_revenue"]["state"] == "failing"
    assert catalog["measure:order_count"]["state"] == "healthy"
    assert catalog["metric:discount_rate"]["state"] == "unchecked"
    member = client.get("/v1/console/members/metric:completed_revenue", headers=ANALYST).json()
    assert member["health"]["state"] == "failing"
    assert member["reconciliations"][0]["id"] == "R1"
    assert member["formula"]
    history = client.get("/v1/console/runs", headers=ANALYST).json()
    assert [r["run_id"] for r in history["runs"]] == [run["run_id"]]


def test_a_failing_rule_marks_what_depends_on_it(graph, duckdb_connector, tmp_path):
    data = graph.project.model_dump(mode="json")
    rule = next(r for r in data["business_rules"] if r["name"] == "active_customer")
    rule["tests"][0]["expect"] = not rule["tests"][0]["expect"]
    broken = compile_project(Project.model_validate(data))
    engine = Engine(
        broken, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")})
    )
    console = Console(SemanticService(engine), runs_path=tmp_path / "none.jsonl", admins=set())
    catalog = {i["id"]: i for i in console.catalog()["items"]}
    assert catalog["rule:active_customer"]["state"] == "failing"
    assert catalog["rule:active_customer"]["reasons"][0]["code"] == "rule_tests_fail"
    # In the example only a process uses the rule; it needs attention, with the reason.
    retention = {p["name"]: p for p in console.processes()}["customer_retention"]
    assert retention["state"] == "attention"
    assert {"code": "depends_on_failing", "member": "rule:active_customer"} in retention["reasons"]


def test_process_graph_carries_health_and_owners(setup):
    client, _, _ = setup
    processes = client.get("/v1/console/processes", headers=ANALYST).json()
    assert processes, "the example model declares processes"
    name = processes[0]["name"]
    graph = client.get(f"/v1/console/graph?focus=process:{name}", headers=ANALYST).json()
    kinds = {n["kind"] for n in graph["nodes"]}
    assert {"process", "metric", "team"} <= kinds
    assert all("state" in n for n in graph["nodes"] if n["kind"] in ("metric", "rule"))
    assert any(e["relation"] == "owns" for e in graph["edges"])


def test_member_detail_for_a_rule(setup):
    client, _, _ = setup
    body = client.get("/v1/console/members/rule:active_customer", headers=ANALYST).json()
    assert body["detail"]["kind"] == "rule"
    assert body["tests"]["total"] > 0 and body["tests"]["failed"] == 0
    assert body["impact"]["affected"]["metrics"] or body["impact"]["affected"]["rules"]


def test_runs_log_ignores_other_events(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text('{"event": "query"}\n\n', encoding="utf-8")
    assert list(read_runs(path)) == []
    assert list(read_runs(tmp_path / "missing.jsonl")) == []


def test_console_is_served_or_explained(setup):
    client, _, _ = setup
    response = client.get("/console/", follow_redirects=True)
    assert response.status_code in (200, 404) or "console" in response.text.lower()


def test_admin_flag(graph):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    console = Console(SemanticService(engine), admins={"lead@example.com"})
    assert console.is_admin(Principal(subject="lead@example.com"))
    # --allow-anonymous, local only: recognized by the marker, not by the name.
    assert console.is_admin(Principal(subject="anonymous", local_anonymous=True))
    assert not console.is_admin(Principal(subject="anonymous"))
    assert not console.is_admin(Principal(subject="someone@example.com"))


def test_console_files_map_next_segment_prefetches(tmp_path):
    from fastapi import FastAPI

    from cosmos_semantic.api import _console_files

    (tmp_path / "index.html").write_text("<html>console</html>", encoding="utf-8")
    (tmp_path / "usage" / "__next.usage").mkdir(parents=True)
    (tmp_path / "usage" / "__next.usage" / "__PAGE__.txt").write_text("rsc", encoding="utf-8")
    app = FastAPI()
    app.mount("/console", _console_files(tmp_path))
    client = TestClient(app)
    assert client.get("/console/").text == "<html>console</html>"
    assert client.get("/console/usage/__next.usage.__PAGE__.txt").text == "rsc"
    assert client.get("/console/usage/__next.nothing.__PAGE__.txt").status_code == 404
