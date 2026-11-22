"""Trino behavior that needs no server (DEC-43); the comparative suites cover the rest."""

from __future__ import annotations

import datetime as dt
import decimal

import pytest
import sqlglot

from cosmos_semantic.connectors import DataSourceConfig, connect
from cosmos_semantic.errors import ConnectorError, ModelError
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.reference import load_cases

from .conftest import CASES

SCHEMA_MAP = {"public": "cosmos"}


def _plan(graph, request: QueryRequest, principal: Principal | None = None) -> str:
    planner = Planner(graph, "trino", SCHEMA_MAP)
    request = request.model_copy(update={"as_of": dt.date(2026, 11, 20)})
    return planner.plan(request, principal or Principal()).sql


def _adapted(sql: str) -> str:
    """Hand-written PostgreSQL, as in the reference queries."""
    from cosmos_semantic import dialects

    return dialects.adapt(sqlglot.parse_one(sql, read="postgres"), "trino").sql("trino")


# -- SQL ---------------------------------------------------------------------


def test_the_source_type_uses_the_trino_dialect():
    assert DataSourceConfig(type="trino").dialect == "trino"


def test_weeks_start_on_monday(graph):
    """Trino's DATE_TRUNC('week', ...) is the ISO week, starting on Monday, as elsewhere."""
    sql = _plan(
        graph, QueryRequest(metrics=["order_count"], dimensions=["orders.ordered_at__week"])
    )
    assert "DATE_TRUNC('WEEK', orders.ordered_at)" in sql


def test_reference_sql_keeps_date_trunc_for_weeks():
    case = next(c for c in load_cases(CASES.with_name("queries_advanced.yml")) if c.id == "A06")
    assert "DATE_TRUNC('WEEK', ordered_at)" in case.sql_for("trino")


def test_tables_live_in_the_mapped_schema(graph):
    request = QueryRequest(metrics=["revenue"], dimensions=["customers.region"])
    sql = _plan(graph, request, Principal(user={"region": "Midwest"}))
    assert "cosmos.orders" in sql
    assert "public." not in sql


def test_every_reference_query_plans_and_parses_as_trino(graph):
    """Every case the engine answers prints SQL that Trino's parser in SQLGlot reads back, and
    the hand-written SQL is translated; a live server checks that both return the same numbers."""
    cases = load_cases(CASES) + load_cases(CASES.with_name("queries_advanced.yml"))
    planned = 0
    for case in cases:
        if case.request is None or case.expect_error:
            continue
        sql = _plan(graph, case.request, Principal(user=case.user, channel=case.channel))
        sqlglot.parse_one(sql, read="trino")
        reference = case.sql_for("trino")
        if reference is not None:
            sqlglot.parse_one(reference, read="trino")
            assert "DOUBLE PRECISION" not in reference
            assert "INTERVAL '" not in reference  # interval literals become DATE_ADD
        planned += 1
    assert planned >= 50


def _is_date(node) -> bool:
    return isinstance(node, sqlglot.exp.CurrentDate) or (
        isinstance(node, sqlglot.exp.Cast) and node.to.is_type(sqlglot.exp.DataType.Type.DATE)
    )


def test_no_reference_or_zoo_sql_subtracts_dates():
    """In Trino a date minus a date is an interval, not a number of days: day numbers are
    written with DATE_DIFF('day', ...) instead (A37)."""
    from cosmos_semantic.reconcile import load_zoo

    from .conftest import ROOT

    cases = load_cases(CASES) + load_cases(CASES.with_name("queries_advanced.yml"))
    sqls = {c.id: c.sql_for("trino") for c in cases}
    zoo = load_zoo(ROOT / "examples" / "ecommerce" / "divergence_zoo.yml")
    sqls.update({c.id: c.sql_for("trino") for c in zoo})
    checked = 0
    for case_id, sql in sqls.items():
        if sql is None:
            continue
        for sub in sqlglot.parse_one(sql, read="trino").find_all(sqlglot.exp.Sub):
            assert not (_is_date(sub.this) and _is_date(sub.expression)), case_id
        checked += 1
    assert checked >= 50
    assert "DATE_DIFF('day'" in sqls["A37"]


