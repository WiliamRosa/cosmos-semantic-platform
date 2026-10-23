"""AI-assisted semantic discovery (ADR 0009).

A language model suggests glossary concepts (with English and Portuguese labels), business
rules, metrics and descriptions for an existing semantic model. Nothing is applied: every
suggestion is validated by the engine (compiled into a copy of the model and, when a data
source is given, planned and run), and the accepted ones are written to a separate YAML file
for a person to review, as a pull request would be.

The model sees names, types, descriptions and existing definitions. Sample values are sent only
when the caller opts in, and never for columns classified as personal or sensitive.

The LLM sits behind ``SuggestionModel``; ``ClaudeSuggestionModel`` calls the Claude API through
the optional ``anthropic`` package (``uv sync --extra ai``), and tests use a fake model.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from cosmos_semantic.errors import CosmosError, QueryError
from cosmos_semantic.expressions import table_ref
from cosmos_semantic.spec import BusinessRule, Concept, Metric, Project

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph
    from cosmos_semantic.connectors import Connector

DEFAULT_MODEL = "claude-opus-5-5"
MAX_SAMPLES = 10

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


SUGGESTIONS_SCHEMA = _object(
    {
        "concepts": {
            "type": "array",
            "items": _object(
                {
                    "concept": {"type": "string"},
                    "maps_to": {"type": "string"},
                    "label_en": {"type": "string"},
                    "label_pt": {"type": "string"},
                    "synonyms_en": _strings(),
                    "synonyms_pt": _strings(),
                    "definition": {"type": "string"},
                    "rationale": {"type": "string"},
                }
            ),
        },
        "rules": {
            "type": "array",
            "items": _object(
                {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["predicate", "classification", "derived"]},
                    "description": {"type": "string"},
                    "model": {"type": "string"},
                    "inputs": {
                        "type": "array",
                        "items": _object(
                            {
                                "name": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": ["categorical", "time", "number", "boolean"],
                                },
                                "column": {"type": "string"},
                            }
                        ),
                    },
                    "logic": {"type": "string"},
                    "rationale": {"type": "string"},
                }
            ),
        },
        "metrics": {
            "type": "array",
            "items": _object(
                {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["simple", "ratio", "derived"]},
                    "description": {"type": "string"},
                    "measure": _NULLABLE_STRING,
                    "numerator": _NULLABLE_STRING,
                    "denominator": _NULLABLE_STRING,
                    "expr": _NULLABLE_STRING,
                    "filter": _NULLABLE_STRING,
                    "rationale": {"type": "string"},
                }
            ),
        },
        "descriptions": {
            "type": "array",
            "items": _object({"member": {"type": "string"}, "description": {"type": "string"}}),
        },
    }
)

INSTRUCTIONS = """\
You help a data team enrich a semantic model: a governed layer of metrics, dimensions, business
rules and a business glossary that AI agents and BI tools query. You receive the current model as
JSON. Suggest what a business user would expect to find but is missing:

- concepts: glossary entries for terms people use in questions, each mapped to an existing member
  (`metric:<name>`, `measure:<name>`, `dimension:<model>.<dimension>`, `rule:<name>` or
  `entity:<name>`), with an English and a Portuguese label and synonyms in both languages.
- rules: reusable business definitions over the columns of one model. `logic` is a SQL
  expression (PostgreSQL syntax) that uses each input as `{input}`; a classification returns a
  label with CASE WHEN. Each input names the model dimension it binds to in `column`.
- metrics: new metrics built only from existing measures and metrics. `filter` and `expr` refer to
  members as `{name}` or `{model.dimension}`. Use null for fields a metric type does not use.
- descriptions: short descriptions for members that have none (`model`, `model.dimension`,
  `measure:<name>` or `metric:<name>`).

