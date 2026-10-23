"""Industry packs (ADR 0009): the retail pack, mapped onto a database with its own naming,
returns the same numbers as the hand-written model on every available database."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from cosmos_semantic import demo_data
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ADBCPostgresConnector, DuckDBConnector
from cosmos_semantic.errors import QueryError
from cosmos_semantic.interchange import dump_project
from cosmos_semantic.packs import PACKS_DIR, apply_pack, available, map_tables
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.spec import Project

SOURCES = ["duckdb_connector", "postgres_connector", "sqlserver_connector"]
EXAMPLE = Path(__file__).parents[1] / "examples" / "ecommerce" / "semantic"
MIDWEST = Principal(user={"region": "Midwest"})

# How "the customer" named things: other table names, `id` keys, prefixes, synonyms.
RENAMES = {
    "orders": (
        "tbl_sales_orders",
        {
            "order_id": "id",
            "customer_id": "client_id",
            "ordered_at": "order_date",
            "status": "order_status",
            "amount": "total_amount",
            "discount": "discount_amount",
        },
    ),
    "customers": (
        "dim_clients",
        {
            "customer_id": "id",
            "name": "full_name",
            "tax_id": "ssn",
            "email": "email_address",
            "segment": "customer_segment",
            "last_order_at": "last_purchase_date",
        },
    ),
    "order_items": (
        "order_lines",
        {"order_item_id": "id", "quantity": "qty", "unit_price": "price"},
    ),
    "payments": (
        "payment_transactions",
        {
            "payment_id": "id",
            "method": "payment_method",
            "value": "amount_paid",
            "paid_at": "payment_date",
        },
    ),
    "products": ("products", {"product_id": "id", "name": "product_name"}),
}
SCHEMA = "shop"


def customer_tables(tables):
    out = {}
    for logical, (physical, columns) in RENAMES.items():
        table = tables[logical]
        if logical == "customers":
            table = table.drop_columns(["health_plan"])  # this customer has no such column
        if logical == "order_items":
            table = table.drop_columns(["seller_id"])  # and no sellers at all
        out[physical] = table.rename_columns([columns.get(n, n) for n in table.column_names])
    return out


_loaded: set[str] = set()


@pytest.fixture(params=SOURCES)
def connector(request, tables):
    connector = request.getfixturevalue(request.param)
    if request.param not in _loaded:
        renamed = customer_tables(tables)
        schema = connector.config.schema_map.get(SCHEMA, SCHEMA)
        if isinstance(connector, DuckDBConnector):
            demo_data.load_duckdb(connector.connection, renamed, schema)
        elif isinstance(connector, ADBCPostgresConnector):
            demo_data.load_postgres(connector.connection, renamed, schema)
        else:
            demo_data.load_sql(connector, renamed, schema)
        _loaded.add(request.param)
    return connector


def rows(graph, connector, request, principal=None):
    planner = Planner(graph, connector.dialect, connector.config.schema_map)
    out = connector.execute(planner.plan(request, principal).sql).to_pylist()
    return sorted(
        (
            tuple(round(float(v), 4) if isinstance(v, int | float) else v for v in r.values())
            for r in out
        ),
        key=str,
    )


def same(a, b):
    assert len(a) == len(b), (a, b)
    for x, y in zip(a, b, strict=True):
        for u, v in zip(x, y, strict=True):
            if isinstance(u, float) and v is not None:
                assert math.isclose(u, v, rel_tol=1e-6), (x, y)
            else:
                assert u == v, (x, y)


CASES = [
    (QueryRequest(metrics=["revenue", "order_count"], dimensions=["orders.status"]), None),
    (QueryRequest(metrics=["completed_revenue"], dimensions=["orders.ordered_at__month"]), None),
    (
        QueryRequest(metrics=["net_revenue", "discount_rate"], dimensions=["orders.has_discount"]),
        None,
    ),
    (QueryRequest(metrics=["collection_gap", "items_per_order", "orders_per_buyer"]), None),
    (QueryRequest(metrics=["revenue"], dimensions=["orders.order_size_band"]), None),
    (QueryRequest(metrics=["paid_value"], dimensions=["payments.method"]), None),
    (QueryRequest(metrics=["revenue"], dimensions=["products.category"]), None),
    (
        QueryRequest(
            metrics=["average_order_value", "customer_count"],
            dimensions=["customers.segment"],
            filters=["{rule.active_customer}"],
        ),
        MIDWEST,
    ),
]


def test_pack_on_a_differently_named_database_matches_the_native_model(graph, connector):
    result = apply_pack("retail", connector, schema=SCHEMA)
    report = result.report
    assert report["tables"] == {
        "orders": "tbl_sales_orders",
        "order_items": "order_lines",
        "payments": "payment_transactions",
        "customers": "dim_clients",
        "products": "products",
    }
    assert report["missing_tables"] == ["sellers"]
    assert report["missing_columns"] == {
        "order_items": ["seller_id"],
        "customers": ["health_plan"],
    }
    assert report["columns_renamed"]["orders"]["ordered_at"] == "order_date"
    assert report["rule_tests"]["passed"] == report["rule_tests"]["total"] > 0
    removed = {r["member"] for r in report["removed"]}
    assert {"model:sellers", "dimension:customers.health_plan"} <= removed
    # Nothing that the database supports is lost: every metric survives and runs.
    assert not any(m.startswith(("metric:", "rule:", "concept:")) for m in removed), removed
    assert report["metrics_checked"] == len(graph.metrics)

    # The draft is a valid model file on its own, and answers like the native model.
    text = dump_project(result.project)
    pack_graph = compile_project(Project.model_validate(yaml.safe_load(text)))
    assert dict(pack_graph.models["customers"].dimensions)["tax_id"].sensitivity == "personal"
    for request, principal in CASES:
        same(
            rows(graph, connector, request, principal),
            rows(pack_graph, connector, request, principal),
        )


def test_pack_model_matches_the_example():
    """The pack is seeded from the example; they must not drift apart."""
    for path in (PACKS_DIR / "retail" / "semantic").glob("*.yml"):
        pack = path.read_text(encoding="utf-8").split("\n", 1)[1]
        example = (EXAMPLE / path.name).read_text(encoding="utf-8").split("\n", 1)[1]
        assert pack == example, path.name


def test_listing_and_errors(duckdb_connector):
    assert [p["name"] for p in available()] == ["retail"]
    with pytest.raises(QueryError, match="unknown pack"):
        apply_pack("banking", duckdb_connector)
    with pytest.raises(QueryError, match="no such table"):
        apply_pack("retail", duckdb_connector, overrides={"orders": "nope"})


def test_mapping_rules_and_overrides():
    manifest = {
        "tables": {
            "orders": {"synonyms": ["pedidos"], "columns": {"amount": ["valor_total"]}},
            "customers": {"synonyms": [], "columns": {}},
        }
    }
    catalog = {
        "tb_pedidos": ["id", "valor_total", "order_status"],
        "customer": ["customer_id", "segmento"],
        "customers_backup": ["customer_id"],
    }
    needed = {"orders": {"order_id", "amount", "status"}, "customers": {"customer_id", "segment"}}
    m = map_tables(manifest, catalog, needed)
    assert m.tables == {"orders": "tb_pedidos", "customers": "customer"}
    assert m.columns["orders"] == {
        "order_id": "id",
        "amount": "valor_total",
        "status": "order_status",
    }
    assert m.missing_columns == {"customers": ["segment"]}
    m = map_tables(manifest, catalog, needed, {"customers.segment": "segmento"})
    assert m.columns["customers"]["segment"] == "segmento"