def test_the_check_catches_a_date_minus_a_date():
    sql = "SELECT CAST(d AS DATE) - DATE '2000-01-01' FROM t"
    subs = list(sqlglot.parse_one(sql, read="trino").find_all(sqlglot.exp.Sub))
    assert any(_is_date(s.this) and _is_date(s.expression) for s in subs)


def test_plural_interval_units_become_singular():
    sql = _adapted("SELECT d + INTERVAL '3 days', ordered_at - INTERVAL '2 hours' FROM t")
    assert "DATE_ADD('DAY', 3, d)" in sql
    assert "DATE_ADD('HOUR', -2, ordered_at)" in sql
    assert "DAYS" not in sql and "HOURS" not in sql


def test_plural_hours_on_a_date_read_it_as_a_timestamp():
    sql = _adapted("SELECT CAST('2026-11-20' AS DATE) + INTERVAL '12 hours'")
    assert sql == "SELECT DATE_ADD('HOUR', 12, CAST(CAST('2026-11-20' AS DATE) AS TIMESTAMP))"


def test_hour_grain_truncates_a_timestamp():
    """Trino refuses DATE_TRUNC('hour', date); the column is read as a TIMESTAMP first."""
    from sqlglot import exp

    from cosmos_semantic import dialects

    column = exp.column("created_at", table="customers")
    sql = dialects.trunc(column, "hour", "trino").sql("trino")
    assert sql == "DATE_TRUNC('HOUR', CAST(customers.created_at AS TIMESTAMP(6)))"
    assert dialects.trunc(column, "day", "trino").sql("trino") == (
        "DATE_TRUNC('DAY', customers.created_at)"
    )


def test_hour_grain_in_a_planned_query(graph):
    sql = _plan(
        graph, QueryRequest(metrics=["order_count"], dimensions=["orders.ordered_at__hour"])
    )
    assert "DATE_TRUNC('HOUR', CAST(orders.ordered_at AS TIMESTAMP(6)))" in sql


def test_date_minus_interval_becomes_date_add():
    sql = _adapted("SELECT 1 FROM t WHERE d >= CURRENT_DATE - INTERVAL '45' DAY")
    assert "DATE_ADD('DAY', -45, CURRENT_DATE)" in sql
    assert "INTERVAL" not in sql


def test_weeks_and_quarters_of_interval_arithmetic_use_date_add():
    """Trino's interval literals have no WEEK or QUARTER; DATE_ADD takes both."""
    sql = _adapted("SELECT d + INTERVAL '2' WEEK, d - INTERVAL '1' QUARTER FROM t")
    assert "DATE_ADD('WEEK', 2, d)" in sql
    assert "DATE_ADD('QUARTER', -1, d)" in sql


def test_hours_on_a_date_read_it_as_a_timestamp():
    sql = _adapted("SELECT CAST('2026-11-20' AS DATE) + INTERVAL '12' HOUR")
    assert sql == "SELECT DATE_ADD('HOUR', 12, CAST(CAST('2026-11-20' AS DATE) AS TIMESTAMP))"
    sql = _adapted("SELECT CURRENT_DATE - INTERVAL '30' MINUTE")
    assert "DATE_ADD('MINUTE', -30, CAST(CURRENT_DATE AS TIMESTAMP))" in sql


def test_days_on_a_date_keep_the_date():
    sql = _adapted("SELECT CAST('2026-11-20' AS DATE) + INTERVAL '1' MONTH")
    assert sql == "SELECT DATE_ADD('MONTH', 1, CAST('2026-11-20' AS DATE))"


def test_hours_on_a_timestamp_are_left_alone():
    sql = _adapted("SELECT ordered_at + INTERVAL '5' HOUR FROM t")
    assert sql == "SELECT DATE_ADD('HOUR', 5, ordered_at) FROM t"


