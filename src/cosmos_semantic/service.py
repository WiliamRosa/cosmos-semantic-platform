"""Service shared by the REST API, the MCP server and the CLI: querying with evidence,
auditing, replay, impact analysis and serialization.

Admins are the subjects listed in ``COSMOS_CONSOLE_ADMINS`` (ADR 0012), plus the user that
``--allow-anonymous`` creates (local development), recognized by how it was created
(``Principal.local_anonymous``), never by its name. Only they see audit data about other people,
such as who queried the members a change affects.

Reconciliation cases (DEC-45) are declared by the operator in a file (``COSMOS_CHECK_CASES``,
the format ``cosmos check --cases`` reads). The API and MCP run them by id, never SQL from the
caller, and refuse a case when a row filter applies to it: row filters cannot be applied to its
hand-written SQL, so its row diff could show rows the caller may not see."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import pyarrow as pa

from cosmos_semantic.catalog import Catalog
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import (
    CosmosError,
    NotFoundError,
    PolicyError,
    QueryError,
    RateLimitError,
)
from cosmos_semantic.evidence import (
    EvidenceBuilder,
    merge_row_filters,
    new_query_id,
    result_hash,
    sql_hash,
)
from cosmos_semantic.expressions import describe_filter
from cosmos_semantic.health import Health
from cosmos_semantic.planner import CompiledQuery, Filter, Principal, QueryRequest

audit_log = logging.getLogger("cosmos.audit")
log = logging.getLogger(__name__)

# Most values `dimension_values` returns: the default and the hard maximum.
MAX_DIMENSION_VALUES = 100
# Most distinct values fetched (most frequent first) before the top ones are chosen in Python.
# Past it, `truncated` is set and ties at that far cut-off may differ between databases.
VALUES_FETCH_CEILING = 10_000
# Most rows of a reconciliation's row diff returned by the API and MCP: the hard maximum.
MAX_RECONCILE_DIFF_ROWS = 200
# Least time between two runs of the same case through the API or MCP, in seconds; the
# default of COSMOS_RECONCILE_MIN_INTERVAL.
RECONCILE_MIN_INTERVAL = 60.0
# A case id as the audit log records it; anything else is recorded as `<invalid>`.
CASE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


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


def _iso_day(value: Any) -> str | None:
    """A day as an ISO date, whatever type the database returns it as (DATE, TIMESTAMP,
    Oracle's DATE with a time of day, or text)."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return dt.date.fromisoformat(str(value)[:10]).isoformat()


def _diff_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: [jsonable(v) for v in value] if k == "key" else jsonable(value)
        for k, value in row.items()
    }


class _LockedConnector:
    """A connector whose every statement holds the service's lock, and only that statement:
    connections cannot be shared across threads, but a reconciliation runs many statements,
    and queries must be able to run between them."""

    def __init__(self, connector: Any, lock: threading.Lock) -> None:
        self._connector = connector
        self._lock = lock

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._connector.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connector, name)


