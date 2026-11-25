"""Exporting to Databricks Unity Catalog metric views, first slice (DEC-42).

Layout: one metric view per model that has measures, sourced on the model's table. Its
``joins`` are the model's many-to-one joins one step away (the ones the engine infers from
entities), as ``source.<key> = <model>.<key>``; its ``dimensions`` are the model's own and those
of the joined models (``customers_segment``); its ``measures`` are the model's measures and the
simple, ratio and derived metrics whose measures all live on that model.

Each measure is one aggregate expression, built the way the planner builds it: a metric filter
goes inside the aggregation (``SUM(CASE WHEN status = 'completed' THEN amount END)``), a ratio is
``CAST(num AS DOUBLE) / NULLIF(den, 0)``, every division in a derived metric is real and null on
a zero denominator, and ``avg`` averages ``CAST(x AS DOUBLE)``. Metrics are written inline
rather than with ``MEASURE()`` composition, so they work on every metric view version.

What the export cannot express exactly is refused, unless the caller accepts the loss: running
totals and rolling windows, offsets, metrics spanning models, and filters on another model, a
time grain, a business rule or ``$user``. Row filters and sensitivity are not exported
(governance stays in Cosmos), nor are dimensions reached through more than one join,
descriptions, labels and owners; each one present is a warning.
"""

from __future__ import annotations

from typing import Any

import yaml
from sqlglot import exp

from cosmos_semantic.errors import QueryError
from cosmos_semantic.expressions import parse_template, qualify, ref_index, substitute
from cosmos_semantic.interchange import ExportResult
from cosmos_semantic.interchange.cube import _plural, refuse_policy_loss
from cosmos_semantic.spec import (
    CumulativeMetric,
    DerivedMetric,
    Project,
    RatioMetric,
    SimpleMetric,
)

DIALECT = "databricks"
# The metric view YAML version this export writes.
VERSION = 0.1
# The name a metric view gives its own source in join conditions.
SOURCE = "source"
_LATER = ("business_rules", "glossary", "teams", "systems", "processes")
_AGGS = {"sum": exp.Sum, "min": exp.Min, "max": exp.Max, "avg": exp.Avg}


class _NotExported(Exception):
    """A metric the Databricks export cannot carry; the message is the reason."""


def _own(text: str) -> exp.Expression:
    """A column expression of the view's own model, each bare column written ``source.<col>``,
    so it cannot be confused with a joined model's column of the same name."""
    return qualify(text, SOURCE, DIALECT)


# Functions that read the clock: in a metric view they follow the warehouse's clock, while the
# planner pins them to the query's as_of.
_CLOCK = (
    exp.CurrentDate,
    exp.CurrentDatetime,
    exp.CurrentTime,
    exp.CurrentTimestamp,
    exp.CurrentTimestampLTZ,
)
_CLOCK_NAMES = {"NOW", "GETDATE", "SYSDATE", "SYSTIMESTAMP", "CURDATE", "LOCALTIMESTAMP"}


def _reads_clock(tree: exp.Expression) -> bool:
    return any(
        isinstance(node, _CLOCK)
        or (isinstance(node, exp.Anonymous) and str(node.this).upper() in _CLOCK_NAMES)
        for node in tree.walk()
    )


def _sql(tree: exp.Expression) -> str:
    return tree.sql(dialect=DIALECT)


def _double(tree: exp.Expression) -> exp.Expression:
    return exp.Cast(this=tree, to=exp.DataType.build("double"))


def _divide(num: exp.Expression, den: exp.Expression) -> exp.Expression:
    """Real division, null on a zero denominator, as the planner divides."""
    return exp.Div(
        this=_double(num), expression=exp.Nullif(this=den, expression=exp.Literal.number(0))
    )


