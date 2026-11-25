"""Query planner and SQL generation.

Fan-out strategy: measures are grouped by their owner model; each group becomes a CTE that
starts at the owner model and only follows many-to-one joins, so no row is repeated before
aggregation. The CTEs are then joined on the requested dimensions.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from cosmos_semantic import dialects
from cosmos_semantic.compiler import (
    DimensionRef,
    JoinEdge,
    SemanticGraph,
    metrics_under,
    offset_input_problem,
    to_date_grain_problem,
)
from cosmos_semantic.errors import PolicyError, QueryError
from cosmos_semantic.expressions import (
    COMPARISONS,
    LIST_OPS,
    Template,
    check_request_expression,
    literal,
    parse_template,
    qualify,
    ref_index,
    ref_placeholder,
    structured_template,
    substitute,
    table_ref,
)
from cosmos_semantic.ontology import RULE_DIALECT, bind_rule, rule_logic
from cosmos_semantic.spec import (
    RECOMPUTED_WINDOW_AGGS,
    WINDOW_AGGS,
    CumulativeMetric,
    DerivedMetric,
    Measure,
    RatioMetric,
    SimpleMetric,
    cumulative_agg_problem,
    offset_grain_problem,
    offset_problem,
    parse_offset,
    window_agg_refusal,
)

# Most values in the list of an `in` or `not in` structured filter.
MAX_FILTER_VALUES = 1000
FilterScalar = (
    StrictStr | StrictBool | StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)]
)
FILTERS_DESCRIPTION = (
    "Conditions on dimensions, applied to the facts before aggregation and ANDed with the row "
    "filters: standard SQL expressions with `{model.dimension}` references "
    "(`{orders.status} = 'shipped'`, a quote inside a string doubled), or objects "
    "`{dimension, op, value}` whose value the server writes as a literal of the data source's "
    "dialect, so the caller never quotes it."
)


class Filter(BaseModel):
    """A structured filter (DEC-50): a dimension, an operator and a value. The server builds
    the literal for the data source's dialect, so no caller quotes or escapes a value."""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(
        max_length=200,
        description="`model.dimension`, optionally with a time grain (`__month`), or `rule.name`.",
    )
    op: Literal["=", "!=", "<>", "<", "<=", ">", ">=", "in", "not in", "is null", "is not null"]
    value: FilterScalar | list[FilterScalar] | None = Field(
        None,
        description=(
            "A string, number or boolean; a non-empty list for `in` and `not in`; "
            "absent for `is null` and `is not null`. Dates are ISO strings."
        ),
    )

    @field_validator("op", mode="before")
    @classmethod
    def _normal_op(cls, value: Any) -> Any:
        return " ".join(value.lower().split()) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _value_fits_op(self) -> Filter:
        if self.op in COMPARISONS:
            if self.value is None or isinstance(self.value, list):
                raise ValueError(
                    f"`{self.op}` takes one value; use `is null` or `in` for null and lists"
                )
        elif self.op in LIST_OPS:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"`{self.op}` takes a non-empty list of values")
            if len(self.value) > MAX_FILTER_VALUES:
                raise ValueError(f"`{self.op}` takes at most {MAX_FILTER_VALUES} values")
        elif self.value is not None:
            raise ValueError(f"`{self.op}` takes no value")
        return self


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: list[str] = Field(min_length=1)
    dimensions: list[str] = []
    filters: list[str | Filter] = Field([], description=FILTERS_DESCRIPTION)
    order_by: list[str] = Field(
        [], description="Requested members; prefix with `-` for descending order."
    )
    metric_filters: list[str] = Field(
        [],
        description=(
            "Conditions on the values of requested metrics, applied after aggregation at the "
            "requested grain, such as `{revenue} > 1000`: a metric compared with a number "
            "(`=`, `<>`, `<`, `<=`, `>`, `>=`) or `IS [NOT] NULL`, joined by `AND`."
        ),
    )
    limit: int | None = Field(None, ge=1)
    as_of: dt.date | None = Field(
        None,
        description="Reference date for CURRENT_DATE; default: today in the model's time zone.",
    )
    rules_as_of: dt.date | None = Field(
        None,
        description=(
            "Business rule versions in effect on this date; default: `as_of`. Set it to today "
            "to apply the current rules to a past date, or to a past date for the old rules."
        ),
    )


@dataclass
class Principal:
    """Who is asking: attributes already extracted from the token, and the channel."""

    user: dict[str, Any] = field(default_factory=dict)
    channel: Literal["api", "mcp", "cli"] = "cli"
    subject: str | None = None
    # Set only for the user that `--allow-anonymous` creates (local development). Admin rights
    # depend on this marker, never on the subject's name, which any credential could carry.
    local_anonymous: bool = False

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
    rules_as_of: dt.date | None = None
    trace: QueryTrace = field(default_factory=lambda: QueryTrace())


@dataclass
class QueryTrace:
    """What the planner used to build a query: the raw material of the evidence envelope."""

    measures: dict[str, list[str | None]] = field(default_factory=dict)
    rules: dict[str, set[str]] = field(default_factory=dict)
    # rule -> effective_from of the version used (None: an unversioned rule)
    rule_versions: dict[str, dt.date | None] = field(default_factory=dict)
    row_filters: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, str]] = field(default_factory=list)
    one_to_many: list[dict[str, Any]] = field(default_factory=list)
    # Conditions on metric values (DEC-29), as applied: {"metric", "op", "value", "text"}.
    metric_filters: list[dict[str, Any]] = field(default_factory=list)
    # Rolling windows (DEC-34): per metric, the request filters on its time dimension that
    # choose the output days after the window ("after") and those that still restrict the
    # facts before it, because their shape cannot be split ("before").
    window_filters: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Input:
    """An aggregated measure, with the filter of the metric that uses it (if any)."""

    measure: str
    filter: str | None


@dataclass
class _Group:
    model: str
    inputs: dict[_Input, str] = field(default_factory=dict)
    # (N, unit) for the inputs read N days or months earlier (DEC-18); one CTE per offset.
    offset: tuple[int, str] | None = None
    # N for the facts a rolling window of N days reads (DEC-34): the request's filters on the
    # time dimension reach back far enough to fill the window of every day they keep.
    lookback: int | None = None
    # The measure whose distinct values, by the requested dimensions, a rolling window
    # recomputes from the facts (DEC-49): the CTE groups without aggregating.
    facts: str | None = None

    @property
    def cte(self) -> str:
        if self.facts is not None:
            name = f"{self.facts}_facts"
            return name if self.lookback is None else f"{name}_lookback_{self.lookback}_days"
        if self.lookback is not None:
            return f"{self.model}_agg_lookback_{self.lookback}_days"
        if self.offset is None:
            return f"{self.model}_agg"
        amount, unit = self.offset
        return f"{self.model}_agg_offset_{amount}_{unit}"


