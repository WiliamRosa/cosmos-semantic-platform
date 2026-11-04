"""Connectors: run read-only SQL and always return an Arrow table.

ADBC is the default path; SQLAlchemy is the fallback for sources without a mature ADBC driver
(ADR 0007). DuckDB serves development and tests without a server.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import sqlglot
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from cosmos_semantic.errors import ConnectorError, ModelError

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

DIALECTS = {
    "postgres": "postgres",
    "duckdb": "duckdb",
    "databricks": "databricks",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
    "mysql": "mysql",
    "mariadb": "mysql",  # SQLGlot has no MariaDB dialect; the MySQL one covers what we generate
    "oracle": "oracle",
}


class CostRates(BaseModel):
    """Estimated cost of a query on a data source, for `cosmos usage`."""

    model_config = ConfigDict(extra="forbid")

    per_query: float = Field(0.0, ge=0)
    per_second: float = Field(0.0, ge=0, description="Per second of query duration.")
    per_1k_rows: float = Field(0.0, ge=0, description="Per 1,000 rows returned.")
    currency: str = "USD"


class DataSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "postgres", "duckdb", "databricks", "snowflake", "sqlserver", "mysql", "mariadb", "oracle"
    ]
    driver: Literal["adbc", "sqlalchemy", "native"] | None = Field(
        None,
        description=(
            "Default: adbc for postgres, native for duckdb, databricks, snowflake and oracle, "
            "sqlalchemy for sqlserver, mysql and mariadb."
        ),
    )
    uri: str | None = None
    path: str | None = Field(None, description="DuckDB database file.")
    options: dict[str, Any] = {}
    schema_map: dict[str, str] = Field(
        {},
        description="Per-source schema renames, such as `public: dbo`, without changing the model.",
    )
    max_rows: int = Field(10_000, ge=1)
    timeout_seconds: int = Field(30, ge=1)
    cost: CostRates | None = Field(None, description="Rates for the usage and cost report.")

    @property
    def dialect(self) -> str:
        return DIALECTS[self.type]


class ConnectionsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_sources: dict[str, DataSourceConfig]
    default: str | None = None


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ModelError(f"environment variable `{name}` is not set")

        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_connections(path: str | Path) -> ConnectionsFile:
    with Path(path).open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return ConnectionsFile.model_validate(_expand_env(raw))


def ensure_read_only(sql: str, dialect: str) -> None:
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise ConnectorError(f"invalid SQL: {exc}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ConnectorError("only a single SELECT query is allowed")
    for node in statements[0].walk():
        if isinstance(
            node, exp.Insert | exp.Update | exp.Delete | exp.Merge | exp.Create | exp.Drop
        ):
            raise ConnectorError("only read access is allowed")


class Connector(ABC):
    def __init__(self, config: DataSourceConfig) -> None:
        self.config = config

    @property
    def dialect(self) -> str:
        return self.config.dialect

    def execute(self, sql: str, *, max_rows: int | None = None) -> pa.Table:
        ensure_read_only(sql, self.dialect)
        limit = max_rows or self.config.max_rows
        try:
            table = self._run(sql, limit + 1)
        except ConnectorError:
            raise
        except Exception as exc:  # driver errors vary per source
            raise ConnectorError(f"{type(exc).__name__}: {exc}") from exc
        if table.num_rows > limit:
            raise ConnectorError(
                f"the result exceeded the {limit}-row limit; use filters, fewer dimensions or `limit`"
            )
        return table

    def run_script(self, sql: str) -> None:
        """Runs setup DDL/DML (demo data). Never used for queries."""
        raise ConnectorError(f"{type(self).__name__} does not run scripts")

    @abstractmethod
    def _run(self, sql: str, fetch_limit: int) -> pa.Table: ...

    def close(self) -> None:  # noqa: B027
        pass


class DuckDBConnector(Connector):
    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        import duckdb

        self._con = duckdb.connect(config.path or ":memory:")

    @property
    def connection(self):
        return self._con

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        relation = self._con.sql(sql)
        batch = min(fetch_limit, 100_000)
        if hasattr(relation, "to_arrow_reader"):
            reader = relation.to_arrow_reader(batch)
        else:
            reader = relation.fetch_arrow_reader(batch)
        return _read_limited(reader, fetch_limit)

    def run_script(self, sql: str) -> None:
        self._con.execute(sql)

    def close(self) -> None:
        self._con.close()


class ADBCPostgresConnector(Connector):
    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        import adbc_driver_manager.dbapi as dbapi
        import adbc_driver_postgresql

        if not config.uri:
            raise ModelError("a postgres source needs `uri`")
        driver = _loadable_path(adbc_driver_postgresql._driver_path())
        self._con = dbapi.connect(driver=driver, db_kwargs={"uri": config.uri})

    @property
    def connection(self):
        return self._con

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        try:
            with self._con.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    f"SET LOCAL statement_timeout = {int(self.config.timeout_seconds) * 1000}"
                )
                cur.execute(sql)
                return _postgres_numeric(_read_limited(cur.fetch_record_batch(), fetch_limit))
        finally:
            self._con.rollback()

    def run_script(self, sql: str) -> None:
        with self._con.cursor() as cur:
            cur.execute(sql)
        self._con.commit()

    def close(self) -> None:
        self._con.close()


class DatabricksConnector(Connector):
    """Databricks SQL warehouse; the official connector fetches results as Arrow (CloudFetch)."""

    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        from databricks import sql as dbsql

        opts = {k: v for k, v in config.options.items() if v not in (None, "")}
        auth: dict[str, Any] = {}
        if opts.get("access_token"):
            auth["access_token"] = opts["access_token"]
        elif opts.get("profile"):
            # Databricks CLI profile (OAuth): `databricks auth login --profile <name>`.
            from databricks.sdk.core import Config

            sdk_config = Config(profile=opts["profile"])
            opts.setdefault("server_hostname", sdk_config.hostname)
            auth["credentials_provider"] = lambda: sdk_config.authenticate
        else:
            raise ModelError("a databricks source needs options.access_token or options.profile")
        missing = [k for k in ("server_hostname", "http_path") if not opts.get(k)]
        if missing:
            raise ModelError(f"a databricks source needs options: {', '.join(missing)}")
        self._con = dbsql.connect(
            server_hostname=opts["server_hostname"],
            http_path=opts["http_path"],
            catalog=opts.get("catalog"),
            schema=opts.get("schema"),
            **auth,
        )

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        with self._con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchmany_arrow(fetch_limit)

    def run_script(self, sql: str) -> None:
        with self._con.cursor() as cur:
            for statement in sqlglot.transpile(sql, read="databricks", write="databricks"):
                cur.execute(statement)

    def close(self) -> None:
        self._con.close()


class SnowflakeConnector(Connector):
    """Snowflake warehouse; the official connector fetches results as Arrow.

    Authentication is whatever ``snowflake.connector.connect`` accepts in ``options``: a key pair
    (``private_key_file``), a named connection from ``~/.snowflake/connections.toml``
    (``connection_name``), an ``authenticator`` such as ``externalbrowser``, or a password.
    """

    # Session settings that make results match the other databases: weeks start on Monday and
    # week numbers are ISO; a query never runs longer than the source's timeout.
    _SESSION = {"WEEK_START": 1, "WEEK_OF_YEAR_POLICY": 1}

    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        import snowflake.connector

        opts = {k: v for k, v in config.options.items() if v not in (None, "")}
        if not opts.get("connection_name"):
            missing = [k for k in ("account", "user") if not opts.get(k)]
            if missing:
                raise ModelError(
                    f"a snowflake source needs options: {', '.join(missing)} "
                    "(or options.connection_name)"
                )
        if opts.get("private_key_file"):
            opts["private_key_file"] = os.path.expanduser(opts["private_key_file"])
        session = {
            **self._SESSION,
            "STATEMENT_TIMEOUT_IN_SECONDS": int(config.timeout_seconds),
            **opts.pop("session_parameters", {}),
        }
        self._con = snowflake.connector.connect(session_parameters=session, **opts)

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        with self._con.cursor() as cur:
            cur.execute(sql)
            batches, count = [], 0
            for batch in cur.fetch_arrow_batches():
                batches.append(batch)
                count += batch.num_rows
                if count >= fetch_limit:
                    break
            if batches:
                table = pa.concat_tables(batches).slice(0, fetch_limit)
            else:
                table = _empty_table([d.name for d in cur.description or []])
        return _fold_upper_names(table)

    def run_script(self, sql: str) -> None:
        with self._con.cursor() as cur:
            for statement in sqlglot.transpile(sql, read="snowflake", write="snowflake"):
                cur.execute(statement)

    def close(self) -> None:
        self._con.close()


class OracleConnector(Connector):
    """Oracle Database through python-oracledb in thin mode: pure Python, no Instant Client.

    ``options`` go to ``oracledb.connect``: ``user``, ``password`` and ``dsn``
    (``host:port/service`` or a TNS alias). Wallet options (``config_dir``, ``wallet_location``,
    ``wallet_password``) pass through as well, but only Oracle Database Free is tested.
    """

    # Session settings that make literals and sorting behave like the other databases.
    _SESSION = (
        "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
        "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF' "
        "NLS_NUMERIC_CHARACTERS = '.,' NLS_SORT = BINARY NLS_COMP = BINARY"
    )

    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        import oracledb

        opts = {k: v for k, v in config.options.items() if v not in (None, "")}
        missing = [k for k in ("user", "dsn") if not opts.get(k)]
        if missing:
            raise ModelError(f"an oracle source needs options: {', '.join(missing)}")
        for key in ("config_dir", "wallet_location"):
            if opts.get(key):
                opts[key] = os.path.expanduser(opts[key])
        self._con = oracledb.connect(**opts)
        self._con.call_timeout = int(config.timeout_seconds) * 1000
        with self._con.cursor() as cur:
            cur.execute(self._SESSION)

    @property
    def connection(self):
        return self._con

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        with self._con.cursor() as cur:
            cur.arraysize = min(fetch_limit, 10_000)
            cur.execute(sql)
            names = _unique_names([d[0] for d in cur.description or []])
            rows = cur.fetchmany(fetch_limit)
        if not rows:
            return _fold_upper_names(_empty_table(names))
        return _fold_upper_names(
            pa.table({n: [_oracle_value(r[i]) for r in rows] for i, n in enumerate(names)})
        )

    def run_script(self, sql: str) -> None:
        with self._con.cursor() as cur:
            cur.execute(sql)
        self._con.commit()

    def close(self) -> None:
        self._con.close()


def _oracle_value(value: Any) -> Any:
    """LOBs arrive as handles; read them so Arrow sees plain values."""
    if hasattr(value, "read"):
        return value.read()
    return value


def _fold_upper_names(table: pa.Table) -> pa.Table:
    """Snowflake and Oracle fold unquoted identifiers to upper case, so the aliases the planner
    writes (``orders__status``) come back as ``ORDERS__STATUS``. Every alias the engine generates
    is lower case, so an all-upper-case name is folded back; mixed-case names stay as they are."""
    names = [n.lower() if n.isupper() else n for n in table.column_names]
    return table.rename_columns(names)


def _empty_table(names: list[str]) -> pa.Table:
    """An empty result has no data to take the types from; build a table with null-typed
    columns that keep the names."""
    return pa.table({n: pa.array([], type=pa.null()) for n in _unique_names(names)})


class SQLAlchemyConnector(Connector):
    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        import sqlalchemy as sa

        if not config.uri:
            raise ModelError(f"a {config.type} source using sqlalchemy needs `uri`")
        self._engine = sa.create_engine(config.uri)
        setup = _session_setup(config)
        if setup:

            @sa.event.listens_for(self._engine, "connect")
            def _on_connect(dbapi_con, _record) -> None:
                cur = dbapi_con.cursor()
                try:
                    for statement in setup:
                        cur.execute(statement)
                finally:
                    cur.close()

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        import sqlalchemy as sa

        with self._engine.connect() as con:
            result = con.execute(sa.text(sql))
            names = _unique_names(list(result.keys()))
            rows = result.fetchmany(fetch_limit)
        return pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)})

    def run_script(self, sql: str) -> None:
        import sqlalchemy as sa

        with self._engine.begin() as con:
            con.execute(sa.text(sql))

    def close(self) -> None:
        self._engine.dispose()


def _session_setup(config: DataSourceConfig) -> list[str]:
    """Per-connection settings for SQLAlchemy sources. MySQL and MariaDB: ``||`` concatenates
    (as in standard SQL) and queries stop at the source's timeout."""
    if config.type == "mysql":
        return [
            "SET SESSION sql_mode = CONCAT(@@sql_mode, ',PIPES_AS_CONCAT')",
            f"SET SESSION max_execution_time = {int(config.timeout_seconds) * 1000}",
        ]
    if config.type == "mariadb":
        return [
            "SET SESSION sql_mode = CONCAT(@@sql_mode, ',PIPES_AS_CONCAT')",
            f"SET SESSION max_statement_time = {int(config.timeout_seconds)}",
        ]
    return []


