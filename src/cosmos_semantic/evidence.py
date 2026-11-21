"""Evidence envelope (ADR 0008): every answer carries what is needed to verify it.

The envelope states which definitions, rules, policies and data source produced a number,
under which model version and reference date, plus hashes that let anyone replay the query and
check that it still returns the same result. Traceability is not proof of correctness: the
envelope reports rule test results and caveats instead of claiming more than it knows.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
import uuid
from functools import cached_property
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import pyarrow as pa

from cosmos_semantic.compiler import SemanticGraph
from cosmos_semantic.expressions import REF_RE
from cosmos_semantic.ontology import Glossary, rule_logic, run_rule_tests
from cosmos_semantic.planner import CompiledQuery, Principal, QueryRequest, QueryTrace
from cosmos_semantic.spec import (
    CumulativeMetric,
    DerivedMetric,
    RatioMetric,
    SimpleMetric,
)

REPRODUCIBILITY_CAVEAT = (
    "Replaying this query with the same model hash and as_of returns the same result only if "
    "the source data has not changed."
)


def engine_version() -> str:
    try:
        return version("cosmos-semantic")
    except PackageNotFoundError:
        return "unknown"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_hash(graph: SemanticGraph) -> str:
    """Hash of the compiled project's content; any change to the model changes it."""
    data = graph.project.model_dump(mode="json")
    return _sha256(json.dumps(data, sort_keys=True, ensure_ascii=False))


def sql_hash(sql: str) -> str:
    return _sha256(sql)


def _canonical(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, decimal.Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        return float(f"{value:.10g}")
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).isoformat()
    return value


def result_hash(table: pa.Table) -> str:
    """Order-independent hash of a result, stable across drivers of the same database."""
    rows = [[_canonical(row[name]) for name in table.column_names] for row in table.to_pylist()]
    rows.sort(key=lambda r: json.dumps(r, default=str))
    return _sha256(json.dumps({"columns": table.column_names, "rows": rows}, default=str))


