"""The BigQuery connector, loader, bootstrap and starter against a fake client (EXTRA-2).

``google.cloud.bigquery`` and the few Google modules the code imports are replaced in
``sys.modules`` by small fakes, so these tests need no network, no project and no credentials,
and pass whether or not ``google-cloud-bigquery`` is installed. The fake client records every
call and answers queries from DuckDB (translated from BigQuery SQL by SQLGlot), with a canned
``INFORMATION_SCHEMA``.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
import sys
import types
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import sqlglot
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from cosmos_semantic.cli import app
from cosmos_semantic.connectors import DataSourceConfig, connect, load_connections
from cosmos_semantic.errors import ConnectorError, ModelError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ecommerce"
SECRET = "-----BEGIN PRIVATE KEY-----\nNOT-A-REAL-KEY-0123456789\n-----END PRIVATE KEY-----\n"


# -- the fake Google modules ---------------------------------------------------------------


class NotFound(Exception):
    pass


class FakeRetry:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def with_timeout(self, timeout: float) -> FakeRetry:
        return FakeRetry(timeout)


class QueryJobConfig:
    def __init__(self, **kwargs) -> None:
        self.maximum_bytes_billed = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class LoadJobConfig:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class SchemaField:
    def __init__(self, name: str, field_type: str) -> None:
        self.name, self.field_type = name, field_type


class FakeRows:
    def __init__(self, table: pa.Table) -> None:
        self.table = table

    def to_arrow(self, create_bqstorage_client: bool = True) -> pa.Table:
        assert create_bqstorage_client is False
        return self.table


class FakeJob:
    def __init__(self, client: FakeClient, call: dict) -> None:
        self.client, self.call = client, call

    def result(self, timeout=None, retry=None, max_results=None):
        self.call["result"] = {"timeout": timeout, "retry": retry, "max_results": max_results}
        if "sql" not in self.call:  # a load job
            return None
        return FakeRows(self.client.answer(self.call["sql"]))


class FakeClient:
    """Records what the code asks for; answers queries from ``backend`` (DuckDB) and
    ``catalog`` (the rows of ``<dataset>.INFORMATION_SCHEMA``)."""

    instances: list[FakeClient] = []
    backend: duckdb.DuckDBPyConnection | None = None
    catalog: dict[str, list[dict]] = {}

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.project = kwargs["project"]
        self.queries: list[dict] = []
        self.loads: list[dict] = []
        self.datasets: set[str] = set()
        self.created: list[str] = []
        self.closed = False
        FakeClient.instances.append(self)

    def query(self, sql, job_config=None, retry=None, job_retry="unset"):
        call = {"sql": sql, "job_config": job_config, "retry": retry, "job_retry": job_retry}
        self.queries.append(call)
        return FakeJob(self, call)

    def answer(self, sql: str) -> pa.Table:
        if "INFORMATION_SCHEMA" in sql:
            kind = "keys" if "KEY_COLUMN_USAGE" in sql else "columns"
            return pa.Table.from_pylist(self.catalog.get(kind, []))
        assert self.backend is not None, f"no backend for {sql}"
        duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        return self.backend.sql(duck).to_arrow_table()

    def load_table_from_file(self, buffer, destination, size=None, job_config=None):
        data = buffer.read()
        call = {
            "destination": destination,
            "size": size,
            "job_config": job_config,
            "table": pq.read_table(io.BytesIO(data)),
            "bytes": len(data),
        }
        self.loads.append(call)
        return FakeJob(self, call)

    def get_dataset(self, dataset):
        if dataset not in self.datasets:
            raise NotFound(dataset)
        return dataset

    def create_dataset(self, dataset):
        self.datasets.add(dataset)
        self.created.append(dataset)
        return dataset

    def close(self) -> None:
        self.closed = True


class FakeCredentials:
    files: list[str] = []

    @classmethod
    def from_service_account_file(cls, path: str):
        cls.files.append(path)
        Path(path).read_text(encoding="utf-8")  # as the real one does; never printed
        return cls()


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # a package, so submodules resolve from sys.modules
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def fake_bigquery(monkeypatch):
    """Installs the fake modules for one test; monkeypatch restores the real ones after."""
    FakeClient.instances = []
    FakeClient.backend = None
    FakeClient.catalog = {}
    FakeCredentials.files = []
    bigquery = _module(
        "google.cloud.bigquery",
        Client=FakeClient,
        QueryJobConfig=QueryJobConfig,
        LoadJobConfig=LoadJobConfig,
        SchemaField=SchemaField,
        DEFAULT_RETRY=FakeRetry(),
        SourceFormat=types.SimpleNamespace(PARQUET="PARQUET"),
        WriteDisposition=types.SimpleNamespace(WRITE_TRUNCATE="WRITE_TRUNCATE"),
    )
    exceptions = _module("google.api_core.exceptions", NotFound=NotFound)
    service_account = _module("google.oauth2.service_account", Credentials=FakeCredentials)
    cloud = _module("google.cloud", bigquery=bigquery)
    api_core = _module("google.api_core", exceptions=exceptions)
    oauth2 = _module("google.oauth2", service_account=service_account)
    google = _module("google", cloud=cloud, api_core=api_core, oauth2=oauth2)
    for module in (google, cloud, bigquery, api_core, exceptions, oauth2, service_account):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return FakeClient


def _source(**options) -> DataSourceConfig:
    return DataSourceConfig(
        type="bigquery",
        options={"project": "test-project", **options},
        schema_map={"public": "cosmos_demo"},
        timeout_seconds=17,
        max_rows=5,
    )


# -- query jobs ----------------------------------------------------------------------------


def test_each_query_job_has_the_timeout_byte_cap_no_job_retry_and_the_row_limit(fake_bigquery):
    connector = connect(_source(maximum_bytes_billed="1000000000", location="EU"))
    fake_bigquery.backend = duckdb.connect()
    connector.execute("SELECT 1 AS one")
    connector.execute("SELECT 2 AS two", max_rows=3)
    connector.close()

    (client,) = fake_bigquery.instances
    assert client.kwargs == {"project": "test-project", "location": "EU"}
    assert client.closed
    first, second = client.queries
    for call in (first, second):
        assert call["job_config"].job_timeout_ms == 17_000
        assert call["job_config"].maximum_bytes_billed == 1_000_000_000
        assert isinstance(call["job_config"].maximum_bytes_billed, int)
        assert call["job_retry"] is None  # a failed job is not run again
        assert call["retry"].timeout == 17
        assert call["result"]["timeout"] == 17
        assert call["result"]["retry"] is call["retry"]
    # One row more than the limit is fetched, to tell a full result from a cut one.
    assert first["result"]["max_results"] == 5 + 1
    assert second["result"]["max_results"] == 3 + 1


def test_without_a_byte_cap_none_is_set(fake_bigquery):
    connector = connect(_source())
    fake_bigquery.backend = duckdb.connect()
    connector.execute("SELECT 1 AS one")
    (call,) = fake_bigquery.instances[0].queries
    assert call["job_config"].maximum_bytes_billed is None


def test_a_result_over_the_row_limit_fails(fake_bigquery):
    connector = connect(_source())
    fake_bigquery.backend = duckdb.connect()
    with pytest.raises(ConnectorError, match="5-row limit"):
        connector.execute("SELECT * FROM range(20) AS t(n)")
    assert connector.execute("SELECT * FROM range(5) AS t(n)").num_rows == 5


def test_script_statements_run_as_separate_jobs_with_the_same_limits(fake_bigquery):
    connector = connect(_source(maximum_bytes_billed=10))
    fake_bigquery.backend = duckdb.connect()
    connector.run_script("CREATE SCHEMA IF NOT EXISTS s; CREATE TABLE s.t (x INT64)")
    calls = fake_bigquery.instances[0].queries
    assert len(calls) == 2
    for call in calls:
        assert call["job_config"].job_timeout_ms == 17_000
        assert call["job_config"].maximum_bytes_billed == 10
        assert call["job_retry"] is None
        assert call["result"]["timeout"] == 17
        assert call["result"]["max_results"] is None


def test_a_write_is_refused_before_any_job_starts(fake_bigquery):
    connector = connect(_source())
    with pytest.raises(ConnectorError, match="single SELECT"):
        connector.execute("DELETE FROM cosmos_demo.orders WHERE TRUE")
    assert fake_bigquery.instances[0].queries == []


# -- credentials ---------------------------------------------------------------------------


def test_credentials_file_comes_from_options_and_is_never_logged(
    fake_bigquery, tmp_path, monkeypatch, caplog, capsys
):
    key = tmp_path / "secret-key-dir" / "reader.json"
    key.parent.mkdir()
    key.write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    caplog.set_level(logging.DEBUG)

    connector = connect(_source(credentials_file="~/secret-key-dir/reader.json"))
    fake_bigquery.backend = duckdb.connect()
    connector.execute("SELECT 1 AS one")
    with pytest.raises(ConnectorError):
        connector.execute("SELECT * FROM missing_table")
    connector.close()

    # The ~ is expanded. Compared as paths: on Windows, expanduser keeps the "/" of the option.
    assert [Path(f) for f in FakeCredentials.files] == [key]
    (client,) = fake_bigquery.instances
    assert isinstance(client.kwargs["credentials"], FakeCredentials)
    printed = capsys.readouterr()
    for text in (caplog.text, printed.out, printed.err):
        assert "NOT-A-REAL-KEY" not in text
        assert "secret-key-dir" not in text


def test_credentials_file_is_read_only_from_options(fake_bigquery, tmp_path, monkeypatch):
    key = tmp_path / "reader.json"
    key.write_text(SECRET, encoding="utf-8")
    # Neither the environment, nor a top-level field, nor the uri gives the connector a key file.
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    monkeypatch.setenv("COSMOS_BIGQUERY_CREDENTIALS_FILE", str(key))
    connect(
        DataSourceConfig(
            type="bigquery", uri=f"bigquery://p?credentials_path={key}", options={"project": "p"}
        )
    )
    assert FakeCredentials.files == []
    assert "credentials" not in fake_bigquery.instances[0].kwargs  # Application Default Creds
    with pytest.raises(ValidationError):
        DataSourceConfig(type="bigquery", credentials_file=str(key), options={"project": "p"})
    # An empty value (an unset `${VAR:-}`) counts as absent.
    connect(DataSourceConfig(type="bigquery", options={"project": "p", "credentials_file": ""}))
    assert FakeCredentials.files == []


def test_the_emulator_endpoint_uses_anonymous_credentials(fake_bigquery, monkeypatch):
    anonymous = type("AnonymousCredentials", (), {})
    client_options = type("ClientOptions", (), {"__init__": lambda s, **kw: s.__dict__.update(kw)})
    monkeypatch.setitem(
        sys.modules,
        "google.api_core.client_options",
        _module("google.api_core.client_options", ClientOptions=client_options),
    )
    monkeypatch.setitem(sys.modules, "google.auth", _module("google.auth"))
    monkeypatch.setitem(
        sys.modules,
        "google.auth.credentials",
        _module("google.auth.credentials", AnonymousCredentials=anonymous),
    )
    connector = connect(_source(api_endpoint="http://localhost:9050", credentials_file=""))
    kwargs = fake_bigquery.instances[0].kwargs
    assert isinstance(kwargs["credentials"], anonymous)
    assert kwargs["client_options"].api_endpoint == "http://localhost:9050"
    assert connector.emulated
    assert FakeCredentials.files == []


def test_an_endpoint_with_a_key_file_is_refused(fake_bigquery):
    """The endpoint would win with anonymous credentials and ignore the key without a word."""
    with pytest.raises(ModelError, match="both options.api_endpoint and options.credentials_file"):
        connect(_source(api_endpoint="http://localhost:9050", credentials_file="/never/read.json"))
    assert fake_bigquery.instances == []
    assert FakeCredentials.files == []


# -- cosmos check warnings -----------------------------------------------------------------


def _write_connections(path: Path, sources: dict) -> Path:
    path.write_text(yaml.safe_dump({"data_sources": sources}, sort_keys=False), encoding="utf-8")
    return path


def test_check_warns_about_a_bigquery_source_without_a_byte_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("COSMOS_RUNS_LOG", raising=False)
    connections = _write_connections(
        tmp_path / "connections.yml",
        {
            "local": {"type": "duckdb", "path": "x.duckdb"},
            "uncapped": {"type": "bigquery", "options": {"project": "p"}},
            "empty_cap": {
                "type": "bigquery",
                "options": {"project": "p", "maximum_bytes_billed": ""},
            },
            "capped": {
                "type": "bigquery",
                "options": {"project": "p", "maximum_bytes_billed": 1_000_000_000},
            },
            "emulator": {
                "type": "bigquery",
                "options": {"project": "p", "api_endpoint": "http://localhost:19050"},
            },
        },
    )
    result = CliRunner().invoke(
        app, ["check", "-m", str(EXAMPLE / "semantic"), "-c", str(connections)]
    )
    assert result.exit_code == 0, result.output
    warnings = [line for line in result.stderr.splitlines() if line.startswith("warning:")]
    assert len(warnings) == 2
    assert "'uncapped' (bigquery) has no options.maximum_bytes_billed" in warnings[0]
    assert "'empty_cap'" in warnings[1]
    assert "warning:" not in result.stdout


def test_check_does_not_warn_about_the_example_connections(monkeypatch):
    monkeypatch.delenv("COSMOS_RUNS_LOG", raising=False)
    for var in (
        "COSMOS_BIGQUERY_ENDPOINT",
        "COSMOS_BIGQUERY_CREDENTIALS_FILE",
        "COSMOS_BIGQUERY_MAXIMUM_BYTES_BILLED",
    ):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(
        app,
        [
            "check",
            "-m",
            str(EXAMPLE / "semantic"),
            "-c",
            str(EXAMPLE / "connections.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "warning:" not in result.stderr
    # Without the endpoint the example source is BigQuery itself, capped at 1 GB, and a key
    # file alone does not trip the endpoint refusal.
    options = load_connections(EXAMPLE / "connections.yml").data_sources["bigquery"].options
    assert options["api_endpoint"] == ""
    assert int(options["maximum_bytes_billed"]) == 1_000_000_000


def test_the_example_with_a_key_file_and_no_endpoint_connects(fake_bigquery, tmp_path, monkeypatch):
    key = tmp_path / "key.json"
    key.write_text(SECRET, encoding="utf-8")
    monkeypatch.delenv("COSMOS_BIGQUERY_ENDPOINT", raising=False)
    monkeypatch.setenv("COSMOS_BIGQUERY_CREDENTIALS_FILE", str(key))
    connect(load_connections(EXAMPLE / "connections.yml").data_sources["bigquery"])
    assert [Path(f) for f in FakeCredentials.files] == [key]
    assert "client_options" not in fake_bigquery.instances[0].kwargs


# -- load_bigquery -------------------------------------------------------------------------


def _demo_tables() -> dict[str, pa.Table]:
    orders = pa.table(
        {
            "order_id": pa.array([1, 2], pa.int64()),
            "status": pa.array(["paid", "open"], pa.string()),
            "amount": pa.array([10.5, 3.25], pa.float64()),
            "ordered_on": pa.array([dt.date(2026, 1, 1), dt.date(2026, 1, 2)], pa.date32()),
            "ordered_at": pa.array(
                [dt.datetime(2026, 1, 1, 9), dt.datetime(2026, 1, 2, 18, 30)], pa.timestamp("us")
            ),
        }
    )
    customers = pa.table({"customer_id": pa.array([7], pa.int64())})
    return {"orders": orders, "customers": customers}


def test_load_writes_parquet_with_an_explicit_schema_and_truncates(fake_bigquery):
    from cosmos_semantic.demo_data import load

    connector = connect(_source())
    tables = _demo_tables()
    load(connector, tables)  # into the dataset that `public` maps to

    (client,) = fake_bigquery.instances
    assert client.created == ["cosmos_demo"]  # created because it was missing
    assert client.queries == []  # no DML: the sandbox refuses it
    assert [c["destination"] for c in client.loads] == [
        "test-project.cosmos_demo.orders",
        "test-project.cosmos_demo.customers",
    ]
    orders = client.loads[0]
    config = orders["job_config"].kwargs
    assert config["source_format"] == "PARQUET"
    assert config["write_disposition"] == "WRITE_TRUNCATE"
    assert [(f.name, f.field_type) for f in config["schema"]] == [
        ("order_id", "INT64"),
        ("status", "STRING"),
        ("amount", "FLOAT64"),
        ("ordered_on", "DATE"),
        ("ordered_at", "DATETIME"),  # a time without a zone is a civil DATETIME
    ]
    assert orders["size"] == orders["bytes"]
    assert orders["table"].equals(tables["orders"])
    assert all("result" in c for c in client.loads)  # each load job is awaited


def test_an_existing_dataset_is_not_created_again(fake_bigquery):
    from cosmos_semantic.demo_data import load_bigquery

    connector = connect(_source())
    client = fake_bigquery.instances[0]
    client.datasets.add("analytics")
    load_bigquery(connector, _demo_tables(), "analytics")
    assert client.created == []
    assert {c["destination"].split(".")[1] for c in client.loads} == {"analytics"}


# -- cosmos bootstrap ----------------------------------------------------------------------


def _catalog_backend() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA cosmos_demo")
    con.execute(
        "CREATE TABLE cosmos_demo.customers (customer_id BIGINT, region VARCHAR, score DOUBLE)"
    )
    con.execute("INSERT INTO cosmos_demo.customers VALUES (1, 'South', 1.5), (2, 'North', 2.5)")
    con.execute(
        "CREATE TABLE cosmos_demo.orders (order_id BIGINT, customer_id BIGINT, "
        "quantity BIGINT, amount DOUBLE, tax DECIMAL(38, 9), ordered_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO cosmos_demo.orders VALUES "
        "(10, 1, 2, 9.5, 1.25, '2026-01-01 10:00'), (11, 2, 1, 4.0, 0.5, '2026-01-02 11:00')"
    )
    return con


CATALOG = {
    "columns": [
        {"table_name": t, "column_name": c, "data_type": d}
        for t, c, d in [
            ("customers", "customer_id", "INT64"),
            ("customers", "region", "STRING"),
            ("customers", "score", "FLOAT64"),
            ("orders", "order_id", "INT64"),
            ("orders", "customer_id", "INT64"),
            ("orders", "quantity", "INT64"),
            ("orders", "amount", "FLOAT64"),
            ("orders", "tax", "BIGNUMERIC(38, 9)"),
            ("orders", "ordered_at", "DATETIME"),
        ]
    ],
    "keys": [{"table_name": "orders", "column_name": "order_id"}],
}


def test_bootstrap_reads_the_dataset_information_schema_and_numeric_types(
    fake_bigquery, tmp_path, monkeypatch, caplog
):
    from cosmos_semantic.bootstrap import Column

    assert Column("x", "INT64").kind == "number"
    assert Column("x", "FLOAT64").kind == "number"
    assert Column("x", "BIGNUMERIC").kind == "number"
    assert Column("x", "BIGNUMERIC(38, 9)").kind == "number"
    assert Column("x", "DATETIME").kind == "time"

    key = tmp_path / "secret-key-dir" / "reader.json"
    key.parent.mkdir()
    key.write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("COSMOS_TEST_BQ_KEY", str(key))
    (tmp_path / "connections.yml").write_text(
        "default: bq\n"
        "data_sources:\n"
        "  bq:\n"
        "    type: bigquery\n"
        "    options:\n"
        "      project: test-project\n"
        "      maximum_bytes_billed: 1000000000\n"
        "      credentials_file: ${COSMOS_TEST_BQ_KEY}\n"
        "    schema_map: { public: cosmos_demo }\n",
        encoding="utf-8",
    )
    fake_bigquery.backend = _catalog_backend()
    fake_bigquery.catalog = CATALOG
    caplog.set_level(logging.DEBUG)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app, ["bootstrap", "-o", "draft/model.yml", "--report", "report.json"]
    )
    assert result.exit_code == 0, result.output
    assert "2 tables" in result.output

    (client,) = fake_bigquery.instances
    assert client.closed
    catalog = [c["sql"] for c in client.queries if "INFORMATION_SCHEMA" in c["sql"]]
    assert len(catalog) == 2
    columns_sql, keys_sql = catalog
    assert "FROM `cosmos_demo`.INFORMATION_SCHEMA.COLUMNS" in columns_sql
    assert "FROM `cosmos_demo`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS c" in keys_sql
    assert "JOIN `cosmos_demo`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS k" in keys_sql
    for sql in catalog:
        sqlglot.parse_one(sql, read="bigquery")
        # BigQuery has no unqualified information_schema.
        assert not re.search(r"(?<![.`])\binformation_schema\b", sql, re.IGNORECASE)
    for call in client.queries:  # every bootstrap query is an ordinary, capped job
        assert call["job_config"].maximum_bytes_billed == 1_000_000_000
        assert call["job_retry"] is None

    draft = yaml.safe_load((tmp_path / "draft" / "model.yml").read_text(encoding="utf-8"))
    models = {m["name"]: m for m in draft["semantic_models"]}
    orders = models["orders"]
    measures = {m["name"]: m["agg"] for m in orders["measures"]}
    assert {"quantity", "amount", "tax"} <= set(measures)  # INT64, FLOAT64, BIGNUMERIC
    dims = {d["name"]: d["type"] for d in orders.get("dimensions", [])}
    assert dims["ordered_at"] == "time"
    assert "tax" not in dims and "amount" not in dims
    assert {"name": "customer", "type": "foreign", "expr": "customer_id"} in [
        {k: e[k] for k in ("name", "type", "expr") if k in e} for e in orders["entities"]
    ]
    customers = models["customers"]
    assert "score" in {m["name"] for m in customers["measures"]}
    assert {d["name"]: d["type"] for d in customers["dimensions"]}["region"] == "categorical"

    keys = {
        k["table"]: k["source"] for k in yaml.safe_load(Path("report.json").read_text())["keys"]
    }
    assert keys == {"orders": "declared", "customers": "inferred (unique, not null)"}

    for text in (result.output, caplog.text, Path("report.json").read_text()):
        assert "NOT-A-REAL-KEY" not in text
        assert "secret-key-dir" not in text


def test_bootstrap_marks_timestamp_columns_and_keeps_datetime_columns(fake_bigquery):
    """A TIMESTAMP is an instant in UTC: the draft says so (`column_type: timestamp`) and the
    planner reads it as its UTC date and time; a DATETIME column stays as before (DEC-57)."""
    from cosmos_semantic.bootstrap import bootstrap
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.planner import Planner, Principal, QueryRequest

    backend = duckdb.connect()
    backend.execute("CREATE SCHEMA cosmos_demo")
    backend.execute(
        "CREATE TABLE cosmos_demo.events (event_id BIGINT, kind VARCHAR, "
        "happened_at TIMESTAMPTZ, logged_at TIMESTAMP, event_date DATE)"
    )
    backend.execute(
        "INSERT INTO cosmos_demo.events VALUES (1, 'a', '2026-01-01 10:00+00', "
        "'2026-01-01 10:00', '2026-01-01')"
    )
    fake_bigquery.backend = backend
    fake_bigquery.catalog = {
        "columns": [
            {"table_name": "events", "column_name": c, "data_type": d}
            for c, d in [
                ("event_id", "INT64"),
                ("kind", "STRING"),
                ("happened_at", "TIMESTAMP"),
                ("logged_at", "DATETIME"),
                ("event_date", "DATE"),
            ]
        ],
        "keys": [{"table_name": "events", "column_name": "event_id"}],
    }
    connector = connect(
        DataSourceConfig(
            type="bigquery", options={"project": "p"}, schema_map={"public": "cosmos_demo"}
        )
    )
    project, report = bootstrap(connector)
    dims = {d.name: d for d in project.semantic_models[0].dimensions}
    assert dims["happened_at"].column_type == "timestamp"
    assert dims["logged_at"].column_type is None and dims["event_date"].column_type is None
    assert any("happened_at: TIMESTAMP" in w for w in report["warnings"])

    planner = Planner(compile_project(project), "bigquery", {"public": "cosmos_demo"})
    sql = planner.plan(
        QueryRequest(
            metrics=["events_count"],
            dimensions=["events.happened_at__day", "events.logged_at__day"],
            filters=["{events.happened_at} >= DATE '2026-01-01'"],
            as_of=dt.date(2026, 1, 2),
        ),
        Principal(),
    ).sql
    sqlglot.parse_one(sql, read="bigquery")
    assert "TIMESTAMP_TRUNC(DATETIME(events.happened_at, 'UTC'), DAY)" in sql
    assert "DATETIME(events.happened_at, 'UTC') >= CAST('2026-01-01' AS DATE)" in sql
    assert "TIMESTAMP_TRUNC(events.logged_at, DAY)" in sql


# -- cosmos init ---------------------------------------------------------------------------


def _uncomment_block(text: str, name: str) -> str:
    """Uncomments one commented data source in the starter's connections.yml."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  # {name}:")
    out = lines[:start]
    i = start
    while (
        i < len(lines)
        and lines[i].startswith("  # ")
        and (i == start or not re.match(r"  # [a-z]+:", lines[i]))
    ):
        out.append("  " + lines[i][len("  # ") :])
        i += 1
    return "".join(out + lines[i:])