@dataclass(frozen=True)
class _OffsetUse:
    """A derived metric's input read at an earlier period, for the request checks."""

    metric: str
    input: str
    offset: str


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
        return dialects.trunc(value, grain, self.dialect)

    def _table(self, name: str, alias: str) -> exp.Table:
        return table_ref(name, self.dialect, self.schema_map).as_(alias)

    # -- public entry point ----------------------------------------------------

    def plan(self, request: QueryRequest, principal: Principal | None = None) -> CompiledQuery:
        principal = principal or Principal()
        g = self.graph
        self._trace = QueryTrace()
        now = self.now()
        as_of = request.as_of or now.date()
        self._rules_as_of = request.rules_as_of or as_of

        dims = [g.resolve_dimension(d) for d in request.dimensions]
        if len({d.alias for d in dims}) != len(dims):
            raise QueryError("dimension requested more than once")
        filters = self._request_filters(request.filters)
        for ref in dims:
            self._check_sensitivity(ref, principal)

        groups: dict[tuple[str, tuple[int, str] | None], _Group] = {}
        metric_exprs: dict[str, exp.Expression] = {}
        cumulative: dict[str, CumulativeMetric] = {}
        windows: dict[str, CumulativeMetric] = {}
        self._offset_uses: list[_OffsetUse] = []
        for name in request.metrics:
            if name in metric_exprs:
                raise QueryError(f"metric `{name}` requested more than once")
            metric_exprs[name] = self._metric_expr(name, groups, set())
            metric = g.metrics.get(name)
            if isinstance(metric, CumulativeMetric):
                (windows if metric.window else cumulative)[name] = metric
        shifts = {grp.cte: grp.offset for grp in groups.values() if grp.offset is not None}
        time = self._offset_time(dims) if shifts else None
        kept = self._metric_conditions(request, [*cumulative, *windows])

        models = {g.measure_owner[i.measure] for grp in groups.values() for i in grp.inputs}
        data_sources = {g.models[m].spec.data_source for m in models}
        if len(data_sources) > 1:
            raise QueryError("the requested metrics live in different data sources")

        ctes: list[tuple[str, exp.Select]] = []
        used_models: set[str] = set()
        for group in groups.values():
            select, joined = self._group_select(group, dims, filters, principal)
            ctes.append((group.cte, select))
            used_models |= joined
        # Rolling windows read their facts from a CTE of their own when the request filters
        # the time dimension (DEC-34); without such a filter the SQL is unchanged.
        # A window over a distinct count (DEC-49) reads the distinct values of each day from a
        # facts CTE, under the same filters, widened as above when they can be.
        lookbacks: dict[tuple[str, int], tuple[_Group, list[Template]]] = {}
        facts: dict[tuple[str, int | None], tuple[_Group, list[Template]]] = {}
        sources: dict[str, str] = {}
        for name, metric in windows.items():
            widened = self._lookback_filters(name, metric, dims, filters)
            days = metric.window_days()
            assert days is not None
            owner = g.measure_owner[metric.measure]
            if g.models[owner].measures[metric.measure].agg in RECOMPUTED_WINDOW_AGGS:
                reach = None if widened is None else days
                group, _ = facts.setdefault(
                    (metric.measure, reach),
                    (
                        _Group(owner, lookback=reach, facts=metric.measure),
                        filters if widened is None else widened,
                    ),
                )
                group.inputs.setdefault(_Input(metric.measure, None), metric.measure)
                sources[name] = group.cte
                continue
            if widened is None:
                continue
            group, _ = lookbacks.setdefault((owner, days), (_Group(owner, lookback=days), widened))
            group.inputs.setdefault(_Input(metric.measure, None), metric.measure)
            sources[name] = group.cte
        for group, widened in [*lookbacks.values(), *facts.values()]:
            select, joined = self._group_select(group, dims, widened, principal)
            ctes.append((group.cte, select))
            used_models |= joined
        for name, metric in windows.items():
            column = metric_exprs[name]
            window = self._window_select(column, metric, dims, sources.get(name))
            ctes.append((f"{name}_window", window))
            metric_exprs[name] = exp.column(column.name, table=f"{name}_window")

        final = self._final_select(
            ctes, dims, metric_exprs, cumulative, request, shifts, time, kept
        )
        clock = now if request.as_of is None else dt.datetime.combine(request.as_of, dt.time())
        final = _pin_clock(final, as_of, clock)
        columns = [OutputColumn(d.alias, "dimension", _dim_member(d)) for d in dims] + [
            OutputColumn(m, "metric", m) for m in request.metrics
        ]
        return CompiledQuery(
            sql=dialects.adapt(final, self.dialect).sql(dialect=self.dialect, pretty=True),
            dialect=self.dialect,
            columns=columns,
            data_source=next(iter(data_sources)) if data_sources else None,
            models=sorted(used_models),
            as_of=as_of,
            rules_as_of=self._rules_as_of,
            trace=self._trace,
        )

    def plan_values(
        self,
        dimension: str,
        principal: Principal | None = None,
        *,
        limit: int,
        filters: list[str | Filter] | None = None,
    ) -> CompiledQuery:
        """Distinct values of a categorical or boolean dimension, each with the number of rows
        of the dimension's own model that carry it, after that model's row filters; most
        frequent first; the caller breaks ties, not each database's collation. Personal or
        sensitive dimensions are refused
        for every channel: listing their values would enumerate personal data."""
        principal = principal or Principal()
        g = self.graph
        self._trace = QueryTrace()
        now = self.now()
        self._rules_as_of = now.date()
        ref = g.resolve_dimension(dimension)
        dim = g.dimension(ref)
        if dim.sensitivity:
            raise PolicyError(
                f"`{ref.model}.{ref.name}` is {_LEVEL[dim.sensitivity]} data (LGPD); "
                "its values are never listed"
            )
        if ref.grain or dim.type not in ("categorical", "boolean"):
            raise QueryError(
                f"`{dimension}` is a {dim.type} dimension; values are listed only for "
                "categorical and boolean dimensions (a time dimension gets its date range)"
            )
        templates = self._request_filters(filters or [])
        counts, joined = self._group_select(
            _Group(ref.model), [ref], templates, principal, label="dimension_values"
        )
        counts = counts.select(exp.Count(this=exp.Star()).as_("value_count"))
        final = (
            exp.select(exp.column(ref.alias), exp.column("value_count"))
            .from_("dimension_values")
            .with_("dimension_values", as_=counts)
            .order_by(exp.Ordered(this=exp.column("value_count"), desc=True))
            .limit(limit)
        )
        final = _pin_clock(final, now.date(), now)
        return CompiledQuery(
            sql=dialects.adapt(final, self.dialect).sql(dialect=self.dialect, pretty=True),
            dialect=self.dialect,
            columns=[OutputColumn(ref.alias, "dimension", _dim_member(ref))],
            data_source=g.models[ref.model].spec.data_source,
            models=sorted(joined),
            as_of=now.date(),
            rules_as_of=self._rules_as_of,
            trace=self._trace,
        )

    def plan_time_range(
        self,
        dimension: str,
        principal: Principal | None = None,
        *,
        filters: list[str | Filter] | None = None,
    ) -> CompiledQuery:
        """The first and last day of a time dimension that hold rows of its own model, and how
        many distinct days do, after that model's row filters (DEC-24). Days are the model's
        local days, the business time zone, as in a `__day` grouping: the dimension is
        truncated to the day by the same path, then the days are aggregated. Rows whose time is
        null hold no day. With no rows, the minimum and maximum are null and the count is 0.
        Personal or sensitive dimensions are refused for every channel, as in ``plan_values``."""
        principal = principal or Principal()
        g = self.graph
        self._trace = QueryTrace()
        now = self.now()
        self._rules_as_of = now.date()
        ref = g.resolve_dimension(dimension)
        dim = g.dimension(ref)
        if dim.sensitivity:
            raise PolicyError(
                f"`{ref.model}.{ref.name}` is {_LEVEL[dim.sensitivity]} data (LGPD); "
                "its range is never listed"
            )
        if dim.type != "time":
            raise QueryError(f"`{dimension}` is a {dim.type} dimension, not a time dimension")
        if ref.grain:
            raise QueryError(
                f"`{dimension}` has a grain; ask for `{ref.model}.{ref.name}`: "
                "the range is always in days"
            )
        day = DimensionRef(ref.model, ref.name, "day")
        templates = self._request_filters(filters or [])
        days, joined = self._group_select(
            _Group(ref.model), [day], templates, principal, label="time_range"
        )
        column = exp.column(day.alias)
        final = (
            exp.select(
                exp.Min(this=column.copy()).as_("min_day"),
                exp.Max(this=column.copy()).as_("max_day"),
                # COUNT(column) skips the null day of rows without a time.
                exp.Count(this=column.copy()).as_("distinct_days"),
            )
            .from_("time_range")
            .with_("time_range", as_=days)
        )
        final = _pin_clock(final, now.date(), now)
        return CompiledQuery(
            sql=dialects.adapt(final, self.dialect).sql(dialect=self.dialect, pretty=True),
            dialect=self.dialect,
            columns=[OutputColumn(day.alias, "dimension", _dim_member(ref))],
            data_source=g.models[ref.model].spec.data_source,
            models=sorted(joined),
            as_of=now.date(),
            rules_as_of=self._rules_as_of,
            trace=self._trace,
        )

    # -- request filters ---------------------------------------------------------

    def _request_filters(self, filters: list[str | Filter]) -> list[Template]:
        """The request's filters as templates: an expression string is parsed as standard SQL
        (DEC-53) and checked; a structured filter (DEC-50) is built as the same tree, its value
        checked against the dimension's type and written as a literal by SQLGlot, never parsed
        from text."""
        templates = []
        for item in filters:
            if isinstance(item, str):
                template = parse_template(item, self.dialect, error=QueryError)
                check_request_expression(template)
            else:
                template = structured_template(item.dimension, item.op, item.value, self.dialect)
                self._check_filter_value(item, template.text)
            templates.append(template)
        return templates

    def _check_filter_value(self, item: Filter, text: str) -> None:
        g = self.graph
        try:
            ref = g.resolve_dimension(item.dimension)
        except QueryError:
            # A rule that applies to several models is resolved per model by the planner.
            name = item.dimension[5:] if item.dimension.startswith("rule.") else None
            if name and g.rule_models.get(name):
                return
            raise
        kind = g.dimension(ref).type
        values = item.value if isinstance(item.value, list) else [item.value]
        values = [v for v in values if v is not None]
        if kind == "boolean":
            ok = all(isinstance(v, bool) for v in values)
            expected = "true or false"
        elif kind == "number":
            ok = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
            expected = "numbers"
        elif kind == "time":
            ok = all(isinstance(v, str) and _iso_moment(v) for v in values)
            expected = "ISO dates or date-times as strings (`2026-01-31`)"
        else:
            ok = all(not isinstance(v, bool) for v in values)
            expected = "strings or numbers"
        if not ok:
            raise QueryError(
                f"filter `{text}`: `{item.dimension}` is a {kind} dimension; "
                f"compare it with {expected}"
            )

    def _rule_expr(self, ref: DimensionRef) -> str:
        """The rule version in effect on ``rules_as_of``, bound to the model's columns."""
        rule = self.graph.rules[ref.name]
        version = rule.version_at(self._rules_as_of)
        self._trace.rule_versions[ref.name] = version.effective_from
        return bind_rule(rule_logic(version), self.graph.rule_bindings[(ref.name, ref.model)])

    def _check_sensitivity(self, ref: DimensionRef, principal: Principal) -> None:
        level = self.graph.dimension(ref).sensitivity
        if principal.is_agent and level:
            raise PolicyError(
                f"`{ref.model}.{ref.name}` is {_LEVEL[level]} data (LGPD) and never reaches "
                "AI agents, neither as a grouping nor as a filter"
            )

    # -- metrics ---------------------------------------------------------------

    def _metric_expr(
        self,
        name: str,
        groups: dict[tuple[str, tuple[int, str] | None], _Group],
        stack: set[str],
        extra_filter: str | None = None,
        offset: tuple[int, str] | None = None,
    ) -> exp.Expression:
        g = self.graph
        if name in g.measure_owner:
            return self._input(_Input(name, extra_filter), groups, offset)
        metric = g.metrics.get(name)
        if metric is None:
            raise QueryError(f"metric `{name}` does not exist")
        if name in stack:
            raise QueryError(f"metric cycle at `{name}`")
        stack = stack | {name}

        if isinstance(metric, SimpleMetric):
            item = _Input(metric.measure, _and(metric.filter, extra_filter))
            return self._input(item, groups, offset)
        if isinstance(metric, RatioMetric):
            flt = _and(metric.filter, extra_filter)
            num = self._metric_expr(metric.numerator, groups, stack, flt, offset)
            den = self._metric_expr(metric.denominator, groups, stack, flt, offset)
            return exp.Div(
                this=exp.Cast(this=num, to=exp.DataType.build("double")),
                expression=exp.Nullif(this=den, expression=exp.Literal.number(0)),
            )
        if isinstance(metric, DerivedMetric):
            template = parse_template(metric.expr, self.dialect)
            template.tree = template.tree.transform(_safe_division)
            shifted = metric.offsets()
            if shifted and offset is not None:
                raise QueryError(
                    f"`{name}` has offsets and is read with an offset itself; "
                    "offsets do not combine yet"
                )

            def resolve(ref: str) -> exp.Expression:
                if ref not in shifted:
                    return self._metric_expr(ref, groups, stack, extra_filter, offset)
                text = shifted[ref]
                # An alias reads its input's metric (DEC-44); the CTE is the metric's model's
                # at that offset, so an unshifted read of the same metric stays apart.
                read = metric.target(ref)
                problem = offset_problem(text)
                if problem:
                    raise QueryError(f"`{name}`: {problem}")
                for under in metrics_under(g, read):
                    problem = offset_input_problem(g.metrics[under], read)
                    if problem:
                        raise QueryError(f"`{name}`: {problem}")
                self._offset_uses.append(_OffsetUse(name, read, text))
                return self._metric_expr(read, groups, stack, extra_filter, parse_offset(text))

            return exp.Paren(this=substitute(template, resolve))
        if isinstance(metric, CumulativeMetric):
            if metric.window:
                if metric.window_days() is None:
                    raise QueryError(
                        f"`{name}`: window `{metric.window}` is not supported; "
                        "only windows in days or weeks, such as `7 days` or `2 weeks`"
                    )
            agg = g.models[g.measure_owner[metric.measure]].measures[metric.measure].agg
            # Model validation refuses both too; kept for graphs compiled without `strict`.
            if metric.window:
                problem = window_agg_refusal(agg)
            else:
                problem = cumulative_agg_problem(metric, agg)
            if problem:
                raise QueryError(f"`{name}`: {problem}")
            if extra_filter or stack != {name}:
                raise QueryError(f"`{name}`: a cumulative metric cannot be part of another metric")
            return self._input(_Input(metric.measure, None), groups)
        raise QueryError(f"unsupported metric type in `{name}`")

    def _input(
        self,
        item: _Input,
        groups: dict[tuple[str, tuple[int, str] | None], _Group],
        offset: tuple[int, str] | None = None,
    ) -> exp.Expression:
        owner = self.graph.measure_owner[item.measure]
        group = groups.setdefault((owner, offset), _Group(owner, offset=offset))
        if item not in group.inputs:
            suffix = "" if item.filter is None else f"__f{len(group.inputs)}"
            group.inputs[item] = f"{item.measure}{suffix}"
        return exp.column(group.inputs[item], table=group.cte)

    def _offset_time(self, dims: list[DimensionRef]) -> DimensionRef:
        """The request's time dimension for inputs read at an earlier period (DEC-18): exactly
        one, at a grain each offset is a whole number of periods of."""
        uses = self._offset_uses
        first = uses[0]
        what = f"`{first.metric}` reads `{first.input}` {first.offset} earlier, so it"
        time_dims = [d for d in dims if self.graph.dimension(d).type == "time"]
        if len(time_dims) != 1:
            found = "none" if not time_dims else f"{len(time_dims)}"
            raise QueryError(
                f"{what} needs exactly one time dimension in the request ({found} requested)"
            )
        time = time_dims[0]
        for use in uses:
            problem = offset_grain_problem(use.offset, time.grain)
            if problem:
                raise QueryError(f"`{use.metric}` reads `{use.input}` earlier: {problem}")
        return time

    def _metric_conditions(
        self, request: QueryRequest, running: list[str]
    ) -> exp.Expression | None:
        """The request's ``metric_filters`` as one condition over the output columns (DEC-29),
        or None without them. Each filter is parsed strictly by ``parse_metric_filter``; only
        requested metrics are compared. Running totals, rolling windows and inputs read at an
        earlier period are refused for now as a first-slice limit; they are computed inside
        ``cosmos_result`` before the outer WHERE, so lifting it needs no change to the SQL."""
        if not request.metric_filters:
            return None
        if len(request.metric_filters) > MAX_METRIC_FILTERS:
            raise QueryError(f"at most {MAX_METRIC_FILTERS} metric_filters are accepted")
        if running:
            raise QueryError(
                f"metric_filters do not combine with cumulative metrics yet (`{running[0]}` "
                "requested); request it without metric_filters"
            )
        if self._offset_uses:
            use = self._offset_uses[0]
            raise QueryError(
                f"metric_filters do not combine with metrics that read an input at an earlier "
                f"period yet (`{use.metric}` reads `{use.input}` {use.offset} earlier)"
            )
        conditions: list[exp.Expression] = []
        for text in request.metric_filters:
            for item in parse_metric_filter(text):
                if item.metric not in request.metrics:
                    raise QueryError(
                        f"metric filter `{text}` compares `{item.metric}`, which is not among "
                        "the requested metrics; add it to `metrics`"
                    )
                self._trace.metric_filters.append(
                    {"metric": item.metric, "op": item.op, "value": item.value, "text": item.text}
                )
                conditions.append(item.condition())
        return exp.and_(*conditions)

    # -- one CTE per owner model -------------------------------------------------

    def _group_select(
        self,
        group: _Group,
        dims: list[DimensionRef],
        filters: list[Template],
        principal: Principal,
        extra: list[tuple[str, str, str]] | None = None,
        label: str | None = None,
    ) -> tuple[exp.Select, set[str]]:
        """One aggregation CTE starting at ``group.model``.

        Dimensions and filters the base cannot reach through many-to-one joins (for example,
        product category for order revenue) go through a *bridge*: distinct pairs of the base
        key and those dimensions, built from a model that reaches both sides. Each base row then
        counts once per value it relates to, never once per child row.
        """
        g = self.graph
        base = group.model
        label = label or group.cte
        joins: dict[str, JoinEdge] = {}
        reach = g.reachable(base)

        def resolve_request(r: str) -> DimensionRef:
            r = _unshifted(r)[0]
            return g.resolve_dimension(r, context=base if r.startswith("rule.") else None)

        remote_dims = [d for d in dims if d.model not in reach]
        local_filters, remote_filters = [], []
        for template in filters:
            models = set()
            for r in template.refs:
                try:
                    models.add(resolve_request(r).model)
                except QueryError:
                    models.add(base)
            outside = {m for m in models if m not in reach}
            if outside and outside != models:
                raise QueryError(
                    f"filter `{template.text}` mixes members reachable from `{base}` with "
                    "members that are not; split it into separate filters"
                )
            (remote_filters if outside else local_filters).append(template)
        filters = local_filters
        bridge = None
        if remote_dims or remote_filters:
            bridge = self._bridge(base, remote_dims, remote_filters, principal, label)

        def need(model: str) -> None:
            for edge in g.join_path(base, model):
                joins.setdefault(edge.target, edge)

        def dim_sql(ref: DimensionRef, condition: bool = False, shift: int = 0) -> exp.Expression:
            if ref.model != base and ref.model not in g.reachable(base):
                raise QueryError(
                    f"`{ref.model}.{ref.name}` is not reachable from `{base}` "
                    "(only many-to-one joins are allowed)"
                )
            need(ref.model)
            dim = g.dimension(ref)
            expr, read = dim.expr or dim.name, self.dialect
            if ref.model in g.rule_models.get(ref.name, ()):
                self._trace.rules.setdefault(ref.name, set()).add(
                    "filter" if condition else "value"
                )
                expr, read = self._rule_expr(ref), RULE_DIALECT
            column = qualify(expr, ref.model, read)
            if shift:
                # A time reference read N days later (DEC-34): shifted before the truncation.
                column = dialects.add_days(column, shift)
            if ref.grain:
                column = self._trunc(column, ref.grain)
            if dim.type == "boolean" and self.dialect == "tsql":
                column = _tsql_boolean(column, condition)
            return column

        def request_ref(r: str, condition: bool = False) -> exp.Expression:
            r, shift = _unshifted(r)
            ref = g.resolve_dimension(r, context=base if r.startswith("rule.") else None)
            self._check_sensitivity(ref, principal)
            return dim_sql(ref, condition, shift)

        def model_ref(context: str):
            return lambda r, condition: dim_sql(g.resolve_dimension(r, context=context), condition)

        def fill(template: Template, resolver, user=None) -> exp.Expression:
            return substitute(
                template,
                lambda r: resolver(r, False),
                user,
                resolve_condition=lambda r: resolver(r, True),
            )

        dim_exprs = [
            exp.column(d.alias, table="bridge")
            if bridge is not None and d in remote_dims
            else dim_sql(d)
            for d in dims
        ]
        projections = [e.copy().as_(d.alias) for e, d in zip(dim_exprs, dims, strict=True)]
        for alias, model, expr in extra or []:
            need(model)
            column = qualify(expr, model, self.dialect)
            dim_exprs.append(column)
            projections.append(column.copy().as_(alias))
        conditions = [fill(t, request_ref) for t in filters]

        for item, alias in group.inputs.items():
            self._trace.measures.setdefault(item.measure, [])
            if item.filter not in self._trace.measures[item.measure]:
                self._trace.measures[item.measure].append(item.filter)
            measure = g.models[base].measures[item.measure]
            condition = (
                fill(parse_template(item.filter, self.dialect), model_ref(base))
                if item.filter
                else None
            )
            if group.facts is not None:
                # The distinct values themselves, grouped with the dimensions (DEC-49).
                value = qualify(measure.expr or measure.name, base, self.dialect)
                if condition is not None:
                    value = exp.Case(ifs=[exp.If(this=condition, true=value)])
                dim_exprs.append(value)
                projections.append(value.copy().as_(alias))
                continue
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
                    self._trace.row_filters.append(
                        {
                            "model": model,
                            "name": row_filter.name,
                            "filter": row_filter.filter,
                            "user_fields": {f: principal.user.get(f) for f in template.user_fields},
                            "applied_in": label,
                        }
                    )
                    conditions.append(
                        fill(template, model_ref(model), self._user_resolver(principal))
                    )

        select = exp.select(*projections).from_(self._table(g.models[base].spec.table, base))
        for target, edge in _ordered(joins, base):
            self._trace.joins.append(
                {"from": edge.source, "to": target, "entity": edge.entity, "in": label}
            )
            on = exp.EQ(
                this=qualify(edge.source_expr, edge.source, self.dialect),
                expression=qualify(edge.target_expr, edge.target, self.dialect),
            )
            select = select.join(
                self._table(g.models[target].spec.table, target), on=on, join_type="left"
            )
        if bridge is not None:
            bridge_select, key_expr, inner = bridge
            select = select.join(
                bridge_select.subquery("bridge"),
                on=exp.EQ(
                    this=qualify(key_expr, base, self.dialect),
                    expression=exp.column("cosmos_bridge_key", table="bridge"),
                ),
                join_type="inner" if inner else "left",
            )
        if conditions:
            select = select.where(exp.and_(*conditions))
        if dim_exprs:
            # Group by expressions, not ordinals: SQL Server does not accept GROUP BY 1.
            select = select.group_by(*[e.copy() for e in dim_exprs])
        return select, {base, *joins}

    def _bridge(
        self,
        base: str,
        dims: list[DimensionRef],
        filters: list[Template],
        principal: Principal,
        label: str,
    ) -> tuple[exp.Select, str, bool]:
        """Distinct (base key, dimensions) pairs from a model that reaches both sides."""
        g = self.graph
        targets = {d.model for d in dims}
        for template in filters:
            targets |= {g.resolve_dimension(_unshifted(r)[0]).model for r in template.refs}
        targets.discard(base)
        candidates = [
            m
            for m in g.models
            if m != base and base in g.reachable(m) and targets <= g.reachable(m)
        ]
        names = ", ".join(sorted(f"{d.model}.{d.name}" for d in dims)) or "the filter"
        if not candidates:
            raise QueryError(
                f"{names} is not reachable from `{base}`, and no model links them: one-to-many "
                f"attribution needs a model that reaches both `{base}` and the dimension"
            )
        distance = {m: len(g.join_path(m, base)) for m in candidates}
        best = min(distance.values())
        closest = sorted(m for m in candidates if distance[m] == best)
        if len(closest) > 1:
            raise QueryError(
                f"{names} can be attributed to `{base}` through several models "
                f"({', '.join(closest)}); the attribution would be ambiguous"
            )
        via = closest[0]
        key = next((e for e in g.models[base].entities.values() if e.type == "primary"), None)
        if key is None:
            raise QueryError(f"`{base}` has no primary entity; one-to-many attribution needs one")
        key_expr = key.expr or key.name
        bridge_select, _ = self._group_select(
            _Group(via),
            dims,
            filters,
            principal,
            extra=[("cosmos_bridge_key", base, key_expr)],
            label=f"{label} bridge via {via}",
        )
        self._trace.one_to_many.append(
            {
                "base": base,
                "via": via,
                "dimensions": [f"{d.model}.{d.name}" for d in dims],
                "filters": [t.text for t in filters],
                "in": label,
            }
        )
        return bridge_select, key_expr, bool(filters)

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
        shifts: dict[str, tuple[int, str]] | None = None,
        time: DimensionRef | None = None,
        kept: exp.Expression | None = None,
    ) -> exp.Select:
        shifts = shifts or {}
        # A CTE only feeding another one (an aggregate under a rolling window) is not joined.
        used = {c.table for e in metric_exprs.values() for c in e.find_all(exp.Column)}
        names = [name for name, _ in ctes if name in used]
        # With more than one CTE and dimensions, the "spine" is the union of the dimension
        # combinations of all CTEs; each CTE is LEFT JOINed to it with null-safe equality.
        # Postgres rejects FULL JOIN with IS NOT DISTINCT FROM. A CTE of inputs read at an
        # earlier period (DEC-18) adds its own periods to the spine, unshifted, and joins on
        # its period moved forward by the offset: the rows are the periods with facts, and a
        # period whose earlier period has none gets NULL. Plain equality on the period, so a
        # null period never matches.
        use_spine = (len(names) > 1 or bool(shifts)) and bool(dims)
        anchor = "dimensions" if use_spine else names[0]

        def dim_out(d: DimensionRef) -> exp.Expression:
            return exp.column(d.alias, table=anchor)

        projections: list[exp.Expression] = [dim_out(d).as_(d.alias) for d in dims]
        for name, expression in metric_exprs.items():
            if name in cumulative:
                expression = self._cumulative(expression, cumulative[name], dims, dim_out)
            projections.append(expression.as_(name))

        body = select = exp.select(*projections).from_(exp.to_table(anchor))
        if kept is not None:
            # Metric filters (DEC-29): an outer WHERE over the result at the requested grain,
            # read from a CTE (SQL Server accepts WITH only at the top). Ordering and the limit
            # apply to the rows kept.
            outputs = [d.alias for d in dims] + list(metric_exprs)
            select = exp.select(*[exp.column(c) for c in outputs]).from_(_FILTERED).where(kept)
        if use_spine:
            for name in names:
                conditions: list[exp.Expression] = []
                for d in dims:
                    mine = exp.column(d.alias, table=name)
                    if name in shifts and d == time:
                        moved = dialects.shift(mine, *shifts[name])
                        conditions.append(exp.EQ(this=dim_out(d), expression=moved))
                    else:
                        conditions.append(exp.NullSafeEQ(this=dim_out(d), expression=mine))
                body = body.join(exp.to_table(name), on=exp.and_(*conditions), join_type="left")
        else:
            for name in names[1:]:
                body = body.join(exp.to_table(name), join_type="cross")
        if kept is None:
            select = body
        for name, cte in ctes:
            select = select.with_(name, as_=cte)
        if use_spine:
            parts = [exp.select(*[exp.column(d.alias) for d in dims]).from_(n) for n in names]
            spine: exp.Query = parts[0]
            for part in parts[1:]:
                spine = exp.union(spine, part, distinct=True)
            select = select.with_(anchor, as_=spine)
        if kept is not None:
            select = select.with_(_FILTERED, as_=body)

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

    def _time_dim(self, metric: CumulativeMetric, dims: list[DimensionRef]) -> DimensionRef:
        time_dims = [d for d in dims if self.graph.dimension(d).type == "time"]
        if len(time_dims) != 1:
            raise QueryError(f"`{metric.name}` needs exactly one time dimension in the request")
        return time_dims[0]

    def _lookback_filters(
        self,
        name: str,
        metric: CumulativeMetric,
        dims: list[DimensionRef],
        filters: list[Template],
    ) -> list[Template] | None:
        """The request's filters for the facts a rolling window reads (DEC-34), or None when
        none of them is on the window's time dimension or none can be split.

        A filter on the time dimension (the column, or it at day grain or coarser) made of
        ranges (``>=``, ``>``, ``<``, ``<=``, ``BETWEEN``), equalities and ``IN`` lists against
        values without references, joined by ``AND`` and ``OR`` (``AND`` only with conditions
        on other members), is widened: each lower bound is read N - 1 days later (N for the
        column itself, whose time of day the bound may cut), so the facts reach back far enough
        to fill the window of every day the filter keeps. Upper bounds stay as written at a
        whole-day grain; on the column itself they are read one day earlier, so a bound inside
        the last day does not cut that day's later facts out of its window. Any other filter on the time dimension (``NOT``, a
        function of it, ``<>``, an hour grain) still restricts the facts before the window, as
        before DEC-34. Filters on other members are unchanged."""
        if not filters:
            return None
        time = self._time_dim(metric, dims)
        days = metric.window_days()
        if time.grain != "day" or days is None:
            return None  # refused by `_window_select`
        widened: list[Template] = []
        after: list[str] = []
        before: list[str] = []
        for template in filters:
            hits = {
                i: ref
                for i, r in enumerate(template.refs)
                if (ref := self._same_dimension(r, time)) is not None
            }
            if not hits:
                widened.append(template)
                continue
            split = _widen(template, hits, days)
            if split is None:
                widened.append(template)
                before.append(template.text)
            else:
                widened.append(split)
                after.append(template.text)
        if after or before:
            self._trace.window_filters[name] = {"after": after, "before": before}
        return widened if after else None

    def _same_dimension(self, r: str, time: DimensionRef) -> DimensionRef | None:
        """``r`` resolved, when it is ``time``'s dimension at any grain or none."""
        if r.startswith("rule."):
            return None
        try:
            ref = self.graph.resolve_dimension(r)
        except QueryError:
            return None
        return ref if (ref.model, ref.name) == (time.model, time.name) else None

    def _window_select(
        self,
        value: exp.Column,
        metric: CumulativeMetric,
        dims: list[DimensionRef],
        lookback: str | None = None,
    ) -> exp.Select:
        """Rolling window as a self-join of the daily aggregate, portable to every source (SQL
        Server has no RANGE frame with an interval): each day ``cur`` combines the rows ``prev``
        of the same other dimensions whose day is in [cur - (N - 1) days, cur]. The output has
        the days of the aggregate, as ``grain_to_date``; a null day has a null value.

        With ``lookback`` (DEC-34), ``prev`` reads that CTE instead: the same daily aggregate,
        with the request's filters on the time dimension widened to reach N - 1 days further
        back. ``cur`` keeps the request's filters, so they choose the output days, and the
        window of each day is full.

        A window over a distinct count (DEC-49) is recomputed: ``prev`` is ``lookback``, the
        facts CTE of the distinct values of each day, and the window counts the values of its
        days once each, so a customer seen on several days of the window counts once."""
        time = self._time_dim(metric, dims)
        if time.grain != "day":
            raise QueryError(
                f"`{metric.name}`: a rolling window needs the time dimension at day grain "
                f"(`{_dim_member(time)}` requested); other grains are not supported yet"
            )
        days = metric.window_days()
        assert days is not None
        source = exp.to_table(value.table)
        earlier = exp.to_table(lookback) if lookback else source
        cur = {d: exp.column(d.alias, table="cur") for d in dims}
        prev = {d: exp.column(d.alias, table="prev") for d in dims}
        on = [
            exp.LTE(this=prev[time], expression=cur[time]),
            exp.GTE(this=dialects.add_days(prev[time], days - 1), expression=cur[time]),
        ] + [exp.NullSafeEQ(this=prev[d], expression=cur[d]) for d in dims if d != time]
        owner = self.graph.measure_owner[metric.measure]
        agg = self.graph.models[owner].measures[metric.measure].agg
        if agg in RECOMPUTED_WINDOW_AGGS:
            assert lookback is not None
            distinct = exp.Count(
                this=exp.Distinct(expressions=[exp.column(value.name, table="prev")])
            )
            # A null day matches no day, as in the other windows: its value is null, not 0.
            combined: exp.Expression = exp.Case(
                ifs=[
                    exp.If(
                        this=exp.Is(this=cur[time].copy(), expression=exp.null()),
                        true=exp.null(),
                    )
                ],
                default=distinct,
            )
        else:
            combined = _COMBINE_AGGS[agg](this=exp.column(value.name, table="prev"))
        return (
            exp.select(*[c.copy().as_(d.alias) for d, c in cur.items()], combined.as_(value.name))
            .from_(source.copy().as_("cur"))
            .join(earlier.copy().as_("prev"), on=exp.and_(*on), join_type="left")
            .group_by(*[c.copy() for c in cur.values()])
        )

    def _cumulative(self, value, metric: CumulativeMetric, dims, dim_out) -> exp.Expression:
        time = self._time_dim(metric, dims)
        problem = metric.grain_to_date and to_date_grain_problem(metric.grain_to_date, time.grain)
        if problem:
            raise QueryError(f"`{metric.name}` (`{_dim_member(time)}` requested): {problem}")
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
        owner = self.graph.measure_owner[metric.measure]
        agg = self.graph.models[owner].measures[metric.measure].agg
        return exp.Window(
            this=_COMBINE_AGGS[agg](this=value),
            partition_by=partition or None,
            order=exp.Order(
                expressions=[
                    exp.Ordered(this=null_last, nulls_first=default_nulls_first),
                    exp.Ordered(this=dim_out(time), nulls_first=default_nulls_first),
                ]
            ),
            spec=spec,
        )


