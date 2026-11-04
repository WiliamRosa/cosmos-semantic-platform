"""Snowflake behavior that needs no account; the comparative suites cover the rest."""

from __future__ import annotations

import pyarrow as pa
import pytest

from cosmos_semantic.bootstrap import _folded
from cosmos_semantic.connectors import DataSourceConfig, _snowflake_names, connect
from cosmos_semantic.errors import ModelError


def test_upper_case_result_names_are_folded_back():
    table = pa.table({"ORDERS__STATUS": ["a"], "REVENUE": [1], "MixedCase": [2], "lower": [3]})
    assert _snowflake_names(table).column_names == [
        "orders__status",
        "revenue",
        "MixedCase",
        "lower",
    ]


def test_catalog_names_are_folded_only_when_upper_case():
    assert _folded("ORDER_ITEMS") == "order_items"
    assert _folded("OrderItems") == "OrderItems"
    assert _folded(42) == 42


def test_a_source_without_account_or_connection_name_is_rejected():
    pytest.importorskip("snowflake.connector")
    config = DataSourceConfig(type="snowflake", options={"user": "x", "account": ""})
    with pytest.raises(ModelError, match="account"):
        connect(config)
