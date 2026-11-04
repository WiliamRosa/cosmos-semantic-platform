"""AI-assisted discovery (ADR 0009): suggestions are validated by the engine, never applied.

No API key is needed: a fake model stands in for Claude, and a fake client checks the request.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.discovery import (
    DEFAULT_MODEL,
    SUGGESTIONS_SCHEMA,
    ClaudeSuggestionModel,
    build_context,
    discover,
)
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.interchange import dump_project
from cosmos_semantic.spec import Project

SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
]


def metric(name, type_, **fields):
    base = {
        "name": name,
        "type": type_,
        "description": f"{name} (suggested)",
        "measure": None,
        "numerator": None,
        "denominator": None,
        "expr": None,
        "filter": None,
        "rationale": "test",
    }
    return base | fields


SUGGESTIONS = {
    "rules": [
        {
            "name": "high_value_order",
            "kind": "predicate",
            "description": "Order of 1,000 or more",
            "model": "orders",
            "inputs": [{"name": "amount", "type": "number", "column": "amount"}],
            "logic": "{amount} >= 1000",
            "rationale": "used in sales reviews",
        },
        {
            "name": "bad_rule",
            "kind": "predicate",
            "description": "Uses an input it does not declare",
            "model": "orders",
            "inputs": [{"name": "amount", "type": "number", "column": "amount"}],
            "logic": "{missing} > 1",
            "rationale": "invalid on purpose",
        },
    ],
    "metrics": [
        metric("pending_revenue", "simple", measure="revenue", filter="{status} = 'pending'"),
        metric("high_value_revenue", "simple", measure="revenue", filter="{rule.high_value_order}"),
        metric("revenue_per_buyer", "ratio", numerator="revenue", denominator="buyers"),
        metric("ghost_metric", "derived", expr="{no_such_measure} * 2"),
        metric("revenue", "simple", measure="revenue"),
    ],
    "concepts": [
        {
            "concept": "pending_sales",
            "maps_to": "metric:pending_revenue",
            "label_en": "Pending sales",
            "label_pt": "Vendas pendentes",
            "synonyms_en": ["open orders value"],
            "synonyms_pt": ["pedidos em aberto"],
            "definition": "Value of orders not yet completed",
            "rationale": "common question",
        },
        {
            "concept": "phantom",
            "maps_to": "metric:does_not_exist",
            "label_en": "Phantom",
            "label_pt": "Fantasma",
            "synonyms_en": [],
            "synonyms_pt": [],
            "definition": "Points nowhere",
            "rationale": "invalid on purpose",
        },
        {
            "concept": "customer",
            "maps_to": "entity:customer",
            "label_en": "Customer",
            "label_pt": "Cliente",
            "synonyms_en": [],
            "synonyms_pt": [],
            "definition": "Already exists",
            "rationale": "duplicate",
        },
    ],
    "descriptions": [
        {"member": "measure:discounts", "description": "Discounts granted on orders"},
        {"member": "measure:revenue", "description": "Already has one"},
        {"member": "orders.nope", "description": "Unknown"},
    ],
}


class FakeModel:
    name = "fake-model"

    def __init__(self, answer):
        self.answer = answer
        self.context = None

    def suggest(self, context):
        self.context = context
        return self.answer


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


def test_suggestions_are_validated_by_running_them(graph, connector):
    report = discover(graph, FakeModel(SUGGESTIONS), connector)
    assert [r["name"] for r in report.rules] == ["high_value_order"]
    assert [m["name"] for m in report.metrics] == [
        "pending_revenue",
        "high_value_revenue",  # builds on the rule accepted just before
        "revenue_per_buyer",
    ]
    assert [c["concept"] for c in report.concepts] == ["pending_sales"]
    assert report.descriptions == [
        {"member": "measure:discounts", "description": "Discounts granted on orders"}
    ]
    rejected = {(r["kind"], r["name"]): r["reason"] for r in report.rejected}
    assert "`{missing}`, which is not in `inputs`" in rejected[("rule", "bad_rule")]
    assert "no_such_measure" in rejected[("metric", "ghost_metric")]
    assert "already exists" in rejected[("metric", "revenue")]
    assert ("concept", "phantom") in rejected and ("concept", "customer") in rejected
    assert rejected[("description", "measure:revenue")] == "already described"
    # Each rejection keeps the suggestion as sent, for a reviewer to fix by hand.
    ghost = next(r for r in report.rejected if r["name"] == "ghost_metric")
    assert ghost["suggestion"]["name"] == "ghost_metric"


def test_accepted_file_merges_into_the_model(graph):
    report = discover(graph, FakeModel(SUGGESTIONS))
    text = dump_project(report.project())
    extra = Project.model_validate(yaml.safe_load(text))
    data = graph.project.model_dump(mode="json")
    for key in ("glossary", "business_rules", "metrics"):
        data[key] += extra.model_dump(mode="json")[key]
    merged = compile_project(Project.model_validate(data))
    assert "pending_revenue" in merged.metrics and "high_value_order" in merged.rules
    # The original model is untouched.
    assert "pending_revenue" not in graph.metrics


def test_context_never_samples_sensitive_columns(graph, duckdb_connector):
    context = build_context(graph, duckdb_connector, samples=True)
    customers = next(m for m in context["models"] if m["name"] == "customers")
    dims = {d["name"]: d for d in customers["dimensions"]}
    assert "Midwest" in dims["region"]["sample_values"]
    for name in ("tax_id", "email", "health_plan", "name"):
        if name in dims:
            assert "sample_values" not in dims[name], name
    assert "sample_values" not in json.dumps(build_context(graph, duckdb_connector))


class FakeClient:
    def __init__(self, stop_reason="end_turn", payload=None):
        self.calls = []
        self.stop_reason = stop_reason
        self.payload = payload if payload is not None else SUGGESTIONS
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=json.dumps(self.payload)),
            ],
        )


def test_claude_request_uses_strict_structured_output(graph):
    client = FakeClient()
    llm = ClaudeSuggestionModel(client=client)
    report = discover(graph, llm)
    assert report.model == DEFAULT_MODEL == "claude-opus-5-5"
    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SUGGESTIONS_SCHEMA}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    context = json.loads(call["messages"][0]["content"])
    assert {m["name"] for m in context["models"]} >= {"orders", "customers"}


def _strict(schema) -> bool:
    if schema.get("type") == "object":
        return (
            schema.get("additionalProperties") is False
            and set(schema["required"]) == set(schema["properties"])
            and all(_strict(s) for s in schema["properties"].values())
        )
    if schema.get("type") == "array":
        return _strict(schema["items"])
    return True


def test_schema_is_strict():
    assert _strict(SUGGESTIONS_SCHEMA)


def test_refusal_and_truncation_are_errors(graph):
    for reason, message in (("refusal", "declined"), ("max_tokens", "cut off")):
        llm = ClaudeSuggestionModel(client=FakeClient(stop_reason=reason))
        with pytest.raises(CosmosError, match=message):
            discover(graph, llm)
