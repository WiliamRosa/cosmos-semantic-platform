"""Power BI import (ADR 0009): `.bim` and TMDL give the same model, and the imported model
returns the same numbers as the native one on every available database."""

from __future__ import annotations

import calendar as cal
import datetime as dt
import json
import math
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from cosmos_semantic import demo_data
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import QueryError
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
        "bigquery_connector",
        "trino_connector",
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
              {"name": "A", "expression": "COUNTX(T, T[v] * 2)"},
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
        ("COUNTX(Items, [quantity] * [unit_price])", "(DAX COUNTX has no equivalent)"),
        ("PRODUCTX(Items, [quantity])", "(DAX PRODUCTX has no equivalent)"),
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


@pytest.mark.parametrize(
    ("dax", "agg", "expr"),
    [
        (
            "AVERAGEX('Items', 'Items'[quantity] * 'Items'[unit_price])",
            "avg",
            "quantity * unit_price",
        ),
        ("AVERAGEX(Items, [quantity])", "avg", "quantity"),
        ("MINX(Items, [unit_price] * 0.5)", "min", "unit_price * 0.5"),
        ("MAXX(Items, -2 * ([quantity]))", "max", "(-2) * quantity"),
        # A row with one BLANK side of `+` or `-` is a value in DAX (BLANK + 5 = 5), so it is
        # averaged, not skipped; a row with both sides BLANK is BLANK and skipped.
        (
            "AVERAGEX(Items, [quantity] + [discount])",
            "avg",
            "COALESCE(quantity + discount, quantity, discount)",
        ),
        (
            "MINX(Items, [unit_price] - [discount])",
            "min",
            "COALESCE(unit_price - discount, unit_price, -discount)",
        ),
        (
            "MAXX(Items, ([quantity] - 1) * [unit_price] * 0.5)",
            "max",
            "(COALESCE(quantity, 0) - 1) * unit_price * 0.5",
        ),
        ("AVERAGEX(Items, 1 + [discount])", "avg", "1 + COALESCE(discount, 0)"),
    ],
)
def test_averagex_minx_maxx_over_own_table_become_measures(tmp_path, dax, agg, expr):
    result = import_model("powerbi", _items_bim(tmp_path, {"M": dax}))
    items = next(m for m in result.project.semantic_models if m.name == "items")
    measure = next(m for m in items.measures if m.name == "m")
    assert (measure.agg, measure.expr) == (agg, expr)
    assert not any("`M`" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        ("AVERAGEX(Other, Other[x])", "AVERAGEX over `Other`, which is not the measure's own"),
        ("MINX(FILTER(Items, [quantity] > 1), [quantity])", "(DAX FILTER has no equivalent)"),
        ("MAXX(Items, RELATED(Other[x]) * [quantity])", "(DAX RELATED has no equivalent)"),
        ("AVERAGEX(Items, [quantity] / [unit_price])", "AVERAGEX row expression divides"),
        ("MINX(Items, [quantity] / 2)", "MINX row expression divides"),
        ("MAXX(Items, Other[x] * [quantity])", "`Other[x]`, a column of another table"),
        ("AVERAGEX(Items, [quantity] * [Base])", "references the measure `[Base]`"),
        ("MINX(Items, [category])", "MINX over the non-numeric column `Items[category]`"),
        ("MAXX(Items, -[quantity])", "negates a column or an expression"),
        ("AVERAGEX(Items)", "AVERAGEX takes a table and a row expression"),
    ],
)
def test_other_averagex_minx_maxx_forms_stay_warnings(tmp_path, dax, reason):
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
    "bigquery_connector",
    "trino_connector",
]


def _number(value):
    return None if value is None else round(float(value), 6)


def _load_items(connector) -> str:
    """Loads SUMX_ROWS as `powerbi_sumx_items` and returns its qualified name."""
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
    return f"{connector.config.schema_map.get('public', 'public')}.powerbi_sumx_items"


@pytest.mark.parametrize("source", SOURCES)
def test_sumx_matches_hand_written_sql_with_nulls(source, request, tmp_path):
    connector = request.getfixturevalue(source)
    table = _load_items(connector)
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


# AVERAGEX, MINX and MAXX over the same rows: (DAX, hand-written SQL that spells out the DAX
# BLANK rules on its own). The DAX results are worked out by hand from SUMX_ROWS.
ITERATOR_MEASURES = {
    # Integer column: DAX averages as a decimal (2.5); SQL Server's AVG would truncate to 2.
    "Avg Quantity": ("AVERAGEX(Items, [quantity])", "AVG(quantity * 1.0)"),
    # BLANK * x is BLANK: the row is skipped, not averaged as 0.
    "Avg Revenue": ("AVERAGEX(Items, [quantity] * [unit_price])", "AVG(quantity * unit_price)"),
    # BLANK + x is x: the row counts; BLANK + BLANK is BLANK and is skipped.
    "Avg Extra": (
        "AVERAGEX(Items, [quantity] + [discount])",
        "AVG(CASE WHEN quantity IS NULL AND discount IS NULL THEN NULL "
        "ELSE COALESCE(quantity, 0) + COALESCE(discount, 0) END)",
    ),
    "Min Net": (
        "MINX(Items, [unit_price] - [discount])",
        "MIN(CASE WHEN unit_price IS NULL AND discount IS NULL THEN NULL "
        "ELSE COALESCE(unit_price, 0) - COALESCE(discount, 0) END)",
    ),
    "Max Net": (
        "MAXX('Items', 'Items'[unit_price] - Items[discount])",
        "MAX(CASE WHEN unit_price IS NULL AND discount IS NULL THEN NULL "
        "ELSE COALESCE(unit_price, 0) - COALESCE(discount, 0) END)",
    ),
    "Min Scaled": (
        "MINX(Items, ([quantity] - 1) * [unit_price] * 0.5)",
        "MIN((COALESCE(quantity, 0) - 1) * unit_price * 0.5)",
    ),
    "Max Quantity": ("MAXX(Items, [quantity])", "MAX(quantity)"),
}
ITERATOR_EXPECTED = {
    "avg_quantity": {"a": 2.5, "b": 2.5, "c": None, None: 2.5},
    "avg_revenue": {"a": 20.0, "b": 4.25, "c": None, None: 9.5},
    "avg_extra": {"a": 2.666667, "b": 2.75, "c": None, None: 2.7},
    "min_net": {"a": 3.0, "b": 1.0, "c": 7.0, None: 1.0},
    "max_net": {"a": 9.0, "b": 2.5, "c": 7.0, None: 9.0},
    "min_scaled": {"a": -2.5, "b": 0.0, "c": -3.5, None: -3.5},
    "max_quantity": {"a": 3.0, "b": 4.0, "c": None, None: 4.0},
}


