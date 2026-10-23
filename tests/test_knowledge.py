"""Business knowledge graph (ADR 0009): teams, systems, processes, ownership and export."""

from __future__ import annotations

import pytest

from cosmos_semantic.catalog import Catalog
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.impact import analyze, changed_members
from cosmos_semantic.knowledge import build_graph, owner_of
from cosmos_semantic.spec import Project


def variant(graph, change) -> dict:
    data = graph.project.model_dump(mode="json")
    change(data)
    return data


def test_system_outage_reaches_metrics_processes_and_owners(graph):
    report = analyze(graph, ["system:payment_gateway"])
    assert report["affected"]["models"] == ["payments"]
    assert report["affected"]["metrics"] == ["collection_gap"]
    assert report["affected"]["processes"] == ["order_to_cash"]
    assert report["source_systems"] == ["payment_gateway"]
    assert set(report["owners"]) == {"controllership"}


def test_rule_change_reaches_its_process(graph):
    report = analyze(graph, ["rule:active_customer"])
    assert report["affected"]["processes"] == ["customer_retention"]
    assert report["owners"]["sales-leadership"] == [
        "process:customer_retention",
        "rule:active_customer",
    ]


def test_measures_and_dimensions_inherit_the_model_owner(graph):
    assert owner_of(graph, "metric:net_revenue") == "controllership"
    assert owner_of(graph, "measure:revenue") == "digital-commerce"
    assert owner_of(graph, "dimension:payments.method") == "controllership"
    assert owner_of(graph, "measure:paid_value") == "controllership"


def test_unowned_members_are_flagged(graph):
    data = variant(graph, lambda d: d["metrics"][0].pop("owner"))
    other = compile_project(Project.model_validate(data))
    name = data["metrics"][0]["name"]
    report = analyze(other, [f"metric:{name}"])
    assert f"metric:{name}" in report["owners"]["unowned"]


def test_explain_answers_who_owns_and_where_from(graph):
    catalog = Catalog(graph)
    metric = catalog.explain("metric:collection_gap")
    assert metric["owner"] == "controllership"
    assert metric["source_systems"] == ["payment_gateway", "storefront"]
    assert metric["processes"] == ["order_to_cash"]
    team = catalog.explain("team:controllership")
    assert team["contact"] and "metric:net_revenue" in team["owns"]
    system = catalog.explain("storefront")
    assert system["kind"] == "system" and "customer_retention" in system["processes"]
    process = catalog.explain("process:order_to_cash")
    assert process["systems"] == ["storefront", "payment_gateway"]


def test_validation_catches_broken_links(graph):
    def broken(d):
        d["systems"][0]["feeds"].append("ghost_model")
        d["processes"][0]["systems"].append("ghost_system")
        d["processes"][0]["uses"].append("metric:ghost_metric")

    with pytest.raises(ModelError) as exc:
        compile_project(Project.model_validate(variant(graph, broken)))
    text = str(exc.value)
    assert "ghost_model" in text and "ghost_system" in text and "ghost_metric" in text


def test_owner_outside_the_declared_teams_warns(graph):
    data = variant(graph, lambda d: d["metrics"][0].update(owner="nobody"))
    other = compile_project(Project.model_validate(data))
    assert any("`nobody` is not a declared team" in i.message for i in other.issues)


def test_changed_processes_and_systems_are_detected(graph):
    data = variant(graph, lambda d: d["processes"][1]["uses"].append("metric:net_revenue"))
    assert changed_members(graph, compile_project(Project.model_validate(data))) == [
        "process:customer_retention"
    ]


def test_graph_export_and_focus(graph):
    kg = build_graph(graph)
    kinds = {n["kind"] for n in kg.as_dict()["nodes"]}
    assert {"team", "system", "process", "model", "metric", "measure", "rule", "concept"} <= kinds
    relations = {(e["source"], e["target"], e["relation"]) for e in kg.edges}
    assert ("system:payment_gateway", "model:payments", "feeds") in relations
    assert ("team:controllership", "metric:net_revenue", "owns") in relations
    assert ("metric:net_revenue", "process:order_to_cash", "used_by") in relations
    assert len(relations) == len(kg.edges)  # no duplicate edges

    focused = kg.focus("process:customer_retention")
    assert "rule:active_customer" in focused.nodes
    assert "metric:collection_gap" not in focused.nodes
    assert "team:sales-leadership" in focused.nodes

    mermaid = kg.render("mermaid")
    assert mermaid.startswith("flowchart LR") and "-->|feeds|" in mermaid
    dot = kg.render("dot")
    assert dot.startswith("digraph cosmos") and dot.rstrip().endswith("}")
    with pytest.raises(QueryError):
        kg.render("png")
    with pytest.raises(QueryError):
        kg.focus("metric:nope")
