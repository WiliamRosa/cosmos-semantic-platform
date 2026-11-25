from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from cosmos_semantic import demo_data
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import DataSourceConfig, connect
from cosmos_semantic.loader import load_project

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ecommerce" / "semantic"
CASES = ROOT / "reference" / "queries.yml"


def pytest_configure(config):
    """CLI help as plain, unwrapped text everywhere. Typer forces a Rich terminal when it sees
    GITHUB_ACTIONS (colour codes, 80-column wrapping), so a test reading `--help` would pass
    locally and fail on CI. Runs before test modules import Typer."""
    os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
    os.environ.setdefault("TERMINAL_WIDTH", "200")


@pytest.fixture(scope="session")
def tables():
    return demo_data.generate(600, seed=7, anchor=dt.date.today())


@pytest.fixture(scope="session")
def graph():
    return compile_project(load_project(EXAMPLE))


@pytest.fixture(scope="session")
def duckdb_connector(tables):
    connector = connect(DataSourceConfig(type="duckdb"))
    demo_data.load(connector, tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def postgres_connector(tables):
    uri = os.environ.get("COSMOS_PG_URI")
    if not uri:
        pytest.skip("set COSMOS_PG_URI to run against Postgres")
    connector = connect(DataSourceConfig(type="postgres", uri=uri))
    demo_data.load(connector, tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def sqlserver_connector(tables):
    uri = os.environ.get("COSMOS_MSSQL_URI")
    if not uri:
        pytest.skip("set COSMOS_MSSQL_URI to run against SQL Server")
    connector = connect(
        DataSourceConfig(type="sqlserver", uri=uri, schema_map={"public": "cosmos"})
    )
    demo_data.load(connector, tables)
    yield connector
    connector.close()


def _uri_connector(kind: str, variable: str, tables, **config):
    uri = os.environ.get(variable)
    if not uri:
        pytest.skip(f"set {variable} to run against {kind}")
    connector = connect(DataSourceConfig(type=kind, uri=uri, **config))
    demo_data.load(connector, tables)
    return connector


@pytest.fixture(scope="session")
def mysql_connector(tables):
    connector = _uri_connector("mysql", "COSMOS_MYSQL_URI", tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def mariadb_connector(tables):
    connector = _uri_connector("mariadb", "COSMOS_MARIADB_URI", tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def oracle_connector(tables):
    env = os.environ.get
    if not env("COSMOS_ORACLE_DSN"):
        pytest.skip("set COSMOS_ORACLE_DSN (with COSMOS_ORACLE_USER and COSMOS_ORACLE_PASSWORD)")
    user = env("COSMOS_ORACLE_USER", "cosmos")
    connector = connect(
        DataSourceConfig(
            type="oracle",
            options={
                "dsn": env("COSMOS_ORACLE_DSN"),
                "user": user,
                "password": env("COSMOS_ORACLE_PASSWORD"),
            },
            # `public` is a reserved word in Oracle; the demo tables live in the user's schema.
            schema_map={"public": user},
        )
    )
    demo_data.load(connector, tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def databricks_connector(tables):
    env = os.environ.get
    if not env("DATABRICKS_HTTP_PATH") or not (
        env("DATABRICKS_TOKEN") or env("DATABRICKS_CONFIG_PROFILE")
    ):
        pytest.skip(
            "set DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN (with DATABRICKS_SERVER_HOSTNAME) "
            "or DATABRICKS_CONFIG_PROFILE"
        )
    connector = connect(
        DataSourceConfig(
            type="databricks",
            options={
                "server_hostname": env("DATABRICKS_SERVER_HOSTNAME"),
                "http_path": env("DATABRICKS_HTTP_PATH"),
                "access_token": env("DATABRICKS_TOKEN"),
                "profile": env("DATABRICKS_CONFIG_PROFILE"),
                "catalog": env("DATABRICKS_CATALOG", "cosmos_demo"),
            },
        )
    )
    demo_data.load(connector, tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def snowflake_connector(tables):
    env = os.environ.get
    if not env("SNOWFLAKE_CONNECTION_NAME") and not (
        env("SNOWFLAKE_ACCOUNT") and env("SNOWFLAKE_USER")
    ):
        pytest.skip(
            "set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER (with SNOWFLAKE_PRIVATE_KEY_FILE), "
            "or SNOWFLAKE_CONNECTION_NAME"
        )
    connector = connect(
        DataSourceConfig(
            type="snowflake",
            options={
                "connection_name": env("SNOWFLAKE_CONNECTION_NAME"),
                "account": env("SNOWFLAKE_ACCOUNT"),
                "user": env("SNOWFLAKE_USER"),
                "private_key_file": env("SNOWFLAKE_PRIVATE_KEY_FILE"),
                "role": env("SNOWFLAKE_ROLE"),
                "warehouse": env("SNOWFLAKE_WAREHOUSE"),
                "database": env("SNOWFLAKE_DATABASE", "COSMOS_DEMO"),
            },
        )
    )
    demo_data.load(connector, tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def bigquery_connector(tables):
    """BigQuery (DEC-40): a real project, or the emulator when COSMOS_BIGQUERY_ENDPOINT is set
    (``ghcr.io/goccy/bigquery-emulator``, as in CI)."""
    env = os.environ.get
    if not env("COSMOS_BIGQUERY_PROJECT"):
        pytest.skip(
            "set COSMOS_BIGQUERY_PROJECT (with COSMOS_BIGQUERY_ENDPOINT for the emulator, or "
            "Application Default Credentials / COSMOS_BIGQUERY_CREDENTIALS_FILE)"
        )
    connector = connect(
        DataSourceConfig(
            type="bigquery",
            options={
                "project": env("COSMOS_BIGQUERY_PROJECT"),
                "location": env("COSMOS_BIGQUERY_LOCATION"),
                "credentials_file": env("COSMOS_BIGQUERY_CREDENTIALS_FILE"),
                "api_endpoint": env("COSMOS_BIGQUERY_ENDPOINT"),
            },
            schema_map={"public": env("COSMOS_BIGQUERY_DATASET", "cosmos_demo")},
            timeout_seconds=20 if env("COSMOS_BIGQUERY_ENDPOINT") else 120,
        )
    )
    demo_data.load(connector, tables)
    yield connector
    connector.close()


@pytest.fixture(scope="session")
def trino_connector(tables):
    """Trino or Starburst (DEC-43): COSMOS_TRINO_URI names the server and a catalog that takes
    CREATE TABLE and INSERT, such as the memory catalog of the container in
    deploy/docker-compose.yml (``trino://cosmos@localhost:18080/memory``). A password comes
    from COSMOS_TRINO_PASSWORD, never from the URI in a shared shell history."""
    connector = _uri_connector(
        "trino",
        "COSMOS_TRINO_URI",
        tables,
        options={"password": os.environ.get("COSMOS_TRINO_PASSWORD")},
        schema_map={"public": os.environ.get("COSMOS_TRINO_SCHEMA", "cosmos")},
        timeout_seconds=120,
    )
    yield connector
    connector.close()


# -- TPC-H (DEC-4): examples/tpch on the data of DuckDB's `tpch` extension ----

TPCH = ROOT / "examples" / "tpch"


def tpch_connector(scale_factor: float):
    """An in-memory DuckDB with the TPC-H tables at ``scale_factor``. DuckDB downloads its
    ``tpch`` extension once; without network access the calling test is skipped, or fails
    when ``COSMOS_REQUIRE_TPCH=1`` (for a run that must not lose the TPC-H cases silently)."""
    import duckdb

    connector = connect(DataSourceConfig(type="duckdb"))
    try:
        try:
            connector.run_script("LOAD tpch")
        except duckdb.Error:
            connector.run_script("INSTALL tpch; LOAD tpch")
    except duckdb.Error as exc:
        connector.close()
        reason = (
            f"DuckDB's tpch extension is not available (no network?): {str(exc).splitlines()[0]}"
        )
        if os.environ.get("COSMOS_REQUIRE_TPCH") == "1":
            pytest.fail(f"{reason}; COSMOS_REQUIRE_TPCH=1 requires it")
        pytest.skip(reason)
    try:
        connector.run_script(f"CALL dbgen(sf={float(scale_factor)})")
    except BaseException:
        connector.close()
        raise
    return connector


@pytest.fixture(scope="session")
def tpch_graph():
    return compile_project(load_project(TPCH / "semantic"))


# -- TPC-H on the container databases (DEC-47) -------------------------------------------------
#
# The TPC-H data comes from DuckDB (`tpch_connector`) and is copied into each configured
# container database, where the same cases run. The tables live apart from the e-commerce demo
# (whose `orders` would collide): in the schema `tpch` on Postgres (first on the search path)
# and Oracle (the session's current schema; the user is created by deploy/oracle-setup.sql), and
# in a database `tpch` on SQL Server, MySQL and MariaDB. The model's tables are unqualified, so
# they resolve there as they would for a user whose connection points at the TPC-H data.

TPCH_SCHEMA = "tpch"
TPCH_SOURCES = ("postgres", "sqlserver", "mysql", "mariadb", "oracle")
_TPCH_URI_VARIABLES = {
    "postgres": "COSMOS_PG_URI",
    "sqlserver": "COSMOS_MSSQL_URI",
    "mysql": "COSMOS_MYSQL_URI",
    "mariadb": "COSMOS_MARIADB_URI",
}


def tpch_tables(duckdb_connector) -> dict:
    """The tables of the TPC-H model as Arrow tables, read from a ``tpch_connector``. The
    free-text ``*_comment`` columns are left out: nothing reads them, and they are a third of
    the bytes to insert."""
    con = duckdb_connector.connection
    out = {}
    for model in load_project(TPCH / "semantic").semantic_models:
        columns = con.sql(f"SELECT * FROM {model.table}").columns
        keep = ", ".join(c for c in columns if not c.endswith("_comment"))
        out[model.table] = con.sql(f"SELECT {keep} FROM {model.table}").to_arrow_table()
    return out


def tpch_source(kind: str):
    """A connector to ``kind`` whose unqualified tables resolve in the TPC-H schema or
    database, created if needed. Skips when the database's variable is not set."""
    env = os.environ.get
    if kind == "oracle":
        if not env("COSMOS_ORACLE_DSN"):
            pytest.skip(
                "set COSMOS_ORACLE_DSN (with COSMOS_ORACLE_USER and COSMOS_ORACLE_PASSWORD)"
            )
        connector = connect(
            DataSourceConfig(
                type="oracle",
                options={
                    "dsn": env("COSMOS_ORACLE_DSN"),
                    "user": env("COSMOS_ORACLE_USER", "cosmos"),
                    "password": env("COSMOS_ORACLE_PASSWORD"),
                },
            )
        )
        try:
            connector.run_script(f"ALTER SESSION SET CURRENT_SCHEMA = {TPCH_SCHEMA}")
        except Exception as exc:
            connector.close()
            pytest.fail(
                f"Oracle has no {TPCH_SCHEMA.upper()} schema ({exc}); run deploy/oracle-setup.sql "
                "again as SYSTEM"
            )
        return connector
    variable = _TPCH_URI_VARIABLES[kind]
    uri = env(variable)
    if not uri:
        pytest.skip(f"set {variable} to run against {kind}")
    if kind == "postgres":
        connector = connect(DataSourceConfig(type=kind, uri=uri))
        try:
            connector.run_script(f"CREATE SCHEMA IF NOT EXISTS {TPCH_SCHEMA}")
            # A session setting, committed: queries roll back their own transactions only.
            connector.run_script(f"SET search_path TO {TPCH_SCHEMA}")
        except BaseException:
            connector.close()
            raise
        return connector
    return connect(DataSourceConfig(type=kind, uri=_tpch_database(uri, kind)))


def _tpch_database(uri: str, kind: str) -> str:
    """Creates the database ``tpch`` on the server of ``uri`` and returns a URI that points at
    it. CREATE DATABASE cannot run inside a transaction on SQL Server, hence AUTOCOMMIT."""
    import sqlalchemy as sa

    url = sa.engine.make_url(uri)
    if kind == "sqlserver":
        create = f"IF DB_ID('{TPCH_SCHEMA}') IS NULL CREATE DATABASE {TPCH_SCHEMA}"
    else:
        create = f"CREATE DATABASE IF NOT EXISTS {TPCH_SCHEMA}"
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as con:
            con.execute(sa.text(create))
    finally:
        engine.dispose()
    return url.set(database=TPCH_SCHEMA).render_as_string(hide_password=False)


# The TPC-H primary keys. Declared so that joins find an index: MariaDB has no hash join by
# default, and lineitem JOIN orders without one could exceed the statement timeout.
_TPCH_KEYS = {
    "lineitem": ("l_orderkey", "l_linenumber"),
    "orders": ("o_orderkey",),
    "customer": ("c_custkey",),
    "nation": ("n_nationkey",),
    "region": ("r_regionkey",),
    "part": ("p_partkey",),
}


def load_tpch(connector, tables: dict, chunk: int = 500) -> None:
    """Replaces each table in the connection's default schema with a copy of ``tables``, with
    exact types (DECIMAL(15,2) stays a decimal, never a float) and the TPC-H primary key when
    its columns are loaded. Rows go in multi-row INSERTs of ``chunk`` rows (SQL Server takes
    at most 1,000 per statement), with literals written here rather than through SQLGlot,
    which would take longer than the inserts. The statements go straight to the driver's
    cursor without parameters, so nothing in a string (``:name``, ``%``) is read as a bind
    parameter; strings are quoted by doubling ``'``, and on MySQL and MariaDB by doubling the
    backslash as well. On Oracle, where a long statement is slow to parse, rows go through
    bind variables in batches instead. Checks the row count of each table after the load."""
    dialect = connector.dialect
    for name, table in tables.items():
        connector.run_script(f"DROP TABLE IF EXISTS {name}")
        columns = [
            f"{field.name} {_tpch_type(table.column(field.name), dialect)}"
            for field in table.schema
        ]
        key = _TPCH_KEYS.get(name, ())
        if key and all(c in table.column_names for c in key):
            columns.append(f"PRIMARY KEY ({', '.join(key)})")
        connector.run_script(f"CREATE TABLE {name} ({', '.join(columns)})")
        names = ", ".join(table.column_names)
        if dialect == "oracle":
            binds = ", ".join(f":{i}" for i in range(1, table.num_columns + 1))
            con = connector.connection
            with con.cursor() as cur:
                for batch in table.to_batches(max_chunksize=10_000):
                    rows = list(zip(*(c.to_pylist() for c in batch.columns), strict=True))
                    cur.executemany(f"INSERT INTO {name} ({names}) VALUES ({binds})", rows)
            con.commit()
        else:
            for batch in table.to_batches(max_chunksize=chunk):
                rows = zip(*(column.to_pylist() for column in batch.columns), strict=True)
                values = ", ".join(
                    "(" + ", ".join(_tpch_literal(v, dialect) for v in row) + ")" for row in rows
                )
                _run_unparameterized(connector, f"INSERT INTO {name} ({names}) VALUES {values}")
        loaded = connector.execute(f"SELECT COUNT(*) AS n FROM {name}").to_pylist()[0]["n"]
        assert loaded == table.num_rows, f"{name}: {loaded} rows loaded of {table.num_rows}"


def _run_unparameterized(connector, sql: str) -> None:
    """Runs ``sql`` on the driver's own cursor with no parameters. SQLAlchemy's ``text()``
    would read ``:name`` as a bind parameter, and pymysql and pymssql format ``%`` whenever
    parameters are passed, even empty ones. DuckDB and ADBC Postgres take no parameters here."""
    from cosmos_semantic.connectors import SQLAlchemyConnector

    if not isinstance(connector, SQLAlchemyConnector):
        connector.run_script(sql)
        return
    con = connector._engine.raw_connection()
    try:
        cur = con.cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()
        con.commit()
    finally:
        con.close()


def _tpch_type(column, dialect: str) -> str:
    import pyarrow as pa
    import pyarrow.compute as pc
    from sqlglot import exp

    kind = column.type
    if pa.types.is_integer(kind):
        sql = "INT" if kind.bit_width <= 32 else "BIGINT"
    elif pa.types.is_decimal(kind):
        sql = f"DECIMAL({kind.precision}, {kind.scale})"
    elif pa.types.is_string(kind):
        longest = pc.max(pc.utf8_length(column)).as_py() or 1
        sql = f"VARCHAR({longest})"
    elif pa.types.is_date32(kind):
        sql = "DATE"
    else:
        raise TypeError(f"no SQL type for the TPC-H column type {kind}")
    return exp.DataType.build(sql).sql(dialect=dialect)


def _tpch_literal(value, dialect: str) -> str:
    import decimal

    if value is None:
        return "NULL"
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dt.date):
        # T-SQL has no DATE literal; it converts an ISO string to a DATE column exactly.
        return f"'{value.isoformat()}'" if dialect == "tsql" else f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        if dialect == "mysql":  # a backslash escapes in MySQL and MariaDB string literals
            value = value.replace("\\", "\\\\")
        return "'" + value.replace("'", "''") + "'"
    raise TypeError(f"no SQL literal for the TPC-H value {value!r}")
