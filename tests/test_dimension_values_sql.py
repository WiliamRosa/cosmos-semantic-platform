"""`dimension_values` returns the same value counts as hand-written SQL, on every database
that is configured (DuckDB always; the others when their environment variables are set), and
keeps the same values at the cut-off whatever order the database returns ties in."""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest
from sqlglot import exp

from cosmos_semantic import service as service_module
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.expressions import remap_sql
from cosmos_semantic.planner import Planner, Principal
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

# The engine's "today", pinned: rule.active_customer is "a purchase in the 90 days before it".
PINNED = dt.date.today() - dt.timedelta(days=30)
ACTIVE = "last_order_at >= {since}"

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
    "rule_active_customer": (
        "rule.active_customer",
        {"region": "West"},
        f"SELECT CASE WHEN {ACTIVE} THEN 1 WHEN NOT ({ACTIVE}) THEN 0 END, COUNT(*) "
        "FROM public.customers WHERE region = 'West' "
        f"GROUP BY CASE WHEN {ACTIVE} THEN 1 WHEN NOT ({ACTIVE}) THEN 0 END",
    ),
}


def _norm(value):
    return int(value) if isinstance(value, bool) else value


@pytest.mark.parametrize("case", CASES, ids=str)
@pytest.mark.parametrize("source", SOURCES)
def test_dimension_values_match_hand_written_sql(case, source, graph, request, monkeypatch):
    connector = request.getfixturevalue(source)
    monkeypatch.setattr(Planner, "now", lambda _self: dt.datetime.combine(PINNED, dt.time()))
    dimension, user, sql = CASES[case]
    engine = Engine(graph, ConnectionsFile(data_sources={"db": connector.config}))
    engine._connectors["db"] = connector
    service = SemanticService(engine, audit_path=None)

    result = service.dimension_values(dimension, Principal(user=user, channel="api"))

    got = {_norm(v["value"]): v["count"] for v in result["values"]}
    since = PINNED - dt.timedelta(days=90)
    day = exp.cast(exp.Literal.string(since.isoformat()), "DATE").sql(dialect=connector.dialect)
    sql = remap_sql(sql.format(since=day), connector.dialect, connector.config.schema_map)
    want = {
        _norm(value): int(count)
        for value, count in (tuple(r.values()) for r in connector.execute(sql).to_pylist())
    }
    assert result["truncated"] is False
    assert result["evidence"]["as_of"] == PINNED.isoformat()
    assert got == want
    if case == "rule_active_customer":
        assert {0, 1} <= set(want)  # both sides of the pinned date are exercised
    elif case != "boolean_has_discount":
        assert None in want  # the null value is counted like any other


class _TiesConnector:
    """Returns fixed (status, count) rows in a given order, as a database would after
    `ORDER BY count DESC` with its own collation deciding the ties."""

    dialect = "duckdb"

    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *, max_rows=None):
        rows = self.rows[:max_rows] if max_rows else self.rows
        return pa.table(
            {"orders__status": [r[0] for r in rows], "value_count": [r[1] for r in rows]}
        )


def _values(graph, rows, **kwargs):
    engine = Engine(graph, ConnectionsFile(data_sources={"db": DataSourceConfig(type="duckdb")}))
    engine._connectors["db"] = _TiesConnector(rows)
    service = SemanticService(engine, audit_path=None)
    return service.dimension_values("orders.status", Principal(channel="api"), **kwargs)


def test_ties_at_the_cut_off_keep_the_same_values_on_every_database(graph):
    case_insensitive = [("top", 9), ("b", 4), ("B", 4), (None, 4), ("a", 4), ("z", 1)]
    binary = [("top", 9), ("B", 4), ("a", 4), ("b", 4), (None, 4), ("z", 1)]
    first = _values(graph, case_insensitive, limit=3)
    second = _values(graph, binary, limit=3)
    assert (
        first["values"]
        == second["values"]
        == [
            {"value": "top", "count": 9},
            {"value": "B", "count": 4},
            {"value": "a", "count": 4},
        ]
    )
    assert first["truncated"] is True
    # The hash and row count cover exactly the pairs returned, not the rows fetched.
    assert first["evidence"]["result"] == second["evidence"]["result"]
    assert first["evidence"]["result"]["row_count"] == 3
    everything = _values(graph, binary)
    assert everything["values"][-2:] == [{"value": None, "count": 4}, {"value": "z", "count": 1}]
    assert everything["evidence"]["result"]["hash"] != first["evidence"]["result"]["hash"]


def test_beyond_the_fetch_ceiling_the_list_is_truncated(graph, monkeypatch):
    monkeypatch.setattr(service_module, "VALUES_FETCH_CEILING", 3)
    result = _values(graph, [("a", 5), ("b", 4), ("c", 3), ("d", 2)])
    assert [v["value"] for v in result["values"]] == ["a", "b", "c"]
    assert result["truncated"] is True