def _unique_names(names: list[str]) -> list[str]:
    """Unnamed or repeated result columns (SQL Server returns "" for expressions without an
    alias) would collapse into one Arrow column; give each a distinct name instead."""
    seen: dict[str, int] = {}
    out = []
    for i, name in enumerate(names, start=1):
        base = name or f"column_{i}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.append(base if count == 0 else f"{base}_{count + 1}")
    return out


def _postgres_numeric(table: pa.Table) -> pa.Table:
    """The ADBC driver returns Postgres NUMERIC as text (an opaque type). Converts it to an exact
    decimal using the values' own scale; falls back to float64 if 38 digits are not enough."""
    import decimal

    for i, field in enumerate(table.schema):
        if (field.metadata or {}).get(b"ADBC:postgresql:typname") != b"numeric":
            continue
        column = table.column(i)
        if isinstance(column.type, pa.ExtensionType):
            column = pa.chunked_array(
                [chunk.storage for chunk in column.chunks], type=column.type.storage_type
            )
        values = [
            None if t is None or t == "NaN" else decimal.Decimal(t) for t in column.to_pylist()
        ]
        known = [v.as_tuple() for v in values if v is not None]
        scale = max((max(-t.exponent, 0) for t in known), default=0)
        whole = max((max(len(t.digits) + t.exponent, 1) for t in known), default=1)
        if scale <= 18 and whole + scale <= 38:
            array = pa.array(values, type=pa.decimal128(38, scale))
        else:
            array = pa.array([None if v is None else float(v) for v in values], type=pa.float64())
        table = table.set_column(i, pa.field(field.name, array.type), array)
    return table


