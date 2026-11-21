"""A metric's own reconciliation tolerance (DEC-39): a legacy report rounded to cents or
thousands agrees within the tolerance the metric declares, and differs outside it."""

from __future__ import annotations

import json

import pytest

from cosmos_semantic.checks import append_run, run_checks
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile
from cosmos_semantic.engine import Engine
from cosmos_semantic.evidence import EvidenceBuilder, model_hash
from cosmos_semantic.health import Health
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.reconcile import ZooCase, check_zoo_case, reconcile_sql
from cosmos_semantic.service import SemanticService
from cosmos_semantic.spec import Project, SimpleMetric, json_schema

SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
    "bigquery_connector",
]

# Average order value by status, rounded to one decimal in the legacy report (as zoo case Z09):
# each row is off by at most 0.05.
AOV_ROUNDED = {
    "request": {"metrics": ["average_order_value"], "dimensions": ["orders.status"]},
    "sql": "SELECT status, ROUND(SUM(amount) / COUNT(*), 1) FROM public.orders GROUP BY status",
    "sql_by_dialect": {
        "postgres": "SELECT status, ROUND(CAST(SUM(amount) / COUNT(*) AS NUMERIC), 1) "
        "FROM public.orders GROUP BY status",
    },
}
# Net revenue by status in thousands, as a finance report shows it: each row is off by at
# most 500, under 1% of every status's net revenue.
NET_IN_THOUSANDS = {
    "request": {"metrics": ["net_revenue"], "dimensions": ["orders.status"]},
    "sql": "SELECT status, ROUND(SUM(amount - discount), -3) FROM public.orders GROUP BY status",
    "sql_by_dialect": {
        "postgres": "SELECT status, ROUND(CAST(SUM(amount - discount) AS NUMERIC), -3) "
        "FROM public.orders GROUP BY status",
    },
}

# (case, the metric's declared tolerance)
CASES = [
    (
        ZooCase(
            id="T01",
            title="Report rounded to one decimal, within abs_tolerance 0.05",
            expect={"status": "match"},
            **AOV_ROUNDED,
        ),
        {"abs_tolerance": 0.05},
    ),
    (
        ZooCase(
            id="T02",
            title="Report rounded to one decimal, outside abs_tolerance 0.001",
            expect={"status": "divergent", "causes": ["rounding"]},
            **AOV_ROUNDED,
        ),
        {"abs_tolerance": 0.001},
    ),
    (
        ZooCase(
            id="T03",
            title="Report in thousands, within rel_tolerance 1%",
            expect={"status": "match"},
            **NET_IN_THOUSANDS,
        ),
        {"rel_tolerance": 0.01},
    ),
    (
        ZooCase(
            id="T04",
            title="Report in thousands, outside rel_tolerance 0.001%",
            expect={"status": "divergent"},
            **NET_IN_THOUSANDS,
        ),
        {"rel_tolerance": 0.00001},
    ),
]


def with_tolerance(graph, metric: str, tolerance: dict):
    data = graph.project.model_dump(mode="json")
    next(m for m in data["metrics"] if m["name"] == metric)["reconciliation"] = tolerance
    return compile_project(Project.model_validate(data))


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize(("case", "tolerance"), CASES, ids=lambda c: getattr(c, "id", ""))
def test_declared_tolerance_decides(case, tolerance, graph, connector):
    tolerant = with_tolerance(graph, case.request.metrics[0], tolerance)
    ok, report = check_zoo_case(case, tolerant, connector)
    found = [c["code"] for c in report["causes"]]
    assert ok, f"{case.id}: status={report['status']} causes={found} rows={report['rows']}"
    assert report["tolerance"]["source"] == "metric"
    # Without the declared tolerance, a rounded report never agrees.
    _, strict = check_zoo_case(case, graph, connector)
    assert strict["status"] == "divergent"
    assert strict["tolerance"] == {
        "abs_tolerance": 1e-6,
        "rel_tolerance": 1e-9,
        "source": "default",
    }


def test_report_states_tolerance_and_differences(graph, duckdb_connector):
    case, tolerance = CASES[0]
    tolerant = with_tolerance(graph, "average_order_value", tolerance)
    report = reconcile_sql(tolerant, duckdb_connector, case.request, case.sql)
    assert report["status"] == "match"
    assert report["tolerance"] == {"abs_tolerance": 0.05, "rel_tolerance": 1e-9, "source": "metric"}
    assert report["rows"]["within_tolerance"] == len(report["tolerated"]) > 0
    for row in report["tolerated"]:
        assert row["difference"] == pytest.approx(row["legacy"] - row["cosmos"], abs=1e-6)
        assert 0 < abs(row["difference"]) <= 0.05 + 1e-9

    strict = reconcile_sql(graph, duckdb_connector, case.request, case.sql)
    assert strict["rows"]["within_tolerance"] == 0 and strict["tolerated"] == []
    assert all(e["difference"] is not None for e in strict["examples"])


