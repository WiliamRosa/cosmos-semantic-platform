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
