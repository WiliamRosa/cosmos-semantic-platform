from __future__ import annotations

import pytest
import yaml

from cosmos_semantic.catalog import Catalog
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import ModelError, PolicyError
from cosmos_semantic.loader import load_project
from cosmos_semantic.ontology import Glossary, run_rule_tests
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.reference import load_cases
from cosmos_semantic.spec import Project

from .conftest import CASES, EXAMPLE

QUESTIONS = [c for c in load_cases(CASES) if c.question is not None]


@pytest.mark.parametrize("case", QUESTIONS, ids=lambda c: c.id)
def test_agent_questions_with_synonyms_only(case, graph):
    hits = Glossary(graph.project).search(case.question)
    assert hits and hits[0].maps_to == case.expect_maps_to


def test_rule_examples_pass():
    results = run_rule_tests(load_project(EXAMPLE))
    assert results and all(r.passed for r in results), [r for r in results if not r.passed]


def test_failing_rule_example_is_reported():
    project = Project.model_validate(
        yaml.safe_load(
            """
version: 1
semantic_models: [{name: m, table: t, dimensions: [{name: v, type: number}]}]
business_rules:
  - name: grande
    kind: predicate
    owner: x
    effective_from: 2026-01-01
    inputs: {v: number}
    logic: "{v} > 10"
    applies_to: [{model: m, bind: {v: v}}]
    tests: [{given: {v: 5}, expect: true}]
"""
        )
    )
    [result] = run_rule_tests(project)
    assert not result.passed


def test_rule_becomes_dimension_and_filter(graph):
    planner = Planner(graph)
    by_band = planner.plan(
        QueryRequest(metrics=["order_count"], dimensions=["orders.order_size_band"])
    )
    assert "CASE" in by_band.sql and "Large" in by_band.sql
    active = planner.plan(
        QueryRequest(metrics=["customer_count"], filters=["{rule.active_customer}"]),
        Principal(user={"region": "Midwest"}),
    )
    assert "last_order_at" in active.sql


def test_rule_inherits_sensitivity_of_its_inputs():
    data = yaml.safe_load(
        """
version: 1
semantic_models:
  - name: c
    table: t
    dimensions: [{name: plano, type: categorical, sensitivity: sensitive}]
    measures: [{name: n, agg: count}]
business_rules:
  - name: tem_plano
    kind: predicate
    owner: x
    effective_from: 2026-01-01
    inputs: {p: categorical}
    logic: "{p} <> 'Nenhum'"
    applies_to: [{model: c, bind: {p: plano}}]
"""
    )
    graph = compile_project(Project.model_validate(data))
    with pytest.raises(PolicyError):
        Planner(graph).plan(
            QueryRequest(metrics=["n"], filters=["{rule.tem_plano}"]), Principal(channel="mcp")
        )


def test_rule_validation_errors():
    data = yaml.safe_load(
        """
version: 1
semantic_models: [{name: m, table: t}]
business_rules:
  - name: faixa
    kind: classification
    inputs: {v: number}
    decision_table: [{when: "{v} > 1", then: A}]
    applies_to: [{model: m, bind: {}}]
"""
    )
    graph = compile_project(Project.model_validate(data), strict=False)
    messages = " | ".join(str(i) for i in graph.issues)
    assert "otherwise" in messages
    assert "without a binding" in messages
    assert "owner" in messages  # a warning, not an error


def test_glossary_validation():
    data = yaml.safe_load(
        """
version: 1
semantic_models: [{name: m, table: t, measures: [{name: n, agg: count}]}]
glossary:
  - {concept: a, pref_label: {pt: A}, alt_labels: {pt: [termo]}, maps_to: "metric:n", broader: b}
  - {concept: b, pref_label: {pt: B}, alt_labels: {pt: [termo]}, broader: a}
  - {concept: c, pref_label: {pt: C}, maps_to: "metric:inexistente"}
"""
    )
    graph = compile_project(Project.model_validate(data), strict=False)
    messages = " | ".join(str(i) for i in graph.issues)
    assert "cycle" in messages
    assert "does not exist" in messages
    assert "is also in" in messages
    with pytest.raises(ModelError):
        compile_project(Project.model_validate(data))


def test_catalog_explains_rules_and_concepts(graph):
    catalog = Catalog(graph)
    rule = catalog.explain("active_customer")
    assert rule["kind"] == "concept"
    assert rule["defined_by"]["kind"] == "rule"
    assert rule["defined_by"]["owner"] == "sales-leadership"
    hits = catalog.search_concepts("comprador ativo")
    assert hits[0]["how_to_use"] == 'filters: ["{rule.active_customer}"]'
    members = catalog.members()
    assert any(
        d["name"] == "customers.health_plan" and d["sensitivity"] for d in members["dimensions"]
    )