def test_check_runs_and_health_state_the_tolerance(graph, duckdb_connector, tmp_path):
    case, tolerance = CASES[0]
    tolerant = with_tolerance(graph, "average_order_value", tolerance)
    runs = tmp_path / "runs.jsonl"
    run = run_checks(
        tolerant,
        model_hash=EvidenceBuilder(tolerant).model_hash,
        connector=duckdb_connector,
        cases=[case.model_copy(update={"expect": None})],
    )
    append_run(runs, run)
    [item] = run["reconciliations"]
    assert item["outcome"] == "pass"
    assert item["tolerance"]["source"] == "metric"
    state = Health(tolerant, runs_path=runs).states()["metric:average_order_value"]
    assert state["state"] == "healthy"
    assert state["reasons"][0]["tolerance"] == item["tolerance"]

    # Under the default tolerance the same report fails, and the reason keeps its shape.
    run = run_checks(
        graph,
        model_hash=EvidenceBuilder(graph).model_hash,
        connector=duckdb_connector,
        cases=[case.model_copy(update={"expect": None})],
    )
    append_run(runs, run)
    assert run["reconciliations"][0]["tolerance"]["source"] == "default"
    state = Health(graph, runs_path=runs).states()["metric:average_order_value"]
    assert state["state"] == "failing"
    assert "tolerance" not in state["reasons"][0]


def test_evidence_states_a_declared_tolerance_only(graph, duckdb_connector, tmp_path):
    tolerant = with_tolerance(graph, "average_order_value", {"abs_tolerance": 0.01})
    request = QueryRequest(metrics=["average_order_value", "order_count"])

    def evidence(g):
        engine = Engine(g, ConnectionsFile(data_sources={"src": duckdb_connector.config}))
        engine._connectors["src"] = duckdb_connector
        service = SemanticService(engine, audit_path=tmp_path / "audit.jsonl")
        return service.query(request, Principal())["evidence"]

    metrics = {m["name"]: m for m in evidence(tolerant)["metrics"]}
    assert metrics["average_order_value"]["reconciliation"] == {
        "abs_tolerance": 0.01,
        "rel_tolerance": 1e-9,
    }
    assert "reconciliation" not in metrics["order_count"]
    plain = evidence(graph)
    assert all("reconciliation" not in m for m in plain["metrics"])
    assert plain["sql"]["hash"] == evidence(tolerant)["sql"]["hash"]


def test_model_hash_unchanged_without_a_declared_tolerance(graph):
    dumped = graph.project.model_dump(mode="json")
    assert all("reconciliation" not in m for m in dumped["metrics"])
    tolerant = with_tolerance(graph, "average_order_value", {"abs_tolerance": 0.01})
    assert model_hash(tolerant) != model_hash(graph)
    # A round trip through the dump keeps the hash: the field is omitted, not null.
    assert model_hash(compile_project(Project.model_validate(dumped))) == model_hash(graph)


METRIC = {"name": "m", "type": "simple", "measure": "x"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"abs_tolerance": 0.01}, (0.01, 1e-9)),
        ({"rel_tolerance": 0.001}, (1e-6, 0.001)),
        ({"abs_tolerance": 1, "rel_tolerance": 0}, (1.0, 0.0)),
        ({"abs_tolerance": 0}, (0.0, 1e-9)),
    ],
)
def test_spec_accepts_non_negative_tolerances(value, expected):
    metric = SimpleMetric.model_validate(METRIC | {"reconciliation": value})
    assert metric.reconciliation.tolerances() == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"abs_tolerance": -0.01}, "greater than or equal to 0"),
        ({"rel_tolerance": -1}, "greater than or equal to 0"),
        ({"abs_tolerance": "0.01"}, "valid number"),
        ({"abs_tolerance": True}, "valid number"),
        ({"abs_tolerance": float("inf")}, "finite number"),
        ({"abs_tolerance": float("nan")}, "finite number"),
        ({"abs_tol": 0.01}, "Extra inputs are not permitted"),
        ({"abs_tolerance": 0.01, "mode": "cents"}, "Extra inputs are not permitted"),
        ({}, "needs `abs_tolerance`, `rel_tolerance` or both"),
    ],
)
def test_spec_refuses_bad_tolerances(value, message):
    with pytest.raises(ValueError, match=message):
        SimpleMetric.model_validate(METRIC | {"reconciliation": value})


def test_every_metric_type_can_declare_a_tolerance():
    project = Project.model_validate(
        {
            "version": 1,
            "metrics": [
                {
                    "name": "a",
                    "type": "simple",
                    "measure": "x",
                    "reconciliation": {"abs_tolerance": 1},
                },
                {
                    "name": "b",
                    "type": "ratio",
                    "numerator": "x",
                    "denominator": "y",
                    "reconciliation": {"rel_tolerance": 0.001},
                },
                {
                    "name": "c",
                    "type": "derived",
                    "expr": "{x}",
                    "reconciliation": {"abs_tolerance": 1},
                },
                {
                    "name": "d",
                    "type": "cumulative",
                    "measure": "x",
                    "reconciliation": {"abs_tolerance": 1},
                },
            ],
        }
    )
    assert all(m.reconciliation for m in project.metrics)
    schema = json.dumps(json_schema())
    assert "ReconciliationSettings" in schema and "abs_tolerance" in schema
