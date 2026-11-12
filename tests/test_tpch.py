"""TPC-H reference case (DEC-4): the engine returns the same result as hand-written SQL on
the TPC-H model, at a small scale factor. The timed version is in tests/perf."""

from __future__ import annotations

import pytest

from cosmos_semantic.reference import load_cases, run_case

from .conftest import TPCH, tpch_connector

CASES = load_cases(TPCH / "queries.yml")


@pytest.fixture(scope="module")
def tpch_small():
    connector = tpch_connector(0.01)
    yield connector
    connector.close()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_tpch_reference_duckdb(case, tpch_graph, tpch_small):
    result = run_case(case, tpch_graph, tpch_small)
    assert result.status == "pass", f"{case.id}: {result.detail}"


def test_tpch_cases_are_unique():
    assert CASES
    assert len({c.id for c in CASES}) == len(CASES)
    assert all(c.request is not None and c.sql for c in CASES)
