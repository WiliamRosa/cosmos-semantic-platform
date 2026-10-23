"""Impact analysis (ADR 0008): what a change to a rule, metric, measure, dimension or model
affects, and who used it.

The dependency graph comes from the compiled semantic graph; consumers come from the audit log.
With two versions of a model, the changed members are found first and their impact is reported
together, which is what a reviewer needs on a pull request.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cosmos_semantic.compiler import SemanticGraph
from cosmos_semantic.errors import QueryError
from cosmos_semantic.expressions import REF_RE
from cosmos_semantic.knowledge import owner_of, upstream_systems
from cosmos_semantic.spec import CumulativeMetric, DerivedMetric, RatioMetric, SimpleMetric

KINDS = ("model", "measure", "metric", "dimension", "rule", "concept", "system", "process")


@dataclass
class Impact:
    targets: list[str]
    metrics: set[str] = field(default_factory=set)
    measures: set[str] = field(default_factory=set)
    dimensions: set[str] = field(default_factory=set)
    rules: set[str] = field(default_factory=set)
    row_filters: set[str] = field(default_factory=set)
    concepts: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    processes: set[str] = field(default_factory=set)
    systems: set[str] = field(default_factory=set)

    def members(self) -> set[str]:
        """Names as they appear in requests: metrics, measures, `model.dim`, `rule.name`."""
        return self.metrics | self.measures | self.dimensions | {f"rule.{r}" for r in self.rules}

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": self.targets,
            "affected": {
                "metrics": sorted(self.metrics),
                "measures": sorted(self.measures),
                "dimensions": sorted(self.dimensions),
                "rules": sorted(self.rules),
                "row_filters": sorted(self.row_filters),
                "concepts": sorted(self.concepts),
                "models": sorted(self.models),
                "processes": sorted(self.processes),
            },
        }


class DependencyGraph:
    """Reverse dependencies: for each member, the members that depend on it."""

    def __init__(self, graph: SemanticGraph) -> None:
        self.graph = graph
        self.dependents: dict[str, set[str]] = defaultdict(set)
        self._build()

    def _add(self, used: str, by: str) -> None:
        self.dependents[used].add(by)

    def _dimension_refs(self, text: str, context: str | None) -> list[str]:
        out = []
        for ref in REF_RE.findall(text or ""):
            try:
                resolved = self.graph.resolve_dimension(ref, context=context)
            except Exception:  # unresolvable references are the validator's job
                continue
            if resolved.name in self.graph.rules:
                out.append(f"rule:{resolved.name}")
            else:
                out.append(f"dimension:{resolved.model}.{resolved.name}")
        return out

    def _build(self) -> None:
        g = self.graph
        for name, owner in g.measure_owner.items():
            self._add(f"model:{owner}", f"measure:{name}")
        for model, node in g.models.items():
            for dim in node.dimensions:
                if dim in g.rules:
                    continue
                self._add(f"model:{model}", f"dimension:{model}.{dim}")
            for row_filter in node.spec.row_filters:
                target = f"row_filter:{model}.{row_filter.name}"
                for ref in self._dimension_refs(row_filter.filter, model):
                    self._add(ref, target)

        for name, metric in g.metrics.items():
            me = f"metric:{name}"
            refs: list[str] = []
            if isinstance(metric, SimpleMetric | CumulativeMetric):
                refs = [metric.measure]
            elif isinstance(metric, RatioMetric):
                refs = [metric.numerator, metric.denominator]
            elif isinstance(metric, DerivedMetric):
                refs = REF_RE.findall(metric.expr)
            for ref in refs:
                kind = "measure" if ref in g.measure_owner else "metric"
                self._add(f"{kind}:{ref}", me)
            flt = getattr(metric, "filter", None)
            if flt:
                context = g.measure_owner.get(getattr(metric, "measure", ""), None)
                for ref in self._dimension_refs(flt, context):
                    self._add(ref, me)

        for name, rule in g.rules.items():
            me = f"rule:{name}"
            for binding in rule.applies_to:
                node = g.models.get(binding.model)
                for target in binding.bind.values():
                    if node is not None and target in node.dimensions and target not in g.rules:
                        self._add(f"dimension:{binding.model}.{target}", me)
                    else:
                        self._add(f"model:{binding.model}", me)

        for concept in g.project.glossary:
            if concept.maps_to:
                kind, _, target = concept.maps_to.partition(":")
                if kind == "metric" and target in g.measure_owner:
                    kind = "measure"
                self._add(f"{kind}:{target}", f"concept:{concept.concept}")
            if concept.broader:
                self._add(f"concept:{concept.broader}", f"concept:{concept.concept}")

        for system in g.project.systems:
            for model in system.feeds:
                self._add(f"system:{system.name}", f"model:{model}")
        for process in g.project.processes:
            me = f"process:{process.name}"
            for system in process.systems:
                self._add(f"system:{system}", me)
            for target in process.uses:
                try:
                    self._add(self.normalize(target), me)
                except QueryError:  # reported by validation
                    continue

    def normalize(self, target: str) -> str:
        kind, sep, name = target.partition(":")
        if not sep:
            raise QueryError(f"target `{target}` must look like kind:name ({', '.join(KINDS)})")
        g = self.graph
        if kind == "metric" and name in g.measure_owner:
            kind = "measure"
        valid = {
            "model": name in g.models,
            "measure": name in g.measure_owner,
            "metric": name in g.metrics,
            "rule": name in g.rules,
            "concept": any(c.concept == name for c in g.project.glossary),
            "system": any(s.name == name for s in g.project.systems),
            "process": any(p.name == name for p in g.project.processes),
            "dimension": "." in name
            and name.split(".")[0] in g.models
            and name.split(".")[1] in g.models[name.split(".")[0]].dimensions,
        }
        if kind not in valid:
            raise QueryError(f"unknown kind `{kind}`; use one of {', '.join(KINDS)}")
        if not valid[kind]:
            raise QueryError(f"`{target}` does not exist in the model")
        return f"{kind}:{name}"

    def impact(self, targets: list[str]) -> Impact:
        result = Impact(targets=targets)
        seen: set[str] = set()
        stack = [self.normalize(t) for t in targets]
        while stack:
            item = stack.pop()
            if item in seen:
                continue
            seen.add(item)
            stack.extend(self.dependents.get(item, ()))
        for item in seen:
            kind, _, name = item.partition(":")
            bucket = {
                "metric": result.metrics,
                "measure": result.measures,
                "dimension": result.dimensions,
                "rule": result.rules,
                "row_filter": result.row_filters,
                "concept": result.concepts,
                "model": result.models,
                "process": result.processes,
                "system": result.systems,
            }[kind]
            bucket.add(name)
        # Models that reach an affected model through joins are listed, but not expanded: a
        # change to `customers` does not change order revenue unless a query groups or
        # filters by customer attributes, which the consumers section shows.
        joiners = set()
        for model in list(result.models):
            joiners |= {
                source for source in self.graph.models if model in self.graph.reachable(source)
            }
        result.models |= joiners
        return result


def _resolve(graph: SemanticGraph, ref: str) -> str:
    """A dimension reference as written in a request, normalized to `model.dimension` or
    `rule.name`; anything else is returned as is."""
    try:
        resolved = graph.resolve_dimension(ref)
    except Exception:  # metric names and stale references are not dimensions
        return ref
    if resolved.name in graph.rules:
        return f"rule.{resolved.name}"
    return f"{resolved.model}.{resolved.name}"


def consumers(audit_path: str | Path, impact: Impact, graph: SemanticGraph) -> list[dict[str, Any]]:
    """Who queried any affected member, according to the audit log."""
    path = Path(audit_path)
    if not path.exists():
        return []
    members = impact.members()
    rule_names = impact.rules
    usage: dict[tuple, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record.get("outcome") != "ok":
                continue
            request = record.get("request") or {}
            used = set(request.get("metrics", []))
            used.update(_resolve(graph, d) for d in request.get("dimensions", []))
            for flt in request.get("filters", []):
                used.update(_resolve(graph, r) for r in REF_RE.findall(flt))
            used.update(f"rule.{r}" for r in record.get("rules", []) if r in rule_names)
            hits = sorted(used & members)
            if not hits:
                continue
            principal = record.get("principal") or {}
            key = (principal.get("subject"), principal.get("channel"))
            entry = usage.setdefault(
                key,
                {
                    "subject": key[0],
                    "channel": key[1],
                    "queries": 0,
                    "last_at": None,
                    "members": set(),
                },
            )
            entry["queries"] += 1
            entry["members"].update(hits)
            at = record.get("at")
            if at and (entry["last_at"] is None or at > entry["last_at"]):
                entry["last_at"] = at
    out = []
    for entry in sorted(usage.values(), key=lambda e: (-e["queries"], str(e["subject"]))):
        out.append(entry | {"members": sorted(entry["members"])})
    return out


def changed_members(old: SemanticGraph, new: SemanticGraph) -> list[str]:
    """Members whose definition differs between two versions of a model (added, removed or
    edited), as impact targets on the new model when they still exist there."""
    changes: list[str] = []

    def dump(obj) -> str:
        return json.dumps(obj.model_dump(mode="json"), sort_keys=True)

    def compare(kind: str, a: dict, b: dict, render=lambda n: n) -> None:
        for name in sorted(set(a) | set(b)):
            if name not in a or name not in b or dump(a[name]) != dump(b[name]):
                changes.append(f"{kind}:{render(name)}")

    compare("metric", old.metrics, new.metrics)
    compare("rule", old.rules, new.rules)
    compare(
        "concept",
        {c.concept: c for c in old.project.glossary},
        {c.concept: c for c in new.project.glossary},
    )
    compare(
        "system", {x.name: x for x in old.project.systems}, {x.name: x for x in new.project.systems}
    )
    compare(
        "process",
        {x.name: x for x in old.project.processes},
        {x.name: x for x in new.project.processes},
    )
    old_measures = {n: old.models[o].measures[n] for n, o in old.measure_owner.items()}
    new_measures = {n: new.models[o].measures[n] for n, o in new.measure_owner.items()}
    compare("measure", old_measures, new_measures)
    old_dims = {
        f"{m}.{d}": spec
        for m, node in old.models.items()
        for d, spec in node.dimensions.items()
        if d not in old.rules
    }
    new_dims = {
        f"{m}.{d}": spec
        for m, node in new.models.items()
        for d, spec in node.dimensions.items()
        if d not in new.rules
    }
    compare("dimension", old_dims, new_dims)
    for model in sorted(set(old.models) | set(new.models)):
        a, b = old.models.get(model), new.models.get(model)
        if (
            a is None
            or b is None
            or (
                a.spec.table,
                dump_list(a.spec.entities),
                dump_list(a.spec.row_filters),
            )
            != (b.spec.table, dump_list(b.spec.entities), dump_list(b.spec.row_filters))
        ):
            changes.append(f"model:{model}")
    return changes


def dump_list(items) -> str:
    return json.dumps([i.model_dump(mode="json") for i in items], sort_keys=True)


def analyze(
    graph: SemanticGraph,
    targets: list[str],
    *,
    audit_path: str | Path | None = None,
    removed: list[str] | None = None,
) -> dict[str, Any]:
    deps = DependencyGraph(graph)
    present = []
    for target in targets:
        try:
            present.append(deps.normalize(target))
        except QueryError:
            if removed is None:
                raise
            removed.append(target)
    impact = deps.impact(present)
    report = impact.as_dict()
    report["owners"] = _by_owner(graph, impact)
    report["source_systems"] = upstream_systems(graph, impact.models)
    if removed:
        report["removed"] = removed
    report["consumers"] = consumers(audit_path, impact, graph) if audit_path else []
    return report


def _by_owner(graph: SemanticGraph, impact: Impact) -> dict[str, list[str]]:
    """Affected members grouped by the team that owns them (`unowned` when nobody does)."""
    members = (
        [f"metric:{m}" for m in impact.metrics]
        + [f"measure:{m}" for m in impact.measures]
        + [f"rule:{r}" for r in impact.rules]
        + [f"process:{p}" for p in impact.processes]
        + [f"model:{m}" for m in impact.models]
    )
    owners: dict[str, list[str]] = {}
    for member in sorted(members):
        owners.setdefault(owner_of(graph, member) or "unowned", []).append(member)
    return dict(sorted(owners.items()))
