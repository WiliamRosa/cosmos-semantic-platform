"""Metric reconciliation (ADR 0008): compare a governed metric with an existing definition and
explain why the numbers differ.

Two comparisons are supported:

- **Metric versus SQL:** a Cosmos request against a legacy SQL query that returns the same
  dimensions and one value column (matched by name, or by position).
- **Model versus model:** the same request on two semantic models, for example an imported dbt
  or Cube model against the Cosmos model.

When the numbers differ, the engine runs *probes*: variants of the governed query, each one
reproducing a classic mistake. A probe that matches the other side on every row is reported as
a likely cause. Probes are evidence, not proof: two different causes can produce the same
numbers, and every cause that fits is listed.

Running arbitrary SQL can read data that row filters hide, so SQL reconciliation is an
operator tool (CLI), not an API or MCP feature.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cosmos_semantic.compiler import SemanticGraph, compile_project
from cosmos_semantic.connectors import Connector
from cosmos_semantic.errors import CosmosError, QueryError
from cosmos_semantic.evidence import EvidenceBuilder, _canonical, sql_hash
from cosmos_semantic.expressions import REF_RE, remap_sql
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.reference import server_today
from cosmos_semantic.spec import Dimension, Measure, SimpleMetric

ABS_TOL = 1e-6
REL_TOL = 1e-9
MAX_CATEGORY_VALUES = 8
TIME_SHIFTS_HOURS = (1, 2, 3, 4, 5, -1, -2, -3, -4, -5)
PROBE_PREFIX = "probe__"

Rows = dict[tuple, Any]


@dataclass
class Cause:
    code: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "summary": self.summary, "detail": self.detail}


def _key(values: list[Any]) -> tuple:
    return tuple(_canonical(v) for v in values)


def _number(value: Any) -> float | None:
    value = _canonical(value)
    if value is None or isinstance(value, str):
        return None
    return float(value)


def _same(a: Any, b: Any) -> bool:
    x, y = _number(a), _number(b)
    if x is None or y is None:
        return _canonical(a) == _canonical(b)
    return math.isclose(x, y, rel_tol=REL_TOL, abs_tol=ABS_TOL)


def rows_match(a: Rows, b: Rows) -> bool:
    return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)


def to_rows(records: list[dict[str, Any]], dims: int) -> Rows:
    """Rows keyed by the first ``dims`` columns, valued by the next one (order of the query)."""
    out: Rows = {}
    for record in records:
        values = list(record.values())
        if len(values) < dims + 1:
            raise QueryError(f"expected {dims} dimension columns and one value column")
        out[_key(values[:dims])] = values[dims]
    return out


def _align(records: list[dict[str, Any]], dim_names: list[str], value_name: str) -> Rows:
    """Matches columns by name when they exist, otherwise by position."""
    if records and all(n in records[0] for n in [*dim_names, value_name]):
        records = [{n: r[n] for n in [*dim_names, value_name]} for r in records]
    elif records and len(records[0]) != len(dim_names) + 1:
        raise QueryError(
            f"the legacy SQL must return {len(dim_names)} dimension columns and one value "
            f"column (named {[*dim_names, value_name]} or in that order); it returned "
            f"{list(records[0])}"
        )
    return to_rows(records, len(dim_names))


class Reconciler:
    def __init__(
        self,
        graph: SemanticGraph,
        connector: Connector,
        principal: Principal | None = None,
    ) -> None:
        self.graph = graph
        self.connector = connector
        self.principal = principal or Principal()
        self.evidence = EvidenceBuilder(graph)

    # -- running queries ---------------------------------------------------------

    def _planner(self, graph: SemanticGraph | None = None) -> Planner:
        return Planner(
            graph or self.graph, self.connector.dialect, self.connector.config.schema_map
        )

    def _run(self, request: QueryRequest, graph: SemanticGraph | None = None) -> tuple[Rows, Any]:
        compiled = self._planner(graph).plan(request, self.principal)
        records = self.connector.execute(compiled.sql, max_rows=200_000).to_pylist()
        return to_rows(records, len(request.dimensions)), compiled

    def _run_sql(self, sql: str) -> list[dict[str, Any]]:
        sql = remap_sql(sql, self.connector.dialect, self.connector.config.schema_map)
        return self.connector.execute(sql, max_rows=200_000).to_pylist()

    # -- public entry points -----------------------------------------------------

    def against_sql(self, request: QueryRequest, legacy_sql: str) -> dict[str, Any]:
        request = self._prepare(request)
        cosmos, compiled = self._run(request)
        dim_names = [c.name for c in compiled.columns if c.kind == "dimension"]
        legacy = _align(self._run_sql(legacy_sql), dim_names, request.metrics[0])
        report = self._compare(request, cosmos, legacy, compiled)
        report["legacy"] = {"kind": "sql", "sql_hash": sql_hash(legacy_sql)}
        return report

    def against_model(self, request: QueryRequest, other: SemanticGraph) -> dict[str, Any]:
        request = self._prepare(request)
        cosmos, compiled = self._run(request)
        try:
            legacy, _ = self._run(request, other)
        except CosmosError as exc:
            raise QueryError(f"the other model cannot answer this request: {exc}") from exc
        report = self._compare(request, cosmos, legacy, compiled)
        name = request.metrics[0]
        other_builder = EvidenceBuilder(other)
        report["legacy"] = {
            "kind": "model",
            "model_hash": other_builder.model_hash,
            "formula": other_builder.formula(name)
            if name in other.metrics or name in other.measure_owner
            else None,
        }
        report["definitions_differ"] = report["legacy"]["formula"] != report["cosmos"]["formula"]
        return report

    def _prepare(self, request: QueryRequest) -> QueryRequest:
        if len(request.metrics) != 1:
            raise QueryError("reconciliation compares one metric at a time")
        if request.as_of is None:
            # Legacy SQL runs on the server's clock; the governed query must use the same "today".
            request = request.model_copy(update={"as_of": server_today(self.connector)})
        return request

    # -- comparison --------------------------------------------------------------

    def _compare(self, request, cosmos: Rows, legacy: Rows, compiled) -> dict[str, Any]:
        matched, differing = [], []
        for key in sorted(set(cosmos) & set(legacy), key=str):
            (matched if _same(cosmos[key], legacy[key]) else differing).append(key)
        only_cosmos = sorted(set(cosmos) - set(legacy), key=str)
        only_legacy = sorted(set(legacy) - set(cosmos), key=str)
        status = "match" if not (differing or only_cosmos or only_legacy) else "divergent"
        total_c = sum(_number(v) or 0 for v in cosmos.values())
        total_l = sum(_number(v) or 0 for v in legacy.values())
        name = request.metrics[0]
        report: dict[str, Any] = {
            "status": status,
            "metric": name,
            "request": request.model_dump(mode="json", exclude_defaults=True),
            "rows": {
                "matched": len(matched),
                "differing": len(differing),
                "only_in_cosmos": len(only_cosmos),
                "only_in_legacy": len(only_legacy),
            },
            "totals": {
                "cosmos": total_c,
                "legacy": total_l,
                "difference": total_l - total_c,
                "difference_pct": (total_l - total_c) / total_c * 100 if total_c else None,
            },
            "examples": [
                {"key": list(k), "cosmos": cosmos[k], "legacy": legacy[k]} for k in differing[:5]
            ]
            + [{"key": list(k), "cosmos": cosmos[k], "legacy": None} for k in only_cosmos[:3]]
            + [{"key": list(k), "cosmos": None, "legacy": legacy[k]} for k in only_legacy[:3]],
            "cosmos": {
                "formula": self.evidence.formula(name),
                "sql_hash": sql_hash(compiled.sql),
                "model_hash": self.evidence.model_hash,
                "as_of": compiled.as_of.isoformat() if compiled.as_of else None,
            },
            "causes": [],
        }
        if status == "divergent":
            report["causes"] = [c.as_dict() for c in self.diagnose(request, cosmos, legacy)]
            if not report["causes"]:
                report["causes"] = [
                    Cause(
                        "unexplained",
                        "No probe reproduces the legacy numbers; compare the definitions by hand.",
                    ).as_dict()
                ]
        return report

    # -- diagnosis ---------------------------------------------------------------

    def diagnose(self, request: QueryRequest, cosmos: Rows, legacy: Rows) -> list[Cause]:
        causes: list[Cause] = []
        priority = {"different_metric": 1, "unexplained": 2}
        for probe in (
            self._probe_value_transform,
            self._probe_missing_null_group,
            self._probe_other_metric,
            self._probe_category_subset,
            self._probe_derived,
        ):
            try:
                causes.extend(probe(request, cosmos, legacy))
            except CosmosError:
                continue
        # Specific mechanisms first; "it is another metric" is the least specific explanation.
        return sorted(causes, key=lambda c: priority.get(c.code, 0))

    def _probe_value_transform(self, request, cosmos: Rows, legacy: Rows) -> list[Cause]:
        """Integer division, truncated averages and rounding are visible in the values alone."""
        if cosmos.keys() != legacy.keys():
            return []
        pairs = [(_number(cosmos[k]), _number(legacy[k])) for k in cosmos]
        if any(c is None or v is None for c, v in pairs):
            return []
        if all(math.isclose(math.trunc(c), v, abs_tol=ABS_TOL) for c, v in pairs):
            return [
                Cause(
                    "truncation",
                    "The legacy values are the governed values truncated to integers: integer "
                    "division or an average over an integer column.",
                )
            ]
        for digits in range(0, 5):
            if all(math.isclose(round(c, digits), v, abs_tol=ABS_TOL) for c, v in pairs):
                return [
                    Cause(
                        "rounding",
                        f"The legacy values are the governed values rounded to {digits} decimals.",
                        {"decimals": digits},
                    )
                ]
        return []

    def _probe_missing_null_group(self, request, cosmos: Rows, legacy: Rows) -> list[Cause]:
        only_cosmos = set(cosmos) - set(legacy)
        if not only_cosmos or set(legacy) - set(cosmos):
            return []
        if not all(any(v is None for v in key) for key in only_cosmos):
            return []
        if not all(_same(cosmos[k], legacy[k]) for k in legacy):
            return []
        dims = [
            request.dimensions[i]
            for i in range(len(request.dimensions))
            if any(key[i] is None for key in only_cosmos)
        ]
        return [
            Cause(
                "null_group_dropped",
                "The legacy query drops the rows whose "
                f"{', '.join(dims)} is null: an INNER JOIN without a match, or an IS NOT NULL "
                "filter. The governed metric keeps them in their own group.",
                {"dimensions": dims, "missing_rows": len(only_cosmos)},
            )
        ]

    def _probe_other_metric(self, request, cosmos: Rows, legacy: Rows) -> list[Cause]:
        """The legacy query may compute a different metric of the same model."""
        g = self.graph
        name = request.metrics[0]
        # Cumulative metrics equal plain totals at coarse grains; listing them only confuses.
        candidates = sorted(
            (set(g.metrics) | set(g.measure_owner))
            - {name}
            - {m for m, spec in g.metrics.items() if spec.type == "cumulative"}
        )
        planner = self._planner()
        compatible = []
        for candidate in candidates:
            try:
                planner.plan(request.model_copy(update={"metrics": [candidate]}), self.principal)
                compatible.append(candidate)
            except CosmosError:
                continue
        causes = []
        for candidate in compatible:
            rows, _ = self._run(request.model_copy(update={"metrics": [candidate]}))
            if rows_match(rows, legacy):
                causes.append(
                    Cause(
                        "different_metric",
                        f"The legacy query computes `{candidate}` "
                        f"({self.evidence.formula(candidate)}), not `{name}`.",
                        {"metric": candidate, "formula": self.evidence.formula(candidate)},
                    )
                )
        return causes

    def _base_measure(self, name: str) -> tuple[str, str | None] | None:
        """The single additive measure behind a metric, and the metric's own filter."""
        g = self.graph
        if name in g.measure_owner:
            return name, None
        metric = g.metrics.get(name)
        if isinstance(metric, SimpleMetric):
            return metric.measure, metric.filter
        return None

    def _probe_category_subset(self, request, cosmos: Rows, legacy: Rows) -> list[Cause]:
        """Legacy includes or excludes some categories (for example, canceled orders)."""
        base = self._base_measure(request.metrics[0])
        if base is None:
            return []
        measure, own_filter = base
        g = self.graph
        owner = g.measure_owner[measure]
        if g.models[owner].measures[measure].agg not in ("sum", "count"):
            return []
        causes = []
        for dim_name, dim in g.models[owner].dimensions.items():
            if dim.type not in ("categorical", "boolean") or dim.sensitivity:
                continue
            member = f"{owner}.{dim_name}"
            if member in request.dimensions:
                continue
            split_request = request.model_copy(
                update={"metrics": [measure], "dimensions": [*request.dimensions, member]}
            )
            try:
                split, _ = self._run(split_request)
            except CosmosError:
                continue
            by_value: dict[Any, Rows] = {}
            for key, value in split.items():
                by_value.setdefault(key[-1], {})[key[:-1]] = value
            values = sorted(by_value, key=str)
            if not 1 < len(values) <= MAX_CATEGORY_VALUES:
                continue
            for size in range(1, len(values) + 1):
                for subset in itertools.combinations(values, size):
                    combined: Rows = {}
                    for v in subset:
                        for key, value in by_value[v].items():
                            combined[key] = (combined.get(key) or 0) + (_number(value) or 0)
                    if rows_match(combined, legacy):
                        included = [str(v) for v in subset]
                        if len(subset) == len(values):
                            # Every category included means "no filter", which is only news
                            # for the dimension the metric's own filter uses.
                            if not own_filter or dim_name not in REF_RE.findall(own_filter) + [
                                r.split(".")[-1] for r in REF_RE.findall(own_filter)
                            ]:
                                break
                            summary = (
                                f"The legacy query does not apply `{own_filter}`: it includes "
                                f"every value of {member} ({', '.join(included)})."
                            )
                        else:
                            summary = (
                                f"The legacy query counts {member} in {included}"
                                + (
                                    f", while `{request.metrics[0]}` applies `{own_filter}`"
                                    if own_filter
                                    else ""
                                )
                                + "."
                            )
                        causes.append(
                            Cause(
                                "filter_difference",
                                summary,
                                {
                                    "dimension": member,
                                    "legacy_includes": included,
                                    "all_values": [str(v) for v in values],
                                    "governed_filter": own_filter,
                                },
                            )
                        )
                        break
                else:
                    continue
                break
        return causes

    def _probe_derived(self, request, cosmos: Rows, legacy: Rows) -> list[Cause]:
        """Probes that need extra members: fan-out, inner-join drops, row counts instead of
        distinct counts, and time zone shifts. They are added to a copy of the model."""
        g = self.graph
        base = self._base_measure(request.metrics[0])
        project = g.project.model_copy(deep=True)
        models = {m.name: m for m in project.semantic_models}
        probes: list[tuple[str, QueryRequest, Cause]] = []

        if base is not None:
            measure_name, own_filter = base
            owner = g.measure_owner[measure_name]
            measure = g.models[owner].measures[measure_name]
            parent_column = measure.expr or measure.name
            qualified_filter = self._qualify_refs(own_filter, owner) if own_filter else None
            children = [e.source for edges in g.edges.values() for e in edges if e.target == owner]
            for child in sorted(set(children)):
                anchor = f"{PROBE_PREFIX}{owner}_one"
                if not any(d.name == anchor for d in models[owner].dimensions):
                    models[owner].dimensions.append(Dimension(name=anchor, type="number", expr="1"))
                force_join = f"{{{owner}.{anchor}}} = 1"
                key = g.models[owner].entities
                pk = next((e for e in key.values() if e.type == "primary"), None)
                if measure.agg in ("sum", "count"):
                    name = f"{PROBE_PREFIX}fanout_{child}"
                    expr = (
                        f"{owner}.{parent_column}"
                        if measure.expr or measure.agg != "count"
                        else f"{owner}.{(pk.expr or pk.name) if pk else parent_column}"
                    )
                    models[child].measures.append(Measure(name=name, agg=measure.agg, expr=expr))
                    probes.append(
                        (
                            name,
                            self._probe_request(request, name, qualified_filter, force_join),
                            Cause(
                                "fan_out",
                                f"The legacy query joins `{child}` (one-to-many) before "
                                f"aggregating `{measure_name}`, so each `{owner}` row counts "
                                f"once per `{child}` row.",
                                {"joined_model": child, "measure": measure_name},
                            ),
                        )
                    )
                if measure.agg in ("count", "count_distinct") and pk is not None:
                    name = f"{PROBE_PREFIX}inner_{child}"
                    distinct_expr = measure.expr or (pk.expr or pk.name)
                    models[child].measures.append(
                        Measure(name=name, agg="count_distinct", expr=f"{owner}.{distinct_expr}")
                    )
                    probes.append(
                        (
                            name,
                            self._probe_request(request, name, qualified_filter, force_join),
                            Cause(
                                "inner_join_drop",
                                f"The legacy query only counts `{owner}` rows that have at "
                                f"least one `{child}` row (an INNER JOIN), dropping the others.",
                                {"joined_model": child, "measure": measure_name},
                            ),
                        )
                    )
            if measure.agg == "count_distinct":
                name = f"{PROBE_PREFIX}rows_{measure_name}"
                models[owner].measures.append(Measure(name=name, agg="count", expr=measure.expr))
                probes.append(
                    (
                        name,
                        self._probe_request(request, name, qualified_filter, None),
                        Cause(
                            "count_not_distinct",
                            f"The legacy query counts rows of `{measure.expr}` instead of "
                            "distinct values.",
                            {"measure": measure_name},
                        ),
                    )
                )

        for index, member in enumerate(request.dimensions):
            ref = g.resolve_dimension(member)
            dim = g.dimension(ref)
            if dim.type != "time" or not ref.grain:
                continue
            for hours in TIME_SHIFTS_HOURS:
                suffix = f"p{hours}" if hours > 0 else f"m{-hours}"
                name = f"{PROBE_PREFIX}{ref.name}_{suffix}"
                expr = f"{dim.expr or dim.name} + INTERVAL '{hours}' HOUR"
                models[ref.model].dimensions.append(Dimension(name=name, type="time", expr=expr))
                dims = list(request.dimensions)
                dims[index] = f"{ref.model}.{name}__{ref.grain}"
                probes.append(
                    (
                        name,
                        request.model_copy(update={"dimensions": dims}),
                        Cause(
                            "time_shift",
                            f"The legacy query buckets `{ref.model}.{ref.name}` shifted by "
                            f"{hours:+d} hours (for example, a UTC conversion) before "
                            f"truncating to {ref.grain}.",
                            {"dimension": f"{ref.model}.{ref.name}", "hours": hours},
                        ),
                    )
                )

        if not probes:
            return []
        probe_graph = compile_project(project)
        causes = []
        for _name, probe_request, cause in probes:
            try:
                rows, _ = self._run(probe_request, probe_graph)
            except CosmosError:
                continue
            if rows_match(rows, legacy):
                causes.append(cause)
        return causes

    def _probe_request(
        self, request: QueryRequest, metric: str, metric_filter: str | None, force_join: str | None
    ) -> QueryRequest:
        filters = list(request.filters)
        if metric_filter:
            filters.append(metric_filter)
        if force_join:
            filters.append(force_join)
        return request.model_copy(update={"metrics": [metric], "filters": filters})

    def _qualify_refs(self, text: str, model: str) -> str:
        """Pins unqualified references to the metric's own model, so the filter keeps its
        meaning when a probe starts from another model."""

        def pin(match) -> str:
            ref = match.group(1)
            if "." in ref:
                return match.group(0)
            if ref in self.graph.models[model].dimensions:
                return "{" + f"{model}.{ref}" + "}"
            return match.group(0)

        return REF_RE.sub(pin, text)