def export_databricks(
    project: Project,
    *,
    catalog: str | None = None,
    allow_policy_loss: bool = False,
    allow_metric_loss: bool = False,
) -> ExportResult:
    """Writes one Databricks metric view YAML document per model with measures, each after a
    comment naming its model. See ``metric_views``."""
    views, warnings = metric_views(
        project,
        catalog=catalog,
        allow_policy_loss=allow_policy_loss,
        allow_metric_loss=allow_metric_loss,
    )
    docs = [
        f"# Metric view for the model `{name}`\n"
        + yaml.safe_dump(view, sort_keys=False, allow_unicode=True, width=100)
        for name, view in views.items()
    ]
    return ExportResult("---\n".join(docs), warnings)


def metric_views(
    project: Project,
    *,
    catalog: str | None = None,
    allow_policy_loss: bool = False,
    allow_metric_loss: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The metric views (model name -> YAML document) and the warnings. Refuses, unless
    ``allow_policy_loss``, a project with row filters or sensitivity, and, unless
    ``allow_metric_loss``, one with metrics a metric view cannot carry here. ``catalog``
    prefixes each two-part table name (``schema.table``)."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.errors import MetricLossError

    graph = compile_project(project)  # only valid models are exported
    warnings: list[str] = []
    refuse_policy_loss(project, "Databricks", allow_policy_loss, warnings)

    writer = _Writer(graph, warnings)
    if writer.lost and not allow_metric_loss:
        reasons = "; ".join(f"{k}: {v}" for k, v in writer.lost.items())
        raise MetricLossError(
            f"{_plural(len(writer.lost), 'metric')} ({', '.join(writer.lost)}) would NOT be "
            f"exported ({reasons}); the Databricks metric views would not have them. Pass "
            "--allow-metric-loss to export anyway"
        )
    for name, reason in writer.lost.items():
        warnings.append(f"metrics.{name}: {reason}; it is not exported to Databricks")
    views = writer.views(catalog)

    if project.user_context:
        warnings.append(
            f"user_context: {', '.join(project.user_context)} not exported to Databricks (only "
            "row filters use it)"
        )
    if project.timezone:
        warnings.append(
            "timezone: not exported to Databricks; relative dates such as today follow the "
            f"warehouse session, not {project.timezone}"
        )
    described = [
        *(m.name for m in project.semantic_models if m.description or m.owner),
        *(m.name for m in project.metrics if m.description or m.label or m.owner),
    ]
    if described:
        warnings.append(
            "descriptions, labels and owners: not exported to Databricks in this slice "
            f"({len(described)} members have them)"
        )
    for key in _LATER:
        items = getattr(project, key)
        if items:
            warnings.append(f"{key}: not exported to Databricks yet ({len(items)} in the project)")
    if project.custom_extensions:
        warnings.append(
            f"custom_extensions: {', '.join(project.custom_extensions)} not exported to "
            "Databricks yet"
        )
    return views, list(dict.fromkeys(warnings))


class _Writer:
    """Builds each measure's aggregate expression and decides which view it goes on."""

    def __init__(self, graph, warnings: list[str]) -> None:
        self.graph = graph
        self.warnings = warnings
        self.lost: dict[str, str] = {}
        self.current = ""  # the metric being built, for the warnings of its filters
        # model -> measures written on its view, as (name, aggregate expression)
        self.measures: dict[str, list[tuple[str, exp.Expression]]] = {m: [] for m in graph.models}
        for node in graph.models.values():
            for measure in node.spec.measures:
                tree, _ = self._expr(measure.name, None, set())
                self.measures[node.name].append((measure.name, tree))
        for metric in graph.project.metrics:
            self.current = metric.name
            try:
                tree, models = self._expr(metric.name, None, set())
            except _NotExported as exc:
                self.lost[metric.name] = str(exc)
                continue
            if len(models) > 1:
                self.lost[metric.name] = (
                    f"it spans models ({', '.join(sorted(models))}), and a metric view "
                    "aggregates one source table; the engine aggregates each model at its own "
                    "grain"
                )
                continue
            self.measures[models.pop()].append((metric.name, tree))

    # -- views ------------------------------------------------------------------------------

    def views(self, catalog: str | None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        partial: list[str] = []
        for node in self.graph.models.values():
            measures = self.measures[node.name]
            if not measures:
                continue
            table = _table(node.spec.table, catalog)
            view: dict[str, Any] = {"version": VERSION, "source": table}
            joins, dims = self._joins(node, catalog)
            for name in [table, *(j["source"] for j in joins)]:
                if name.count(".") != 2 and name not in partial:
                    partial.append(name)
            if joins:
                view["joins"] = joins
            taken = {name for name, _ in measures}
            own = [
                {"name": d.name, "expr": _sql(_own(d.expr or d.name))} for d in node.spec.dimensions
            ]
            own_names = {d["name"] for d in own}
            view["dimensions"] = []
            for dim in [*own, *dims]:
                if dim["name"] in taken:
                    clash = (
                        "one of the model's own dimensions"
                        if dim not in own and dim["name"] in own_names
                        else "a measure"
                    )
                    self.warnings.append(
                        f"semantic_models.{node.name}: the dimension `{dim['name']}` has the "
                        f"name of {clash} in its metric view; dimension skipped"
                    )
                    continue
                taken.add(dim["name"])
                view["dimensions"].append(dim)
            if not view["dimensions"]:
                del view["dimensions"]
            view["measures"] = [{"name": name, "expr": _sql(tree)} for name, tree in measures]
            out[node.name] = view
        if partial:
            self.warnings.append(
                f"source: the tables {', '.join(partial)} are not catalog.schema.table names; "
                "Databricks resolves them against the catalog and schema where the view is "
                "created (pass --catalog)"
            )
        return out

    def _joins(
        self, node, catalog: str | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """The one-step many-to-one joins whose target has dimensions, and those dimensions."""
        edges = self.graph.edges.get(node.name, [])
        targets = [e.target for e in edges]
        joins: list[dict[str, Any]] = []
        dims: list[dict[str, str]] = []
        for edge in edges:
            where = f"semantic_models.{node.name}.entities.{edge.entity}"
            target = self.graph.models[edge.target].spec
            if not target.dimensions:
                continue
            if targets.count(edge.target) > 1:
                self.warnings.append(
                    f"{where}: `{node.name}` joins `{edge.target}` more than once; that join "
                    "is not exported"
                )
                continue
            if edge.target == SOURCE:
                self.warnings.append(
                    f"{where}: a joined model named `{SOURCE}` clashes with the metric view's "
                    "name for its own source; join skipped"
                )
                continue
            local = _sql(_own(edge.source_expr))
            remote = _sql(qualify(edge.target_expr, edge.target, DIALECT))
            source = _table(target.table, catalog)
            joins.append({"name": edge.target, "source": source, "on": f"{local} = {remote}"})
            for dim in target.dimensions:
                dims.append(
                    {
                        "name": f"{edge.target}_{dim.name}",
                        "expr": _sql(qualify(dim.expr or dim.name, edge.target, DIALECT)),
                    }
                )
        further = sorted(self.graph.reachable(node.name) - {node.name, *targets})
        if further:
            self.warnings.append(
                f"semantic_models.{node.name}: the dimensions of {', '.join(further)} are "
                "reached through more than one join and are not exported (no nested joins in "
                "this slice)"
            )
        return joins, dims

    # -- measures and metrics ---------------------------------------------------------------

    def _expr(
        self, name: str, condition: str | None, stack: set[str]
    ) -> tuple[exp.Expression, set[str]]:
        """The aggregate expression of a measure or metric, with ``condition`` (a metric filter
        from above) applied inside each aggregation, and the models whose measures it uses."""
        g = self.graph
        if name in g.measure_owner:
            node, measure = g.measure(name)
            return self._aggregate(measure, node.name, condition), {node.name}
        if name in self.lost:
            raise _NotExported(f"it uses `{name}`, which is not exported")
        metric = g.metrics[name]
        stack = stack | {name}
        if isinstance(metric, SimpleMetric):
            return self._expr(metric.measure, _and(metric.filter, condition), stack)
        if isinstance(metric, RatioMetric):
            flt = _and(metric.filter, condition)
            num, a = self._expr(metric.numerator, flt, stack)
            den, b = self._expr(metric.denominator, flt, stack)
            return _divide(num, den), a | b
        if isinstance(metric, DerivedMetric):
            if metric.inputs:
                raise _NotExported(
                    "it reads an input at an earlier period (`offset`), which a metric view "
                    "measure cannot express"
                )
            template = parse_template(metric.expr, DIALECT)
            bare = [c.sql() for c in template.tree.find_all(exp.Column) if ref_index(c) is None]
            if bare:
                raise _NotExported(
                    f"its expression uses bare columns ({', '.join(bare)}), not only measures "
                    "and metrics"
                )
            template.tree = template.tree.transform(
                lambda n: _divide(n.this, n.expression) if isinstance(n, exp.Div) else n
            )
            models: set[str] = set()

            def resolve(ref: str) -> exp.Expression:
                tree, used = self._expr(ref, condition, stack)
                models.update(used)
                return tree

            return exp.Paren(this=substitute(template, resolve)), models
        if isinstance(metric, CumulativeMetric):
            if stack != {name}:
                raise _NotExported(f"it uses the cumulative metric `{name}`")
            raise _NotExported(
                "it is a running total or a rolling window, which this slice does not export "
                "to metric views"
            )
        raise _NotExported(f"metrics of type `{metric.type}` are not exported")

    def _aggregate(self, measure, model: str, condition: str | None) -> exp.Expression:
        cond = self._condition(condition, model) if condition else None
        if measure.agg == "count" and measure.expr is None:
            if cond is None:
                return exp.Count(this=exp.Star())
            return exp.Count(this=exp.Case(ifs=[exp.If(this=cond, true=exp.Literal.number(1))]))
        value = _own(measure.expr or measure.name)
        if cond is not None:
            value = exp.Case(ifs=[exp.If(this=cond, true=value)])
        if measure.agg == "count":
            return exp.Count(this=value)
        if measure.agg == "count_distinct":
            return exp.Count(this=exp.Distinct(expressions=[value]))
        if measure.agg == "avg":
            value = _double(value)
        return _AGGS[measure.agg](this=value)

    def _condition(self, text: str, model: str) -> exp.Expression:
        """A metric filter over the measure's own model, with its dimensions' expressions."""
        template = parse_template(text, DIALECT)
        if template.user_fields:
            raise _NotExported("its filter uses $user, which only Cosmos resolves")
        node = self.graph.models[model]

        def resolve(ref: str) -> exp.Expression:
            try:
                dim = self.graph.resolve_dimension(ref, context=model)
            except QueryError as exc:
                raise _NotExported(
                    f"its filter reference `{ref}` does not resolve ({exc})"
                ) from None
            if dim.model in self.graph.rule_models.get(dim.name, ()):
                # A rule is a dimension of its models too; the planner reads the version in
                # effect on rules_as_of, with the clock pinned to as_of.
                raise _NotExported(f"its filter uses the business rule `{dim.name}` (`{ref}`)")
            if dim.model != model:
                raise _NotExported(f"its filter uses `{ref}` from another model ({dim.model})")
            if dim.grain:
                raise _NotExported(f"its filter uses a time grain (`{ref}`)")
            spec = node.dimensions[dim.name]
            return _own(spec.expr or spec.name)

        tree = substitute(template, resolve)
        if _reads_clock(tree):
            self.warnings.append(
                f"metrics.{self.current}: its filter reads the clock (such as CURRENT_DATE); in "
                "the metric view it follows the warehouse's clock, not the query's as_of"
            )
        return tree


def _table(name: str, catalog: str | None) -> str:
    """The table as written, with ``catalog`` before a ``schema.table`` name."""
    return f"{catalog}.{name}" if catalog and name.count(".") == 1 else name


def _and(a: str | None, b: str | None) -> str | None:
    if a and b:
        return f"({a}) AND ({b})"
    return a or b
