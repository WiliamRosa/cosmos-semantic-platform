"""Service shared by the REST API and the MCP server: querying, auditing and serialization."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import math
import threading
import time
from typing import Any

from cosmos_semantic.catalog import Catalog
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.planner import CompiledQuery, Principal, QueryRequest

audit_log = logging.getLogger("cosmos.audit")


def jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


class SemanticService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.catalog = Catalog(engine.graph)
        # Connections cannot be shared across threads; one query at a time per process.
        self._lock = threading.Lock()

    def compile(self, request: QueryRequest, principal: Principal) -> CompiledQuery:
        try:
            compiled = self.engine.compile(request, principal)
        except CosmosError as exc:
            self._audit(principal, request, None, error=exc)
            raise
        return compiled

    def query(
        self, request: QueryRequest, principal: Principal, *, max_rows: int | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        compiled = self.compile(request, principal)
        try:
            with self._lock:
                table = self.engine.connector(compiled.data_source).execute(
                    compiled.sql, max_rows=request.limit
                )
        except CosmosError as exc:
            self._audit(principal, request, compiled, error=exc)
            raise
        rows = [{k: jsonable(v) for k, v in row.items()} for row in table.to_pylist()]
        truncated = max_rows is not None and len(rows) > max_rows
        self._audit(principal, request, compiled, rows=len(rows), ms=time.perf_counter() - started)
        return {
            "columns": [
                {"name": c.name, "kind": c.kind, "member": c.member} for c in compiled.columns
            ],
            "rows": rows[:max_rows] if truncated else rows,
            "row_count": len(rows),
            "truncated": truncated,
        }

    def _source(self, compiled: CompiledQuery | None) -> str | None:
        if compiled is None:
            return None
        try:
            return self.engine._source_name(compiled.data_source)
        except CosmosError:
            return compiled.data_source

    def _audit(
        self,
        principal: Principal,
        request: QueryRequest,
        compiled: CompiledQuery | None,
        *,
        rows: int | None = None,
        ms: float | None = None,
        error: Exception | None = None,
    ) -> None:
        """Every query is logged: who asked, which metrics, the generated SQL and the data source."""
        record = {
            "event": "query",
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "subject": principal.subject,
            "channel": principal.channel,
            "metrics": request.metrics,
            "dimensions": request.dimensions,
            "filters": request.filters,
            "data_source": self._source(compiled),
            "sql": compiled.sql if compiled else None,
            "rows": rows,
            "duration_ms": round(ms * 1000, 1) if ms is not None else None,
            "outcome": "error" if error else "ok",
            "error": f"{type(error).__name__}: {error}" if error else None,
        }
        audit_log.info(json.dumps(record, ensure_ascii=False))
