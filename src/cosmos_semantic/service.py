"""Service shared by the REST API, the MCP server and the CLI: querying with evidence,
auditing, replay, impact analysis and serialization.

Admins are the subjects listed in ``COSMOS_CONSOLE_ADMINS`` (ADR 0012), plus the user that
``--allow-anonymous`` creates (local development), recognized by how it was created
(``Principal.local_anonymous``), never by its name. Only they see audit data about other people,
such as who queried the members a change affects."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

from cosmos_semantic.catalog import Catalog
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import CosmosError, PolicyError, QueryError
from cosmos_semantic.evidence import EvidenceBuilder, new_query_id, result_hash, sql_hash
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
    def __init__(
        self,
        engine: Engine,
        *,
        audit_path: str | Path | None = None,
        admins: set[str] | None = None,
    ) -> None:
        self.engine = engine
        self.catalog = Catalog(engine.graph)
        self.evidence = EvidenceBuilder(engine.graph)
        path = audit_path or os.environ.get("COSMOS_AUDIT_LOG")
        self.audit_path = Path(path) if path else None
        if admins is None:
            raw = os.environ.get("COSMOS_CONSOLE_ADMINS", "")
            admins = {s.strip() for s in raw.split(",") if s.strip()}
        self.admins = admins
        # Connections cannot be shared across threads; one query at a time per process.
        self._lock = threading.Lock()

    def compile(self, request: QueryRequest, principal: Principal) -> CompiledQuery:
        try:
            compiled = self.engine.compile(request, principal)
        except CosmosError as exc:
            self._audit({"request": request, "principal": principal}, error=exc)
            raise
        return compiled

    def query(
        self,
        request: QueryRequest,
        principal: Principal,
        *,
        max_rows: int | None = None,
        query_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        compiled = self.compile(request, principal)
        # Pin the reference date in the request, so the audit record replays the same "today".
        request = request.model_copy(update={"as_of": compiled.as_of})
        source = self._source(compiled)
        try:
            with self._lock:
                table = self.engine.connector(compiled.data_source).execute(
                    compiled.sql, max_rows=request.limit
                )
        except CosmosError as exc:
            self._audit(
                {"request": request, "principal": principal, "compiled": compiled}, error=exc
            )
            raise
        evidence = self.evidence.build(
            request=request,
            compiled=compiled,
            principal=principal,
            data_source=source,
            table=table,
            query_id=query_id or new_query_id(),
        )
        rows = [{k: jsonable(v) for k, v in row.items()} for row in table.to_pylist()]
        truncated = max_rows is not None and len(rows) > max_rows
        self._audit(
            {"request": request, "principal": principal, "compiled": compiled},
            evidence=evidence,
            ms=time.perf_counter() - started,
        )
        return {
            "columns": [
                {"name": c.name, "kind": c.kind, "member": c.member} for c in compiled.columns
            ],
            "rows": rows[:max_rows] if truncated else rows,
            "row_count": len(rows),
            "truncated": truncated,
            "evidence": evidence,
        }

    def is_admin(self, principal: Principal) -> bool:
        # `--allow-anonymous` is for local development; the person at the keyboard sees all.
        if principal.local_anonymous:
            return True
        return principal.subject is not None and principal.subject in self.admins

    # -- impact ------------------------------------------------------------------

    def impact(self, target: str, *, requester: Principal) -> dict[str, Any]:
        """What a change to `target` (`kind:name`, as `cosmos impact` takes it) affects,
        grouped by owner, source systems and processes. Read-only.

        Consumers (who queried the affected members, from the audit log) are personal data:
        they are returned only to admins, under `consumers`. For everyone else the key is
        absent, and `consumers_visible` is false."""
        from cosmos_semantic.impact import analyze

        admin = self.is_admin(requester)
        audit = self.audit_path if admin else None
        report = analyze(self.engine.graph, [target], audit_path=audit)
        if not admin:
            report.pop("consumers", None)
        report["consumers_visible"] = admin
        return report

    # -- replay ------------------------------------------------------------------

    def find_audit(self, query_id: str) -> dict[str, Any]:
        if self.audit_path is None or not self.audit_path.exists():
            raise QueryError("no audit log is configured (COSMOS_AUDIT_LOG)")
        with self.audit_path.open(encoding="utf-8") as fh:
            for line in fh:
                record = json.loads(line)
                if record.get("query_id") == query_id:
                    return record
        raise QueryError(f"query `{query_id}` is not in the audit log")

    def replay(self, query_id: str, *, requester: Principal | None = None) -> dict[str, Any]:
        """Re-runs an audited query with its original request, user context and as_of, and
        tells whether the model, the generated SQL or the result changed."""
        record = self.find_audit(query_id)
        if record.get("outcome") != "ok":
            raise QueryError(f"query `{query_id}` did not succeed; there is nothing to replay")
        original = record["principal"]
        if requester is not None and requester.subject != original.get("subject"):
            raise PolicyError("only the user who ran a query can replay it")
        request = QueryRequest.model_validate(record["request"])
        principal = Principal(
            user=original.get("user_context") or {},
            channel=original.get("channel", "cli"),
            subject=original.get("subject"),
        )
        compiled = self.engine.compile(request, principal)
        with self._lock:
            table = self.engine.connector(compiled.data_source).execute(
                compiled.sql, max_rows=request.limit
            )
        now = {
            "model_hash": self.evidence.model_hash,
            "sql_hash": sql_hash(compiled.sql),
            "result_hash": result_hash(table),
            "row_count": table.num_rows,
        }
        then = {k: record[k] for k in ("model_hash", "sql_hash", "result_hash", "row_count")}
        same = {k: now[k] == then[k] for k in now}
        if all(same.values()):
            verdict = "identical"
            explanation = "Same model, same SQL and same result."
        elif not same["model_hash"]:
            verdict = "model_changed"
            explanation = "The semantic model changed since the original query."
        elif not same["sql_hash"]:
            verdict = "engine_changed"
            explanation = "Same model, but the engine generated different SQL."
        else:
            verdict = "data_changed"
            explanation = "Same model and SQL; the source data changed."
        return {
            "query_id": query_id,
            "verdict": verdict,
            "explanation": explanation,
            "as_of": record["request"].get("as_of"),
            "original": then,
            "replayed": now,
            "matches": same,
        }

    # -- auditing ----------------------------------------------------------------

    def _source(self, compiled: CompiledQuery | None) -> str | None:
        if compiled is None:
            return None
        try:
            return self.engine._source_name(compiled.data_source)
        except CosmosError:
            return compiled.data_source

    def _audit(
        self,
        context: dict[str, Any],
        *,
        evidence: dict[str, Any] | None = None,
        ms: float | None = None,
        error: Exception | None = None,
    ) -> None:
        """Every query is recorded: who asked, what, the generated SQL, the data source and
        the hashes needed to replay it."""
        request: QueryRequest = context["request"]
        principal: Principal = context["principal"]
        compiled: CompiledQuery | None = context.get("compiled")
        record = {
            "event": "query",
            "query_id": evidence["query_id"] if evidence else None,
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "principal": {
                "subject": principal.subject,
                "channel": principal.channel,
                "user_context": principal.user,
            },
            "request": request.model_dump(mode="json", exclude_defaults=True),
            "data_source": self._source(compiled),
            "model_hash": self.evidence.model_hash,
            "sql": compiled.sql if compiled else None,
            "sql_hash": sql_hash(compiled.sql) if compiled else None,
            "result_hash": evidence["result"]["hash"] if evidence else None,
            "row_count": evidence["result"]["row_count"] if evidence else None,
            "rules": [r["name"] for r in evidence["rules"]] if evidence else [],
            "duration_ms": round(ms * 1000, 1) if ms is not None else None,
            "outcome": "error" if error else "ok",
            "error": f"{type(error).__name__}: {error}" if error else None,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        audit_log.info(line)
        if self.audit_path is not None:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