def test_other_dialects_are_not_touched():
    from cosmos_semantic import dialects

    sql = "SELECT CAST('2026-11-20' AS DATE) + INTERVAL '12' HOUR"
    for dialect in ("duckdb", "postgres", "snowflake", "databricks"):
        tree = sqlglot.parse_one(sql, read="postgres")
        assert dialects.adapt(tree, dialect).sql(dialect) == tree.sql(dialect)


# -- Connector, with a fake client ---------------------------------------------


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows: list = []
        self.closed = False

    def execute(self, sql):
        self.connection.executed.append(sql)
        self.description, self._rows = self.connection.results.pop(0)

    def fetchmany(self, size):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def fetchall(self):
        rows, self._rows = self._rows, []
        self.connection.fetched += 1
        return rows

    def close(self):
        self.closed = True
        self.connection.cursors_closed += 1


class FakeConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.executed: list[str] = []
        self.results: list = []
        self.fetched = 0
        self.cursors_closed = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_trino(monkeypatch):
    pytest.importorskip("trino")
    import trino.dbapi

    made: list[FakeConnection] = []

    def fake_connect(**kwargs):
        made.append(FakeConnection(**kwargs))
        return made[-1]

    monkeypatch.setattr(trino.dbapi, "connect", fake_connect)
    return made


def _description(*names):
    return [(n, "varchar", None, None, None, None, None) for n in names]


def test_the_uri_gives_host_port_user_and_catalog(fake_trino):
    connect(DataSourceConfig(type="trino", uri="trino://cosmos@localhost:18080/memory"))
    kwargs = fake_trino[0].kwargs
    assert kwargs["host"] == "localhost"
    assert kwargs["port"] == 18080
    assert kwargs["user"] == "cosmos"
    assert kwargs["catalog"] == "memory"
    assert "auth" not in kwargs
    assert kwargs["verify"] is True


def test_queries_stop_at_the_source_timeout(fake_trino):
    config = DataSourceConfig(
        type="trino",
        uri="trino://cosmos@localhost:18080/memory",
        timeout_seconds=45,
        options={"session_properties": {"query_priority": 2}},
    )
    connect(config)
    assert fake_trino[0].kwargs["session_properties"] == {
        "query_max_run_time": "45s",
        "query_priority": 2,
    }


def test_session_properties_cannot_lift_the_timeout(fake_trino):
    config = DataSourceConfig(
        type="trino",
        uri="trino://cosmos@localhost:18080/memory",
        options={"session_properties": {"query_max_run_time": "10h"}},
    )
    with pytest.raises(ModelError, match="timeout_seconds"):
        connect(config)
    assert fake_trino == []


def test_disabled_tls_verification_is_logged_without_values(fake_trino, caplog):
    config = DataSourceConfig(
        type="trino",
        uri="trino://analyst@h:443/c?http_scheme=https",
        options={"password": "s3cret", "verify": "false"},
    )
    with caplog.at_level("WARNING", logger="cosmos_semantic.connectors"):
        connect(config)
    assert "verification is disabled" in caplog.text
    assert "s3cret" not in caplog.text and "analyst" not in caplog.text


def test_a_password_uses_basic_authentication_over_https(fake_trino):
    import trino.auth

    config = DataSourceConfig(
        type="trino",
        uri="trino://analyst@example.trino.galaxy.starburst.io:443/sample?http_scheme=https",
        options={"password": "s3cret", "verify": "false"},
    )
    connect(config)
    kwargs = fake_trino[0].kwargs
    assert kwargs["http_scheme"] == "https"
    assert kwargs["verify"] is False
    assert isinstance(kwargs["auth"], trino.auth.BasicAuthentication)


def test_an_access_token_uses_jwt_authentication(fake_trino):
    import trino.auth

    config = DataSourceConfig(
        type="trino",
        options={"host": "h", "user": "u", "catalog": "c", "access_token": "token"},
    )
    connect(config)
    assert isinstance(fake_trino[0].kwargs["auth"], trino.auth.JWTAuthentication)