@pytest.mark.parametrize("source", SOURCES)
def test_averagex_minx_maxx_match_hand_written_sql_with_nulls(source, request, tmp_path):
    connector = request.getfixturevalue(source)
    table = _load_items(connector)
    try:
        measures = {name: dax for name, (dax, _) in ITERATOR_MEASURES.items()}
        other, warnings = imported("powerbi", _items_bim(tmp_path, measures))
        assert not [w for w in warnings if "not imported" in w and "`Base`" not in w]
        planner = Planner(other, connector.dialect, connector.config.schema_map)
        metrics = list(ITERATOR_EXPECTED)

        def cosmos(dimensions: list[str]) -> dict:
            sql = planner.plan(QueryRequest(metrics=metrics, dimensions=dimensions)).sql
            out = {}
            for raw in connector.execute(sql).to_pylist():
                row = {k.lower(): v for k, v in raw.items()}
                key = row["items__category"] if dimensions else None
                out[key] = {m: _number(row[m]) for m in metrics}
            return out

        got = {**cosmos([]), **cosmos(["items.category"])}
        # Aliased: some drivers name every unaliased AVG column alike.
        columns = ", ".join(
            f"{sql} AS {metric}"
            for metric, (_, sql) in zip(metrics, ITERATOR_MEASURES.values(), strict=True)
        )
        expected = {}
        for row in connector.execute(f"SELECT {columns} FROM {table}").to_pylist():
            expected[None] = dict(zip(metrics, map(_number, row.values()), strict=True))
        grouped = f"SELECT category, {columns} FROM {table} GROUP BY category"
        for row in connector.execute(grouped).to_pylist():
            key, *values = row.values()
            expected[key] = dict(zip(metrics, map(_number, values), strict=True))
        for metric, values in ITERATOR_EXPECTED.items():
            assert {k: v[metric] for k, v in expected.items()} == values, metric
            assert {k: v[metric] for k, v in got.items()} == values, metric
    finally:
        connector.run_script(f"DROP TABLE {table}")


# -- TOTALYTD / TOTALQTD / TOTALMTD over each aggregation (DEC-21) --------------------------


def _to_date_bim(directory: Path, measures: dict[str, str]) -> Path:
    columns = (("sold_at", "dateTime"), ("amount", "double"), ("buyer", "string"))
    model = {
        "model": {
            "tables": [
                {
                    "name": "Sales",
                    "columns": [{"name": c, "dataType": t, "sourceColumn": c} for c, t in columns],
                    "partitions": [
                        {"name": "Sales", "source": {"type": "query", "query": "SELECT * FROM t"}}
                    ],
                    "measures": [{"name": k, "expression": v} for k, v in measures.items()],
                }
            ]
        }
    }
    path = directory / "m.bim"
    path.write_text(json.dumps(model), encoding="utf-8")
    return path


def test_to_date_over_min_and_max_imports_as_a_running_min_and_max(tmp_path):
    path = _to_date_bim(
        tmp_path,
        {
            "Top": "MAX(Sales[amount])",
            "Top YTD": "TOTALYTD([Top], Sales[sold_at])",
            "Low QTD": "TOTALQTD(MIN(Sales[amount]), Sales[sold_at])",
            "Amount MTD": "TOTALMTD(SUM(Sales[amount]), Sales[sold_at])",
            "Rows YTD": "TOTALYTD(COUNTROWS(Sales), Sales[sold_at])",
        },
    )
    graph, warnings = imported("powerbi", path)
    assert warnings == []
    metrics = {m.name: m for m in graph.project.metrics}
    assert (metrics["top_ytd"].measure, metrics["top_ytd"].grain_to_date) == ("top", "year")
    assert metrics["low_qtd"].grain_to_date == "quarter"
    assert metrics["amount_mtd"].grain_to_date == "month"
    request = QueryRequest(
        metrics=["top_ytd", "low_qtd", "amount_mtd", "rows_ytd"], dimensions=["sales.sold_at__day"]
    )
    sql = Planner(graph, "duckdb").plan(request).sql.upper()
    assert "MAX(SALES_AGG.TOP) OVER" in sql
    assert "MIN(SALES_AGG.LOW_QTD_BASE) OVER" in sql
    assert "SUM(SALES_AGG.AMOUNT_MTD_BASE) OVER" in sql


@pytest.mark.parametrize(
    ("dax", "agg"),
    [
        ("TOTALYTD(AVERAGE(Sales[amount]), Sales[sold_at])", "avg"),
        ("TOTALQTD(DISTINCTCOUNTNOBLANK(Sales[buyer]), Sales[sold_at])", "count_distinct"),
        ("TOTALMTD([Mean], Sales[sold_at])", "avg"),
    ],
)
def test_to_date_over_avg_or_distinct_count_is_a_warning(tmp_path, dax, agg):
    path = _to_date_bim(tmp_path, {"Mean": "AVERAGE(Sales[amount])", "To Date": dax})
    graph, warnings = imported("powerbi", path)
    # Not imported, so the imported model still passes validation.
    assert "to_date" not in {m.name for m in graph.project.metrics}
    joined = "\n".join(warnings)
    assert "measure `To Date` not imported" in joined
    assert f"an `{agg}` measure, cannot be accumulated from per-period values" in joined


# -- DATESINPERIOD as a rolling window in days (DEC-23) -------------------------------------


def _window_bim(directory: Path, measures: dict[str, str], related: bool = True) -> Path:
    """`Sales` (a date, an integer day number, an amount, a buyer) and a `Calendar` date table
    (`Day`, read from `cal_day`) related to it, or left unrelated."""

    def table(name: str, physical: str, columns, extra: dict[str, str]):
        return {
            "name": name,
            "columns": [{"name": c, "dataType": t, "sourceColumn": s} for c, t, s in columns],
            "partitions": [
                {"name": name, "source": {"type": "query", "query": f"SELECT * FROM {physical}"}}
            ],
            "measures": [{"name": k, "expression": v} for k, v in extra.items()],
        }

    sales = [
        (c, t, c)
        for c, t in [
            ("sold_on", "dateTime"),
            ("day_no", "int64"),
            ("amount", "double"),
            ("buyer", "string"),
        ]
    ]
    base = {"Amount": "SUM(Sales[amount])", "Mean": "AVERAGE(Sales[amount])"}
    relationships = [
        {"fromTable": "Sales", "fromColumn": "sold_on", "toTable": "Calendar", "toColumn": "Day"}
    ]
    model = {
        "model": {
            "tables": [
                table("Sales", "public.powerbi_window_sales", sales, {**base, **measures}),
                table(
                    "Calendar",
                    "public.powerbi_window_calendar",
                    [("Day", "dateTime", "cal_day")],
                    {},
                ),
            ],
            "relationships": relationships if related else [],
        }
    }
    path = directory / "m.bim"
    path.write_text(json.dumps(model), encoding="utf-8")
    return path