class EvidenceBuilder:
    """Builds envelopes for one compiled model; rule tests run once per model version."""

    def __init__(self, graph: SemanticGraph) -> None:
        self.graph = graph
        self.glossary = Glossary(graph.project)

    @cached_property
    def model_hash(self) -> str:
        return model_hash(self.graph)

    @cached_property
    def rule_tests(self) -> dict[tuple[str, dt.date | None], dict[str, Any]]:
        """Test status per rule version."""
        status: dict[tuple[str, dt.date | None], dict[str, Any]] = {}
        for result in run_rule_tests(self.graph.project):
            entry = status.setdefault(
                (result.rule, result.version), {"total": 0, "passed": 0, "failures": []}
            )
            entry["total"] += 1
            entry["passed"] += result.passed
            if not result.passed:
                entry["failures"].append(result.detail)
        return status

    def build(
        self,
        *,
        request: QueryRequest,
        compiled: CompiledQuery,
        principal: Principal,
        data_source: str | None,
        table: pa.Table,
        query_id: str | None = None,
    ) -> dict[str, Any]:
        g = self.graph
        trace = compiled.trace
        caveats = [REPRODUCIBILITY_CAVEAT]
        metrics = [
            self._metric(name) | {"time": self.time(name, request, trace)}
            for name in request.metrics
        ]
        for item in trace.one_to_many:
            what = ", ".join(item["dimensions"]) or "filters " + ", ".join(item["filters"])
            caveats.append(
                f"{what} is attributed to `{item['base']}` through `{item['via']}`: each "
                f"`{item['base']}` row counts once in every value it relates to, so totals "
                "across those values can exceed the overall total."
            )
        if trace.metric_filters:
            kept = " AND ".join(f["text"] for f in trace.metric_filters)
            caveats.append(
                f"Only the rows where {kept} are returned (metric_filters, applied after "
                "aggregation at the requested grain): a sum of the returned rows is not the "
                "metric's total. A comparison with a NULL value keeps no row."
            )
        rules = []
        for name, uses in sorted(trace.rules.items()):
            rule = g.rules[name]
            version = rule.version_at(compiled.rules_as_of or dt.date.today())
            tests = self.rule_tests.get(
                (name, version.effective_from), {"total": 0, "passed": 0, "failures": []}
            )
            status = (
                "untested"
                if tests["total"] == 0
                else "passed"
                if tests["passed"] == tests["total"]
                else "failing"
            )
            if status != "passed":
                caveats.append(f"Rule `{name}` has {status} tests.")
            if not rule.owner or not rule.effective_from:
                caveats.append(f"Rule `{name}` has no owner or effective date.")
            current = version.effective_from == rule.effective_from
            if not current:
                caveats.append(
                    f"Rule `{name}` used its version effective from {version.effective_from}, "
                    f"not the current one (effective from {rule.effective_from})."
                )
            rules.append(
                {
                    "name": name,
                    "kind": rule.kind,
                    "used_as": sorted(uses),
                    "owner": rule.owner,
                    "effective_from": version.effective_from.isoformat()
                    if version.effective_from
                    else None,
                    "current_version": current,
                    "versions": len(rule.history) + 1,
                    "logic": rule_logic(version),
                    "tests": {"status": status, "passed": tests["passed"], "total": tests["total"]},
                    "concepts": [c.concept for c in self.glossary.for_target(f"rule:{name}")],
                }
            )
        dimensions = []
        for column in compiled.columns:
            if column.kind != "dimension":
                continue
            ref = g.resolve_dimension(column.member)
            dim = g.dimension(ref)
            dimensions.append(
                {
                    "member": column.member,
                    "column": column.name,
                    "type": dim.type,
                    "description": dim.description,
                    "rule": ref.name if ref.name in trace.rules else None,
                }
            )
        return {
            "query_id": query_id or new_query_id(),
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "engine_version": engine_version(),
            "model_hash": self.model_hash,
            "as_of": compiled.as_of.isoformat() if compiled.as_of else None,
            "rules_as_of": compiled.rules_as_of.isoformat() if compiled.rules_as_of else None,
            "timezone": g.project.timezone or "UTC",
            "data_source": {"name": data_source, "dialect": compiled.dialect},
            "request": request.model_dump(mode="json", exclude_defaults=True),
            "principal": {
                "subject": principal.subject,
                "channel": principal.channel,
                "user_context": principal.user,
            },
            "metrics": metrics,
            "dimensions": dimensions,
            "rules": rules,
            # Conditions on metric values (DEC-29), after the row filters and aggregation.
            "metric_filters": trace.metric_filters,
            "policies": {
                "row_filters": merge_row_filters(trace.row_filters),
                "sensitive_data_blocked_for_agents": principal.is_agent,
            },
            "lineage": {
                "models": compiled.models,
                "joins": trace.joins,
                "one_to_many": trace.one_to_many,
            },
            "sql": {"text": compiled.sql, "hash": sql_hash(compiled.sql)},
            "result": {
                "row_count": table.num_rows,
                "columns": table.column_names,
                "hash": result_hash(table),
            },
            "caveats": caveats,
        }

    # -- definitions -----------------------------------------------------------

    def _metric(self, name: str) -> dict[str, Any]:
        g = self.graph
        concepts = [c.concept for c in self.glossary.for_target(f"metric:{name}")]
        if name in g.measure_owner:
            owner = g.measure_owner[name]
            measure = g.models[owner].measures[name]
            return {
                "name": name,
                "type": "measure",
                "description": measure.description,
                "formula": self.formula(name),
                "measures": [self._measure(name)],
                "concepts": concepts,
            }
        metric = g.metrics[name]
        entry = {
            "name": name,
            "type": metric.type,
            "description": metric.description,
            "formula": self.formula(name),
            "measures": [self._measure(m) for m in sorted(self._leaf_measures(name))],
            "concepts": concepts,
        }
        if metric.reconciliation is not None:
            # The tolerance reconciliation and check runs use for this metric (DEC-39). Only
            # present when the metric declares one, so other envelopes keep their shape.
            abs_tol, rel_tol = metric.reconciliation.tolerances()
            entry["reconciliation"] = {"abs_tolerance": abs_tol, "rel_tolerance": rel_tol}
        return entry

    def time(
        self, name: str, request: QueryRequest, trace: QueryTrace | None = None
    ) -> dict[str, Any] | None:
        """The time semantics of a cumulative or offset metric (DEC-37), None for the others.

        ``kind`` is ``grain_to_date`` (``period`` null for a running total over all time),
        ``window`` (``window`` as written and its ``days``) or ``offset`` (each input read at
        an earlier period, through nested metrics too). ``time_dimension`` and ``query_grain``
        are the request's time dimension and its grain. ``filters_applied`` says when the
        request's ``filters`` and the row filters restrict the rows: ``before`` the window
        or shift, so a filter on the time dimension also leaves out the earlier periods a
        window or offset would read; or, for a rolling window (DEC-34), ``time_after``: the
        filters on the time dimension choose the output days and the window still reads the
        N - 1 days before them, while the other filters restrict the facts first. A rolling
        window is ``before`` only when ``trace`` (the query's) shows a filter on its time
        dimension whose shape the planner could not split.
        """
        g = self.graph
        metric = g.metrics.get(name) if name not in g.measure_owner else None
        if isinstance(metric, CumulativeMetric):
            if metric.window:
                time: dict[str, Any] = {
                    "kind": "window",
                    "window": metric.window,
                    "days": metric.window_days(),
                }
            else:
                time = {"kind": "grain_to_date", "period": metric.grain_to_date}
        else:
            shifts = self._shifts(name)
            if not shifts:
                return None
            time = {"kind": "offset", "shifts": shifts}
        refs = [g.resolve_dimension(d) for d in request.dimensions]
        times = [r for r in refs if g.dimension(r).type == "time"]
        ref = times[0] if len(times) == 1 else None
        applied = "before"
        if time["kind"] == "window":
            split = trace.window_filters.get(name) if trace else None
            applied = "before" if split and split["before"] else "time_after"
        return time | {
            "time_dimension": request.dimensions[refs.index(ref)] if ref else None,
            "query_grain": ref.grain if ref else None,
            "filters_applied": applied,
        }

    def _shifts(self, name: str, seen: frozenset[str] = frozenset()) -> list[dict[str, str]]:
        """The inputs read at an earlier period under ``name`` (DEC-18), each with the metric
        that reads it, the input and the offset: ``name``'s own shifts first, in ``expr``
        order, then those of the metrics it references, depth-first in ``expr`` order, each
        listed once."""
        g = self.graph
        if name in seen or name in g.measure_owner or name not in g.metrics:
            return []
        metric = g.metrics[name]
        seen = seen | {name}
        refs: list[str] = []
        own: list[dict[str, str]] = []
        if isinstance(metric, RatioMetric):
            refs = [metric.numerator, metric.denominator]
        elif isinstance(metric, DerivedMetric):
            refs = list(dict.fromkeys(REF_RE.findall(metric.expr)))
            offsets = metric.offsets()
            own = [
                {"metric": name, "input": ref, "offset": offsets[ref]}
                for ref in refs
                if ref in offsets
            ]
        out = own
        for ref in refs:
            for shift in self._shifts(ref, seen):
                if shift not in out:
                    out.append(shift)
        return out

    def _measure(self, name: str) -> dict[str, Any]:
        owner = self.graph.measure_owner[name]
        measure = self.graph.models[owner].measures[name]
        return {
            "name": name,
            "model": owner,
            "table": self.graph.models[owner].spec.table,
            "agg": measure.agg,
            "expr": measure.expr,
        }

    def _leaf_measures(self, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        g = self.graph
        if name in g.measure_owner:
            return {name}
        if name in seen or name not in g.metrics:
            return set()
        metric = g.metrics[name]
        refs: list[str] = []
        if isinstance(metric, SimpleMetric | CumulativeMetric):
            refs = [metric.measure]
        elif isinstance(metric, RatioMetric):
            refs = [metric.numerator, metric.denominator]
        elif isinstance(metric, DerivedMetric):
            refs = REF_RE.findall(metric.expr)
        out: set[str] = set()
        for ref in refs:
            out |= self._leaf_measures(ref, seen | {name})
        return out

    def formula(self, name: str, seen: frozenset[str] = frozenset()) -> str:
        """The metric's definition expanded down to aggregations over model columns."""
        g = self.graph
        if name in g.measure_owner:
            owner = g.measure_owner[name]
            m = g.models[owner].measures[name]
            if m.expr is None and m.agg == "count":
                return f"COUNT({owner}.*)"
            from cosmos_semantic.expressions import qualify

            column = qualify(m.expr or m.name, owner, "postgres").sql()
            if m.agg == "count_distinct":
                return f"COUNT(DISTINCT {column})"
            return f"{m.agg.upper()}({column})"
        if name in seen:
            return name
        metric = g.metrics[name]
        seen = seen | {name}
        if isinstance(metric, SimpleMetric):
            inner = self.formula(metric.measure, seen)
            return f"{inner} WHERE {metric.filter}" if metric.filter else inner
        if isinstance(metric, RatioMetric):
            num = self.formula(metric.numerator, seen)
            den = self.formula(metric.denominator, seen)
            where = f" WHERE {metric.filter}" if metric.filter else ""
            return f"({num}) / ({den}){where}"
        if isinstance(metric, DerivedMetric):
            offsets = metric.offsets()

            def part(match) -> str:
                ref = match.group(1)
                inner = f"({self.formula(ref, seen)})"
                if ref in offsets:
                    return f"{inner} {offsets[ref]} earlier"
                return inner

            return REF_RE.sub(part, metric.expr)
        if isinstance(metric, CumulativeMetric):
            inner = self.formula(metric.measure, seen)
            if metric.window:
                return f"{inner} over the {metric.window} ending on each day"
            period = metric.grain_to_date or "all time"
            owner = g.measure_owner.get(metric.measure)
            agg = g.models[owner].measures[metric.measure].agg if owner else None
            running = {"min": "running minimum", "max": "running maximum"}.get(agg, "running total")
            return f"{running} of {inner} within each {period}"
        return name


def merge_row_filters(applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per row filter, listing every CTE where it was applied."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in applied:
        key = (item["model"], item["name"])
        entry = merged.setdefault(
            key, {k: v for k, v in item.items() if k != "applied_in"} | {"applied_in": []}
        )
        if item["applied_in"] not in entry["applied_in"]:
            entry["applied_in"].append(item["applied_in"])
    return list(merged.values())


def new_query_id() -> str:
    return f"q_{uuid.uuid4().hex[:20]}"