def test_the_starter_bigquery_block_parses_once_uncommented(fake_bigquery, tmp_path, monkeypatch):
    result = CliRunner().invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    path = tmp_path / "connections.yml"
    text = _uncomment_block(path.read_text(encoding="utf-8"), "bigquery")
    assert "\n  bigquery:\n    type: bigquery\n" in text
    path.write_text(text, encoding="utf-8")

    monkeypatch.setenv("COSMOS_BIGQUERY_PROJECT", "my-project")
    monkeypatch.delenv("COSMOS_BIGQUERY_LOCATION", raising=False)
    config = load_connections(path).data_sources["bigquery"]
    assert config.type == "bigquery" and config.dialect == "bigquery"
    assert config.options == {
        "project": "my-project",
        "location": "US",
        "maximum_bytes_billed": 1_000_000_000,
    }
    assert config.schema_map == {"public": "cosmos_demo"}

    connect(config)  # every option is one the connector knows
    assert fake_bigquery.instances[0].kwargs == {"project": "my-project", "location": "US"}

    # The key file line, uncommented too, is an option the connector accepts.
    keyed = text.replace("    # credentials_file:", "    credentials_file:")
    assert keyed != text
    path.write_text(keyed, encoding="utf-8")
    options = load_connections(path).data_sources["bigquery"].options
    assert options["credentials_file"] == "~/keys/cosmos-reader.json"