# -- rolling windows under a filter on their time dimension (DEC-34) -------------------------

# A reference read N days later is written ``{member}@N`` in a widened template's refs; the
# ``{member}`` syntax cannot produce ``@``, so a request never does.
_SHIFT = "@"
# Grains whose period holds whole days: a bound on them holds for the whole day or not at all.
_WHOLE_DAY_GRAINS = ("day", "week", "month", "quarter", "year")
_FLIPPED: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.GT: exp.LT,
    exp.GTE: exp.LTE,
    exp.LT: exp.GT,
    exp.LTE: exp.GTE,
    exp.EQ: exp.EQ,
}


def _unshifted(r: str) -> tuple[str, int]:
    """A reference of a widened template and the days it is read later (0 for the others)."""
    head, mark, days = r.partition(_SHIFT)
    return (head, int(days)) if mark else (r, 0)


def _widen(template: Template, hits: dict[int, DimensionRef], days: int) -> Template | None:
    """``template`` with its bounds on the time dimension reaching ``days`` - 1 days further
    back (see ``Planner._lookback_filters``), or None when its shape cannot be split.

    The result only has to keep a superset of the facts the windows need: the output days are
    chosen by the original filter, and the self-join keeps only the N days of each window. For
    a day D kept by the filter and a fact on day P in [D - (N - 1), D], a bound ``g(t) >= X``
    held on D (``g`` the truncation to the reference's grain, or none) holds for
    ``g(P + N - 1 days)``, since ``g`` is monotonic and gives the same value for every moment of
    a day; on the column itself, a moment of D is before P + N days, hence the extra day.

    Upper bounds need nothing later, but the window of D reads all of D: on the column itself
    a bound ``t <= Y`` held by a moment of D may still cut D's later facts, so it is read one
    day earlier (``t - 1 day <= Y``), which every moment of D meets. At a whole-day grain a
    bound holds for the whole day, so it stays as written. Each bound is widened on its own, so
    ``OR`` of ranges and ``IN`` lists (an ``OR`` of equalities) stay supersets too."""
    refs = list(template.refs)

    def time_ref(node: exp.Expression) -> bool:
        index = ref_index(node)
        if index is None or index not in hits:
            return False
        grain = hits[index].grain
        return grain is None or grain in _WHOLE_DAY_GRAINS

    def constant(node: exp.Expression) -> bool:
        # Request filters reach columns only through references (check_request_expression).
        return not any(True for _ in node.find_all(exp.Column))

    def moved(node: exp.Expression, shift: int) -> exp.Expression:
        index = ref_index(node)
        assert index is not None
        if shift == 0:
            return node.copy()
        refs.append(f"{template.refs[index]}{_SHIFT}{shift}")
        return ref_placeholder(len(refs) - 1)

    def column(node: exp.Expression) -> bool:
        index = ref_index(node)
        return index is not None and hits[index].grain is None

    def later(node: exp.Expression) -> exp.Expression:
        """For a lower bound: N days later on the column, N - 1 at a whole-day grain."""
        return moved(node, days if column(node) else days - 1)

    def earlier(node: exp.Expression) -> exp.Expression:
        """For an upper bound: one day earlier on the column, as written at a grain."""
        return moved(node, -1 if column(node) else 0)

    def equal(ref: exp.Expression, value: exp.Expression) -> exp.Expression:
        return exp.And(
            this=exp.LTE(this=earlier(ref), expression=value.copy()),
            expression=exp.GTE(this=later(ref), expression=value.copy()),
        )

    def bound(node: exp.Expression) -> exp.Expression | None:
        if type(node) in _FLIPPED:
            op: type[exp.Expression] = type(node)
            ref, value = node.this, node.expression
            if time_ref(value) and constant(ref):
                op, ref, value = _FLIPPED[op], value, ref
            if not (time_ref(ref) and constant(value)):
                return None
            if op is exp.EQ:
                return exp.Paren(this=equal(ref, value))
            if op in (exp.GT, exp.GTE):
                return op(this=later(ref), expression=value.copy())
            return op(this=earlier(ref), expression=value.copy())
        if isinstance(node, exp.Between):
            low, high = node.args.get("low"), node.args.get("high")
            if node.args.get("symmetric") or not time_ref(node.this):
                return None
            if low is None or high is None or not (constant(low) and constant(high)):
                return None
            return exp.Paren(
                this=exp.And(
                    this=exp.GTE(this=later(node.this), expression=low.copy()),
                    expression=exp.LTE(this=earlier(node.this), expression=high.copy()),
                )
            )
        if isinstance(node, exp.In):
            values = node.expressions
            if any(node.args.get(k) for k in ("query", "unnest", "field")):
                return None  # not a list of values
            if not values or not time_ref(node.this) or not all(constant(v) for v in values):
                return None
            parts = [exp.Paren(this=equal(node.this, v)) for v in values]
            return exp.Paren(this=exp.or_(*parts)) if len(parts) > 1 else parts[0]
        return None

    def widen(node: exp.Expression, under_or: bool) -> exp.Expression | None:
        if isinstance(node, exp.Paren):
            inner = widen(node.this, under_or)
            return None if inner is None else exp.Paren(this=inner)
        if isinstance(node, exp.And | exp.Or):
            nested = under_or or isinstance(node, exp.Or)
            left, right = widen(node.this, nested), widen(node.expression, nested)
            if left is None or right is None:
                return None
            return type(node)(this=left, expression=right)
        if not any(ref_index(c) in hits for c in node.find_all(exp.Column)):
            # Another member: as written under AND; under OR it would mix with the time bounds.
            return None if under_or else node.copy()
        return bound(node)

    tree = widen(template.tree, False)
    if tree is None:
        return None
    return Template(text=template.text, tree=tree, refs=refs, user_fields=template.user_fields)


