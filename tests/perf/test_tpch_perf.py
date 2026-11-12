"""TPC-H performance baseline on DuckDB (DEC-4).

For each case of examples/tpch/queries.yml: the time to compile the request, to run the
engine's SQL and to run the hand-written SQL, and a check that both return the same result.
Scale factor 1 by default; `COSMOS_TPCH_SF` picks another and `COSMOS_TPCH_RUNS` the number
of runs (the best is kept). Not in the default run: `uv run pytest -m perf tests/perf`.
"""

from __future__ import annotations

import os
import time

import pytest

from cosmos_semantic.expressions import remap_sql
from cosmos_semantic.planner import Planner, Principal
from cosmos_semantic.reference import load_cases, run_case, server_today

from ..conftest import TPCH, tpch_connector
from .conftest import TIMINGS

pytestmark = pytest.mark.perf

CASES = load_cases(TPCH / "queries.yml")
SCALE_FACTOR = float(os.environ.get("COSMOS_TPCH_SF", "1"))
RUNS = int(os.environ.get("COSMOS_TPCH_RUNS", "3"))


@pytest.fixture(scope="module")
def tpch_db():
    connector = tpch_connector(SCALE_FACTOR)
    yield connector
    connector.close()


def _best(fn) -> tuple[float, object]:
    best, out = float("inf"), None
    for _ in range(RUNS):
        start = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - start)
    return best, out


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_tpch_perf_duckdb(case, tpch_graph, tpch_db):
    planner = Planner(tpch_graph, tpch_db.dialect, tpch_db.config.schema_map)
    request = case.request.model_copy(update={"as_of": server_today(tpch_db)})
    compile_s, compiled = _best(
        lambda: planner.plan(request, Principal(user=case.user, channel=case.channel))
    )
    hand_sql = remap_sql(case.sql_for(tpch_db.dialect), tpch_db.dialect, tpch_db.config.schema_map)
    engine_s, table = _best(lambda: tpch_db.execute(compiled.sql))
    hand_s, _ = _best(lambda: tpch_db.execute(hand_sql))

    result = run_case(case, tpch_graph, tpch_db)
    assert result.status == "pass", f"{case.id}: {result.detail}"
    TIMINGS.append(
        {
            "id": case.id,
            "sf": SCALE_FACTOR,
            "compile": compile_s,
            "engine": engine_s,
            "hand": hand_s,
            "rows": table.num_rows,
        }
    )
