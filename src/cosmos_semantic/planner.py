"""Planejador de consultas e geração de SQL.

Estratégia contra multiplicação de linhas (fan-out): as medidas são agrupadas pelo modelo
dono; cada grupo vira uma CTE que parte do modelo dono e só faz joins muitos-para-um, então
nenhuma linha se repete antes da agregação. As CTEs são unidas pelas dimensões pedidas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

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
    filters: list[str] = Field([], description="Expressões com referências `{modelo.dimensao}`.")
    order_by: list[str] = Field(
        [], description="Membros pedidos; prefixo `-` para ordem decrescente."
    )
    limit: int | None = Field(None, ge=1)


@dataclass
class Principal:
    """Quem consulta: atributos já extraídos do token e o canal."""

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


@dataclass(frozen=True)
class _Input:
    """Uma medida agregada, com o filtro da métrica que a usa (se houver)."""

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

    def _table(self, name: str, alias: str) -> exp.Table:
        return table_ref(name, self.dialect, self.schema_map).as_(alias)

    # -- entrada pública -----------------------------------------------------

    def plan(self, request: QueryRequest, principal: Principal | None = None) -> CompiledQuery:
        principal = principal or Principal()
        g = self.graph

        dims = [g.resolve_dimension(d) for d in request.dimensions]
        if len({d.alias for d in dims}) != len(dims):
            raise QueryError("dimensão repetida no pedido")
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
                raise QueryError(f"métrica `{name}` repetida no pedido")
            metric_exprs[name] = self._metric_expr(name, groups, set())
            metric = g.metrics.get(name)
            if isinstance(metric, CumulativeMetric):
                cumulative[name] = metric

        models = {g.measure_owner[i.measure] for grp in groups.values() for i in grp.inputs}
        data_sources = {g.models[m].spec.data_source for m in models}
        if len(data_sources) > 1:
            raise QueryError("as métricas pedidas estão em origens diferentes")

        ctes: list[tuple[str, exp.Select]] = []
        used_models: set[str] = set()
        for group in groups.values():
            select, joined = self._group_select(group, dims, filters, principal)
            ctes.append((f"{group.model}_agg", select))
            used_models |= joined

        final = self._final_select(ctes, groups, dims, metric_exprs, cumulative, request)
        columns = [OutputColumn(d.alias, "dimension", _dim_member(d)) for d in dims] + [
            OutputColumn(m, "metric", m) for m in request.metrics
        ]
        return CompiledQuery(
            sql=final.sql(dialect=self.dialect, pretty=True),
            dialect=self.dialect,
            columns=columns,
            data_source=next(iter(data_sources)) if data_sources else None,
            models=sorted(used_models),
        )

    def _check_sensitivity(self, ref: DimensionRef, principal: Principal) -> None:
        level = self.graph.dimension(ref).sensitivity
        if principal.is_agent and level:
            raise PolicyError(
                f"`{ref.model}.{ref.name}` é dado {_LEVEL[level]} (LGPD) e não chega a "
                "agentes de IA nem como agrupamento nem como filtro"
            )

    # -- métricas -----------------------------------------------------------

    def _metric_expr(
        self, name: str, groups: dict[str, _Group], stack: set[str], extra_filter: str | None = None
    ) -> exp.Expression:
        g = self.graph
        if name in g.measure_owner:
            return self._input(_Input(name, extra_filter), groups)
        metric = g.metrics.get(name)
        if metric is None:
            raise QueryError(f"métrica `{name}` não existe")
        if name in stack:
            raise QueryError(f"ciclo na métrica `{name}`")
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
            return exp.Paren(
                this=substitute(
                    template, lambda r: self._metric_expr(r, groups, stack, extra_filter)
                )
            )
        if isinstance(metric, CumulativeMetric):
            if metric.window:
                raise QueryError(f"`{name}`: métricas com `window` ficam fora do MVP")
            if extra_filter or stack != {name}:
                raise QueryError(f"`{name}`: métrica acumulada não pode compor outra métrica")
            return self._input(_Input(metric.measure, None), groups)
        raise QueryError(f"tipo de métrica não suportado em `{name}`")

    def _input(self, item: _Input, groups: dict[str, _Group]) -> exp.Expression:
        owner = self.graph.measure_owner[item.measure]
        group = groups.setdefault(owner, _Group(owner))
        if item not in group.inputs:
            suffix = "" if item.filter is None else f"__f{len(group.inputs)}"
            group.inputs[item] = f"{item.measure}{suffix}"
        return exp.column(group.inputs[item], table=f"{owner}_agg")

    # -- uma CTE por modelo dono --------------------------------------------

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

        def dim_sql(ref: DimensionRef) -> exp.Expression:
            if ref.model != base and ref.model not in g.reachable(base):
                raise QueryError(
                    f"`{ref.model}.{ref.name}` não é alcançável a partir de `{base}` "
                    "(só joins muitos-para-um são permitidos)"
                )
            need(ref.model)
            dim = g.dimension(ref)
            read = RULE_DIALECT if ref.model in g.rule_models.get(ref.name, ()) else self.dialect
            column = qualify(dim.expr or dim.name, ref.model, read)
            if ref.grain:
                column = exp.TimestampTrunc(this=column, unit=exp.Var(this=ref.grain.upper()))
            return column

        def ref_resolver(context: str | None):
            return lambda r: dim_sql(g.resolve_dimension(r, context=context))

        def request_ref(r: str) -> exp.Expression:
            ref = g.resolve_dimension(r, context=base if r.startswith("rule.") else None)
            self._check_sensitivity(ref, principal)
            return dim_sql(ref)

        dim_exprs = [dim_sql(d) for d in dims]
        projections = [e.copy().as_(d.alias) for e, d in zip(dim_exprs, dims, strict=True)]
        conditions = [substitute(t, request_ref) for t in filters]

        for item, alias in group.inputs.items():
            measure = g.models[base].measures[item.measure]
            condition = (
                substitute(parse_template(item.filter, self.dialect), ref_resolver(base))
                if item.filter
                else None
            )
            projections.append(self._aggregate(measure, base, condition).as_(alias))

        # Filtros por linha de todo modelo que participa, inclusive os que entram por join
        # por causa de outro filtro por linha.
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
                        substitute(template, ref_resolver(model), self._user_resolver(principal))
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
        return _SAFE_AGGS[measure.agg](this=value)

    def _user_resolver(self, principal: Principal):
        declared = self.graph.project.user_context

        def resolve(name: str) -> exp.Expression:
            if name not in declared:
                raise PolicyError(f"`$user.{name}` não está declarado em user_context")
            if name not in principal.user or principal.user[name] is None:
                raise PolicyError(
                    f"consulta negada: o contexto do usuário não tem `{name}`, exigido por um "
                    "filtro por linha"
                )
            return literal(principal.user[name])

        return resolve

    # -- junção das CTEs -----------------------------------------------------

    def _final_select(
        self,
        ctes: list[tuple[str, exp.Select]],
        groups: dict[str, _Group],
        dims: list[DimensionRef],
        metric_exprs: dict[str, exp.Expression],
        cumulative: dict[str, CumulativeMetric],
        request: QueryRequest,
    ) -> exp.Select:
        names = [name for name, _ in ctes]
        # Com mais de uma CTE e dimensões, a "espinha" é a união das combinações de dimensões de
        # todas elas; cada CTE entra por LEFT JOIN com igualdade segura para nulos. FULL JOIN com
        # IS NOT DISTINCT FROM não é aceito pelo Postgres.
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
        for item in request.order_by:
            desc = item.startswith("-")
            key = item.lstrip("-")
            known = {d.alias: d for d in dims}
            if key not in metric_exprs and key not in known:
                try:
                    key = self.graph.resolve_dimension(key).alias
                except QueryError as exc:
                    raise QueryError(
                        f"order_by `{item}` não está entre os membros pedidos"
                    ) from exc
                if key not in known:
                    raise QueryError(f"order_by `{item}` não está entre os membros pedidos")
            order.append(exp.Ordered(this=exp.column(key), desc=desc))
        if order:
            select = select.order_by(*order)
        if request.limit:
            select = select.limit(request.limit)
        return select

    def _cumulative(self, value, metric: CumulativeMetric, dims, dim_out) -> exp.Expression:
        time_dims = [d for d in dims if self.graph.dimension(d).type == "time"]
        if len(time_dims) != 1:
            raise QueryError(
                f"`{metric.name}` precisa de exatamente uma dimensão de tempo no pedido"
            )
        time = time_dims[0]
        partition = [dim_out(d) for d in dims if d != time]
        if metric.grain_to_date:
            partition.append(
                exp.TimestampTrunc(
                    this=dim_out(time), unit=exp.Var(this=metric.grain_to_date.upper())
                )
            )
        spec = exp.WindowSpec(
            kind="ROWS", start="UNBOUNDED", start_side="PRECEDING", end="CURRENT ROW"
        )
        return exp.Window(
            this=exp.Sum(this=value),
            partition_by=partition or None,
            order=exp.Order(expressions=[exp.Ordered(this=dim_out(time))]),
            spec=spec,
        )


_LEVEL = {"personal": "pessoal", "sensitive": "pessoal sensível"}


def _and(a: str | None, b: str | None) -> str | None:
    if a and b:
        return f"({a}) AND ({b})"
    return a or b


def _dim_member(d: DimensionRef) -> str:
    member = f"{d.model}.{d.name}"
    return f"{member}__{d.grain}" if d.grain else member


def _ordered(joins: dict[str, JoinEdge], base: str) -> list[tuple[str, JoinEdge]]:
    """Ordena os joins para que cada um venha depois do modelo de onde parte."""
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
            raise QueryError("não foi possível ordenar os joins")
    return ordered
