"""Compilador: valida o projeto e monta o grafo semântico, com joins inferidos das entidades."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.expressions import REF_RE, USER_RE
from cosmos_semantic.spec import (
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
)

TIME_GRAINS = ("hour", "day", "week", "month", "quarter", "year")


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    location: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.location}: {self.message}"


@dataclass(frozen=True)
class JoinEdge:
    """Join muitos-para-um: cada linha de ``source`` encontra no máximo uma linha de ``target``."""

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
        self.issues: list[Issue] = []

    # -- consultas ao grafo -------------------------------------------------

    def measure(self, name: str) -> tuple[ModelNode, Measure]:
        owner = self.measure_owner.get(name)
        if owner is None:
            raise QueryError(f"medida `{name}` não existe")
        node = self.models[owner]
        return node, node.measures[name]

    def resolve_dimension(self, ref: str, *, context: str | None = None) -> DimensionRef:
        """Resolve ``modelo.dimensao``, ``dimensao`` ou ``...__granularidade``."""
        grain = None
        head, _, tail = ref.rpartition("__")
        if head and tail in TIME_GRAINS:
            ref, grain = head, tail
        if "." in ref:
            model, name = ref.split(".", 1)
            node = self.models.get(model)
            if node is None or name not in node.dimensions:
                raise QueryError(f"dimensão `{ref}` não existe")
        else:
            name = ref
            if context and name in self.models[context].dimensions:
                model = context
            else:
                owners = [m for m, n in self.models.items() if name in n.dimensions]
                if not owners:
                    raise QueryError(f"dimensão `{ref}` não existe")
                if len(owners) > 1:
                    options = ", ".join(f"{o}.{name}" for o in owners)
                    raise QueryError(f"dimensão `{ref}` é ambígua; use uma de: {options}")
                model = owners[0]
        dim = self.models[model].dimensions[name]
        if grain and dim.type != "time":
            raise QueryError(f"granularidade `{grain}` só vale para dimensões de tempo")
        return DimensionRef(model, name, grain)

    def dimension(self, ref: DimensionRef) -> Dimension:
        return self.models[ref.model].dimensions[ref.name]

    def join_path(self, source: str, target: str) -> list[JoinEdge]:
        """Menor caminho de joins muitos-para-um; dois caminhos de mesmo tamanho é erro."""
        if source == target:
            return []
        paths = self._shortest_paths(source, target)
        if not paths:
            raise QueryError(f"não há caminho de join de `{source}` até `{target}`")
        if len(paths) > 1:
            shown = " | ".join(" -> ".join([source] + [e.target for e in p]) for p in paths)
            raise QueryError(f"caminho de join ambíguo de `{source}` até `{target}`: {shown}")
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
    """Monta o grafo e valida o projeto. Com ``strict``, qualquer erro levanta ModelError."""
    g = SemanticGraph(project)
    err = lambda loc, msg: g.issues.append(Issue("error", loc, msg))  # noqa: E731
    warn = lambda loc, msg: g.issues.append(Issue("warning", loc, msg))  # noqa: E731

    for name in _dup([m.name for m in project.semantic_models]):
        err(f"semantic_models.{name}", "nome de modelo duplicado")

    for model in project.semantic_models:
        node = ModelNode(model)
        loc = f"semantic_models.{model.name}"
        for name in _dup([d.name for d in model.dimensions]):
            err(loc, f"dimensão `{name}` duplicada")
        for name in _dup([e.name for e in model.entities]):
            err(loc, f"entidade `{name}` duplicada")
        node.entities = {e.name: e for e in model.entities}
        node.dimensions = {d.name: d for d in model.dimensions}
        node.measures = {m.name: m for m in model.measures}
        for measure in model.measures:
            if measure.name in g.measure_owner:
                err(loc, f"medida `{measure.name}` já existe em `{g.measure_owner[measure.name]}`")
            else:
                g.measure_owner[measure.name] = model.name
        g.models.setdefault(model.name, node)

    # Entidades: cada primary/unique pertence a um único modelo; foreign aponta para ele.
    key_owner: dict[str, str] = {}
    for node in g.models.values():
        for entity in node.entities.values():
            if entity.type in ("primary", "unique"):
                if entity.name in key_owner:
                    err(
                        f"semantic_models.{node.name}",
                        f"entidade `{entity.name}` já é chave de `{key_owner[entity.name]}`",
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
                    f"entidade estrangeira `{entity.name}` sem chave",
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

    data_sources = {m.data_source for m in project.semantic_models}
    if len(data_sources) > 1:
        warn("semantic_models", "há mais de uma origem; consultas não cruzam origens")

    # Métricas
    for name in _dup([m.name for m in project.metrics]):
        err(f"metrics.{name}", "nome de métrica duplicado")
    for metric in project.metrics:
        if metric.name in g.measure_owner:
            err(f"metrics.{metric.name}", "já existe uma medida com esse nome")
        g.metrics.setdefault(metric.name, metric)
    for metric in project.metrics:
        loc = f"metrics.{metric.name}"
        for ref in _metric_refs(metric):
            if ref not in g.measure_owner and ref not in g.metrics:
                err(loc, f"referência `{ref}` não é medida nem métrica")
        if isinstance(metric, CumulativeMetric) and metric.window:
            warn(loc, "métricas com `window` ficam fora do MVP; a consulta será recusada")
    _check_metric_cycles(g, err)

    # Ambiguidade de joins entre quaisquer dois modelos alcançáveis
    for source in g.models:
        for target in g.reachable(source) - {source}:
            try:
                g.join_path(source, target)
            except QueryError as exc:
                err(f"semantic_models.{source}", str(exc))

    # Filtros por linha: referências alcançáveis e campos de $user declarados
    for node in g.models.values():
        for row_filter in node.spec.row_filters:
            loc = f"semantic_models.{node.name}.row_filters.{row_filter.name}"
            for ref in REF_RE.findall(row_filter.filter):
                try:
                    dim = g.resolve_dimension(ref, context=node.name)
                    if dim.model not in g.reachable(node.name):
                        err(loc, f"`{ref}` não é alcançável a partir de `{node.name}`")
                except QueryError as exc:
                    err(loc, str(exc))
            for user_field in USER_RE.findall(row_filter.filter):
                if user_field not in project.user_context:
                    err(loc, f"`$user.{user_field}` não está declarado em user_context")

    if strict:
        errors = [i for i in g.issues if i.severity == "error"]
        if errors:
            raise ModelError("modelo inválido:\n" + "\n".join(str(e) for e in errors))
    return g


def _metric_refs(metric: Metric) -> list[str]:
    if isinstance(metric, SimpleMetric | CumulativeMetric):
        return [metric.measure]
    if isinstance(metric, RatioMetric):
        return [metric.numerator, metric.denominator]
    if isinstance(metric, DerivedMetric):
        return REF_RE.findall(metric.expr)
    return []


def _check_metric_cycles(g: SemanticGraph, err) -> None:
    state: dict[str, int] = {}

    def visit(name: str, chain: list[str]) -> None:
        if state.get(name) == 2 or name not in g.metrics:
            return
        if state.get(name) == 1:
            err(f"metrics.{name}", "ciclo entre métricas: " + " -> ".join([*chain, name]))
            return
        state[name] = 1
        for ref in _metric_refs(g.metrics[name]):
            visit(ref, [*chain, name])
        state[name] = 2

    for name in g.metrics:
        visit(name, [])
