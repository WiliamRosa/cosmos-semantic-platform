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
        pytest.skip("defina COSMOS_PG_URI para rodar contra o Postgres")
    connector = connect(DataSourceConfig(type="postgres", uri=uri))
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
            "defina DATABRICKS_HTTP_PATH e DATABRICKS_TOKEN (com DATABRICKS_SERVER_HOSTNAME) "
            "ou DATABRICKS_CONFIG_PROFILE"
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
