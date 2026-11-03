"""Business rule versions: queries use the version in effect on `rules_as_of` (default `as_of`)."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from cosmos_semantic.catalog import Catalog
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile
from cosmos_semantic.engine import Engine
from cosmos_semantic.ontology import run_rule_tests
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.service import SemanticService
from cosmos_semantic.spec import BusinessRule, Project

SOURCES = ["duckdb_connector", "postgres_connector", "sqlserver_connector", "databricks_connector"]
MIDWEST = Principal(user={"region": "Midwest"}, channel="api", subject="ana@example.com")
CHANGE = dt.date(2026, 7, 1)  # active_customer went from 60 to 90 days on this date


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


def active(graph, connector, **dates) -> int:
    request = QueryRequest(metrics=["customer_count"], filters=["{rule.active_customer}"], **dates)
    planner = Planner(graph, connector.dialect, connector.config.schema_map)
    rows = connector.execute(planner.plan(request, MIDWEST).sql).to_pylist()
    return int(next(iter(rows[0].values())) or 0)


def expected(tables, as_of: dt.date, days: int) -> int:
    since = dt.datetime.combine(as_of - dt.timedelta(days=days), dt.time())
    return sum(
        1
        for row in tables["customers"].to_pylist()
        if row["region"] == "Midwest"
        and row["last_order_at"] is not None
        and _as_datetime(row["last_order_at"]) >= since
    )


def _as_datetime(value) -> dt.datetime:
    return value if isinstance(value, dt.datetime) else dt.datetime.combine(value, dt.time())


def test_as_of_picks_the_version_in_effect(graph, connector, tables):
    today = dt.date.today()
    before = CHANGE - dt.timedelta(days=10)
    assert active(graph, connector, as_of=today) == expected(tables, today, 90)
    # A date before the change uses the 60-day version...
    assert active(graph, connector, as_of=before) == expected(tables, before, 60)
    # ...unless the caller asks for the current rules applied to that date.
    current = active(graph, connector, as_of=before, rules_as_of=today)
    assert current == expected(tables, before, 90)


def test_versions_agree_across_databases(graph, request, tables):
    before = CHANGE - dt.timedelta(days=10)
    counts = set()
    for source in SOURCES:
        try:
            connector = request.getfixturevalue(source)
        except pytest.skip.Exception:
            continue
        counts.add(
            (
                active(graph, connector, as_of=before),
                active(graph, connector, as_of=before, rules_as_of=CHANGE),
            )
        )
    assert len(counts) == 1


def test_trace_and_evidence_name_the_version(graph, duckdb_connector, tmp_path):
    engine = Engine(graph, ConnectionsFile(data_sources={"src": duckdb_connector.config}))
    engine._connectors["src"] = duckdb_connector
    service = SemanticService(engine, audit_path=tmp_path / "audit.jsonl")
    request = QueryRequest(
        metrics=["customer_count"],
        filters=["{rule.active_customer}"],
        as_of=CHANGE - dt.timedelta(days=1),
    )
    ev = service.query(request, MIDWEST)["evidence"]
    assert ev["rules_as_of"] == "2026-06-30"
    (rule,) = ev["rules"]
    assert rule["effective_from"] == "2026-01-01"
    assert rule["current_version"] is False and rule["versions"] == 2
    assert "'60' DAY" in rule["logic"]
    assert rule["tests"] == {"status": "passed", "passed": 2, "total": 2}
    assert any("not the current one" in c for c in ev["caveats"])
    # Replay re-runs with the same rule version.
    assert service.replay(ev["query_id"])["verdict"] == "identical"

    now = service.query(request.model_copy(update={"rules_as_of": CHANGE}), MIDWEST)
    (rule,) = now["evidence"]["rules"]
    assert rule["current_version"] is True and "'90' DAY" in rule["logic"]


def test_explain_lists_the_history(graph):
    out = Catalog(graph).explain("rule:active_customer")
    assert out["effective_from"] == "2026-07-01"
    assert out["change_note"].startswith("Window widened")
    assert [h["effective_from"] for h in out["history"]] == ["2026-01-01"]
    assert "earlier version" in out["text"]


def test_tests_run_per_version(graph):
    project = graph.project.model_copy(deep=True)
    rule = next(r for r in project.business_rules if r.name == "active_customer")
    rule.history[0].tests[0].expect = False  # the 60-day version gets a wrong example
    results = [r for r in run_rule_tests(project) if r.rule == "active_customer"]
    failed = {(r.version, r.index) for r in results if not r.passed}
    assert failed == {(dt.date(2026, 1, 1), 0)}


RULE = {
    "name": "band",
    "kind": "classification",
    "effective_from": "2026-07-01",
    "inputs": {"amount": "number"},
    "decision_table": [{"when": "{amount} >= 10", "then": "High"}, {"otherwise": "Low"}],
    "applies_to": [{"model": "orders", "bind": {"amount": "amount"}}],
}


@pytest.mark.parametrize(
    "history,message",
    [
        ([{"effective_from": "2026-08-01", "logic": "'x'"}], "start before"),
        (
            [
                {"effective_from": "2026-01-01", "logic": "'x'"},
                {"effective_from": "2026-01-01", "logic": "'y'"},
            ],
            "same `effective_from`",
        ),
        (
            [{"effective_from": "2026-01-01", "logic": "'x'", "decision_table": []}],
            "either `logic` or `decision_table`",
        ),
    ],
)
def test_invalid_history_is_rejected(history, message):
    with pytest.raises(ValidationError, match=message):
        BusinessRule.model_validate({**RULE, "history": history})


def test_history_needs_a_current_effective_date():
    with pytest.raises(ValidationError, match="start before"):
        BusinessRule.model_validate(
            {
                **RULE,
                "effective_from": None,
                "history": [{"effective_from": "2026-01-01", "logic": "'x'"}],
            }
        )


def test_old_version_logic_is_validated(graph):
    data = graph.project.model_dump(mode="json")
    rule = next(r for r in data["business_rules"] if r["name"] == "active_customer")
    rule["history"][0]["logic"] = "{missing_input} > 1"
    with pytest.raises(Exception, match="missing_input"):
        compile_project(Project.model_validate(data))
