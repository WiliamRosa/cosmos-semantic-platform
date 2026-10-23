"""Business knowledge graph (ADR 0009): teams, systems and processes around the semantic model.

Teams own models, metrics, rules, systems and processes. Systems feed models. Processes run on
systems and rely on metrics, rules and other members. Together with the compiled semantic graph
this answers "who owns this number, where does its data come from, and which processes break
if it changes", and exports the whole graph as JSON, Mermaid or Graphviz DOT.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cosmos_semantic.errors import QueryError

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph

FORMATS = ("json", "mermaid", "dot")


def _dup(names: list[str]) -> set[str]:
    seen, dups = set(), set()
    for name in names:
        (dups if name in seen else seen).add(name)
    return dups


def check_context(g: SemanticGraph, err, warn) -> None:
    from cosmos_semantic.impact import DependencyGraph

    project = g.project
    for kind, items in (
        ("teams", project.teams),
        ("systems", project.systems),
        ("processes", project.processes),
    ):
        for name in _dup([i.name for i in items]):
            err(f"{kind}.{name}", "duplicate name")
    systems = {s.name for s in project.systems}
    for system in project.systems:
        for model in system.feeds:
            if model not in g.models:
                err(f"systems.{system.name}", f"`feeds` names the unknown model `{model}`")
    deps = DependencyGraph(g)
    for process in project.processes:
        loc = f"processes.{process.name}"
        for name in process.systems:
            if name not in systems:
                err(loc, f"unknown system `{name}`")
        for target in process.uses:
            try:
                deps.normalize(target)
            except QueryError as exc:
                err(loc, f"`uses`: {exc}")
    if project.teams:
        teams = {t.name for t in project.teams}
        for loc, owner in owned_items(g):
            if owner not in teams:
                warn(loc, f"owner `{owner}` is not a declared team")


def owned_items(g: SemanticGraph) -> list[tuple[str, str]]:
    """(member, owner) for everything with an explicit owner, members as `kind:name`."""
    project = g.project
    out: list[tuple[str, str]] = []
    out += [(f"model:{m.name}", m.owner) for m in project.semantic_models if m.owner]
    out += [(f"metric:{m.name}", m.owner) for m in project.metrics if m.owner]
    out += [(f"rule:{r.name}", r.owner) for r in project.business_rules if r.owner]
    out += [(f"system:{s.name}", s.owner) for s in project.systems if s.owner]
    out += [(f"process:{p.name}", p.owner) for p in project.processes if p.owner]
    return out


def owner_of(g: SemanticGraph, member: str) -> str | None:
    """The explicit owner of a member; a measure or dimension inherits its model's owner."""
    owners = dict(owned_items(g))
    if member in owners:
        return owners[member]
    kind, _, name = member.partition(":")
    if kind == "measure" and name in g.measure_owner:
        return owners.get(f"model:{g.measure_owner[name]}")
    if kind == "dimension":
        return owners.get(f"model:{name.split('.')[0]}")
    return None


def upstream_systems(g: SemanticGraph, models: set[str]) -> list[str]:
    return sorted({s.name for s in g.project.systems if set(s.feeds) & models})


# -- graph export ------------------------------------------------------------------------


