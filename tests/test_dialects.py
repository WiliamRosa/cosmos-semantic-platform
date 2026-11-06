"""Dialect rewrites (ADR 0011) that need no database; the comparative suites cover the results."""

from __future__ import annotations

import pytest
import sqlglot

from cosmos_semantic import dialects
from cosmos_semantic.connectors import DataSourceConfig, _session_setup, connect
from cosmos_semantic.errors import ModelError


def _adapted(sql: str, dialect: str) -> str:
    return dialects.adapt(sqlglot.parse_one(sql, read="postgres"), dialect).sql(dialect=dialect)


def test_mysql_weeks_start_on_monday():
    sql = _adapted("SELECT DATE_TRUNC('week', ordered_at) FROM t", "mysql")
    assert "WEEKDAY(ordered_at)" in sql and "DATE_TRUNC" not in sql


@pytest.mark.parametrize(
    "unit,model", [("week", "'IW'"), ("month", "'MM'"), ("quarter", "'Q'"), ("year", "'YYYY'")]
)
def test_oracle_truncates_with_format_models(unit, model):
    sql = _adapted(f"SELECT DATE_TRUNC('{unit}', ordered_at) FROM t", "oracle")
    assert f"TRUNC(ordered_at, {model})" in sql


def test_oracle_null_safe_equality_uses_decode():
    sql = _adapted("SELECT 1 FROM a JOIN b ON a.k IS NOT DISTINCT FROM b.k", "oracle")
    assert "DECODE(a.k, b.k, 1, 0) = 1" in sql and "DISTINCT" not in sql


def test_oracle_dates_use_interval_functions_and_no_time_of_day():
    sql = _adapted("SELECT 1 FROM t WHERE d >= CURRENT_DATE - INTERVAL '180' DAY", "oracle")
    assert "TRUNC(CURRENT_DATE) + NUMTODSINTERVAL(-180, 'DAY')" in sql


def test_oracle_group_by_positions_are_expanded():
    sql = _adapted("SELECT status, SUM(amount) AS s FROM t GROUP BY 1", "oracle")
    assert sql.endswith("GROUP BY status")


def test_other_dialects_are_left_alone():
    tree = sqlglot.parse_one("SELECT a IS NOT DISTINCT FROM b FROM t GROUP BY 1")
    assert dialects.adapt(tree, "postgres") is tree


def test_mysql_and_mariadb_sessions_set_their_own_timeouts():
    mysql = _session_setup(DataSourceConfig(type="mysql", uri="x", timeout_seconds=5))
    mariadb = _session_setup(DataSourceConfig(type="mariadb", uri="x", timeout_seconds=5))
    assert any("max_execution_time = 5000" in s for s in mysql)
    assert any("max_statement_time = 5" in s for s in mariadb)
    assert all("PIPES_AS_CONCAT" in (mysql[0], mariadb[0])[i] for i in range(2))


def test_an_oracle_source_without_dsn_is_rejected():
    pytest.importorskip("oracledb")
    with pytest.raises(ModelError, match="dsn"):
        connect(DataSourceConfig(type="oracle", options={"user": "x"}))