def _window(column: str = "'Calendar'[Day]", count: str = "-7", unit: str = "DAY") -> str:
    return f"DATESINPERIOD({column}, MAX({column}), {count}, {unit})"


def test_datesinperiod_in_days_imports_as_a_rolling_window(tmp_path):
    path = _window_bim(
        tmp_path,
        {
            "Amount 7D": f"CALCULATE([Amount], {_window()})",
            "Top 30D": f"CALCULATE(MAX(Sales[amount]), {_window('Sales[sold_on]', '-30', 'day')})",
            "Rows 1D": f"CALCULATE(COUNTROWS(Sales), {_window('Calendar[Day]', '-1')})",
        },
    )
    graph, warnings = imported("powerbi", path)
    metrics = {m.name: m for m in graph.project.metrics}
    assert (metrics["amount_7d"].measure, metrics["amount_7d"].window) == ("amount", "7 days")
    assert metrics["top_30d"].window == "30 days"
    assert metrics["rows_1d"].window == "1 day"
    assert all(m.grain_to_date is None for m in metrics.values())
    # Each one carries the day-grain caveat, naming the time dimension to query it with.
    assert len(warnings) == 3
    joined = "\n".join(warnings)
    assert (
        "`Amount 7D`: imported as a 7-day window; query it only with `calendar.day__day` "
        "(or the fact's own date column related to it)"
    ) in joined
    assert "other date dimensions give different numbers" in joined
    assert "other columns of `Calendar` also gives different numbers" in joined
    # Over the fact's own date there is no other date column to point to.
    assert "query it only with `sales.sold_on__day`, where" in joined
    request = QueryRequest(metrics=["amount_7d"], dimensions=["calendar.day__day"])
    assert "AMOUNT_7D_WINDOW" in Planner(graph, "duckdb").plan(request).sql.upper()


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        (f"CALCULATE([Amount], {_window(unit='MONTH')})", "in a unit other than DAY"),
        (f"CALCULATE([Amount], {_window(unit='YEAR')})", "in a unit other than DAY"),
        (f"CALCULATE([Amount], {_window(count='7')})", "not a negative whole number"),
        (f"CALCULATE([Amount], {_window(count='-1.5')})", "not a negative whole number"),
        (f"CALCULATE([Amount], {_window(count='-0')})", "over zero days"),
        (
            "CALCULATE([Amount], DATESINPERIOD(Calendar[Day], LASTDATE(Calendar[Day]), -7, DAY))",
            "anchored elsewhere than MAX(Calendar[Day])",
        ),
        (
            "CALCULATE([Amount], DATESINPERIOD(Calendar[Day], TODAY(), -7, DAY))",
            "anchored elsewhere than MAX(Calendar[Day])",
        ),
        (
            "CALCULATE([Amount], DATESINPERIOD(Calendar[Day], MAX(Sales[sold_on]), -7, DAY))",
            "anchored elsewhere than MAX(Calendar[Day])",
        ),
        (
            f'CALCULATE([Amount], {_window()}, Sales[buyer] = "a")',
            "CALCULATE with DATESINPERIOD and other filters",
        ),
        (f"CALCULATE([Amount], {_window('Sales[buyer]')})", "`Sales[buyer]`, not a date column"),
        (f"CALCULATE([Mean], {_window()})", "a rolling window over a `avg` measure"),
        (
            f"CALCULATE(DISTINCTCOUNTNOBLANK(Sales[buyer]), {_window()})",
            "a rolling window over a `count_distinct` measure",
        ),
        (
            "CALCULATE([Amount], DATESBETWEEN(Calendar[Day], DATE(2024, 1, 1), MAX(Calendar[Day])))",
            "DAX DATE, DATESBETWEEN has no equivalent",
        ),
    ],
)
def test_other_datesinperiod_forms_stay_warnings(tmp_path, dax, reason):
    graph, warnings = imported("powerbi", _window_bim(tmp_path, {"Rolling": dax}))
    # Not imported, so the imported model still passes validation.
    assert "rolling" not in {m.name for m in graph.project.metrics}
    joined = "\n".join(warnings)
    assert "measure `Rolling` not imported" in joined
    assert reason in joined


def test_datesinperiod_over_an_unrelated_date_table_is_a_warning(tmp_path):
    path = _window_bim(tmp_path, {"Rolling": f"CALCULATE([Amount], {_window()})"}, related=False)
    graph, warnings = imported("powerbi", path)
    assert "rolling" not in {m.name for m in graph.project.metrics}
    assert "`Sales` does not reach through an imported relationship" in "\n".join(warnings)


# (day number, date, amount, buyer): day 3 has no facts but is a day of the windows of days 4
# and 5, and days 6 and 7 have none either, so a window of 3 days on day 8 holds day 8 alone.
# The last fact has no date: Cosmos gives it a null day with a null window (DAX's blank row is
# not checked here).
WINDOW_ROWS = [
    (1, dt.date(2024, 1, 1), 10.0, "a"),
    (1, dt.date(2024, 1, 1), 5.0, "b"),
    (2, dt.date(2024, 1, 2), 7.0, "a"),
    (4, dt.date(2024, 1, 4), 1.0, "b"),
    (5, dt.date(2024, 1, 5), None, "a"),
    (5, dt.date(2024, 1, 5), 20.0, "b"),
    (8, dt.date(2024, 1, 8), 3.0, "a"),
    (None, None, 4.0, "a"),
]
# `amount_3d` by day and buyer, worked out by hand: each buyer has its own window, in DAX as
# in Cosmos (the CALCULATE filter on Calendar[Day] keeps the filter on Sales[buyer]).
WINDOW_BY_BUYER = {
    (1, "a"): 10.0,
    (1, "b"): 5.0,
    (2, "a"): 17.0,
    (4, "b"): 1.0,
    (5, "a"): None,
    (5, "b"): 21.0,
    (8, "a"): 3.0,
}
# metric: (DAX over the `Calendar` date table, the hand-written aggregate, N - 1, the DAX
# numbers by day worked out by hand). DAX takes the N dates ending at MAX(Calendar[Day]),
# which at day grain is the row's day; the hand-written SQL takes the facts of the day
# numbers c.day_no - (N - 1) to c.day_no.
WINDOW_MEASURES = {
    "amount_3d": (
        f"CALCULATE([Amount], {_window(count='-3')})",
        "SUM(p.amount)",
        2,
        {1: 15.0, 2: 22.0, 4: 8.0, 5: 21.0, 8: 3.0},
    ),
    "amount_7d": (
        f"CALCULATE(SUM(Sales[amount]), {_window()})",
        "SUM(p.amount)",
        6,
        {1: 15.0, 2: 22.0, 4: 23.0, 5: 43.0, 8: 31.0},
    ),
    "top_3d": (
        f"CALCULATE(MAX(Sales[amount]), {_window(count='-3')})",
        "MAX(p.amount)",
        2,
        {1: 10.0, 2: 10.0, 4: 7.0, 5: 20.0, 8: 3.0},
    ),
    "rows_1d": (
        f"CALCULATE(COUNTROWS(Sales), {_window(count='-1')})",
        "COUNT(*)",
        0,
        {1: 2.0, 2: 1.0, 4: 1.0, 5: 2.0, 8: 1.0},
    ),
}