@dataclass
class KnowledgeGraph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)

    def node(self, node_id: str, **attrs: Any) -> None:
        kind, _, name = node_id.partition(":")
        self.nodes.setdefault(node_id, {"id": node_id, "kind": kind, "name": name}).update(
            {k: v for k, v in attrs.items() if v is not None}
        )

    def edge(self, source: str, target: str, relation: str) -> None:
        item = {"source": source, "target": target, "relation": relation}
        if item not in self.edges:
            self.edges.append(item)

    def focus(self, center: str) -> KnowledgeGraph:
        """The center node, everything it depends on and everything that depends on it."""
        if center not in self.nodes:
            raise QueryError(f"`{center}` is not in the knowledge graph")
        forward, backward = defaultdict(set), defaultdict(set)
        for e in self.edges:
            forward[e["source"]].add(e["target"])
            backward[e["target"]].add(e["source"])

        def walk(links) -> set[str]:
            seen, stack = set(), [center]
            while stack:
                item = stack.pop()
                if item not in seen:
                    seen.add(item)
                    stack.extend(links[item])
            return seen

        keep = walk(forward) | walk(backward)
        # Owners of the kept nodes, so the focused view still answers "who owns this".
        keep |= {e["source"] for e in self.edges if e["relation"] == "owns" and e["target"] in keep}
        sub = KnowledgeGraph()
        sub.nodes = {k: v for k, v in self.nodes.items() if k in keep}
        sub.edges = [e for e in self.edges if e["source"] in keep and e["target"] in keep]
        return sub

    def as_dict(self) -> dict[str, Any]:
        return {"nodes": sorted(self.nodes.values(), key=lambda n: n["id"]), "edges": self.edges}

    def mermaid(self) -> str:
        lines = ["flowchart LR"]
        shapes = {
            "team": ("([", "])"),
            "system": ("[(", ")]"),
            "process": ("[[", "]]"),
            "rule": ("{{", "}}"),
            "concept": ("(", ")"),
        }
        for node in sorted(self.nodes.values(), key=lambda n: n["id"]):
            left, right = shapes.get(node["kind"], ("[", "]"))
            label = f"{node['kind']}: {node['name']}".replace('"', "'")
            lines.append(f'  {_safe_id(node["id"])}{left}"{label}"{right}')
        for e in self.edges:
            lines.append(f"  {_safe_id(e['source'])} -->|{e['relation']}| {_safe_id(e['target'])}")
        return "\n".join(lines) + "\n"

    def dot(self) -> str:
        shapes = {
            "team": "ellipse",
            "system": "cylinder",
            "process": "component",
            "rule": "hexagon",
            "concept": "note",
        }
        lines = ["digraph cosmos {", "  rankdir=LR;", "  node [fontname=Helvetica];"]
        for node in sorted(self.nodes.values(), key=lambda n: n["id"]):
            shape = shapes.get(node["kind"], "box")
            label = f"{node['kind']}\\n{node['name']}"
            lines.append(f'  "{node["id"]}" [label="{label}", shape={shape}];')
        for e in self.edges:
            lines.append(f'  "{e["source"]}" -> "{e["target"]}" [label="{e["relation"]}"];')
        lines.append("}")
        return "\n".join(lines) + "\n"

    def render(self, fmt: str) -> str | dict[str, Any]:
        if fmt == "json":
            return self.as_dict()
        if fmt == "mermaid":
            return self.mermaid()
        if fmt == "dot":
            return self.dot()
        raise QueryError(f"unknown format `{fmt}`; use one of {', '.join(FORMATS)}")


def _safe_id(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def build_graph(g: SemanticGraph) -> KnowledgeGraph:
    """Teams, systems, processes, models, measures, metrics, rules, concepts and the
    dimensions something depends on. Edges point from the provider to the user of a member
    (``depends_on`` reads "target depends on source")."""
    from cosmos_semantic.impact import DependencyGraph

    project = g.project
    kg = KnowledgeGraph()
    teams = {t.name: t for t in project.teams}
    for team in project.teams:
        kg.node(f"team:{team.name}", description=team.description, contact=team.contact)
    for model in project.semantic_models:
        kg.node(f"model:{model.name}", description=model.description, table=model.table)
    for name, metric in g.metrics.items():
        kg.node(f"metric:{name}", description=metric.description)
    for name, owner in g.measure_owner.items():
        kg.node(f"measure:{name}", description=g.models[owner].measures[name].description)
    for name, rule in g.rules.items():
        kg.node(f"rule:{name}", description=rule.description, versions=len(rule.history) + 1)
    for concept in project.glossary:
        kg.node(f"concept:{concept.concept}", description=concept.definition)
    for system in project.systems:
        kg.node(f"system:{system.name}", description=system.description, system_kind=system.kind)
        for model in system.feeds:
            kg.edge(f"system:{system.name}", f"model:{model}", "feeds")
    for process in project.processes:
        me = f"process:{process.name}"
        kg.node(me, description=process.description)
        for system in process.systems:
            kg.edge(f"system:{system}", me, "runs")

    deps = DependencyGraph(g)
    for used, users in deps.dependents.items():
        for by in users:
            if used.startswith("model:") and by.startswith("dimension:"):
                continue  # every dimension belongs to its model; only referenced ones are drawn
            if used.startswith("system:"):
                continue  # already drawn as `feeds` and `runs`
            kind = by.partition(":")[0]
            relation = {"concept": "defines", "process": "used_by"}.get(kind, "depends_on")
            if kind == "concept" and used.startswith("concept:"):
                relation = "broader"
            for node_id in (used, by):
                kg.node(node_id)
            kg.edge(used, by, relation)
    for edges in g.edges.values():
        for e in edges:
            kg.edge(f"model:{e.target}", f"model:{e.source}", "joined_by")

    for member, owner in owned_items(g):
        kg.node(f"team:{owner}", **({} if owner in teams else {"declared": False}))
        kg.node(member, owner=owner)
        kg.edge(f"team:{owner}", member, "owns")
    return kg
