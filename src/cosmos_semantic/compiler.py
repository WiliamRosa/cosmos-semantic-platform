"""Compiler: validates the project and builds the semantic graph, inferring joins from entities."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.expressions import REF_RE, USER_RE
from cosmos_semantic.spec import (
    BusinessRule,
    CumulativeMetric,
    DerivedMetric,
    Dimension,
    Entity,
    Measure,
    Metric,
    Project,
    RatioMetric,
    SemanticModel,
    SimpleMetric,
    cumulative_agg_problem,
    input_problems,
    offset_problem,
    parse_offset,
    window_agg_refusal,
)

TIME_GRAINS = ("hour", "day", "week", "month", "quarter", "year")


def to_date_grain_problem(to_date: str, grain: str | None) -> str | None:
    """Why a ``to_date``-to-date metric cannot be grouped by ``grain``, or None when it can.

    A grain coarser than the period would only give each bucket's total (DEC-21). A week does
    not nest in a month, quarter or year: the week that crosses into a new period would be
    counted in the old one (DEC-28)."""
    period = TIME_GRAINS.index(to_date)
    # Week nests only in a week; every other grain nests in each coarser one.
    fits = [g for g in TIME_GRAINS[: period + 1] if g != "week" or to_date == "week"]
    if not grain or grain in fits:
        return None
    if TIME_GRAINS.index(grain) > period:
        why = f"each {grain} would only give its total"
    else:
        why = f"a week can cross into a new {to_date} and would be counted in the old one"
    shown = fits[1:]  # hour is accepted too, but rarely what is meant
    use = shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} or {shown[-1]}"
    return (
        f"a {to_date}-to-date metric cannot be grouped by {grain}, since {why}; use the {use} grain"
    )


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    location: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.location}: {self.message}"


@dataclass(frozen=True)
class JoinEdge:
    """Many-to-one join: each ``source`` row matches at most one ``target`` row."""

    source: str
    target: str
    entity: str
    source_expr: str
    target_expr: str


@dataclass
class ModelNode:
    spec: SemanticModel
    entities: dict[str, Entity] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(frozen=True)
class DimensionRef:
    model: str
    name: str
    grain: str | None = None

    @property
    def alias(self) -> str:
        base = f"{self.model}__{self.name}"
        return f"{base}__{self.grain}" if self.grain else base


class SemanticGraph:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.models: dict[str, ModelNode] = {}
        self.measure_owner: dict[str, str] = {}
        self.metrics: dict[str, Metric] = {}
        self.edges: dict[str, list[JoinEdge]] = defaultdict(list)
        self.rules: dict[str, BusinessRule] = {}
        self.rule_models: dict[str, list[str]] = {}
        # (rule, model) -> rule input -> bound SQL expression, shared by every rule version
        self.rule_bindings: dict[tuple[str, str], dict[str, str]] = {}
        self.issues: list[Issue] = []

    # -- graph lookups -------------------------------------------------------

    def measure(self, name: str) -> tuple[ModelNode, Measure]:
        owner = self.measure_owner.get(name)
        if owner is None:
            raise QueryError(f"measure `{name}` does not exist")
        node = self.models[owner]
        return node, node.measures[name]

    def resolve_dimension(self, ref: str, *, context: str | None = None) -> DimensionRef:
        """Resolves ``model.dimension``, ``dimension``, ``rule.name`` or ``...__grain``."""
        grain = None
        head, _, tail = ref.rpartition("__")
        if head and tail in TIME_GRAINS:
            ref, grain = head, tail
        if ref.startswith("rule."):
            name = ref[5:]
            models = self.rule_models.get(name)
            if not models:
                raise QueryError(f"rule `{name}` does not exist")
            if context:
                reach = self.reachable(context)
                candidates = [m for m in models if m in reach]
                if context in candidates:
                    candidates = [context]
            else:
                candidates = models
            if not candidates:
                raise QueryError(
                    f"rule `{name}` does not apply to any model reachable from `{context}`"
                )
            if len(candidates) > 1:
                options = ", ".join(f"{m}.{name}" for m in candidates)
                raise QueryError(f"rule `{name}` applies to several models; use one of: {options}")
            model = candidates[0]
        elif "." in ref:
            model, name = ref.split(".", 1)
            node = self.models.get(model)
            if node is None or name not in node.dimensions:
                raise QueryError(f"dimension `{ref}` does not exist")
        else:
            name = ref
            if context and name in self.models[context].dimensions:
                model = context
            else:
                owners = [m for m, n in self.models.items() if name in n.dimensions]
                if not owners:
                    raise QueryError(f"dimension `{ref}` does not exist")
                if len(owners) > 1:
                    options = ", ".join(f"{o}.{name}" for o in owners)
                    raise QueryError(f"dimension `{ref}` is ambiguous; use one of: {options}")
                model = owners[0]
        dim = self.models[model].dimensions[name]
        if grain and dim.type != "time":
            raise QueryError(f"grain `{grain}` only applies to time dimensions")
        return DimensionRef(model, name, grain)

    def dimension(self, ref: DimensionRef) -> Dimension:
        return self.models[ref.model].dimensions[ref.name]

    def join_path(self, source: str, target: str) -> list[JoinEdge]:
        """Shortest many-to-one join path; two paths of the same length are an error."""
        if source == target:
            return []
        paths = self._shortest_paths(source, target)
        if not paths:
            raise QueryError(f"no join path from `{source}` to `{target}`")
        if len(paths) > 1:
            shown = " | ".join(" -> ".join([source] + [e.target for e in p]) for p in paths)
            raise QueryError(f"ambiguous join path from `{source}` to `{target}`: {shown}")
        return paths[0]

    def reachable(self, source: str) -> set[str]:
        seen, stack = {source}, [source]
        while stack:
            for edge in self.edges[stack.pop()]:
                if edge.target not in seen:
                    seen.add(edge.target)
                    stack.append(edge.target)
        return seen

    def _shortest_paths(self, source: str, target: str) -> list[list[JoinEdge]]:
        frontier: list[list[JoinEdge]] = [[]]
        visited = {source}
        while frontier:
            found: list[list[JoinEdge]] = []
            next_frontier: list[list[JoinEdge]] = []
            level_seen: set[str] = set()
            for path in frontier:
                current = path[-1].target if path else source
                for edge in self.edges[current]:
                    if edge.target in visited:
                        continue
                    new_path = [*path, edge]
                    if edge.target == target:
                        found.append(new_path)
                    else:
                        next_frontier.append(new_path)
                        level_seen.add(edge.target)
            if found:
                return found
            visited |= level_seen
            frontier = next_frontier
        return []


def _dup(names: list[str]) -> set[str]:
    seen: set[str] = set()
    return {n for n in names if n in seen or seen.add(n)}


def compile_project(project: Project, *, strict: bool = True) -> SemanticGraph:
    """Builds the graph and validates the project. With ``strict``, any error raises ModelError."""
    g = SemanticGraph(project)
    err = lambda loc, msg: g.issues.append(Issue("error", loc, msg))  # noqa: E731
    warn = lambda loc, msg: g.issues.append(Issue("warning", loc, msg))  # noqa: E731

    for name in _dup([m.name for m in project.semantic_models]):
        err(f"semantic_models.{name}", "duplicate model name")

    for model in project.semantic_models:
        node = ModelNode(model)
        loc = f"semantic_models.{model.name}"
        for name in _dup([d.name for d in model.dimensions]):
            err(loc, f"duplicate dimension `{name}`")
        for name in _dup([e.name for e in model.entities]):
            err(loc, f"duplicate entity `{name}`")
        node.entities = {e.name: e for e in model.entities}
        node.dimensions = {d.name: d for d in model.dimensions}
        node.measures = {m.name: m for m in model.measures}
        for measure in model.measures:
            if measure.name in g.measure_owner:
                err(
                    loc,
                    f"measure `{measure.name}` already exists in `{g.measure_owner[measure.name]}`",
                )
            else:
                g.measure_owner[measure.name] = model.name
        g.models.setdefault(model.name, node)

    # Entities: each primary/unique key belongs to one model; foreign entities point to it.
    key_owner: dict[str, str] = {}
    for node in g.models.values():
        for entity in node.entities.values():
            if entity.type in ("primary", "unique"):
                if entity.name in key_owner:
                    err(
                        f"semantic_models.{node.name}",
                        f"entity `{entity.name}` is already a key of `{key_owner[entity.name]}`",
                    )
                else:
                    key_owner[entity.name] = node.name
    for node in g.models.values():
        for entity in node.entities.values():
            if entity.type != "foreign":
                continue
            target = key_owner.get(entity.name)
            if target is None:
                err(
                    f"semantic_models.{node.name}",
                    f"foreign entity `{entity.name}` has no key",
                )
                continue
            target_entity = g.models[target].entities[entity.name]
            g.edges[node.name].append(
                JoinEdge(
                    source=node.name,
                    target=target,
                    entity=entity.name,
                    source_expr=entity.expr or entity.name,
                    target_expr=target_entity.expr or target_entity.name,
                )
            )

    if project.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(project.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            err("timezone", f"unknown time zone `{project.timezone}`; use an IANA name")

    data_sources = {m.data_source for m in project.semantic_models}
    if len(data_sources) > 1:
        warn("semantic_models", "more than one data source; queries do not span data sources")

    from cosmos_semantic import ontology

    ontology.attach_rules(g, err, warn)

    # Metrics
    for name in _dup([m.name for m in project.metrics]):
        err(f"metrics.{name}", "duplicate metric name")
    for metric in project.metrics:
        if metric.name in g.measure_owner:
            err(f"metrics.{metric.name}", "a measure with this name already exists")
        g.metrics.setdefault(metric.name, metric)
    for metric in project.metrics:
        loc = f"metrics.{metric.name}"
        for ref in metric_refs(metric):
            if ref not in g.measure_owner and ref not in g.metrics:
                err(loc, f"reference `{ref}` is neither a measure nor a metric")
        if (
            isinstance(metric, CumulativeMetric)
            and metric.window
            and not metric.window_days()
            and not metric.window_months()
        ):
            warn(
                loc,
                "only windows in days, weeks or months, such as `7 days`, `2 weeks` or "
                "`3 months`, are supported; queries will be refused",
            )
        if isinstance(metric, CumulativeMetric) and metric.measure in g.measure_owner:
            agg = g.models[g.measure_owner[metric.measure]].measures[metric.measure].agg
            if metric.window:
                problem = window_agg_refusal(agg, metric.window_months() is not None)
            else:
                problem = cumulative_agg_problem(metric, agg)
            if problem:
                err(loc, problem)
    _check_metric_cycles(g, err)
    _check_offsets(g, err)

    # Join ambiguity between any two reachable models
    for source in g.models:
        for target in g.reachable(source) - {source}:
            try:
                g.join_path(source, target)
            except QueryError as exc:
                err(f"semantic_models.{source}", str(exc))

    # Row filters: references must be reachable and $user fields declared
    for node in g.models.values():
        for row_filter in node.spec.row_filters:
            loc = f"semantic_models.{node.name}.row_filters.{row_filter.name}"
            for ref in REF_RE.findall(row_filter.filter):
                try:
                    dim = g.resolve_dimension(ref, context=node.name)
                    if dim.model not in g.reachable(node.name):
                        err(loc, f"`{ref}` is not reachable from `{node.name}`")
                except QueryError as exc:
                    err(loc, str(exc))
            for user_field in USER_RE.findall(row_filter.filter):
                if user_field not in project.user_context:
                    err(loc, f"`$user.{user_field}` is not declared in user_context")

    ontology.check_glossary(g, err, warn)

    from cosmos_semantic import knowledge

    knowledge.check_context(g, err, warn)

    if strict:
        errors = [i for i in g.issues if i.severity == "error"]
        if errors:
            raise ModelError("invalid model:\n" + "\n".join(str(e) for e in errors))
    return g


def metric_refs(metric: Metric) -> list[str]:
    if isinstance(metric, SimpleMetric | CumulativeMetric):
        return [metric.measure]
    if isinstance(metric, RatioMetric):
        return [metric.numerator, metric.denominator]
    if isinstance(metric, DerivedMetric):
        # An input's alias stands for the metric it reads (DEC-44).
        return [metric.target(ref) for ref in REF_RE.findall(metric.expr)]
    return []


def metrics_under(g: SemanticGraph, name: str) -> list[str]:
    """``name`` and every metric it uses, directly or not; each once, cycles cut."""
    seen: list[str] = []

    def visit(current: str) -> None:
        if current in seen or current not in g.metrics:
            return
        seen.append(current)
        for ref in metric_refs(g.metrics[current]):
            visit(ref)

    visit(name)
    return seen


def _check_offsets(g: SemanticGraph, err) -> None:
    """Inputs of derived metrics read at an earlier period (DEC-18). The planner refuses the
    same cases, for graphs compiled without ``strict``."""
    for metric in g.metrics.values():
        if not isinstance(metric, DerivedMetric) or not metric.inputs:
            continue
        loc = f"metrics.{metric.name}"
        refs = set(REF_RE.findall(metric.expr))
        for problem in input_problems(metric, refs, set(g.metrics) | set(g.measure_owner)):
            err(loc, problem)
        for item in metric.inputs:
            if item.ref not in refs:
                continue
            problem = offset_problem(item.offset)
            if problem:
                err(loc, problem)
            for name in metrics_under(g, item.metric):
                problem = offset_input_problem(g.metrics[name], item.metric)
                if problem:
                    err(loc, problem)
    _check_offset_units(g, err)


def offset_units(g: SemanticGraph, name: str) -> dict[str, str]:
    """Base unit (``day`` or ``month``) -> the first offset written in it, among the offsets
    ``name`` reads its inputs with, directly or through other metrics. Weeks count as days
    and years as months (DEC-46)."""
    units: dict[str, str] = {}
    for under in metrics_under(g, name):
        metric = g.metrics[under]
        if isinstance(metric, DerivedMetric):
            for item in metric.inputs:
                parsed = parse_offset(item.offset)
                if parsed:
                    units.setdefault(parsed[1], item.offset)
    return units


def _check_offset_units(g: SemanticGraph, err) -> None:
    """A metric whose offsets mix days (or weeks) and months (or years) cannot be queried at
    any grain: days fit a day or a week, months a month, a quarter or a year (DEC-46). The
    error goes on the metric where the mix appears, not on every metric above it."""
    mixed = {name for name in g.metrics if len(offset_units(g, name)) > 1}
    for name in sorted(mixed):
        if any(ref in mixed for ref in metric_refs(g.metrics[name])):
            continue
        units = offset_units(g, name)
        err(
            f"metrics.{name}",
            f"`{name}` reads inputs `{units['day']}` and `{units['month']}` earlier; an offset "
            "in days or weeks runs by day or week and one in months or years by month, "
            "quarter or year, so no grain can answer it",
        )


def offset_input_problem(metric: Metric, read: str) -> str | None:
    """Why ``metric``, found under the input ``read`` with an offset, cannot be shifted."""
    if isinstance(metric, CumulativeMetric):
        return (
            f"`{read}` is read with an offset, and it uses the cumulative metric "
            f"`{metric.name}`; offsets of cumulative metrics are not supported"
        )
    if isinstance(metric, DerivedMetric) and metric.inputs:
        return (
            f"`{read}` is read with an offset, and it uses `{metric.name}`, which has offsets "
            "of its own; offsets do not combine yet"
        )
    return None


def _check_metric_cycles(g: SemanticGraph, err) -> None:
    state: dict[str, int] = {}

    def visit(name: str, chain: list[str]) -> None:
        if state.get(name) == 2 or name not in g.metrics:
            return
        if state.get(name) == 1:
            err(f"metrics.{name}", "metric cycle: " + " -> ".join([*chain, name]))
            return
        state[name] = 1
        for ref in metric_refs(g.metrics[name]):
            visit(ref, [*chain, name])
        state[name] = 2

    for name in g.metrics:
        visit(name, [])