@pytest.mark.parametrize("source", SOURCES)
def test_datesinperiod_matches_hand_written_sql_with_empty_days(source, request, tmp_path):
    if source == "trino_connector":
        pytest.skip(
            "Trino's memory connector hangs on this query's self-referencing BETWEEN join "
            "even on a handful of rows (reproduced directly against the server, no Cosmos "
            "code involved); not a timeout to raise, a connector limitation to revisit"
        )
    connector = request.getfixturevalue(source)
    day_no, sold_on, amount, buyer = (list(c) for c in zip(*WINDOW_ROWS, strict=True))
    calendar = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(10)]
    demo_data.load(
        connector,
        {
            "powerbi_window_sales": pa.table(
                [sold_on, day_no, amount, buyer],
                schema=pa.schema(
                    [
                        ("sold_on", pa.date32()),
                        ("day_no", pa.int64()),
                        ("amount", pa.float64()),
                        ("buyer", pa.string()),
                    ]
                ),
            ),
            "powerbi_window_calendar": pa.table(
                [calendar], schema=pa.schema([("cal_day", pa.date32())])
            ),
        },
    )
    schema = connector.config.schema_map.get("public", "public")
    sales = f"{schema}.powerbi_window_sales"
    try:
        measures = {name: dax for name, (dax, *_) in WINDOW_MEASURES.items()}
        other, warnings = imported("powerbi", _window_bim(tmp_path, measures))
        assert not [w for w in warnings if "not imported" in w]
        planner = Planner(other, connector.dialect, connector.config.schema_map)

        def cosmos(dimensions: list[str]) -> dict:
            sql = planner.plan(QueryRequest(metrics=list(WINDOW_MEASURES), dimensions=dimensions))
            out = {}
            for raw in connector.execute(sql.sql).to_pylist():
                row = {k.lower(): v for k, v in raw.items()}
                day = row["calendar__day__day"]
                if day is not None:
                    day = (dt.date.fromisoformat(str(day)[:10]) - calendar[0]).days + 1
                key = (day, row["sales__buyer"]) if len(dimensions) == 2 else day
                out[key] = {m: _number(row[m]) for m in WINDOW_MEASURES}
            return out

        def hand(sql: str) -> dict:
            out = {}
            for row in connector.execute(sql).to_pylist():
                *key, value = row.values()
                key[0] = int(key[0])
                out[tuple(key) if len(key) == 2 else key[0]] = _number(value)
            return out

        by_day = cosmos(["calendar.day__day"])
        by_buyer = cosmos(["calendar.day__day", "sales.buyer"])
        # The fact without a date: a null day with a null window, in a row of its own.
        assert by_day.pop(None) == dict.fromkeys(WINDOW_MEASURES)
        assert by_buyer.pop((None, "a")) == dict.fromkeys(WINDOW_MEASURES)
        for metric, (_, combine, back, values) in WINDOW_MEASURES.items():
            window = f"p.day_no BETWEEN c.day_no - {back} AND c.day_no"
            expected = hand(
                f"SELECT c.day_no, {combine} AS v FROM (SELECT DISTINCT day_no FROM {sales}) c "
                f"JOIN {sales} p ON {window} GROUP BY c.day_no"
            )
            assert expected == values, metric
            # Days without facts (3, 6 and 7) have no row, in Cosmos as in the hand-written SQL.
            assert {day: v[metric] for day, v in by_day.items()} == values, metric
            expected = hand(
                f"SELECT c.day_no, c.buyer, {combine} AS v "
                f"FROM (SELECT DISTINCT day_no, buyer FROM {sales}) c "
                f"JOIN {sales} p ON p.buyer = c.buyer AND {window} GROUP BY c.day_no, c.buyer"
            )
            if metric == "amount_3d":
                assert expected == WINDOW_BY_BUYER
            assert {key: v[metric] for key, v in by_buyer.items()} == expected, metric
    finally:
        connector.run_script(f"DROP TABLE {sales}")
        connector.run_script(f"DROP TABLE {schema}.powerbi_window_calendar")


# -- period shifts as offset inputs (DEC-35) ------------------------------------------------


def _shift_bim(directory: Path, measures: dict[str, str], related: bool = True) -> Path:
    """`Sales` (a date, an amount, a buyer, and the period numbers the hand-written SQL uses)
    and a `Calendar` date table (`cal_day`) related to it, or left unrelated."""

    def table(name: str, physical: str, columns, extra: dict[str, str]):
        return {
            "name": name,
            "columns": [{"name": c, "dataType": t, "sourceColumn": c} for c, t in columns],
            "partitions": [
                {"name": name, "source": {"type": "query", "query": f"SELECT * FROM {physical}"}}
            ],
            "measures": [{"name": k, "expression": v} for k, v in extra.items()],
        }

    sales = [("sold_on", "dateTime"), ("amount", "double"), ("buyer", "string")]
    sales += [(c, "int64") for c in PERIOD_NUMBERS]
    base = {
        "Amount": "SUM(Sales[amount])",
        "Lines": "COUNTROWS(Sales)",
        "Amount YTD": "TOTALYTD([Amount], 'Calendar'[cal_day])",
        "Average": "DIVIDE([Amount], [Lines])",
    }
    relationship = {
        "fromTable": "Sales",
        "fromColumn": "sold_on",
        "toTable": "Calendar",
        "toColumn": "cal_day",
    }
    model = {
        "model": {
            "tables": [
                table("Sales", "public.powerbi_shift_sales", sales, {**base, **measures}),
                table("Calendar", "public.powerbi_shift_calendar", [("cal_day", "dateTime")], {}),
            ],
            "relationships": [relationship] if related else [],
        }
    }
    path = directory / "m.bim"
    path.write_text(json.dumps(model), encoding="utf-8")
    return path