class SemanticService:
    def __init__(
        self,
        engine: Engine,
        *,
        audit_path: str | Path | None = None,
        admins: set[str] | None = None,
        cases_path: str | Path | None = None,
        runs_path: str | Path | None = None,
        reconcile_min_interval: float | None = None,
    ) -> None:
        self.engine = engine
        self.catalog = Catalog(engine.graph)
        self.evidence = EvidenceBuilder(engine.graph)
        # Rule tests and check runs (COSMOS_RUNS_LOG).
        self.health = Health(engine.graph, runs_path=runs_path)
        # Declared reconciliation cases (COSMOS_CHECK_CASES, DEC-45).
        cases = cases_path or os.environ.get("COSMOS_CHECK_CASES")
        self.cases_path = Path(cases) if cases else None
        path = audit_path or os.environ.get("COSMOS_AUDIT_LOG")
        self.audit_path = Path(path) if path else None
        if admins is None:
            raw = os.environ.get("COSMOS_CONSOLE_ADMINS", "")
            admins = {s.strip() for s in raw.split(",") if s.strip()}
        self.admins = admins
        # Connections cannot be shared across threads; one query at a time per process.
        self._lock = threading.Lock()
        # One reconciliation at a time (DEC-45), apart from `_lock`, so that a reconciliation
        # never blocks queries for longer than one of its statements. It also guards the
        # runs log and the time each case last ran.
        self._reconcile_lock = threading.Lock()
        self._last_reconciled: dict[str, float] = {}
        if reconcile_min_interval is None:
            raw = os.environ.get("COSMOS_RECONCILE_MIN_INTERVAL")
            reconcile_min_interval = float(raw) if raw else RECONCILE_MIN_INTERVAL
        self.reconcile_min_interval = max(reconcile_min_interval, 0.0)

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

    def dimension_values(
        self,
        dimension: str,
        principal: Principal,
        *,
        limit: int | None = None,
        filters: list[str | Filter] | None = None,
    ) -> dict[str, Any]:
        """Distinct values of a categorical or boolean dimension with their counts: the rows
        of the dimension's own model (after the caller's row filters and the request's
        ``filters``, expression strings or structured filters, DEC-50) carrying each value.
        At most ``MAX_DIMENSION_VALUES``; audited like a query (event `dimension_values`).
        A time dimension gets its date range instead (``time_range``); ``limit`` does not
        apply to it."""
        started = time.perf_counter()
        filters = list(filters or [])
        request: dict[str, Any] = {"dimensions": [dimension], "limit": limit}
        if filters:
            request["filters"] = [
                f if isinstance(f, str) else f.model_dump(mode="json", exclude_defaults=True)
                for f in filters
            ]
        context: dict[str, Any] = {"request": request, "principal": principal, "filters": filters}
        try:
            graph = self.engine.graph
            ref = graph.resolve_dimension(dimension)
            is_time = graph.dimension(ref).type == "time"
        except CosmosError as exc:
            self._audit(context, error=exc, event="dimension_values")
            raise
        if is_time:
            del request["limit"]
            return self._time_range(dimension, context, started)
        try:
            if limit is not None and limit < 1:
                raise QueryError("limit must be at least 1")
            limit = request["limit"] = min(limit or MAX_DIMENSION_VALUES, MAX_DIMENSION_VALUES)
            # One row more than the ceiling tells whether even the fetched list was cut.
            fetch = VALUES_FETCH_CEILING + 1
            compiled = self.engine.compile_values(
                dimension, principal, limit=fetch, filters=filters
            )
            context["compiled"] = compiled
            with self._lock:
                connector = self.engine.connector(compiled.data_source)
                table = connector.execute(compiled.sql, max_rows=fetch)
        except CosmosError as exc:
            self._audit(context, error=exc, event="dimension_values")
            raise
        column = compiled.columns[0].name
        pairs = [(row[column], int(row["value_count"])) for row in table.to_pylist()]
        # The top values are chosen here, not by each database's collation, so ties at the
        # cut-off keep the same values everywhere: count desc, nulls last, then the value.
        beyond_ceiling = len(pairs) > VALUES_FETCH_CEILING
        pairs = sorted(
            pairs[:VALUES_FETCH_CEILING],
            key=lambda p: (-p[1], p[0] is None, "" if p[0] is None else p[0]),
        )
        kept = pairs[:limit]
        returned = pa.table({"value": [v for v, _ in kept], "count": [n for _, n in kept]})
        evidence = self._values_evidence(
            compiled, {"count_of": f"rows of `{ref.model}`"}, returned, filters
        )
        self._audit(
            context, evidence=evidence, ms=time.perf_counter() - started, event="dimension_values"
        )
        return {
            "dimension": compiled.columns[0].member,
            "values": [{"value": jsonable(v), "count": n} for v, n in kept],
            "truncated": len(pairs) > limit or beyond_ceiling,
            "limit": limit,
            "evidence": evidence,
        }

    def _time_range(
        self, dimension: str, context: dict[str, Any], started: float
    ) -> dict[str, Any]:
        """The first and last day with rows of the dimension's model, and the number of
        distinct days, after the caller's row filters (DEC-24). Days are local days of the
        model (its business time zone); ISO dates; with no rows, null, null and 0."""
        principal: Principal = context["principal"]
        try:
            compiled = self.engine.compile_time_range(
                dimension, principal, filters=context["filters"]
            )
            context["compiled"] = compiled
            with self._lock:
                connector = self.engine.connector(compiled.data_source)
                table = connector.execute(compiled.sql)
        except CosmosError as exc:
            self._audit(context, error=exc, event="dimension_values")
            raise
        row = table.to_pylist()[0] if table.num_rows else {}
        first, last = _iso_day(row.get("min_day")), _iso_day(row.get("max_day"))
        days = int(row.get("distinct_days") or 0)
        returned = pa.table(
            {"min": [first], "max": [last], "distinct_days": [days]},
            schema=pa.schema(
                [("min", pa.string()), ("max", pa.string()), ("distinct_days", pa.int64())]
            ),
        )
        model = compiled.columns[0].member.split(".")[0]
        zone = self.engine.graph.project.timezone or "UTC"
        evidence = self._values_evidence(
            compiled,
            {"count_of": f"days with rows of `{model}`", "timezone": zone},
            returned,
            context["filters"],
        )
        self._audit(
            context, evidence=evidence, ms=time.perf_counter() - started, event="dimension_values"
        )
        return {
            "dimension": compiled.columns[0].member,
            "type": "time",
            "min": first,
            "max": last,
            "distinct_days": days,
            "evidence": evidence,
        }

    def _values_evidence(
        self,
        compiled: CompiledQuery,
        extra: dict[str, Any],
        returned: pa.Table,
        filters: list[str | Filter],
    ) -> dict[str, Any]:
        """The evidence of a `dimension_values` call: what ran, on what, under which filters."""
        versions = compiled.trace.rule_versions
        return {
            "query_id": new_query_id(),
            "as_of": compiled.as_of.isoformat() if compiled.as_of else None,
            "data_source": {"name": self._source(compiled), "dialect": compiled.dialect},
            "model_hash": self.evidence.model_hash,
            **extra,
            "filters": [describe_filter(f) for f in filters],
            "row_filters": merge_row_filters(compiled.trace.row_filters),
            "rules": [
                {
                    "name": r,
                    "effective_from": versions[r].isoformat() if versions.get(r) else None,
                }
                for r in sorted(compiled.trace.rules)
            ],
            "sql": {"text": compiled.sql, "hash": sql_hash(compiled.sql)},
            "result": {"row_count": returned.num_rows, "hash": result_hash(returned)},
        }

    # -- reconcile (DEC-45) ------------------------------------------------------

    def _cases(self) -> list[Any]:
        from cosmos_semantic.reconcile import load_zoo

        if self.cases_path is None:
            return []
        try:
            return load_zoo(self.cases_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # The type only: a parse error may quote the file.
            log.warning("cannot read the reconciliation cases: %s", type(exc).__name__)
            raise QueryError("the declared reconciliation cases cannot be read") from None

    def reconcile_cases(self) -> dict[str, Any]:
        """The reconciliation cases the operator declared: id and title only."""
        return {
            "available": self.cases_path is not None,
            "cases": [{"id": c.id, "title": c.title} for c in self._cases()],
        }

    def reconcile(
        self, case_id: str, principal: Principal, *, max_rows: int | None = None
    ) -> dict[str, Any]:
        """Runs one declared reconciliation case as the caller and records it as a partial
        check run, so health updates. Returns the outcome, the row diff (at most
        ``MAX_RECONCILE_DIFF_ROWS`` rows), the diagnosis and the evidence; audited like a
        query (event `reconcile`).

        Refused (PolicyError) when a row filter applies to the governed side, or when the
        case's SQL may read a table a row filter guards: row filters cannot be applied to
        hand-written SQL. An unknown id is a NotFoundError whose message does not repeat it.
        A case runs at most once per ``reconcile_min_interval`` seconds
        (``COSMOS_RECONCILE_MIN_INTERVAL``, 60 by default); a call sooner is a
        RateLimitError and records no run."""
        from cosmos_semantic.checks import append_run, run_checks
        from cosmos_semantic.reconcile import row_filter_conflicts

        started = time.perf_counter()
        shown = case_id if CASE_ID_RE.fullmatch(case_id) else "<invalid>"
        context: dict[str, Any] = {"request": {"case": shown}, "principal": principal}
        graph = self.engine.graph
        limit = min(max_rows or MAX_RECONCILE_DIFF_ROWS, MAX_RECONCILE_DIFF_ROWS)
        try:
            if max_rows is not None and max_rows < 1:
                raise QueryError("max_rows must be at least 1")
            case = next((c for c in self._cases() if c.id == case_id), None)
            if case is None:
                raise NotFoundError(
                    "no reconciliation case with that id is declared; list the declared cases first"
                )
            request = case.request
            context["request"] = {
                "case": case.id,
                **request.model_dump(mode="json", exclude_defaults=True),
            }
            # The caller's policies: personal data, and the row filters that apply to them.
            compiled = self.engine.compile(request, principal)
            context["compiled"] = compiled
            if compiled.trace.row_filters:
                models = sorted({f["model"] for f in compiled.trace.row_filters})
                raise PolicyError(
                    f"case `{case.id}` reads `{models[0]}`, which has row filters; they cannot "
                    "be applied to the case's hand-written SQL, so it is not run through the "
                    "API or MCP (run `cosmos check` as an operator)"
                )
            with self._lock:  # creating the connection only
                connector = self.engine.connector(compiled.data_source)
            conflicts = row_filter_conflicts(case, graph, connector.dialect)
            if conflicts:
                raise PolicyError(
                    f"case `{case.id}` is not run through the API or MCP: {conflicts[0]}; "
                    "row filters cannot be applied to its hand-written SQL"
                )
            reports: dict[str, dict[str, Any]] = {}
            with self._reconcile_lock:
                now = time.monotonic()
                last = self._last_reconciled.get(case.id)
                if last is not None and now - last < self.reconcile_min_interval:
                    wait = max(1, round(self.reconcile_min_interval - (now - last)))
                    raise RateLimitError(
                        f"case `{case.id}` ran less than {self.reconcile_min_interval:g} "
                        f"seconds ago; try again in {wait} seconds"
                    )
                self._last_reconciled[case.id] = now
                run = run_checks(
                    graph,
                    model_hash=self.evidence.model_hash,
                    connector=_LockedConnector(connector, self._lock),
                    source=self._source(compiled),
                    cases=[case],
                    principal=principal,
                    reports=reports,
                )
                # A run of one case: health keeps the other cases of the latest full run.
                run["partial"] = True
                run["channel"] = principal.channel
                if self.health.runs_path is not None:
                    append_run(self.health.runs_path, run)
        except CosmosError as exc:
            self._audit(context, error=exc, event="reconcile")
            raise
        item = run["reconciliations"][0]
        report = reports.get(case.id)
        if report is None:  # the case errored (connector, SQL); the run recorded it
            # The error type only: a database message may quote the hand-written SQL.
            kind = (item.get("detail") or "CosmosError").split(":")[0]
            error = QueryError(f"reconciliation case `{case.id}` could not run ({kind})")
            self._audit(context, error=error, ms=time.perf_counter() - started, event="reconcile")
            raise error
        diff = report["diff"]
        returned = [_diff_row(row) for row in diff[:limit]]
        versions = compiled.trace.rule_versions
        evidence = {
            "query_id": new_query_id(),
            "run_id": run["run_id"],
            "at": run["at"],
            "as_of": report["cosmos"]["as_of"],
            "data_source": {"name": self._source(compiled), "dialect": compiled.dialect},
            "model_hash": self.evidence.model_hash,
            "formula": report["cosmos"]["formula"],
            # None apply: a case with row filters is refused.
            "row_filters": [],
            "rules": [
                {
                    "name": r,
                    "effective_from": versions[r].isoformat() if versions.get(r) else None,
                }
                for r in sorted(compiled.trace.rules)
            ],
            "sql": {
                "governed_hash": report["cosmos"]["sql_hash"],
                "legacy_hash": report["legacy"]["sql_hash"],
            },
            "tolerance": report["tolerance"],
            "result": {
                "row_count": len(returned),
                "hash": sql_hash(json.dumps(returned, sort_keys=True, default=str)),
            },
        }
        self._audit(context, evidence=evidence, ms=time.perf_counter() - started, event="reconcile")
        metric = report["metric"]
        try:
            states = self.health.states()
            health = states.get(f"metric:{metric}") or states.get(f"measure:{metric}")
        except (OSError, ValueError):
            health = None
        return {
            "case": {"id": case.id, "title": case.title},
            "outcome": item["outcome"],
            "status": report["status"],
            "expected": item.get("expected"),
            "metric": metric,
            "request": report["request"],
            "rows": report["rows"],
            "totals": report["totals"],
            "tolerance": report["tolerance"],
            "diff": returned,
            "diff_truncated": report["diff_truncated"] or len(diff) > limit,
            "tolerated": [_diff_row(row) for row in report["tolerated"]],
            "causes": report["causes"],
            "health": health,
            "recorded": self.health.runs_path is not None,
            "evidence": evidence,
        }

    def is_admin(self, principal: Principal) -> bool:
        # `--allow-anonymous` is for local development; the person at the keyboard sees all.
        if principal.local_anonymous:
            return True
        return principal.subject is not None and principal.subject in self.admins

    # -- explain -----------------------------------------------------------------

    def explain(self, name: str) -> dict[str, Any]:
        """The catalog's explanation plus `health` (state and reasons, as the console shows
        them) for the members that have one: metrics, measures, rules and processes, and the
        member that defines a concept. Health holds no audit data; it is the same for everyone."""
        detail = self.catalog.explain(name)
        try:
            states = self.health.states()
        except (OSError, ValueError) as exc:  # unreadable runs log; JSON errors are ValueErrors
            # The type only: the message of a parse error may quote the file.
            log.warning("cannot read the check runs log: %s", type(exc).__name__)
            g = self.engine.graph
            unreadable = {
                "state": "unchecked",
                "reasons": [{"code": "runs_log_unreadable"}],
                "last_reconciled_at": None,
                "last_tests_run_at": None,
            }
            keys = [f"rule:{n}" for n in g.rules] + [f"metric:{n}" for n in g.metrics]
            keys += [f"measure:{n}" for n in g.measure_owner]
            keys += [f"process:{p.name}" for p in g.project.processes]
            states = {k: unreadable for k in keys}

        def attach(item: dict[str, Any]) -> None:
            health = states.get(f"{item.get('kind')}:{item.get('name')}")
            if health is not None:
                item["health"] = health
            if isinstance(item.get("defined_by"), dict):
                attach(item["defined_by"])

        attach(detail)
        return detail

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
                # Only query records replay; `dimension_values` records are not queries.
                if record.get("query_id") == query_id and record.get("event") == "query":
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
        event: str = "query",
    ) -> None:
        """Every query is recorded: who asked, what, the generated SQL, the data source and
        the hashes needed to replay it."""
        request: QueryRequest | dict[str, Any] = context["request"]
        principal: Principal = context["principal"]
        compiled: CompiledQuery | None = context.get("compiled")
        record = {
            "event": event,
            "query_id": evidence["query_id"] if evidence else None,
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "principal": {
                "subject": principal.subject,
                "channel": principal.channel,
                "user_context": principal.user,
            },
            "request": request
            if isinstance(request, dict)
            else request.model_dump(mode="json", exclude_defaults=True),
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