# How per-period aggregates combine over a rolling window or a running (to-date) cumulative
# metric; other aggregations are refused (DEC-21).
_COMBINE_AGGS = {"sum": exp.Sum, "count": exp.Sum, "min": exp.Min, "max": exp.Max}
assert tuple(_COMBINE_AGGS) == WINDOW_AGGS

_LEVEL = {"personal": "personal", "sensitive": "sensitive personal"}


def _iso_moment(text: str) -> bool:
    """Whether ``text`` is an ISO date or date-time, as a structured filter on time takes."""
    try:
        dt.datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


# -- metric filters (DEC-29) ---------------------------------------------------------------

# The CTE holding the result before the metric filters; the filtered select reads it.
_FILTERED = "cosmos_result"
MAX_METRIC_FILTERS = 20
MAX_METRIC_FILTER_LENGTH = 500

_COMPARISONS: dict[str, type[exp.Expression]] = {
    "=": exp.EQ,
    "<>": exp.NEQ,
    "<": exp.LT,
    "<=": exp.LTE,
    ">": exp.GT,
    ">=": exp.GTE,
}
# The whole grammar. Anything else (functions, columns, strings, parentheses, OR, comments,
# semicolons, quotes) is refused: the text never reaches the SQL, only the parsed parts do.
_METRIC_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<ref>\{[a-z_][a-z0-9_]{0,63}\})"
    r"|(?P<op><=|>=|<>|=|<|>)"
    r"|(?P<number>-?[0-9]{1,18}(?:\.[0-9]{1,18})?)(?![0-9A-Za-z_.])"
    r"|(?P<word>[A-Za-z]+)(?![0-9A-Za-z_])"
    r")"
)
_NUMBER = re.compile(r"-?[0-9]{1,18}(?:\.[0-9]{1,18})?")