PERIOD_NUMBERS = ("day_no", "week_no", "month_no", "quarter_no", "year_no")
DAY = "'Calendar'[cal_day]"


def test_period_shifts_import_as_offset_inputs(tmp_path):
    path = _shift_bim(
        tmp_path,
        {
            "Amount PY": f"CALCULATE([Amount], SAMEPERIODLASTYEAR({DAY}))",
            "Amount PM": f"CALCULATE([Amount], DATEADD({DAY}, -1, MONTH))",
            "Amount 2Q": "CALCULATE([Amount], DATEADD(Calendar[cal_day], -2, quarter))",
            "Amount LY": f"CALCULATE([Amount], DATEADD({DAY}, -1, YEAR))",
            "Lines PW": f"CALCULATE(COUNTROWS(Sales), DATEADD({DAY}, -7, DAY))",
            "Amount PP": f"CALCULATE(SUM(Sales[amount]), PARALLELPERIOD({DAY}, -3, MONTH))",
            "Average PM": f"CALCULATE([Average], DATEADD({DAY}, -1, MONTH))",
            "YoY": "DIVIDE([Amount] - [Amount PY], [Amount PY])",
            "YoY Delta": "[Amount] - [Amount PY]",
            "PY Again": "CALCULATE([Amount PY])",
            "PY Share": "DIVIDE([Amount PY], [Amount])",
        },
    )
    graph, warnings = imported("powerbi", path)
    metrics = {m.name: m for m in graph.project.metrics}
    shifts = {
        "amount_py": ("amount", "12 months"),
        "amount_pm": ("amount", "1 month"),
        "amount_2q": ("amount", "6 months"),
        "amount_ly": ("amount", "12 months"),
        "lines_pw": ("lines_pw_base", "7 days"),
        "amount_pp": ("amount_pp_base", "3 months"),
        "average_pm": ("average", "1 month"),
    }
    for name, (inner, offset) in shifts.items():
        metric = metrics[name]
        assert metric.type == "derived" and metric.expr == "{" + inner + "}", name
        assert [(i.metric, i.offset) for i in metric.inputs] == [(inner, offset)], name
    assert metrics["yoy"].expr == "({amount} - {amount_py}) / ({amount_py})"
    assert metrics["yoy"].inputs == [] and metrics["py_again"].expr == "{amount_py}"
    assert (metrics["py_share"].numerator, metrics["py_share"].denominator) == (
        "amount_py",
        "amount",
    )
    joined = "\n".join(warnings)
    assert "not imported" not in joined
    # One warning per period shift, naming the grains to query it by and the differences.
    assert (
        "`Amount PY`: imported as `amount` read 12 months earlier; query it by "
        "`calendar.cal_day__month` or `calendar.cal_day__quarter` or `calendar.cal_day__year`"
    ) in joined
    assert (
        "`Amount PM`: imported as `amount` read 1 month earlier; query it by "
        "`calendar.cal_day__month` (or by the fact's own"
    ) in joined
    assert (
        "read 7 days earlier; query it by `calendar.cal_day__day` or `calendar.cal_day__week`"
    ) in joined
    assert "29, 30 and 31 March a month back are all the last day of February" in joined
    assert joined.count("gives DAX a partial earlier period") == 6
    assert "`Amount PP`: imported as `amount_pp_base` read 3 months earlier" in joined
    assert "still gives DAX the whole earlier months" in joined
    # Adding or subtracting an offset metric: DAX counts BLANK as zero there.
    assert "`YoY`: adds or subtracts `amount_py`" in joined
    assert "`YoY Delta`: adds or subtracts `amount_py`" in joined
    assert "`PY Share`: adds" not in joined
    request = QueryRequest(metrics=["amount", "yoy"], dimensions=["calendar.cal_day__year"])
    assert "OFFSET_12_MONTH" in Planner(graph, "duckdb").plan(request).sql.upper()


def test_divide_inside_arithmetic_keeps_its_grouping(tmp_path, duckdb_connector):
    """`[A] / DIVIDE([B], [C])` is a / (b / c), not (a / b) / c."""
    path = _shift_bim(
        tmp_path,
        {
            "Nested": "[Amount] / DIVIDE([Lines], [Amount])",
            "Left": "DIVIDE([Amount], [Lines]) / [Lines]",
        },
    )
    graph, _ = imported("powerbi", path)
    metrics = {m.name: m for m in graph.project.metrics}
    assert metrics["nested"].expr == "{amount} / (({lines}) / ({amount}))"
    assert metrics["left"].expr == "(({amount}) / ({lines})) / {lines}"
    # Amount 4 over 2 lines: 4 / (2 / 4) = 8, where (4 / 2) / 4 would be 0.5.
    schema = pa.schema([("amount", pa.float64())])
    table = pa.table([[3.0, 1.0]], schema=schema)
    demo_data.load(duckdb_connector, {"powerbi_shift_sales": table})
    try:
        sql = Planner(graph, "duckdb").plan(QueryRequest(metrics=["nested", "left"])).sql
        assert list(duckdb_connector.execute(sql).to_pylist()[0].values()) == [8.0, 1.0]
    finally:
        duckdb_connector.run_script("DROP TABLE public.powerbi_shift_sales")


def test_divide_with_an_alternate_result_imports_with_a_warning(tmp_path):
    path = _shift_bim(
        tmp_path,
        {
            "Zero": "DIVIDE([Amount], [Lines], 0)",
            "Minus One": "DIVIDE([Amount] - [Lines], [Lines], -1)",
            "Inside": "[Amount] + DIVIDE([Amount], [Lines], 0) + DIVIDE([Lines], [Amount], 1)",
            "Blank": "DIVIDE([Amount], [Lines], BLANK())",
            "Lost": "DIVIDE([Amount], [Missing], 0)",
        },
    )
    graph, warnings = imported("powerbi", path)
    metrics = {m.name: m for m in graph.project.metrics}
    assert metrics["zero"].type == "ratio"
    assert metrics["minus_one"].expr == "({amount} - {lines}) / ({lines})"
    assert {"inside", "blank"} <= set(metrics) and "lost" not in metrics
    joined = "\n".join(warnings)
    for name in ("Zero", "Minus One", "Inside"):
        message = f"measure `{name}`: DIVIDE with an alternate result; DAX returns the alternate"
        assert joined.count(message) == 1, name
    assert "Cosmos returns NULL there" in joined
    assert "`Blank`: DIVIDE" not in joined and "`Lost`: DIVIDE" not in joined