def reconcile_sql(
    graph: SemanticGraph,
    connector: Connector,
    request: QueryRequest,
    legacy_sql: str,
    principal: Principal | None = None,
) -> dict[str, Any]:
    return Reconciler(graph, connector, principal).against_sql(request, legacy_sql)


def reconcile_models(
    graph: SemanticGraph,
    other: SemanticGraph,
    connector: Connector,
    request: QueryRequest,
    principal: Principal | None = None,
) -> dict[str, Any]:
    return Reconciler(graph, connector, principal).against_model(request, other)


# -- divergence zoo ------------------------------------------------------------


class ZooExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["match", "divergent"]
    causes: list[str] = []


class ZooCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    request: QueryRequest
    user: dict[str, Any] = {}
    sql: str
    sql_by_dialect: dict[str, str] = {}
    expect: ZooExpectation

    def sql_for(self, dialect: str) -> str:
        return self.sql_by_dialect.get(dialect, self.sql)


def load_zoo(path: str | Path) -> list[ZooCase]:
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return [ZooCase.model_validate(c) for c in data["cases"]]


def check_zoo_case(
    case: ZooCase, graph: SemanticGraph, connector: Connector
) -> tuple[bool, dict[str, Any]]:
    """Runs one case; it passes when the status matches and every expected cause is found."""
    principal = Principal(user=case.user, channel="cli", subject="reconciliation")
    report = reconcile_sql(
        graph, connector, case.request, case.sql_for(connector.dialect), principal
    )
    found = {c["code"] for c in report["causes"]}
    ok = report["status"] == case.expect.status and set(case.expect.causes) <= found
    return ok, report
