"""Power BI import (ADR 0009): `.bim` and TMDL give the same model, and the imported model
returns the same numbers as the native one on every available database."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from cosmos_semantic import demo_data
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
    # SUMX('Order Items', 'Order Items'[quantity] * 'Order Items'[unit_price])
    (
        QueryRequest(metrics=["item_revenue"]),
        None,
        QueryRequest(metrics=["item_revenue"]),
    ),
    (
        QueryRequest(metrics=["item_revenue", "items_sold"], dimensions=["orders.status"]),
        None,
        QueryRequest(metrics=["item_revenue", "items_sold"], dimensions=["orders.status"]),
    ),
]


def test_bim_and_tmdl_give_the_same_model():
    bim, tmdl = import_model("powerbi", BIM), import_model("powerbi", TMDL)
    assert dump_project(bim.project) == dump_project(tmdl.project)
    assert bim.warnings == tmdl.warnings


@pytest.mark.parametrize("path", [BIM, TMDL], ids=["bim", "tmdl"])
@pytest.mark.parametrize(
    "source",
    [
        "duckdb_connector",
        "postgres_connector",
        "sqlserver_connector",
        "databricks_connector",
        "snowflake_connector",
        "mysql_connector",
        "mariadb_connector",
        "oracle_connector",
    ],
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
        "row-level security on `Customers`",
    ):
        assert expected in joined, expected
    # KEEPFILTERS matches Cosmos filter semantics; a plain CALCULATE filter gets a caveat.
    assert "`Completed Revenue`: in DAX a CALCULATE filter replaces" in joined
    assert "`Big Order Revenue`: in DAX" not in joined
    assert "Item Revenue" not in joined
    items = next(m for m in result.project.semantic_models if m.name == "order_items")
    revenue = next(m for m in items.measures if m.name == "item_revenue")
    assert (revenue.agg, revenue.expr) == ("sum", "quantity * unit_price")
    # Hidden key columns only become dimensions when a filter needs them.
    orders = next(m for m in result.project.semantic_models if m.name == "orders")
    assert "order_id" not in {d.name for d in orders.dimensions}


def test_dependent_measures_fail_together(tmp_path):
    (tmp_path / "m.bim").write_text(
        """{"model": {"tables": [{"name": "T",
            "columns": [{"name": "v", "dataType": "double", "sourceColumn": "v"}],
            "partitions": [{"name": "T", "source": {"type": "query", "query": "SELECT * FROM t"}}],
            "measures": [
              {"name": "A", "expression": "AVERAGEX(T, T[v] * 2)"},
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


def test_distinctcount_counts_blank_and_distinctcountnoblank_matches(tmp_path):
    (tmp_path / "m.bim").write_text(
        """{"model": {"tables": [{"name": "T",
            "columns": [{"name": "v", "dataType": "double", "sourceColumn": "v"}],
            "partitions": [{"name": "T", "source": {"type": "query", "query": "SELECT * FROM t"}}],
            "measures": [
              {"name": "DC", "expression": "DISTINCTCOUNT(T[v])"},
              {"name": "DCNB", "expression": "DISTINCTCOUNTNOBLANK(T[v])"}
            ]}]}}""",
        encoding="utf-8",
    )
    result = import_model("powerbi", tmp_path / "m.bim")
    table = next(m for m in result.project.semantic_models if m.name == "t")
    measures = {m.name: m for m in table.measures}
    assert measures["dc"].agg == "count_distinct"
    assert measures["dcnb"].agg == "count_distinct"
    joined = "\n".join(result.warnings)
    # DISTINCTCOUNT counts BLANK in DAX but count_distinct ignores nulls: flagged.
    assert "`DC`: in DAX a DISTINCTCOUNT counts BLANK" in joined
    assert "DCNB" not in joined


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


# -- SUMX over the measure's own table ------------------------------------------------------


def _items_bim(directory: Path, measures: dict[str, str]) -> Path:
    """`Items` (numeric columns, a text column, a source column that is not a plain SQL
    identifier, a plain measure) and a second table, `Other`."""

    def table(name: str, physical: str, columns: list[tuple[str, str]], extra: dict[str, str]):
        return {
            "name": name,
            "columns": [{"name": c, "dataType": t, "sourceColumn": c} for c, t in columns],
            "partitions": [
                {"name": name, "source": {"type": "query", "query": f"SELECT * FROM {physical}"}}
            ],
            "measures": [{"name": k, "expression": v} for k, v in extra.items()],
        }

    columns = [
        ("category", "string"),
        ("quantity", "int64"),
        ("unit_price", "double"),
        ("discount", "double"),
        ("Unit Cost", "double"),
    ]
    model = {
        "model": {
            "tables": [
                table(
                    "Items",
                    "public.powerbi_sumx_items",
                    columns,
                    {"Base": "SUM(Items[quantity])", **measures},
                ),
                table("Other", "public.other", [("x", "double")], {}),
            ]
        }
    }
    path = directory / "m.bim"
    path.write_text(json.dumps(model), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("dax", "expr"),
    [
        ("SUMX('Items', 'Items'[quantity] * 'Items'[unit_price])", "quantity * unit_price"),
        ("SUMX(Items, Items[quantity] * [unit_price])", "quantity * unit_price"),
        ("SUMX(Items, [quantity])", "quantity"),
        ("SUMX(Items, -2 * ([quantity]))", "(-2) * quantity"),
        (
            "SUMX(Items, ([quantity] - 1) * [unit_price] * 0.5)",
            "(COALESCE(quantity, 0) - 1) * unit_price * 0.5",
        ),
        ("SUMX(Items, [quantity] * ([unit_price] * 2))", "quantity * (unit_price * 2)"),
        # DAX: BLANK + 5 = 5 and BLANK + BLANK = BLANK; SQL: NULL + 5 = NULL.
        (
            "SUMX(Items, [quantity] + [discount])",
            "COALESCE(quantity + discount, quantity, discount)",
        ),
        (
            "SUMX(Items, [unit_price] - [discount])",
            "COALESCE(unit_price - discount, unit_price, -discount)",
        ),
        (
            "SUMX(Items, [unit_price] - [quantity] * [discount])",
            "COALESCE(unit_price - quantity * discount, unit_price, -(quantity * discount))",
        ),
        ("SUMX(Items, 1 + [discount])", "1 + COALESCE(discount, 0)"),
    ],
)
def test_sumx_over_own_table_becomes_a_sum_measure(tmp_path, dax, expr):
    result = import_model("powerbi", _items_bim(tmp_path, {"M": dax}))
    items = next(m for m in result.project.semantic_models if m.name == "items")
    measure = next(m for m in items.measures if m.name == "m")
    assert (measure.agg, measure.expr) == ("sum", expr)
    assert not any("`M`" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        ("AVERAGEX(Items, [quantity] * [unit_price])", "(DAX AVERAGEX has no equivalent)"),
        ("SUMX(Other, Other[x] * 2)", "SUMX over `Other`, which is not the measure's own table"),
        ("SUMX(FILTER(Items, [quantity] > 1), [quantity])", "(DAX FILTER has no equivalent)"),
        ("SUMX(Items, RELATED(Other[x]) * [quantity])", "(DAX RELATED has no equivalent)"),
        ("SUMX(Items, Other[x] * [quantity])", "`Other[x]`, a column of another table"),
        ("SUMX(Items, [quantity] / [unit_price])", "SUMX row expression divides"),
        ("SUMX(Items, [quantity] / 2)", "SUMX row expression divides"),
        ("SUMX(Items, [quantity] * [Base])", "references the measure `[Base]`"),
        ("SUMX(Items, -[quantity])", "negates a column or an expression"),
        ("SUMX(Items, [category] * 2)", "non-numeric column `Items[category]`"),
        ("SUMX(Items, [Unit Cost] * 2)", "is not a plain SQL identifier"),
        ("SUMX(Items, [missing] * 2)", "column `Items[missing]` was not imported"),
        ("SUMX(Items, SUMX(Items, [quantity]))", "is not arithmetic over the table's columns"),
        ("SUMX(Items, [quantity] & [discount])", "is not arithmetic over the table's columns"),
        ("SUMX(Items)", "SUMX takes a table and a row expression"),
    ],
)
def test_other_sumx_forms_stay_warnings(tmp_path, dax, reason):
    result = import_model("powerbi", _items_bim(tmp_path, {"M": dax}))
    items = next(m for m in result.project.semantic_models if m.name == "items")
    assert "m" not in {m.name for m in items.measures}
    warning = next(w for w in result.warnings if w.startswith("measure `M` not imported"))
    assert reason in warning, warning


# Hand-checked rows with NULLs in every numeric column; `c` has no complete row.
SUMX_ROWS = [
    ("a", 2, 10.0, 1.0),
    ("a", None, 5.0, 2.0),
    ("a", 3, None, None),
    ("b", 1, 2.5, None),
    ("b", 4, 1.5, 0.5),
    ("c", None, None, None),
    ("c", None, 7.0, None),
]
SUMX_MEASURES = {
    "Item Revenue": "SUMX(Items, [quantity] * [unit_price])",
    "Net Price": "SUMX('Items', 'Items'[unit_price] - Items[discount])",
    "Extra": "SUMX(Items, [quantity] + [discount])",
    "Scaled": "SUMX(Items, ([quantity] - 1) * [unit_price] * 0.5)",
}
# DAX results worked out by hand: BLANK * x = BLANK, BLANK + x = x, BLANK - x = -x,
# BLANK + BLANK = BLANK, and SUMX over only BLANK rows is BLANK.
SUMX_EXPECTED = {
    "net_price": {"a": 12.0, "b": 3.5, "c": 7.0, None: 22.5},
    "extra": {"a": 8.0, "b": 5.5, "c": None, None: 13.5},
    "scaled": {"a": 2.5, "b": 2.25, "c": -3.5, None: 1.25},
}
SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
]


def _number(value):
    return None if value is None else round(float(value), 6)


@pytest.mark.parametrize("source", SOURCES)
def test_sumx_matches_hand_written_sql_with_nulls(source, request, tmp_path):
    connector = request.getfixturevalue(source)
    data = pa.table(
        list(zip(*SUMX_ROWS, strict=True)),
        schema=pa.schema(
            [
                ("category", pa.string()),
                ("quantity", pa.int64()),
                ("unit_price", pa.float64()),
                ("discount", pa.float64()),
            ]
        ),
    )
    demo_data.load(connector, {"powerbi_sumx_items": data})
    table = f"{connector.config.schema_map.get('public', 'public')}.powerbi_sumx_items"
    try:
        other, warnings = imported("powerbi", _items_bim(tmp_path, SUMX_MEASURES))
        assert not [w for w in warnings if "not imported" in w and "`Base`" not in w]
        planner = Planner(other, connector.dialect, connector.config.schema_map)
        metrics = ["item_revenue", *SUMX_EXPECTED]

        def cosmos(dimensions: list[str]) -> dict:
            sql = planner.plan(QueryRequest(metrics=metrics, dimensions=dimensions)).sql
            out = {}
            for raw in connector.execute(sql).to_pylist():
                row = {k.lower(): v for k, v in raw.items()}
                key = row["items__category"] if dimensions else None
                out[key] = {m: _number(row[m]) for m in metrics}
            return out

        got = {**cosmos([]), **cosmos(["items.category"])}

        def hand(sql: str) -> dict:
            return {
                (r[0] if len(r) == 2 else None): _number(r[-1])
                for r in (list(row.values()) for row in connector.execute(sql).to_pylist())
            }

        expected = {
            **hand(f"SELECT SUM(quantity * unit_price) FROM {table}"),
            **hand(f"SELECT category, SUM(quantity * unit_price) FROM {table} GROUP BY category"),
        }
        assert expected == {"a": 20.0, "b": 8.5, "c": None, None: 28.5}
        assert {k: v["item_revenue"] for k, v in got.items()} == expected
        for metric, values in SUMX_EXPECTED.items():
            assert {k: v[metric] for k, v in got.items()} == values, metric
    finally:
        connector.run_script(f"DROP TABLE {table}")