def test_blank_as_zero_warning_follows_the_shifted_operand(tmp_path):
    path = _shift_bim(
        tmp_path,
        {
            "PY": f"CALCULATE([Amount], SAMEPERIODLASTYEAR({DAY}))",
            "Share": "DIVIDE([PY], [Amount])",
            "Share Copy": "CALCULATE([Share])",
            "Share A": 'CALCULATE([Share], KEEPFILTERS(Sales[buyer] = "a"))',
            "Share Minus": "[Share] - 1",
            "Copy Plus": "[Share Copy] + 1",
            "A Plus": "[Share A] + 1",
            "Over PY": "DIVIDE([Amount] - [Lines], [PY])",
            "Minus Ratio": "[Amount] - DIVIDE([Amount], [PY])",
        },
    )
    graph, warnings = imported("powerbi", path)
    assert {"share", "share_a", "share_minus", "over_py"} <= {m.name for m in graph.project.metrics}
    joined = "\n".join(warnings)
    assert "`Share Minus`: adds or subtracts `share`" in joined
    assert "`Copy Plus`: adds or subtracts `share_copy`" in joined
    assert "`A Plus`: adds or subtracts `share_a`" in joined
    assert "`Minus Ratio`: adds or subtracts `py`" in joined
    # The shifted metric only divides here, outside the subtraction: no BLANK-as-zero warning.
    assert "`Over PY`: adds" not in joined
    assert "`Share`: adds" not in joined


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        (f"CALCULATE([Amount], DATEADD({DAY}, 1, YEAR))", "not a negative whole number"),
        (f"CALCULATE([Amount], DATEADD({DAY}, -1.5, MONTH))", "not a negative whole number"),
        (f"CALCULATE([Amount], DATEADD({DAY}, -0, MONTH))", "by zero periods"),
        (f"CALCULATE([Amount], DATEADD({DAY}, -1, WEEK))", "in a unit other than DAY, MONTH"),
        (f"CALCULATE([Amount], PARALLELPERIOD({DAY}, -1, DAY))", "other than MONTH, QUARTER"),
        (
            f"CALCULATE([Amount], PARALLELPERIOD({DAY}, -1, YEAR))",
            "PARALLELPERIOD by YEAR returns the whole earlier year",
        ),
        (
            f"CALCULATE([Amount], PARALLELPERIOD({DAY}, -2, QUARTER))",
            "By quarter it is DATEADD(..., -2, QUARTER)",
        ),
        (
            f'CALCULATE([Amount], SAMEPERIODLASTYEAR({DAY}), Sales[buyer] = "a")',
            "a period shift and other filters",
        ),
        ("CALCULATE([Amount], SAMEPERIODLASTYEAR(Sales[buyer]))", "not a date column"),
        (
            "CALCULATE([Amount], SAMEPERIODLASTYEAR(Sales[sold_on]))",
            "a column of the measure's own table",
        ),
        (
            "CALCULATE(SUM(Sales[amount]), SAMEPERIODLASTYEAR(Sales[sold_on]))",
            "a column of the measure's own table",
        ),
        (
            f"CALCULATE([Amount YTD], SAMEPERIODLASTYEAR({DAY}))",
            "`amount_ytd`, a cumulative metric",
        ),
        (
            f"CALCULATE([Amount], DATEADD(DATESYTD({DAY}), -1, YEAR))",
            "over a table expression instead of a date column",
        ),
    ],
)
def test_other_period_shifts_stay_warnings(tmp_path, dax, reason):
    graph, warnings = imported("powerbi", _shift_bim(tmp_path, {"Shifted": dax}))
    # Not imported, so the imported model still passes validation, with no helper left.
    assert "shifted" not in {m.name for m in graph.project.metrics}
    sales = next(m for m in graph.project.semantic_models if m.name == "sales")
    assert {m.name for m in sales.measures} == {"amount", "lines"}
    joined = "\n".join(warnings)
    assert "measure `Shifted` not imported" in joined
    assert reason in joined


def test_period_shift_of_an_offset_metric_or_over_an_unrelated_table_is_a_warning(tmp_path):
    measures = {
        "PY": f"CALCULATE([Amount], SAMEPERIODLASTYEAR({DAY}))",
        "PY PY": f"CALCULATE([PY], SAMEPERIODLASTYEAR({DAY}))",
        "Growth": "DIVIDE([Amount] - [PY], [PY])",
        "Growth PM": f"CALCULATE([Growth], DATEADD({DAY}, -1, MONTH))",
        "Sum PY": f"CALCULATE(SUM(Sales[amount]), SAMEPERIODLASTYEAR({DAY}))",
    }
    graph, warnings = imported("powerbi", _shift_bim(tmp_path, measures))
    names = {m.name for m in graph.project.metrics}
    assert {"py", "growth", "sum_py"} <= names and not {"py_py", "growth_pm"} & names
    joined = "\n".join(warnings)
    assert "`PY PY` not imported" in joined and "already reads an earlier period" in joined
    assert "`Growth PM` not imported" in joined
    graph, warnings = imported("powerbi", _shift_bim(tmp_path, measures, related=False))
    assert not {"py", "growth", "sum_py"} & {m.name for m in graph.project.metrics}
    sales = next(m for m in graph.project.semantic_models if m.name == "sales")
    assert {m.name for m in sales.measures} == {"amount", "lines"}
    assert "`Sales` does not reach through an imported relationship" in "\n".join(warnings)


