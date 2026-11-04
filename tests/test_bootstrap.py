"""Model bootstrap from the database catalog (ADR 0008), on every available database."""

from __future__ import annotations

import pytest

from cosmos_semantic.bootstrap import _sensitivity, bootstrap
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.planner import Planner, Principal, QueryRequest

SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
]
SUL = Principal(user={"region": "Midwest"})


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


def run(graph, connector, request, principal=None):
    sql = (
        Planner(graph, connector.dialect, connector.config.schema_map).plan(request, principal).sql
    )
    rows = connector.execute(sql).to_pylist()
    return sorted((tuple(r.values()) for r in rows), key=str)


def same(a, b):
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert x[:-1] == y[:-1]
        assert float(x[-1]) == pytest.approx(float(y[-1]), rel=1e-9)


def test_keys_and_joins_match_the_hand_written_model(graph, connector):
    project, report = bootstrap(connector)
    draft = compile_project(project)
    assert not [i for i in draft.issues if i.severity == "error"]

    def edges(g):
        return {(e.source, e.target) for edges in g.edges.values() for e in edges}

    assert edges(draft) == edges(graph)
    assert {k["table"] for k in report["keys"]} == {
        "customers",
        "orders",
        "order_items",
        "payments",
        "products",
        "sellers",
    }
    assert all(r["orphans"] == 0 for r in report["relationships"])


def test_personal_data_is_flagged_for_review(connector):
    _, report = bootstrap(connector)
    flagged = {item["column"]: item["suggested"] for item in report["sensitivity"]}
    assert flagged == {
        "customers.name": "personal",
        "customers.tax_id": "personal",
        "customers.email": "personal",
        "customers.health_plan": "sensitive",
    }


def test_draft_answers_with_the_same_numbers(graph, connector):
    project, _ = bootstrap(connector)
    draft = compile_project(project)
    same(
        run(draft, connector, QueryRequest(metrics=["amount"], dimensions=["orders.status"])),
        run(graph, connector, QueryRequest(metrics=["revenue"], dimensions=["orders.status"])),
    )
    same(
        run(draft, connector, QueryRequest(metrics=["quantity"], dimensions=["sellers.region"])),
        run(graph, connector, QueryRequest(metrics=["items_sold"], dimensions=["sellers.region"])),
    )
    # Two hops, inferred from column names alone.
    same(
        run(
            draft,
            connector,
            QueryRequest(
                metrics=["value"],
                dimensions=["customers.segment"],
                filters=["{customers.region} = 'Midwest'"],
            ),
        ),
        run(
            graph,
            connector,
            QueryRequest(metrics=["paid_value"], dimensions=["customers.segment"]),
            SUL,
        ),
    )


def test_reports_review_points_and_timing(connector):
    project, report = bootstrap(connector, only=["payments", "orders"])
    assert {m.name for m in project.semantic_models} == {"orders", "payments"}
    assert any("installments" in w for w in report["warnings"])
    assert report["seconds"] >= 0
    payments = next(m for m in project.semantic_models if m.name == "payments")
    assert {m.name: m.agg for m in payments.measures}["value"] == "sum"


@pytest.mark.parametrize(
    "column,table,expected",
    [
        ("cpf", "customers", "personal"),
        ("tax_id", "customers", "personal"),
        ("contact_email", "leads", "personal"),
        ("plano_saude", "beneficiarios", "sensitive"),
        ("name", "customers", "personal"),
        ("name", "products", None),
        ("amount", "orders", None),
    ],
)
def test_sensitivity_heuristics(column, table, expected):
    assert _sensitivity(column, table) == expected


@pytest.mark.parametrize(
    "data_type,kind,integer",
    [
        ("BIGINT", "number", True),
        ("LONG", "number", True),  # Databricks
        ("SHORT", "number", True),
        ("int4", "number", True),
        ("DECIMAL(10,2)", "number", False),
        ("double precision", "number", False),
        ("TIMESTAMP_NTZ", "time", False),  # Databricks
        ("timestamp without time zone", "time", False),
        ("datetime2", "time", False),  # SQL Server
        ("BOOLEAN", "boolean", False),
        ("bit", "boolean", False),
        ("STRING", "categorical", False),
        ("longtext", "categorical", False),  # not a number, despite "long"
        ("interval", "categorical", False),  # not an integer, despite "int"
    ],
)
def test_column_types_from_every_catalog(data_type, kind, integer):
    from cosmos_semantic.bootstrap import Column, _is_integer

    assert Column("c", data_type).kind == kind
    assert _is_integer(data_type) is integer
