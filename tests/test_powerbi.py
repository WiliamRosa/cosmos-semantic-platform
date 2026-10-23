"""Power BI import (ADR 0009): `.bim` and TMDL give the same model, and the imported model
returns the same numbers as the native one on every available database."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.interchange import dump_project, import_model
from cosmos_semantic.interchange.powerbi import parse_dax, physical_table
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.spec import Project

FIXTURES = Path(__file__).parent / "fixtures"
BIM = FIXTURES / "powerbi" / "ecommerce.bim"
TMDL = FIXTURES / "powerbi_tmdl"
MIDWEST = Principal(user={"region": "Midwest"})


def imported(kind: str, path: Path):
    result = import_model(kind, path)
    # The generated YAML must be a valid v1 model on its own.
    project = Project.model_validate(yaml.safe_load(dump_project(result.project)))
    return compile_project(project), result.warnings


def rows(graph, connector, request: QueryRequest, principal=None):
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
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        for u, v in zip(x, y, strict=True):
            if isinstance(u, float) and v is not None:
                assert math.isclose(u, v, rel_tol=1e-6), (x, y)
            else:
                assert u == v, (x, y)


# (native request, principal, imported request): the imported model has no row filters, so
# the native row filter becomes an explicit filter on the imported side.
CASES = [
    (
        QueryRequest(metrics=["revenue", "order_count"], dimensions=["orders.status"]),
        None,
        QueryRequest(metrics=["revenue", "order_count"], dimensions=["orders.status"]),
    ),
    (
        QueryRequest(metrics=["completed_revenue"], dimensions=["orders.ordered_at__month"]),
        None,
        QueryRequest(metrics=["completed_revenue"], dimensions=["orders.order_date__month"]),
    ),
    (
        QueryRequest(
            metrics=["average_order_value", "net_revenue", "discount_rate"],
            dimensions=["orders.status"],
        ),
        None,
        QueryRequest(
            metrics=["average_order_value", "net_revenue", "discount_rate"],
            dimensions=["orders.status"],
        ),
    ),
    (
        QueryRequest(metrics=["collection_gap", "items_per_order", "buyers"]),
        None,
        QueryRequest(metrics=["collection_gap", "items_per_order", "buyers"]),
    ),
    (
        QueryRequest(metrics=["revenue_year_to_date"], dimensions=["orders.ordered_at__month"]),
        None,
        QueryRequest(metrics=["revenue_ytd"], dimensions=["orders.order_date__month"]),
    ),
    (
        QueryRequest(metrics=["revenue", "customer_count"], dimensions=["customers.segment"]),
        MIDWEST,
        QueryRequest(
            metrics=["revenue", "customer_count"],
            dimensions=["customers.segment"],
            filters=["{customers.region} = 'Midwest'"],
        ),
    ),
    (
        QueryRequest(metrics=["paid_value"], dimensions=["payments.method"]),
        None,
        QueryRequest(metrics=["paid_value"], dimensions=["payments.method"]),
    ),
]


def test_bim_and_tmdl_give_the_same_model():
    bim, tmdl = import_model("powerbi", BIM), import_model("powerbi", TMDL)
    assert dump_project(bim.project) == dump_project(tmdl.project)
    assert bim.warnings == tmdl.warnings


@pytest.mark.parametrize("path", [BIM, TMDL], ids=["bim", "tmdl"])
@pytest.mark.parametrize(
    "source", ["duckdb_connector", "postgres_connector", "sqlserver_connector"]
)
def test_imported_model_matches_native(path, source, graph, request):
    connector = request.getfixturevalue(source)
    other, _ = imported("powerbi", path)
    for native, principal, mapped in CASES:
        same(rows(graph, connector, native, principal), rows(other, connector, mapped))


def test_physical_tables_come_from_the_partitions():
    other, _ = imported("powerbi", BIM)
    tables = {name: node.spec.table for name, node in other.models.items()}
    assert tables == {
        "orders": "public.orders",  # Sql.Database navigation
        "customers": "public.customers",  # Databricks catalog navigation
        "order_items": "public.order_items",  # legacy query partition
        "payments": "public.payments",  # Value.NativeQuery
        "products": "public.products",  # Item before Schema
        "sellers": "public.sellers",
    }
    assert physical_table({"type": "m", "expression": "let x = Excel.Workbook(f) in x"}) is None


def test_translations_and_warnings():
    result = import_model("powerbi", BIM)
    metrics = {m.name: m for m in result.project.metrics}
    assert metrics["completed_revenue"].filter == "{orders.status} = 'completed'"
    assert metrics["midwest_or_west_customers"].filter == (
        "{customers.region} IN ('Midwest', 'West')"
    )
    assert metrics["big_order_revenue"].filter == "{orders.amount} >= 1000"
    assert metrics["revenue_ytd"].grain_to_date == "year"
    assert metrics["discount_rate"].numerator == "discount_rate_numerator"
    assert metrics["average_order_value"].label == "Average Order Value"
    joined = "\n".join(result.warnings)
    for expected in (
        "calculated column `Net Amount`",
        "calculated tables",
        "bidirectional filtering",
        "inactive",
        "DAX LASTDATE has no equivalent",
        "variables (VAR/RETURN)",
        "DAX SUMX has no equivalent",
        "row-level security on `Customers`",
    ):
        assert expected in joined, expected
    # KEEPFILTERS matches Cosmos filter semantics; a plain CALCULATE filter gets a caveat.
    assert "`Completed Revenue`: in DAX a CALCULATE filter replaces" in joined
    assert "`Big Order Revenue`: in DAX" not in joined
    # Hidden key columns only become dimensions when a filter needs them.
    orders = next(m for m in result.project.semantic_models if m.name == "orders")
    assert "order_id" not in {d.name for d in orders.dimensions}


def test_dependent_measures_fail_together(tmp_path):
    (tmp_path / "m.bim").write_text(
        """{"model": {"tables": [{"name": "T",
            "columns": [{"name": "v", "dataType": "double", "sourceColumn": "v"}],
            "partitions": [{"name": "T", "source": {"type": "query", "query": "SELECT * FROM t"}}],
            "measures": [
              {"name": "A", "expression": "SUMX(T, T[v] * 2)"},
              {"name": "B", "expression": "[A] + 1"},
              {"name": "C", "expression": "[C] + 1"},
              {"name": "D", "expression": "DIVIDE([A], 2, -1)"}
            ]}]}}""",
        encoding="utf-8",
    )
    result = import_model("powerbi", tmp_path / "m.bim")
    assert result.project.metrics == []
    joined = "\n".join(result.warnings)
    assert "`B` not imported" in joined and "depends on `A`" in joined
    assert "circular reference" in joined
    assert "`D` not imported" in joined


def test_dax_parser_handles_quoting_and_comments():
    tree = parse_dax("CALCULATE( [Net Revenue] /* x */, 'Order Items'[Unit Price] > 10 -- y\n)")
    assert tree == (
        "call",
        "CALCULATE",
        [
            ("mref", "Net Revenue"),
            ("bin", ">", ("col", "Order Items", "Unit Price"), ("num", "10")),
        ],
    )
