"""Runs the reference queries: engine result versus hand-written SQL."""

from __future__ import annotations

import datetime as dt
import decimal
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cosmos_semantic import errors
from cosmos_semantic.compiler import SemanticGraph
from cosmos_semantic.connectors import Connector
from cosmos_semantic.expressions import remap_sql
from cosmos_semantic.planner import Planner, Principal, QueryRequest


class ReferenceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    phase: int
    title: str
    request: QueryRequest | None = None
    user: dict[str, Any] = {}
    channel: Literal["api", "mcp", "cli"] = "cli"
    sql: str | None = None
    sql_by_dialect: dict[str, str] = {}
    expect_error: str | None = None
    expect_empty: bool = False
    question: str | None = None
    expect_maps_to: str | None = None

    def sql_for(self, dialect: str) -> str | None:
        """The reference SQL for a dialect. The shared ``sql`` is written in PostgreSQL;
        Databricks (Spark SQL) rejects some of its syntax, such as ``DOUBLE PRECISION``, so it
        gets a SQLGlot translation unless the case spells a Databricks version."""
        if dialect in self.sql_by_dialect:
            return self.sql_by_dialect[dialect]
        if self.sql is not None and dialect == "databricks":
            import sqlglot

            return sqlglot.transpile(self.sql, read="postgres", write="databricks")[0]
        return self.sql


@dataclass
class CaseResult:
    case: ReferenceCase
    status: Literal["pass", "fail", "skip"]
    detail: str = ""


def load_cases(path: str | Path) -> list[ReferenceCase]:
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return [ReferenceCase.model_validate(c) for c in data["cases"]]


def _norm(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        value = float(value)
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 4)
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    return value


def _rows(rows: list[dict[str, Any]], columns: list[str]) -> list[tuple]:
    out = [tuple(_norm(r[c]) for c in columns) for r in rows]
    return sorted(
        out, key=lambda t: tuple((v is None, str(type(v)), v if v is not None else 0) for v in t)
    )


def _close(a: tuple, b: tuple) -> bool:
    for x, y in zip(a, b, strict=True):
        if isinstance(x, float) and isinstance(y, float | int):
            if not math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-6):
                return False
        elif isinstance(y, float) and isinstance(x, int):
            if not math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-6):
                return False
        elif x != y:
            return False
    return True


def server_today(connector: Connector) -> dt.date:
    sql = (
        "SELECT CAST(GETDATE() AS DATE) AS d"
        if connector.dialect == "tsql"
        else "SELECT CURRENT_DATE AS d"
    )
    value = connector.execute(sql).to_pylist()[0]["d"]
    return value.date() if isinstance(value, dt.datetime) else value


def run_case(
    case: ReferenceCase,
    graph: SemanticGraph,
    connector: Connector,
    *,
    resolver=None,
) -> CaseResult:
    """``resolver`` answers glossary questions: text -> ``maps_to`` of the concept found."""
    if case.question is not None:
        if resolver is None:
            return CaseResult(case, "skip", "no glossary search")
        found = resolver(case.question)
        if found == case.expect_maps_to:
            return CaseResult(case, "pass")
        return CaseResult(case, "fail", f"expected {case.expect_maps_to}, got {found}")

    assert case.request is not None
    principal = Principal(user=case.user, channel=case.channel)
    planner = Planner(graph, connector.dialect, connector.config.schema_map)
    # The hand-written SQL uses the server's clock, so the engine must use the same "today".
    request = case.request.model_copy(update={"as_of": server_today(connector)})
    try:
        compiled = planner.plan(request, principal)
    except errors.CosmosError as exc:
        if case.expect_error and type(exc).__name__ == case.expect_error:
            return CaseResult(case, "pass", str(exc))
        return CaseResult(case, "fail", f"{type(exc).__name__}: {exc}")
    if case.expect_error:
        return CaseResult(
            case, "fail", f"expected {case.expect_error}, but the query was generated"
        )

    expected_sql = case.sql_for(connector.dialect)
    if expected_sql is None:
        return CaseResult(case, "skip", "no reference SQL")
    expected_sql = remap_sql(expected_sql, connector.dialect, connector.config.schema_map)
    got = connector.execute(compiled.sql).to_pylist()
    want = connector.execute(expected_sql).to_pylist()
    columns = [c.name for c in compiled.columns]
    missing = [c for c in columns if want and c not in want[0]]
    if missing:
        return CaseResult(case, "fail", f"the reference SQL lacks columns {missing}")
    got_rows, want_rows = _rows(got, columns), _rows(want, columns)
    if len(got_rows) != len(want_rows):
        return CaseResult(
            case,
            "fail",
            f"{len(got_rows)} rows from the engine, {len(want_rows)} from the reference",
        )
    for g_row, w_row in zip(got_rows, want_rows, strict=True):
        if not _close(g_row, w_row):
            return CaseResult(case, "fail", f"engine {g_row} != reference {w_row}")
    if not got_rows and not case.expect_empty:
        return CaseResult(case, "fail", "both queries returned no rows; the case proves nothing")
    if got_rows and case.expect_empty:
        return CaseResult(case, "fail", f"expected no rows, got {len(got_rows)}")
    return CaseResult(case, "pass", f"{len(got_rows)} rows")
