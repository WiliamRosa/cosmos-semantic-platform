"""Conectores: executam SQL somente leitura e devolvem sempre uma tabela Arrow.

ADBC é o caminho padrão; SQLAlchemy é a reserva para origens sem driver ADBC maduro
(ADR 0007). O DuckDB serve para desenvolvimento e testes sem servidor.
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
    "sqlserver": "tsql",
}


class DataSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["postgres", "duckdb", "databricks", "sqlserver"]
    driver: Literal["adbc", "sqlalchemy", "native"] | None = Field(
        None,
        description="Padrão: adbc para postgres, native para duckdb e databricks, sqlalchemy para sqlserver.",
    )
    uri: str | None = None
    path: str | None = Field(None, description="Arquivo do DuckDB.")
    options: dict[str, Any] = {}
    schema_map: dict[str, str] = Field(
        {}, description="Troca de esquema por origem, como `public: dbo`, sem mudar o modelo."
    )
    max_rows: int = Field(10_000, ge=1)
    timeout_seconds: int = Field(30, ge=1)

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
            raise ModelError(f"variável de ambiente `{name}` não definida")

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
        raise ConnectorError(f"SQL inválido: {exc}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ConnectorError("só uma consulta SELECT é permitida")
    for node in statements[0].walk():
        if isinstance(
            node, exp.Insert | exp.Update | exp.Delete | exp.Merge | exp.Create | exp.Drop
        ):
            raise ConnectorError("só leitura é permitida")


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
        except Exception as exc:  # erros de driver variam por origem
            raise ConnectorError(f"{type(exc).__name__}: {exc}") from exc
        if table.num_rows > limit:
            raise ConnectorError(
                f"o resultado passou do limite de {limit} linhas; use filtros, menos dimensões ou `limit`"
            )
        return table

    def run_script(self, sql: str) -> None:
        """Executa DDL/DML de preparação (dados de demonstração). Nunca usado em consultas."""
        raise ConnectorError(f"{type(self).__name__} não executa scripts")

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
            raise ModelError("origem postgres precisa de `uri`")
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
    """Databricks SQL warehouse; o conector oficial busca resultados em Arrow (CloudFetch)."""

    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        from databricks import sql as dbsql

        opts = {k: v for k, v in config.options.items() if v not in (None, "")}
        auth: dict[str, Any] = {}
        if opts.get("access_token"):
            auth["access_token"] = opts["access_token"]
        elif opts.get("profile"):
            # Perfil do Databricks CLI (OAuth): `databricks auth login --profile <nome>`.
            from databricks.sdk.core import Config

            sdk_config = Config(profile=opts["profile"])
            opts.setdefault("server_hostname", sdk_config.hostname)
            auth["credentials_provider"] = lambda: sdk_config.authenticate
        else:
            raise ModelError("origem databricks precisa de options.access_token ou options.profile")
        missing = [k for k in ("server_hostname", "http_path") if not opts.get(k)]
        if missing:
            raise ModelError(f"origem databricks precisa de options: {', '.join(missing)}")
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


class SQLAlchemyConnector(Connector):
    def __init__(self, config: DataSourceConfig) -> None:
        super().__init__(config)
        import sqlalchemy as sa

        if not config.uri:
            raise ModelError(f"origem {config.type} via sqlalchemy precisa de `uri`")
        self._engine = sa.create_engine(config.uri)

    def _run(self, sql: str, fetch_limit: int) -> pa.Table:
        import sqlalchemy as sa

        with self._engine.connect() as con:
            result = con.execute(sa.text(sql))
            names = list(result.keys())
            rows = result.fetchmany(fetch_limit)
        return pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)})

    def run_script(self, sql: str) -> None:
        import sqlalchemy as sa

        with self._engine.begin() as con:
            con.execute(sa.text(sql))

    def close(self) -> None:
        self._engine.dispose()


def _postgres_numeric(table: pa.Table) -> pa.Table:
    """O driver ADBC entrega NUMERIC do Postgres como texto (tipo opaco). Converte para decimal
    exato, com a escala dos próprios valores; se não couber em 38 dígitos, usa float64."""
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
    """O gerenciador de drivers ADBC não carrega DLLs de caminhos com acentos no Windows;
    o nome curto (8.3) do mesmo arquivo é só ASCII."""
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
        f"o driver ADBC está num caminho com acentos ({path}) e o Windows não tem nome curto para "
        "ele; instale o ambiente Python num caminho sem acentos"
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
            "sqlserver": "sqlalchemy",
        }[config.type]
    )
    if config.type == "duckdb":
        return DuckDBConnector(config)
    if config.type == "postgres" and driver == "adbc":
        return ADBCPostgresConnector(config)
    if config.type == "databricks":
        return DatabricksConnector(config)
    if driver == "sqlalchemy":
        return SQLAlchemyConnector(config)
    raise ModelError(f"combinação não suportada: {config.type} com {driver}")