def test_options_override_the_uri(fake_trino):
    connect(
        DataSourceConfig(
            type="trino", uri="trino://a@h:8080/memory", options={"catalog": "iceberg"}
        )
    )
    assert fake_trino[0].kwargs["catalog"] == "iceberg"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"options": {"host": "h", "user": "u"}}, "needs catalog"),
        ({"uri": "trino://h:8080/memory"}, "needs user"),
        ({"uri": "postgresql://u@h/db"}, "starts with trino://"),
        ({"uri": "trino://u@h/c?password=x"}, "unknown trino uri parameters: password"),
        (
            {"uri": "trino://u@h/c", "options": {"schema": "x"}},
            "unknown trino options: schema",
        ),
    ],
)
def test_incomplete_or_unknown_settings_are_rejected(fake_trino, config, message):
    with pytest.raises(ModelError, match=message):
        connect(DataSourceConfig(type="trino", **config))


def test_errors_do_not_reveal_the_password(fake_trino):
    with pytest.raises(ModelError) as raised:
        connect(DataSourceConfig(type="trino", uri="trino://u:s3cret@h:443"))
    assert "s3cret" not in str(raised.value)


def test_results_come_back_as_arrow(fake_trino):
    connector = connect(DataSourceConfig(type="trino", uri="trino://u@h:8080/memory"))
    con = fake_trino[0]
    con.results.append(
        (
            _description("status", "revenue", "orders__ordered_at__day"),
            [
                ["completed", decimal.Decimal("10.50"), dt.datetime(2026, 11, 20)],
                ["pending", None, dt.datetime(2026, 11, 21)],
            ],
        )
    )
    table = connector.execute("SELECT status, revenue, d FROM t")
    assert table.column_names == ["status", "revenue", "orders__ordered_at__day"]
    assert table.to_pylist()[0] == {
        "status": "completed",
        "revenue": decimal.Decimal("10.50"),
        "orders__ordered_at__day": dt.datetime(2026, 11, 20),
    }
    assert con.cursors_closed == 1


def test_an_empty_result_keeps_the_column_names(fake_trino):
    connector = connect(DataSourceConfig(type="trino", uri="trino://u@h:8080/memory"))
    fake_trino[0].results.append((_description("status", "n"), []))
    table = connector.execute("SELECT status, n FROM t")
    assert table.num_rows == 0
    assert table.column_names == ["status", "n"]


def test_rows_past_the_limit_fail_and_close_the_query(fake_trino):
    connector = connect(DataSourceConfig(type="trino", uri="trino://u@h:8080/memory", max_rows=2))
    con = fake_trino[0]
    con.results.append((_description("n"), [[1], [2], [3], [4]]))
    with pytest.raises(ConnectorError, match="2-row limit"):
        connector.execute("SELECT n FROM t")
    assert con.cursors_closed == 1


def test_writes_are_refused_before_reaching_the_server(fake_trino):
    connector = connect(DataSourceConfig(type="trino", uri="trino://u@h:8080/memory"))
    with pytest.raises(ConnectorError, match="single SELECT"):
        connector.execute("DROP TABLE cosmos.orders")
    assert fake_trino[0].executed == []


def test_a_script_runs_and_reads_each_statement(fake_trino):
    connector = connect(DataSourceConfig(type="trino", uri="trino://u@h:8080/memory"))
    con = fake_trino[0]
    con.results.extend([(_description("result"), [[True]]), (_description("rows"), [[1]])])
    connector.run_script("CREATE SCHEMA IF NOT EXISTS cosmos; CREATE TABLE cosmos.t (n BIGINT)")
    assert con.executed == [
        "CREATE SCHEMA IF NOT EXISTS cosmos",
        "CREATE TABLE cosmos.t (n BIGINT)",
    ]
    assert con.fetched == 2
    connector.close()
    assert con.closed
