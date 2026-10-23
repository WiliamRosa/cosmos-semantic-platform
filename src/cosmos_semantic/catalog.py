"""Metadata for people and agents: members, concepts and explanations (REST API and MCP)."""

from __future__ import annotations

from typing import Any

from cosmos_semantic.compiler import SemanticGraph
from cosmos_semantic.errors import QueryError
from cosmos_semantic.ontology import Glossary, rule_logic
from cosmos_semantic.spec import (
    CumulativeMetric,
    DerivedMetric,
    RatioMetric,
    SimpleMetric,
)


class Catalog:
    def __init__(self, graph: SemanticGraph) -> None:
        self.graph = graph
        self.glossary = Glossary(graph.project)

    def members(self) -> dict[str, list[dict[str, Any]]]:
        g = self.graph
        metrics = [
            {"name": m.name, "type": m.type, "description": m.description}
            for m in g.metrics.values()
        ]
        metrics += [
            {
                "name": name,
                "type": "measure",
                "agg": g.models[owner].measures[name].agg,
                "model": owner,
                "description": g.models[owner].measures[name].description,
            }
            for name, owner in g.measure_owner.items()
        ]
        dimensions = []
        for node in g.models.values():
            for dim in node.dimensions.values():
                item = {
                    "name": f"{node.name}.{dim.name}",
                    "type": dim.type,
                    "description": dim.description,
                }
                if dim.sensitivity:
                    item["sensitivity"] = dim.sensitivity
                if dim.name in g.rules:
                    item["rule"] = dim.name
                dimensions.append(item)
        rules = [
            {
                "name": r.name,
                "kind": r.kind,
                "models": g.rule_models.get(r.name, []),
                "filter": f"{{rule.{r.name}}}" if r.kind == "predicate" else None,
                "description": r.description,
            }
            for r in g.rules.values()
        ]
        return {"metrics": metrics, "dimensions": dimensions, "rules": rules}

    def search_concepts(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        out = []
        for hit in self.glossary.search(text, limit):
            concept = self.glossary.concepts[hit.concept]
            out.append(
                {
                    "concept": hit.concept,
                    "matched": hit.label,
                    "label": concept.pref_label,
                    "synonyms": concept.alt_labels,
                    "definition": hit.definition,
                    "maps_to": hit.maps_to,
                    "how_to_use": self._usage(hit.maps_to),
                    "score": round(hit.score, 3),
                }
            )
        return out

    def _usage(self, target: str | None) -> str | None:
        if not target:
            return None
        kind, _, name = target.partition(":")
        if kind in ("metric", "measure"):
            return f'metrics: ["{name}"]'
        if kind == "rule":
            rule = self.graph.rules.get(name)
            if rule and rule.kind == "predicate":
                return f'filters: ["{{rule.{name}}}"]'
            models = self.graph.rule_models.get(name, [])
            return f'dimensions: ["{models[0]}.{name}"]' if models else None
        if kind == "dimension":
            return f'dimensions: ["{name}"]'
        if kind == "entity":
            return f"entity `{name}`: use the dimensions and measures of the models linked to it"
        return None

    def explain(self, name: str) -> dict[str, Any]:
        g = self.graph
        kind, _, rest = name.partition(":")
        if rest:
            name = rest
        else:
            kind = ""
        if kind in ("", "concept") and name in self.glossary.concepts:
            concept = self.glossary.concepts[name]
            out = {"kind": "concept", "name": name, "definition": concept.definition}
            out["label"] = concept.pref_label
            out["synonyms"] = concept.alt_labels
            if concept.broader:
                out["broader"] = concept.broader
            if concept.maps_to:
                out["defined_by"] = self.explain(concept.maps_to)
            return out
        if kind in ("", "rule") and name in g.rules:
            rule = g.rules[name]
            return {
                "kind": "rule",
                "name": name,
                "rule_kind": rule.kind,
                "description": rule.description,
                "owner": rule.owner,
                "effective_from": rule.effective_from.isoformat() if rule.effective_from else None,
                "logic": rule_logic(rule),
                "applies_to": [b.model for b in rule.applies_to],
                "concepts": self._concepts(f"rule:{name}"),
                "text": _rule_text(rule),
            }
        if kind in ("", "metric") and name in g.metrics:
            metric = g.metrics[name]
            return {
                "kind": "metric",
                "name": name,
                "type": metric.type,
                "description": metric.description,
                "definition": _metric_text(metric),
                "concepts": self._concepts(f"metric:{name}"),
            }
        if kind in ("", "metric", "measure") and name in g.measure_owner:
            owner = g.measure_owner[name]
            measure = g.models[owner].measures[name]
            return {
                "kind": "measure",
                "name": name,
                "model": owner,
                "description": measure.description,
                "definition": f"{measure.agg}({measure.expr or ('*' if measure.agg == 'count' else measure.name)}) in `{owner}`",
                "concepts": self._concepts(f"metric:{name}") + self._concepts(f"measure:{name}"),
            }
        try:
            ref = g.resolve_dimension(name)
        except QueryError as exc:
            raise QueryError(
                f"`{name}` is not a metric, measure, dimension, rule or concept"
            ) from exc
        dim = g.dimension(ref)
        return {
            "kind": "dimension",
            "name": f"{ref.model}.{ref.name}",
            "type": dim.type,
            "description": dim.description,
            "sensitivity": dim.sensitivity,
            "concepts": self._concepts(f"dimension:{ref.model}.{ref.name}"),
        }

    def _concepts(self, target: str) -> list[str]:
        return [c.concept for c in self.glossary.for_target(target)]


def _metric_text(metric) -> str:
    if isinstance(metric, SimpleMetric):
        text = f"the measure `{metric.measure}`"
        return f"{text}, only where {metric.filter}" if metric.filter else text
    if isinstance(metric, RatioMetric):
        return f"`{metric.numerator}` divided by `{metric.denominator}`"
    if isinstance(metric, DerivedMetric):
        return f"the expression {metric.expr}"
    if isinstance(metric, CumulativeMetric):
        period = (
            f"since the start of each {metric.grain_to_date}"
            if metric.grain_to_date
            else "running total"
        )
        return f"`{metric.measure}` {period}"
    return ""


def _rule_text(rule) -> str:
    owner = f", owned by {rule.owner}" if rule.owner else ""
    since = f", effective since {rule.effective_from.isoformat()}" if rule.effective_from else ""
    if rule.kind == "predicate":
        what = f"true when {rule.logic}"
    elif rule.decision_table:
        rows = [
            f"{r.then} if {r.when}" if r.otherwise is None else f"{r.otherwise} otherwise"
            for r in rule.decision_table
        ]
        what = "classifies as " + "; ".join(rows)
    else:
        what = f"computes {rule.logic}"
    return f"{rule.description or rule.name}: {what}{owner}{since}."