@dataclass(frozen=True)
class MetricCondition:
    """One comparison of a requested metric's value: ``op`` is a comparison with ``value`` (a
    number, kept as its validated text), or ``IS NULL`` / ``IS NOT NULL`` with no value."""

    metric: str
    op: str
    value: str | None

    @property
    def text(self) -> str:
        return f"{{{self.metric}}} {self.op}" + ("" if self.value is None else f" {self.value}")

    def condition(self) -> exp.Expression:
        column = exp.column(self.metric)
        if self.value is None:
            test = exp.Is(this=column, expression=exp.null())
            return exp.Not(this=test) if self.op == "IS NOT NULL" else test
        # The value is rebuilt from digits the grammar matched; the sign becomes a negation.
        assert _NUMBER.fullmatch(self.value)
        number: exp.Expression = exp.Literal.number(self.value.lstrip("-"))
        if self.value.startswith("-"):
            number = exp.Neg(this=number)
        return _COMPARISONS[self.op](this=column, expression=number)


def parse_metric_filter(text: str) -> list[MetricCondition]:
    """Parses one metric filter strictly: ``{metric} <op> <number>`` or ``{metric} IS [NOT]
    NULL``, joined by ``AND`` (keywords in any case). Numbers are plain decimals, such as
    ``1000``, ``-2.5`` or ``0.25``: no exponent, no quotes, at most 18 digits on each side of
    the point. Raises QueryError on anything else."""
    if not isinstance(text, str) or not text.strip():
        raise QueryError("a metric filter cannot be empty")
    if len(text) > MAX_METRIC_FILTER_LENGTH:
        raise QueryError(f"a metric filter has at most {MAX_METRIC_FILTER_LENGTH} characters")
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _METRIC_TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            raise QueryError(
                f"metric filter `{text}`: unexpected text at position {pos + 1}; use "
                "`{metric} <op> <number>` or `{metric} IS [NOT] NULL`, joined by AND"
            )
        kind = match.lastgroup
        assert kind is not None
        value = match.group(kind)
        tokens.append((kind, value.upper() if kind == "word" else value))
        pos = match.end()

    def fail(detail: str) -> QueryError:
        return QueryError(
            f"metric filter `{text}`: {detail}; use `{{metric}} <op> <number>` or "
            "`{metric} IS [NOT] NULL`, joined by AND"
        )

    out: list[MetricCondition] = []
    i = 0
    while True:
        if i >= len(tokens) or tokens[i][0] != "ref":
            raise fail("expected a `{metric}` reference")
        metric = tokens[i][1][1:-1]
        i += 1
        if i < len(tokens) and tokens[i][0] == "op":
            op = tokens[i][1]
            i += 1
            if i >= len(tokens) or tokens[i][0] != "number":
                raise fail(f"`{op}` must be followed by a number")
            out.append(MetricCondition(metric, op, tokens[i][1]))
            i += 1
        elif i < len(tokens) and tokens[i] == ("word", "IS"):
            i += 1
            negated = i < len(tokens) and tokens[i] == ("word", "NOT")
            i += negated
            if i >= len(tokens) or tokens[i] != ("word", "NULL"):
                raise fail("`IS` must be followed by `NULL` or `NOT NULL`")
            out.append(MetricCondition(metric, "IS NOT NULL" if negated else "IS NULL", None))
            i += 1
        else:
            raise fail(f"`{{{metric}}}` must be followed by a comparison or `IS [NOT] NULL`")
        if i == len(tokens):
            return out
        if tokens[i] != ("word", "AND"):
            raise fail("conditions are joined by AND only")
        i += 1


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