def _loadable_path(path: str) -> str:
    """The ADBC driver manager cannot load DLLs from non-ASCII paths on Windows; the short
    (8.3) name of the same file is ASCII-only."""
    if os.name != "nt" or path.isascii():
        return path
    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    if (
        ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
        and buffer.value.isascii()
    ):
        return buffer.value
    raise ConnectorError(
        f"the ADBC driver is in a non-ASCII path ({path}) and Windows has no short name for it; "
        "install the Python environment in an ASCII-only path"
    )


def _read_limited(reader: pa.RecordBatchReader, limit: int) -> pa.Table:
    batches, count = [], 0
    for batch in reader:
        batches.append(batch)
        count += batch.num_rows
        if count >= limit:
            break
    if not batches:
        return reader.schema.empty_table()
    return pa.Table.from_batches(batches).slice(0, limit)


def connect(config: DataSourceConfig) -> Connector:
    driver = (
        config.driver
        or {
            "postgres": "adbc",
            "duckdb": "native",
            "databricks": "native",
            "snowflake": "native",
            "oracle": "native",
            "sqlserver": "sqlalchemy",
            "mysql": "sqlalchemy",
            "mariadb": "sqlalchemy",
        }[config.type]
    )
    if config.type == "duckdb":
        return DuckDBConnector(config)
    if config.type == "postgres" and driver == "adbc":
        return ADBCPostgresConnector(config)
    if config.type == "databricks":
        return DatabricksConnector(config)
    if config.type == "snowflake":
        return SnowflakeConnector(config)
    if config.type == "oracle":
        return OracleConnector(config)
    if driver == "sqlalchemy":
        return SQLAlchemyConnector(config)
    raise ModelError(f"unsupported combination: {config.type} with {driver}")