Use snake_case English identifiers. Prefer a few well-grounded suggestions over many guesses, and
explain each one in `rationale`. Never invent tables, columns or members. Sample values, when
present, show what the data looks like; do not copy them into definitions unless they are
category labels.
"""


class SuggestionModel(Protocol):
    name: str

    def suggest(self, context: dict[str, Any]) -> dict[str, Any]: ...


class ClaudeSuggestionModel:
    """Calls the Claude API with a strict JSON schema for the answer."""

    def __init__(self, model: str = DEFAULT_MODEL, client: Any = None) -> None:
        self.name = model
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise CosmosError(
                    "AI discovery needs the optional `anthropic` package: uv sync --extra ai"
                ) from exc
            # Credentials come from ANTHROPIC_API_KEY or an `ant auth login` profile.
            client = anthropic.Anthropic()
        self.client = client

    def suggest(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            import anthropic
        except ImportError:  # a test client does not need the package
            return self._suggest(context)
        try:
            return self._suggest(context)
        except anthropic.AuthenticationError as exc:
            raise CosmosError(f"the Claude API rejected the credentials: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise CosmosError("the Claude API rate limit was reached; try again later") from exc
        except anthropic.APIStatusError as exc:
            raise CosmosError(f"the Claude API returned {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise CosmosError(f"could not reach the Claude API: {exc}") from exc
        except TypeError as exc:
            if "authentication" not in str(exc):
                raise
            raise CosmosError(
                "no Claude API credentials: set ANTHROPIC_API_KEY or run `ant auth login`"
            ) from exc

    def _suggest(self, context: dict[str, Any]) -> dict[str, Any]:
        response = self.client.beta.messages.create(
            model=self.name,
            max_tokens=16000,
            system=INSTRUCTIONS,
            messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": SUGGESTIONS_SCHEMA},
            },
            # On a safety decline, the API retries on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise CosmosError("the model declined to make suggestions for this model")
        if response.stop_reason == "max_tokens":
            raise CosmosError("the suggestions were cut off; try a smaller model (--model-dir)")
        text = next(block.text for block in response.content if block.type == "text")
        return json.loads(text)


# -- context --------------------------------------------------------------------------------


def build_context(
    graph: SemanticGraph, connector: Connector | None = None, *, samples: bool = False
) -> dict[str, Any]:
    """What the language model sees. Sample values are opt-in and skip sensitive columns."""
    project = graph.project
    models = []
    for name, node in graph.models.items():
        dims = []
        for dim_name, dim in node.dimensions.items():
            if dim_name in graph.rules:
                continue
            item: dict[str, Any] = {"name": dim_name, "type": dim.type}
            if dim.description:
                item["description"] = dim.description
            if dim.sensitivity:
                item["sensitivity"] = dim.sensitivity
            elif samples and connector is not None and dim.type == "categorical":
                item["sample_values"] = _samples(connector, node.spec.table, dim.expr or dim_name)
            dims.append(item)
        models.append(
            {
                "name": name,
                "description": node.spec.description,
                "dimensions": dims,
                "measures": [
                    {"name": m, "agg": spec.agg, "description": spec.description}
                    for m, spec in node.measures.items()
                ],
                "joins_to": sorted(graph.reachable(name) - {name}),
            }
        )
    return {
        "models": models,
        "metrics": [
            {"name": m.name, "type": m.type, "description": m.description} for m in project.metrics
        ],
        "rules": [
            {"name": r.name, "kind": r.kind, "description": r.description}
            for r in project.business_rules
        ],
        "concepts": [{"concept": c.concept, "maps_to": c.maps_to} for c in project.glossary],
    }


def _samples(connector: Connector, table: str, expr: str) -> list[Any]:
    source = table_ref(table, connector.dialect, connector.config.schema_map)
    sql = (
        f"SELECT DISTINCT {expr} AS v FROM {source.sql(dialect=connector.dialect)} "
        f"WHERE {expr} IS NOT NULL"
    )
    try:
        rows = connector.execute(sql, max_rows=MAX_SAMPLES).to_pylist()
    except CosmosError:
        return []
    return sorted(str(r["v"]) for r in rows)[:MAX_SAMPLES]


# -- validation -----------------------------------------------------------------------------


@dataclass
class DiscoveryReport:
    model: str
    concepts: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    descriptions: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def project(self) -> Project:
        """The accepted suggestions as a model file of their own, for review."""
        return Project.model_validate(
            {
                "version": 1,
                "glossary": self.concepts,
                "business_rules": self.rules,
                "metrics": self.metrics,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "accepted": {
                "concepts": [c["concept"] for c in self.concepts],
                "rules": [r["name"] for r in self.rules],
                "metrics": [m["name"] for m in self.metrics],
                "descriptions": len(self.descriptions),
            },
            "descriptions": self.descriptions,
            "rejected": self.rejected,
        }


class Validator:
    """Accepts a suggestion only if the model still compiles with it (and, given a data
    source, a query using it runs)."""

    def __init__(self, graph: SemanticGraph, connector: Connector | None = None) -> None:
        self.base = graph.project.model_dump(mode="json")
        self.connector = connector
        self.accepted: dict[str, list[dict[str, Any]]] = {
            "glossary": [],
            "business_rules": [],
            "metrics": [],
        }

    def _compile(self, key: str, item: dict[str, Any]) -> SemanticGraph:
        from cosmos_semantic.compiler import compile_project

        data = json.loads(json.dumps(self.base))
        for k, items in self.accepted.items():
            data[k] = data.get(k, []) + items
        data[key] = data.get(key, []) + [item]
        graph = compile_project(Project.model_validate(data), strict=False)
        errors = [i for i in graph.issues if i.severity == "error"]
        if errors:
            raise QueryError("; ".join(str(e) for e in errors[:3]))
        return graph

    def _run(self, graph: SemanticGraph, metrics: list[str], filters: list[str]) -> None:
        if self.connector is None:
            return
        from cosmos_semantic.planner import Planner, Principal, QueryRequest

        # Placeholder values satisfy row filters; the query only proves the SQL runs.
        user = {name: "__validation__" for name in graph.project.user_context}
        planner = Planner(graph, self.connector.dialect, self.connector.config.schema_map)
        compiled = planner.plan(
            QueryRequest(metrics=metrics, filters=filters), Principal(user=user)
        )
        self.connector.execute(compiled.sql, max_rows=1)

    def try_add(self, key: str, item: dict[str, Any], run: tuple[list, list] | None) -> None:
        graph = self._compile(key, item)
        if run is not None:
            self._run(graph, *run)
        self.accepted[key].append(item)


def _any_measure(base: dict[str, Any], model: str) -> str | None:
    for m in base.get("semantic_models", []):
        if m["name"] == model and m.get("measures"):
            return m["measures"][0]["name"]
    return None


def discover(
    graph: SemanticGraph,
    llm: SuggestionModel,
    connector: Connector | None = None,
    *,
    samples: bool = False,
) -> DiscoveryReport:
    raw = llm.suggest(build_context(graph, connector, samples=samples))
    validator = Validator(graph, connector)
    report = DiscoveryReport(model=llm.name)
    known = set(graph.metrics) | set(graph.measure_owner) | set(graph.rules)
    concepts = {c.concept for c in graph.project.glossary}

    def reject(kind: str, name: str, reason: str) -> None:
        report.rejected.append({"kind": kind, "name": name, "reason": reason})

    # Rules first, then metrics, then concepts: later suggestions may build on earlier ones.
    for s in raw.get("rules", []):
        name = s.get("name", "?")
        if name in known:
            reject("rule", name, "a member with this name already exists")
            continue
        try:
            item = {
                "name": name,
                "kind": s["kind"],
                "description": s.get("description"),
                "inputs": {i["name"]: i["type"] for i in s["inputs"]},
                "logic": s["logic"],
                "applies_to": [
                    {"model": s["model"], "bind": {i["name"]: i["column"] for i in s["inputs"]}}
                ],
            }
            item = {k: v for k, v in item.items() if v is not None}
            measure = _any_measure(validator.base, s["model"])
            predicate = s["kind"] == "predicate" and measure
            run = ([measure], ["{rule." + name + "}"]) if predicate else None
            BusinessRule.model_validate(item)
            validator.try_add("business_rules", item, run)
        except (ValidationError, CosmosError, ValueError, KeyError) as exc:
            reject("rule", name, _reason(exc))
            continue
        report.rules.append(item)
        known.add(name)

    for s in raw.get("metrics", []):
        name = s.get("name", "?")
        if name in known:
            reject("metric", name, "a member with this name already exists")
            continue
        item = {k: v for k, v in s.items() if v is not None and k != "rationale"}
        try:
            validator.try_add("metrics", _metric(item), ([name], []))
        except (ValidationError, CosmosError, ValueError, KeyError) as exc:
            reject("metric", name, _reason(exc))
            continue
        report.metrics.append(_metric(item))
        known.add(name)

    for s in raw.get("concepts", []):
        name = s.get("concept", "?")
        if name in concepts:
            reject("concept", name, "the concept already exists; extend it by hand")
            continue
        try:
            item = {
                "concept": name,
                "pref_label": {"en": s["label_en"], "pt": s["label_pt"]},
                "alt_labels": {"en": s.get("synonyms_en", []), "pt": s.get("synonyms_pt", [])},
                "definition": s.get("definition"),
                "maps_to": s.get("maps_to"),
            }
            Concept.model_validate(item)
            validator.try_add("glossary", item, None)
        except (ValidationError, CosmosError, ValueError, KeyError) as exc:
            reject("concept", name, _reason(exc))
            continue
        report.concepts.append(item)
        concepts.add(name)

    for s in raw.get("descriptions", []):
        member, text = s.get("member", ""), s.get("description", "")
        problem = _description_problem(graph, member)
        if problem:
            reject("description", member, problem)
        elif text.strip():
            report.descriptions.append({"member": member, "description": text.strip()})
    return report


def _metric(item: dict[str, Any]) -> dict[str, Any]:
    from pydantic import TypeAdapter

    TypeAdapter(Metric).validate_python(item)
    return item


def _description_problem(graph: SemanticGraph, member: str) -> str | None:
    kind, sep, name = member.partition(":")
    if not sep:
        kind, name = ("dimension", member) if "." in member else ("model", member)
    if kind == "model":
        node = graph.models.get(name)
        if node is None:
            return "unknown model"
        return "already described" if node.spec.description else None
    if kind == "dimension":
        model, _, dim = name.partition(".")
        node = graph.models.get(model)
        if node is None or dim not in node.dimensions:
            return "unknown dimension"
        return "already described" if node.dimensions[dim].description else None
    if kind == "measure":
        owner = graph.measure_owner.get(name)
        if owner is None:
            return "unknown measure"
        return "already described" if graph.models[owner].measures[name].description else None
    if kind == "metric":
        metric = graph.metrics.get(name)
        if metric is None:
            return "unknown metric"
        return "already described" if metric.description else None
    return f"unknown member kind `{kind}`"


def _reason(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return f"missing field {exc}"
    text = " ".join(str(exc).split())
    return text[:300] or type(exc).__name__
