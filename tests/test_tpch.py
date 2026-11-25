"""TPC-H reference case (DEC-4): the engine returns the same result as hand-written SQL on
the TPC-H model, at a small scale factor. The timed version is in tests/perf.

On the container databases (DEC-47) the same data, copied from DuckDB, must give the
hand-written SQL's result there and the same numbers as DuckDB."""

from __future__ import annotations

import pytest

from cosmos_semantic.planner import Planner, Principal
from cosmos_semantic.reference import _close, _rows, load_cases, run_case

from .conftest import TPCH, TPCH_SOURCES, load_tpch, tpch_connector, tpch_source, tpch_tables

CASES = load_cases(TPCH / "queries.yml")
SCALE_FACTOR = 0.01


@pytest.fixture(scope="module")
def tpch_small():
    connector = tpch_connector(SCALE_FACTOR)
    yield connector
    connector.close()


@pytest.fixture(
    scope="module",
    params=[pytest.param(kind, marks=getattr(pytest.mark, kind)) for kind in TPCH_SOURCES],
)
def tpch_container(request, tpch_small):
    """Each configured container database, loaded once per module with DuckDB's data."""
    connector = tpch_source(request.param)
    try:
        load_tpch(connector, tpch_tables(tpch_small))
    except BaseException:
        connector.close()
        raise
    yield connector
    connector.close()


def _engine_rows(case, graph, connector):
    planner = Planner(graph, connector.dialect, connector.config.schema_map)
    compiled = planner.plan(case.request, Principal(user=case.user, channel=case.channel))
    columns = [c.name for c in compiled.columns]
    return _rows(connector.execute(compiled.sql).to_pylist(), columns)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_tpch_reference_duckdb(case, tpch_graph, tpch_small):
    result = run_case(case, tpch_graph, tpch_small)
    assert result.status == "pass", f"{case.id}: {result.detail}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_tpch_reference_container(case, tpch_graph, tpch_container, tpch_small):
    result = run_case(case, tpch_graph, tpch_container)
    assert result.status == "pass", f"{case.id} on {tpch_container.dialect}: {result.detail}"
    got = _engine_rows(case, tpch_graph, tpch_container)
    want = _engine_rows(case, tpch_graph, tpch_small)
    assert len(got) == len(want), f"{case.id}: {len(got)} rows, {len(want)} on DuckDB"
    for g_row, w_row in zip(got, want, strict=True):
        assert _close(g_row, w_row), f"{case.id}: {g_row} != {w_row} on DuckDB"


def test_tpch_cases_are_unique():
    assert CASES
    assert len({c.id for c in CASES}) == len(CASES)
    assert all(c.request is not None and c.sql for c in CASES)