# (date, amount, buyer): month ends of months of different lengths (31 March and 30 April a
# month back, 29 February 2024 a year back from February 2025), a year change, months
# without facts (April 2025, so May reads none a month back), and a fact without a date.
SHIFT_ROWS = [
    (dt.date(2023, 2, 15), 4.0, "a"),
    (dt.date(2023, 3, 31), 6.0, "b"),
    (dt.date(2024, 1, 31), 10.0, "a"),
    (dt.date(2024, 2, 29), 6.0, "b"),
    (dt.date(2024, 3, 31), 8.0, "a"),
    (dt.date(2024, 4, 30), 5.0, "a"),
    (dt.date(2024, 12, 31), 2.0, "b"),
    (dt.date(2025, 1, 1), 20.0, "a"),
    (dt.date(2025, 1, 8), 3.0, "a"),
    (dt.date(2025, 2, 28), 7.0, "a"),
    (dt.date(2025, 3, 15), 9.0, "b"),
    (dt.date(2025, 3, 31), 1.0, "a"),
    (dt.date(2025, 5, 10), 3.0, "a"),
    (None, 100.0, "a"),
]
SHIFT_CALENDAR = [dt.date(2023, 1, 1) + dt.timedelta(days=i) for i in range(1096)]
# metric: (DAX, aggregate, how far back in months or days, that unit, PARALLELPERIOD or not).
# The hand-written SQL reads the facts whose period number is that many periods lower.
SHIFT_MEASURES = {
    "amount_py": (f"CALCULATE([Amount], SAMEPERIODLASTYEAR({DAY}))", "sum", 12, "MONTH", False),
    "amount_pm": (f"CALCULATE([Amount], DATEADD({DAY}, -1, MONTH))", "sum", 1, "MONTH", False),
    "amount_pq": (f"CALCULATE([Amount], DATEADD({DAY}, -1, QUARTER))", "sum", 3, "MONTH", False),
    "amount_pp": (
        f"CALCULATE(SUM(Sales[amount]), PARALLELPERIOD({DAY}, -1, MONTH))",
        "sum",
        1,
        "MONTH",
        True,
    ),
    "lines_pd": (f"CALCULATE(COUNTROWS(Sales), DATEADD({DAY}, -1, DAY))", "count", 1, "DAY", False),
    "amount_pw": (f"CALCULATE([Amount], DATEADD({DAY}, -7, DAY))", "sum", 7, "DAY", False),
}
SHIFT_GROWTH = {"yoy": "DIVIDE([Amount] - [Amount PY], [Amount PY])"}


def _add_months(day: dt.date, months: int) -> dt.date:
    year, month = divmod(day.month - 1 + months, 12)
    year += day.year
    return dt.date(year, month + 1, min(day.day, cal.monthrange(year, month + 1)[1]))


def _month_end(day: dt.date) -> bool:
    return day.day == cal.monthrange(day.year, day.month)[1]


def _dax_shift(dates: set[dt.date], back: int, unit: str, parallel: bool) -> set[dt.date]:
    """A model of DAX's DATEADD and PARALLELPERIOD over a complete date table, written from
    their documentation: each date moves back, a month shift clamps the day to the month's
    end, and a date on a month's last day takes the rest of the shifted month with it;
    PARALLELPERIOD then widens the result to whole months."""
    if unit == "DAY":
        return {d - dt.timedelta(days=back) for d in dates}
    out = set()
    for day in dates:
        moved = _add_months(day, -back)
        out.add(moved)
        while _month_end(day) and not _month_end(moved):
            moved += dt.timedelta(days=1)
            out.add(moved)
    if parallel and out:
        first, last = min(out).replace(day=1), max(out)
        last = last.replace(day=cal.monthrange(last.year, last.month)[1])
        out = {first + dt.timedelta(days=i) for i in range((last - first).days + 1)}
    return out


def _period(day: dt.date, grain: str) -> int:
    return {
        "day": day.toordinal(),
        "week": (day.toordinal() - 1) // 7,  # 1 January of year 1 was a Monday: ISO weeks
        "month": day.year * 12 + day.month - 1,
        "quarter": day.year * 4 + (day.month - 1) // 3,
        "year": day.year,
    }[grain]


def _dax(metric: str, grain: str, keep=lambda day: True, buyer: str | None = None) -> dict:
    """The DAX value of a period-shift measure for every period of `grain` that has facts,
    after the date filter `keep` (a slicer: it limits the dates in context, then the shift
    replaces it) and an optional buyer."""
    _, agg, back, unit, parallel = SHIFT_MEASURES[metric]
    rows = [r for r in SHIFT_ROWS if buyer is None or r[2] == buyer]
    out = {}
    for period in sorted({_period(r[0], grain) for r in rows if r[0] and keep(r[0])}):
        dates = {d for d in SHIFT_CALENDAR if _period(d, grain) == period and keep(d)}
        shifted = _dax_shift(dates, back, unit, parallel)
        values = [r[1] for r in rows if r[0] in shifted]
        out[period] = (sum(values) if agg == "sum" else len(values)) if values else None
    return out


def test_dax_model_of_month_ends():
    """The cases the documented differences rest on: by day, a month back clamps 29 to
    31 March to the end of February; whole months map to whole months."""
    for day in (29, 30, 31):
        assert _dax_shift({dt.date(2024, 3, day)}, 1, "MONTH", False) == {dt.date(2024, 2, 29)}
    april = {dt.date(2024, 4, d) for d in range(1, 31)}
    assert _dax_shift(april, 1, "MONTH", False) == {dt.date(2024, 3, d) for d in range(1, 32)}
    february = {dt.date(2025, 2, d) for d in range(1, 29)}
    assert dt.date(2024, 2, 29) in _dax_shift(february, 12, "MONTH", False)
    assert _dax_shift({dt.date(2025, 3, 20)}, 1, "MONTH", True) == {
        dt.date(2025, 2, d) for d in range(1, 29)
    }


