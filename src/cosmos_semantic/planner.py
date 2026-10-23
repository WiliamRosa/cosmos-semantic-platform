"""Query planner and SQL generation.

Fan-out strategy: measures are grouped by their owner model; each group becomes a CTE that
starts at the owner model and only follows many-to-one joins, so no row is repeated before
aggregation. The CTEs are then joined on the requested dimensions.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from cosmos_semantic.compiler import DimensionRef, JoinEdge, SemanticGraph
from cosmos_semantic.errors import PolicyError, QueryError
from cosmos_semantic.expressions import (
    Template,
    check_request_expression,
    literal,
    parse_template,
    qualify,
    substitute,
    table_ref,
)
from cosmos_semantic.ontology import RULE_DIALECT
from cosmos_semantic.spec import (
    CumulativeMetric,
    DerivedMetric,
    Measure,
    RatioMetric,
    SimpleMetric,
)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: list[str] = Field(min_length=1)
    dimensions: list[str] = []
    filters: list[str] = Field([], description="Expressions with `{model.dimension}` references.")
    order_by: list[str] = Field(
        [], description="Requested members; prefix with `-` for descending order."
    )
    limit: int | None = Field(None, ge=1)
    as_of: dt.date | None = Field(
        None,
        description="Reference date for CURRENT_DATE; default: today in the model's time zone.",
    )


@dataclass
class Principal:
    """Who is asking: attributes already extracted from the token, and the channel."""

    user: dict[str, Any] = field(default_factory=dict)
    channel: Literal["api", "mcp", "cli"] = "cli"
    subject: str | None = None

    @property
    def is_agent(self) -> bool:
        return self.channel == "mcp"


@dataclass(frozen=True)
class OutputColumn:
    name: str
    kind: Literal["dimension", "metric"]
    member: str


@dataclass
class CompiledQuery:
    sql: str
    dialect: str
    columns: list[OutputColumn]
    data_source: str | None
    models: list[str]
    as_of: dt.date | None = None


@dataclass(frozen=True)
class _Input:
    """An aggregated measure, with the filter of the metric that uses it (if any)."""

    measure: str
    filter: str | None


@dataclass
class _Group:
    model: str
    inputs: dict[_Input, str] = field(default_factory=dict)


_SAFE_AGGS = {
    "sum": exp.Sum,
    "avg": exp.Avg,
    "min": exp.Min,
    "max": exp.Max,
}


class Planner:
    def __init__(
        self,
        graph: SemanticGraph,
        dialect: str = "postgres",
        schema_map: dict[str, str] | None = None,
    ) -> None:
        self.graph = graph
        self.dialect = dialect
        self.schema_map = schema_map or {}

    def now(self) -> dt.datetime:
        """Now in the business time zone, without an explicit offset (model columns are local)."""
        zone = ZoneInfo(self.graph.project.timezone or "UTC")
        return dt.datetime.now(zone).replace(tzinfo=None, microsecond=0)

    def _trunc(self, value: exp.Expression, grain: str) -> exp.Expression:
        unit = grain.upper()
        if unit == "WEEK" and self.dialect == "tsql":
            unit = "ISO_WEEK"  # SQL Server weeks depend on DATEFIRST; ISO weeks start on Monday
        return exp.TimestampTrunc(this=value, unit=exp.Var(this=unit))

    def _table(self, name: str, alias: str) -> exp.Table:
        return table_ref(name, self.dialect, self.schema_map).as_(alias)

    # -- public entry point ----------------------------------------------------

    def plan(self, request: QueryRequest, principal: Principal | None = None) -> CompiledQuery:
        principal = principal or Principal()
        g = self.graph

        dims = [g.resolve_dimension(d) for d in request.dimensions]
        if len({d.alias for d in dims}) != len(dims):
            raise QueryError("dimension requested more than once")
        filters = [parse_template(f, self.dialect, error=QueryError) for f in request.filters]
        for template in filters:
            check_request_expression(template)
        for ref in dims:
            self._check_sensitivity(ref, principal)

        groups: dict[str, _Group] = {}
        metric_exprs: dict[str, exp.Expression] = {}
        cumulative: dict[str, CumulativeMetric] = {}
        for name in request.metrics:
            if name in metric_exprs:
                raise QueryError(f"metric `{name}` requested more than once")
            metric_exprs[name] = self._metric_expr(name, groups, set())
            metric = g.metrics.get(name)
            if isinstance(metric, CumulativeMetric):
                cumulative[name] = metric

        models = {g.measure_owner[i.measure] for grp in groups.values() for i in grp.inputs}
        data_sources = {g.models[m].spec.data_source for m in models}
        if len(data_sources) > 1:
            raise QueryError("the requested metrics live in different data sources")

        ctes: list[tuple[str, exp.Select]] = []
        used_models: set[str] = set()
        for group in groups.values():
            select, joined = self._group_select(group, dims, filters, principal)
            ctes.append((f"{group.model}_agg", select))
            used_models |= joined

        final = self._final_select(ctes, dims, metric_exprs, cumulative, request)
        now = self.now()
        as_of = request.as_of or now.date()
        clock = now if request.as_of is None else dt.datetime.combine(request.as_of, dt.time())
        final = _pin_clock(final, as_of, clock)
        columns = [OutputColumn(d.alias, "dimension", _dim_member(d)) for d in dims] + [
            OutputColumn(m, "metric", m) for m in request.metrics
        ]
        return CompiledQuery(
            sql=final.sql(dialect=self.dialect, pretty=True),
            dialect=self.dialect,
            columns=columns,
            data_source=next(iter(data_sources)) if data_sources else None,
            models=sorted(used_models),
            as_of=as_of,
        )

    def _check_sensitivity(self, ref: DimensionRef, principal: Principal) -> None:
        level = self.graph.dimension(ref).sensitivity
        if principal.is_agent and level:
            raise PolicyError(
                f"`{ref.model}.{ref.name}` is {_LEVEL[level]} data (LGPD) and never reaches "
                "AI agents, neither as a grouping nor as a filter"
            )

    # -- metrics ---------------------------------------------------------------

    def _metric_expr(
        self, name: str, groups: dict[str, _Group], stack: set[str], extra_filter: str | None = None
    ) -> exp.Expression:
        g = self.graph
        if name in g.measure_owner:
            return self._input(_Input(name, extra_filter), groups)
        metric = g.metrics.get(name)
        if metric is None:
            raise QueryError(f"metric `{name}` does not exist")
        if name in stack:
            raise QueryError(f"metric cycle at `{name}`")
        stack = stack | {name}

        if isinstance(metric, SimpleMetric):
            return self._input(_Input(metric.measure, _and(metric.filter, extra_filter)), groups)
        if isinstance(metric, RatioMetric):
            flt = _and(metric.filter, extra_filter)
            num = self._metric_expr(metric.numerator, groups, stack, flt)
            den = self._metric_expr(metric.denominator, groups, stack, flt)
            return exp.Div(
                this=exp.Cast(this=num, to=exp.DataType.build("double")),
                expression=exp.Nullif(this=den, expression=exp.Literal.number(0)),
            )
        if isinstance(metric, DerivedMetric):
            template = parse_template(metric.expr, self.dialect)
            template.tree = template.tree.transform(_safe_division)
            return exp.Paren(
                this=substitute(
                    template, lambda r: self._metric_expr(r, groups, stack, extra_filter)
                )
            )
        if isinstance(metric, CumulativeMetric):
            if metric.window:
                raise QueryError(f"`{name}`: metrics with `window` are not in the MVP")
            if extra_filter or stack != {name}:
                raise QueryError(f"`{name}`: a cumulative metric cannot be part of another metric")
            return self._input(_Input(metric.measure, None), groups)
        raise QueryError(f"unsupported metric type in `{name}`")

    def _input(self, item: _Input, groups: dict[str, _Group]) -> exp.Expression:
        owner = self.graph.measure_owner[item.measure]
        group = groups.setdefault(owner, _Group(owner))
        if item not in group.inputs:
            suffix = "" if item.filter is None else f"__f{len(group.inputs)}"
            group.inputs[item] = f"{item.measure}{suffix}"
        return exp.column(group.inputs[item], table=f"{owner}_agg")

    # -- one CTE per owner model -------------------------------------------------

    def _group_select(
        self,
        group: _Group,
        dims: list[DimensionRef],
        filters: list[Template],
        principal: Principal,
    ) -> tuple[exp.Select, set[str]]:
        g = self.graph
        base = group.model
        joins: dict[str, JoinEdge] = {}

        def need(model: str) -> None:
            for edge in g.join_path(base, model):
                joins.setdefault(edge.target, edge)

        def dim_sql(ref: DimensionRef, condition: bool = False) -> exp.Expression:
            if ref.model != base and ref.model not in g.reachable(base):
                raise QueryError(
                    f"`{ref.model}.{ref.name}` is not reachable from `{base}` "
                    "(only many-to-one joins are allowed)"
                )
            need(ref.model)
            dim = g.dimension(ref)
            read = RULE_DIALECT if ref.model in g.rule_models.get(ref.name, ()) else self.dialect
            column = qualify(dim.expr or dim.name, ref.model, read)
            if ref.grain:
                column = self._trunc(column, ref.grain)
            if dim.type == "boolean" and self.dialect == "tsql":
                column = _tsql_boolean(column, condition)
            return column

        def request_ref(r: str, condition: bool = False) -> exp.Expression:
            ref = g.resolve_dimension(r, context=base if r.startswith("rule.") else None)
            self._check_sensitivity(ref, principal)
            return dim_sql(ref, condition)

        def model_ref(context: str):
            return lambda r, condition: dim_sql(g.resolve_dimension(r, context=context), condition)

        def fill(template: Template, resolver, user=None) -> exp.Expression:
            return substitute(
                template,
                lambda r: resolver(r, False),
                user,
                resolve_condition=lambda r: resolver(r, True),
            )

        dim_exprs = [dim_sql(d) for d in dims]
        projections = [e.copy().as_(d.alias) for e, d in zip(dim_exprs, dims, strict=True)]
        conditions = [fill(t, request_ref) for t in filters]

        for item, alias in group.inputs.items():
            measure = g.models[base].measures[item.measure]
            condition = (
                fill(parse_template(item.filter, self.dialect), model_ref(base))
                if item.filter
                else None
            )
            projections.append(self._aggregate(measure, base, condition).as_(alias))

        # Row filters of every participating model, including models joined only because
        # another row filter needs them.
        applied: set[str] = set()
        while True:
            pending = [m for m in [base, *joins] if m not in applied]
            if not pending:
                break
            for model in pending:
                applied.add(model)
                for row_filter in g.models[model].spec.row_filters:
                    template = parse_template(row_filter.filter, self.dialect)
                    conditions.append(
                        fill(template, model_ref(model), self._user_resolver(principal))
                    )

        select = exp.select(*projections).from_(self._table(g.models[base].spec.table, base))
        for target, edge in _ordered(joins, base):
            on = exp.EQ(
                this=qualify(edge.source_expr, edge.source, self.dialect),
                expression=qualify(edge.target_expr, edge.target, self.dialect),
            )
            select = select.join(
                self._table(g.models[target].spec.table, target), on=on, join_type="left"
            )
        if conditions:
            select = select.where(exp.and_(*conditions))
        if dims:
            # Group by expressions, not ordinals: SQL Server does not accept GROUP BY 1.
            select = select.group_by(*[e.copy() for e in dim_exprs])
        return select, {base, *joins}

    def _aggregate(
        self, measure: Measure, model: str, condition: exp.Expression | None
    ) -> exp.Expression:
        if measure.agg == "count" and measure.expr is None:
            if condition is None:
                return exp.Count(this=exp.Star())
            return exp.Count(
                this=exp.Case(ifs=[exp.If(this=condition, true=exp.Literal.number(1))])
            )
        value = qualify(measure.expr or measure.name, model, self.dialect)
        if condition is not None:
            value = exp.Case(ifs=[exp.If(this=condition, true=value)])
        if measure.agg == "count":
            return exp.Count(this=value)
        if measure.agg == "count_distinct":
            return exp.Count(this=exp.Distinct(expressions=[value]))
        if measure.agg == "avg":
            # AVG over an integer column truncates in SQL Server and returns NUMERIC in Postgres.
            value = exp.Cast(this=value, to=exp.DataType.build("double"))
        return _SAFE_AGGS[measure.agg](this=value)

    def _user_resolver(self, principal: Principal):
        declared = self.graph.project.user_context

        def resolve(name: str) -> exp.Expression:
            if name not in declared:
                raise PolicyError(f"`$user.{name}` is not declared in user_context")
            if name not in principal.user or principal.user[name] is None:
                raise PolicyError(
                    f"query denied: the user context has no `{name}`, which a row filter requires"
                )
            return literal(principal.user[name])

        return resolve

    # -- joining the CTEs --------------------------------------------------------

    def _final_select(
        self,
        ctes: list[tuple[str, exp.Select]],
        dims: list[DimensionRef],
        metric_exprs: dict[str, exp.Expression],
        cumulative: dict[str, CumulativeMetric],
        request: QueryRequest,
    ) -> exp.Select:
        names = [name for name, _ in ctes]
        # With more than one CTE and dimensions, the "spine" is the union of the dimension
        # combinations of all CTEs; each CTE is LEFT JOINed to it with null-safe equality.
        # Postgres rejects FULL JOIN with IS NOT DISTINCT FROM.
        use_spine = len(names) > 1 and bool(dims)
        anchor = "dimensions" if use_spine else names[0]

        def dim_out(d: DimensionRef) -> exp.Expression:
            return exp.column(d.alias, table=anchor)

        projections: list[exp.Expression] = [dim_out(d).as_(d.alias) for d in dims]
        for name, expression in metric_exprs.items():
            if name in cumulative:
                expression = self._cumulative(expression, cumulative[name], dims, dim_out)
            projections.append(expression.as_(name))

        select = exp.select(*projections).from_(exp.to_table(anchor))
        if use_spine:
            for name in names:
                on = exp.and_(
                    *[
                        exp.NullSafeEQ(
                            this=exp.column(d.alias, table=anchor),
                            expression=exp.column(d.alias, table=name),
                        )
                        for d in dims
                    ]
                )
                select = select.join(exp.to_table(name), on=on, join_type="left")
        else:
            for name in names[1:]:
                select = select.join(exp.to_table(name), join_type="cross")
        for name, cte in ctes:
            select = select.with_(name, as_=cte)
        if use_spine:
            parts = [exp.select(*[exp.column(d.alias) for d in dims]).from_(n) for n in names]
            spine: exp.Query = parts[0]
            for part in parts[1:]:
                spine = exp.union(spine, part, distinct=True)
            select = select.with_(anchor, as_=spine)

        order = []
        known = {d.alias for d in dims}
        for item in request.order_by:
            desc = item.startswith("-")
            key = item.lstrip("-")
            if key not in metric_exprs and key not in known:
                try:
                    key = self.graph.resolve_dimension(key).alias
                except QueryError as exc:
                    raise QueryError(
                        f"order_by `{item}` is not among the requested members"
                    ) from exc
                if key not in known:
                    raise QueryError(f"order_by `{item}` is not among the requested members")
            # Nulls always last, ascending or descending: each database has its own default.
            order.append(exp.Ordered(this=exp.column(key), desc=desc, nulls_first=False))
        if order:
            select = select.order_by(*order)
        if request.limit:
            select = select.limit(request.limit)
        return select

    def _cumulative(self, value, metric: CumulativeMetric, dims, dim_out) -> exp.Expression:
        time_dims = [d for d in dims if self.graph.dimension(d).type == "time"]
        if len(time_dims) != 1:
            raise QueryError(f"`{metric.name}` needs exactly one time dimension in the request")
        time = time_dims[0]
        partition = [dim_out(d) for d in dims if d != time]
        if metric.grain_to_date:
            partition.append(self._trunc(dim_out(time), metric.grain_to_date))
        spec = exp.WindowSpec(
            kind="ROWS", start="UNBOUNDED", start_side="PRECEDING", end="CURRENT ROW"
        )
        # A null period sorts last on every database. SQL Server cannot say NULLS LAST inside a
        # window, so the first key does it portably and the second keeps the dialect's default.
        null_last = exp.Case(
            ifs=[
                exp.If(
                    this=exp.Is(this=dim_out(time), expression=exp.null()),
                    true=exp.Literal.number(1),
                )
            ],
            default=exp.Literal.number(0),
        )
        default_nulls_first = Dialect.get_or_raise(self.dialect).NULL_ORDERING == "nulls_are_small"
        return exp.Window(
            this=exp.Sum(this=value),
            partition_by=partition or None,
            order=exp.Order(
                expressions=[
                    exp.Ordered(this=null_last, nulls_first=default_nulls_first),
                    exp.Ordered(this=dim_out(time), nulls_first=default_nulls_first),
                ]
            ),
            spec=spec,
        )


_LEVEL = {"personal": "personal", "sensitive": "sensitive personal"}


def _pin_clock(tree: exp.Expression, as_of: dt.date, clock: dt.datetime) -> exp.Expression:
    """Replaces CURRENT_DATE and CURRENT_TIMESTAMP with the reference date and time: "today"
    becomes the business date, not each server's clock (and time zone), and the query becomes
    reproducible and auditable."""
    day = exp.cast(exp.Literal.string(as_of.isoformat()), "DATE")
    moment = exp.cast(exp.Literal.string(clock.isoformat(sep=" ")), "TIMESTAMP")
    timestamps = tuple(
        getattr(exp, name)
        for name in ("CurrentTimestamp", "CurrentDatetime", "Localtimestamp")
        if hasattr(exp, name)
    )

    def pin(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentDate):
            return day.copy()
        if isinstance(node, timestamps):
            return moment.copy()
        return node

    return tree.transform(pin)


def _safe_division(node: exp.Expression) -> exp.Expression:
    """Division in derived metrics is always real and safe against zero, on every database."""
    if isinstance(node, exp.Div):
        return exp.Div(
            this=exp.Cast(this=node.this, to=exp.DataType.build("double")),
            expression=exp.Nullif(this=node.expression, expression=exp.Literal.number(0)),
        )
    return node


def _tsql_boolean(value: exp.Expression, condition: bool) -> exp.Expression:
    """In SQL Server a BIT column becomes a predicate in conditions, and a predicate becomes
    1/0 when used as a value."""
    is_predicate = not isinstance(value, exp.Column | exp.Literal | exp.Case | exp.Cast)
    if condition:
        return value if is_predicate else exp.EQ(this=value, expression=exp.Literal.number(1))
    if not is_predicate:
        return value
    return exp.Case(
        ifs=[
            exp.If(this=value.copy(), true=exp.Literal.number(1)),
            exp.If(this=exp.Not(this=exp.Paren(this=value.copy())), true=exp.Literal.number(0)),
        ]
    )


def _and(a: str | None, b: str | None) -> str | None:
    if a and b:
        return f"({a}) AND ({b})"
    return a or b


def _dim_member(d: DimensionRef) -> str:
    member = f"{d.model}.{d.name}"
    return f"{member}__{d.grain}" if d.grain else member


def _ordered(joins: dict[str, JoinEdge], base: str) -> list[tuple[str, JoinEdge]]:
    """Orders joins so each one comes after the model it starts from."""
    done, ordered = {base}, []
    remaining = dict(joins)
    while remaining:
        progressed = False
        for target, edge in list(remaining.items()):
            if edge.source in done:
                ordered.append((target, edge))
                done.add(target)
                del remaining[target]
                progressed = True
        if not progressed:
            raise QueryError("could not order the joins")
    return ordered
