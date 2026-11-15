"""`dimension_values` returns the same value counts as hand-written SQL, on every database
that is configured (DuckDB always; the others when their environment variables are set)."""

from __future__ import annotations

import pytest

from cosmos_semantic.connectors import ConnectionsFile
from cosmos_semantic.engine import Engine
from cosmos_semantic.expressions import remap_sql
from cosmos_semantic.planner import Principal
from cosmos_semantic.service import SemanticService

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

# (dimension, user context, hand-written SQL: one row per value, then its count)
CASES = {
    "category_with_nulls": (
        "products.category",
        {},
        "SELECT category, COUNT(*) FROM public.products GROUP BY category",
    ),
    "boolean_has_discount": (
        "orders.has_discount",
        {},
        # Portable boolean: 1/0, so SQL Server and Oracle answer the same as the others.
        "SELECT CASE WHEN discount > 0 THEN 1 WHEN NOT (discount > 0) THEN 0 END, COUNT(*) "
        "FROM public.orders GROUP BY CASE WHEN discount > 0 THEN 1 WHEN NOT (discount > 0) "
        "THEN 0 END",
    ),
    "row_filtered_segment": (
        "customers.segment",
        {"region": "West"},
        "SELECT segment, COUNT(*) FROM public.customers WHERE region = 'West' GROUP BY segment",
    ),
}


def _norm(value):
    return int(value) if isinstance(value, bool) else value


@pytest.mark.parametrize("case", CASES, ids=str)
@pytest.mark.parametrize("source", SOURCES)
def test_dimension_values_match_hand_written_sql(case, source, graph, request):
    connector = request.getfixturevalue(source)
    dimension, user, sql = CASES[case]
    engine = Engine(graph, ConnectionsFile(data_sources={"db": connector.config}))
    engine._connectors["db"] = connector
    service = SemanticService(engine, audit_path=None)

    result = service.dimension_values(dimension, Principal(user=user, channel="api"))

    got = {_norm(v["value"]): v["count"] for v in result["values"]}
    sql = remap_sql(sql, connector.dialect, connector.config.schema_map)
    want = {
        _norm(value): int(count)
        for value, count in (tuple(r.values()) for r in connector.execute(sql).to_pylist())
    }
    assert result["truncated"] is False
    assert got == want
    if case != "boolean_has_discount":
        assert None in want  # the null value is counted like any other