@pytest.mark.parametrize("source", SOURCES)
def test_period_shifts_match_hand_written_sql_and_dax(source, request, tmp_path):
    connector = request.getfixturevalue(source)
    dates = [r[0] for r in SHIFT_ROWS]
    numbers = {
        f"{grain}_no": [None if d is None else _period(d, grain) for d in dates]
        for grain in ("day", "week", "month", "quarter", "year")
    }
    demo_data.load(
        connector,
        {
            "powerbi_shift_sales": pa.table(
                [dates, [r[1] for r in SHIFT_ROWS], [r[2] for r in SHIFT_ROWS]]
                + [numbers[c] for c in PERIOD_NUMBERS],
                schema=pa.schema(
                    [("sold_on", pa.date32()), ("amount", pa.float64()), ("buyer", pa.string())]
                    + [(c, pa.int64()) for c in PERIOD_NUMBERS]
                ),
            ),
            "powerbi_shift_calendar": pa.table(
                [SHIFT_CALENDAR], schema=pa.schema([("cal_day", pa.date32())])
            ),
        },
    )
    schema = connector.config.schema_map.get("public", "public")
    sales = f"{schema}.powerbi_shift_sales"
    try:
        measures = {
            {"amount_py": "Amount PY"}.get(name, name): dax
            for name, (dax, *_) in SHIFT_MEASURES.items()
        }
        other, warnings = imported("powerbi", _shift_bim(tmp_path, {**measures, **SHIFT_GROWTH}))
        assert not [w for w in warnings if "not imported" in w]
        planner = Planner(other, connector.dialect, connector.config.schema_map)

        def cosmos(metrics: list[str], grain: str, by_buyer=False, filters=()) -> dict:
            dimensions = [f"calendar.cal_day__{grain}"] + (["sales.buyer"] if by_buyer else [])
            sql = planner.plan(
                QueryRequest(metrics=metrics, dimensions=dimensions, filters=list(filters))
            ).sql
            out = {}
            for raw in connector.execute(sql).to_pylist():
                row = {k.lower(): v for k, v in raw.items()}
                day = row[f"calendar__cal_day__{grain}"]
                period = (
                    None if day is None else _period(dt.date.fromisoformat(str(day)[:10]), grain)
                )
                key = (period, row["sales__buyer"]) if by_buyer else period
                out[key] = {m: _number(row[m]) for m in metrics}
            return out

        def hand(metric: str, grain: str) -> dict:
            _, agg, back, _, _ = SHIFT_MEASURES[metric]
            column = f"{grain}_no"
            shift = back // {"quarter": 3, "year": 12, "week": 7}.get(grain, 1)
            value = "SUM(p.amount)" if agg == "sum" else "NULLIF(COUNT(p.amount), 0)"
            sql = (
                f"SELECT c.k, {value} AS v FROM (SELECT DISTINCT {column} AS k FROM {sales} "
                f"WHERE {column} IS NOT NULL) c LEFT JOIN {sales} p ON p.{column} = c.k - {shift} "
                "GROUP BY c.k"
            )
            return {
                int(r[0]): _number(r[1])
                for r in (list(x.values()) for x in connector.execute(sql).to_pylist())
            }

        grains = {
            "month": ["amount_py", "amount_pm", "amount_pq", "amount_pp"],
            "quarter": ["amount_py", "amount_pq"],
            "year": ["amount_py"],
            "day": ["lines_pd", "amount_pw"],
            "week": ["amount_pw"],
        }
        for grain, metrics in grains.items():
            got = cosmos(metrics, grain)
            # The fact without a date: a null period whose earlier period is NULL.
            assert got.pop(None) == dict.fromkeys(metrics), grain
            for metric in metrics:
                expected = hand(metric, grain)
                assert expected == _dax(metric, grain), (metric, grain)
                assert {k: v[metric] for k, v in got.items()} == expected, (metric, grain)
        # Hand-checked anchors: February 2025 reads 29 February 2024, May 2025 reads an April
        # without facts, and 30 April 2024 reads all of March, its 31st included.
        month = cosmos(["amount_py", "amount_pm"], "month")
        assert month[_period(dt.date(2025, 2, 1), "month")] == {"amount_py": 6.0, "amount_pm": 23.0}
        assert month[_period(dt.date(2025, 5, 1), "month")]["amount_pm"] is None
        assert month[_period(dt.date(2024, 4, 1), "month")]["amount_pm"] == 8.0
        # The growth over the shifted metric, by year: NULL where the year before has no facts.
        year = cosmos(["amount", "amount_py", "yoy"], "year")
        assert year[2024] == {"amount": 31.0, "amount_py": 10.0, "yoy": 2.1}
        assert year[2023] == {"amount": 10.0, "amount_py": None, "yoy": None}
        # Each buyer is shifted on its own, in DAX as in Cosmos.
        by_buyer = cosmos(["amount_py", "amount_pm"], "month", by_buyer=True)
        by_buyer.pop((None, "a"))
        for buyer in ("a", "b"):
            for metric in ("amount_py", "amount_pm"):
                mine = {k[0]: v[metric] for k, v in by_buyer.items() if k[1] == buyer}
                assert mine == _dax(metric, "month", buyer=buyer), (metric, buyer)

        # Documented differences. A filter that cuts a period: DAX moves the dates left in
        # context (15 to 31 March 2025 read 15 to 28 February), PARALLELPERIOD takes the whole
        # earlier month, and Cosmos filters the earlier period's facts too.
        since = dt.date(2025, 3, 15)
        cut = cosmos(
            ["amount", "amount_pm", "amount_pp", "amount_py"],
            "month",
            filters=[f"{{calendar.cal_day}} >= DATE '{since.isoformat()}'"],
        )
        march = _period(since, "month")
        after = lambda day: day >= since  # noqa: E731
        assert _dax("amount_pm", "month", after)[march] == 7.0
        assert _dax("amount_pp", "month", after)[march] == 7.0
        assert _dax("amount_py", "month", after)[march] == 8.0
        assert cut[march] == {
            "amount": 10.0,
            "amount_pm": None,
            "amount_pp": None,
            "amount_py": None,
        }
        # A filter of whole periods (the year 2025) cuts nothing in DAX, whose January still
        # reads December 2024; in Cosmos the first period's earlier period is filtered out.
        year_2025 = cosmos(
            ["amount_pm", "amount_py"],
            "month",
            filters=["{calendar.cal_day} >= DATE '2025-01-01'"],
        )
        january = _period(dt.date(2025, 1, 1), "month")
        in_2025 = lambda day: day.year == 2025  # noqa: E731
        assert _dax("amount_pm", "month", in_2025)[january] == 2.0
        assert _dax("amount_py", "month", in_2025)[january] == 10.0
        assert year_2025[january] == {"amount_pm": None, "amount_py": None}
        # February reads a January inside the filter, so the month offset agrees again; the
        # year offset reads February 2024, which the filter removes in Cosmos only.
        assert year_2025[january + 1] == {"amount_pm": 23.0, "amount_py": None}
        assert _dax("amount_pm", "month", in_2025)[january + 1] == 23.0
        assert _dax("amount_py", "month", in_2025)[january + 1] == 6.0
        # Periods without facts: Power BI shows April 2025, whose March has facts; Cosmos
        # outputs only the periods with facts.
        assert _period(dt.date(2025, 4, 1), "month") not in cosmos(["amount_pm"], "month")
        # By day, a month back is refused instead of clamping month ends.
        with pytest.raises(QueryError, match="cannot be applied by day"):
            cosmos(["amount_pm"], "day")
        with pytest.raises(QueryError, match="cannot be applied by month"):
            cosmos(["amount_pw"], "month")
    finally:
        connector.run_script(f"DROP TABLE {sales}")
        connector.run_script(f"DROP TABLE {schema}.powerbi_shift_calendar")
