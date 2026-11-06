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
