"""Evidence envelope and replay (ADR 0008), on every available database."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import PolicyError
from cosmos_semantic.evidence import REPRODUCIBILITY_CAVEAT
from cosmos_semantic.expressions import remap_sql
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import SemanticService

SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
    "bigquery_connector",
    "trino_connector",
]
SUDESTE = Principal(user={"region": "Northeast"}, channel="api", subject="ana@example.com")
REQUEST = QueryRequest(
    metrics=["average_order_value", "customer_count"],
    dimensions=["customers.segment"],
    filters=["{rule.active_customer}"],
)


def make_service(graph, connector, tmp_path) -> SemanticService:
    engine = Engine(graph, ConnectionsFile(data_sources={"src": connector.config}))
    engine._connectors["src"] = connector
    return SemanticService(engine, audit_path=tmp_path / "audit.jsonl")


@pytest.fixture(params=SOURCES)
def connector(request):
    return request.getfixturevalue(request.param)


def test_envelope_describes_definitions_rules_and_policies(graph, connector, tmp_path):
    result = make_service(graph, connector, tmp_path).query(REQUEST, SUDESTE)
    ev = result["evidence"]

    assert ev["query_id"].startswith("q_")
    assert ev["as_of"] and ev["timezone"] == "America/New_York"
    assert ev["data_source"]["dialect"] == connector.dialect
    assert ev["principal"] == {
        "subject": "ana@example.com",
        "channel": "api",
        "user_context": {"region": "Northeast"},
    }

    metrics = {m["name"]: m for m in ev["metrics"]}
    assert metrics["average_order_value"]["formula"] == ("(SUM(orders.amount)) / (COUNT(orders.*))")
    assert {m["name"] for m in metrics["average_order_value"]["measures"]} == {
        "revenue",
        "order_count",
    }
    assert "average_order_value" in metrics["average_order_value"]["concepts"]

    [rule] = ev["rules"]
    assert rule["name"] == "active_customer"
    assert rule["used_as"] == ["filter"]
    assert rule["owner"] == "sales-leadership"
    assert rule["tests"] == {"status": "passed", "passed": 2, "total": 2}

    [row_filter] = ev["policies"]["row_filters"]
    assert row_filter["model"] == "customers"
    assert row_filter["user_fields"] == {"region": "Northeast"}
    assert sorted(row_filter["applied_in"]) == ["customers_agg", "orders_agg"]

    assert set(ev["lineage"]["models"]) == {"customers", "orders"}
    assert ev["result"]["row_count"] == result["row_count"] > 0
    assert REPRODUCIBILITY_CAVEAT in ev["caveats"]


def test_replay_of_an_unchanged_query_is_identical(graph, connector, tmp_path):
    service = make_service(graph, connector, tmp_path)
    query_id = service.query(REQUEST, SUDESTE)["evidence"]["query_id"]
    report = service.replay(query_id)
    assert report["verdict"] == "identical", report
    assert all(report["matches"].values())


def test_replay_detects_changed_data(graph, connector, tmp_path):
    if connector.dialect == "trino":
        pytest.skip("Trino's memory connector does not support UPDATE")
    service = make_service(graph, connector, tmp_path)
    request = QueryRequest(metrics=["revenue"], dimensions=["orders.status"])
    query_id = service.query(request, SUDESTE)["evidence"]["query_id"]

    def run(sql: str) -> None:
        connector.run_script(remap_sql(sql, connector.dialect, connector.config.schema_map))

    try:
        run("UPDATE public.orders SET amount = amount + 1 WHERE order_id = 1")
    except Exception as exc:  # BigQuery's free sandbox refuses DML
        if "billingNotEnabled" in str(exc) or "free tier" in str(exc):
            pytest.skip("DML is not allowed in the BigQuery sandbox")
        raise
    try:
        report = service.replay(query_id)
    finally:
        run("UPDATE public.orders SET amount = amount - 1 WHERE order_id = 1")
    assert report["verdict"] == "data_changed", report
    assert report["matches"]["sql_hash"] and not report["matches"]["result_hash"]
    assert service.replay(query_id)["verdict"] == "identical"


def test_replay_detects_changed_model(graph, connector, tmp_path):
    service = make_service(graph, connector, tmp_path)
    query_id = service.query(REQUEST, SUDESTE)["evidence"]["query_id"]

    project = graph.project.model_copy(deep=True)
    project.metrics[0].description = "edited definition text"
    changed = make_service(compile_project(project), connector, tmp_path)
    report = changed.replay(query_id)
    assert report["verdict"] == "model_changed", report
    # A documentation-only change keeps the SQL and the number.
    assert report["matches"]["sql_hash"] and report["matches"]["result_hash"]


def test_only_the_original_user_can_replay(graph, duckdb_connector, tmp_path):
    service = make_service(graph, duckdb_connector, tmp_path)
    query_id = service.query(REQUEST, SUDESTE)["evidence"]["query_id"]
    other = Principal(user={"region": "Midwest"}, channel="api", subject="bob@example.com")
    with pytest.raises(PolicyError):
        service.replay(query_id, requester=other)
    assert service.replay(query_id, requester=SUDESTE)["verdict"] == "identical"


def test_failed_queries_are_audited_without_hashes(graph, duckdb_connector, tmp_path):
    service = make_service(graph, duckdb_connector, tmp_path)
    with pytest.raises(PolicyError):
        service.query(
            QueryRequest(metrics=["customer_count"], dimensions=["customers.tax_id"]),
            Principal(user={"region": "Midwest"}, channel="mcp"),
        )
    record = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert '"outcome": "error"' in record and '"result_hash": null' in record


def test_same_result_hash_on_every_database(graph, request, tmp_path):
    hashes = {}
    for name in SOURCES:
        try:
            connector = request.getfixturevalue(name)
        except pytest.skip.Exception:
            continue
        ev = make_service(graph, connector, tmp_path / name).query(REQUEST, SUDESTE)["evidence"]
        hashes[name] = (ev["result"]["hash"], ev["model_hash"])
    if len(hashes) < 2:
        pytest.skip("needs at least two databases")
    assert len(set(hashes.values())) == 1, hashes


def test_cumulative_formula_names_the_running_aggregate(graph):
    from cosmos_semantic.evidence import EvidenceBuilder

    builder = EvidenceBuilder(graph)
    assert builder.formula("max_order_value_year_to_date") == (
        "running maximum of MAX(orders.amount) within each year"
    )
    assert builder.formula("revenue_year_to_date") == (
        "running total of SUM(orders.amount) within each year"
    )


def test_offset_formula_names_the_earlier_period(graph):
    """A derived metric's input read at an earlier period says so (DEC-18), also through a
    metric that uses it; the unshifted references do not."""
    from cosmos_semantic.evidence import EvidenceBuilder

    builder = EvidenceBuilder(graph)
    assert builder.formula("revenue_prev_month") == "(SUM(orders.amount)) 1 month earlier"
    assert builder.formula("order_count_7_days_earlier") == "(COUNT(orders.*)) 7 days earlier"
    assert builder.formula("revenue_mom_growth") == (
        "((SUM(orders.amount)) - ((SUM(orders.amount)) 1 month earlier)) / "
        "((SUM(orders.amount)) 1 month earlier)"
    )


def test_cumulative_and_offset_metrics_carry_a_time_block(graph, connector, tmp_path):
    """Cumulative and offset metrics state their time semantics as fields (DEC-37), not only
    in the formula text; the request's filters apply before a running total or a shift, and a
    filter on the time dimension chooses a rolling window's output days (DEC-34)."""
    service = make_service(graph, connector, tmp_path)

    def times(request: QueryRequest) -> dict:
        ev = service.query(request, Principal(channel="api"))["evidence"]
        return {m["name"]: m["time"] for m in ev["metrics"]}

    found = times(
        QueryRequest(
            metrics=["revenue_year_to_date", "revenue", "revenue_mom_growth"],
            dimensions=["orders.ordered_at__month"],
            filters=["{orders.status} <> 'cancelled'"],
        )
    )
    assert found["revenue"] is None
    assert found["revenue_year_to_date"] == {
        "kind": "grain_to_date",
        "period": "year",
        "time_dimension": "orders.ordered_at__month",
        "query_grain": "month",
        "filters_applied": "before",
    }
    assert found["revenue_mom_growth"] == {
        "kind": "offset",
        "shifts": [{"metric": "revenue_prev_month", "input": "revenue", "offset": "1 month"}],
        "time_dimension": "orders.ordered_at__month",
        "query_grain": "month",
        "filters_applied": "before",
    }

    found = times(
        QueryRequest(
            metrics=["revenue_rolling_7_days", "order_count_7_days_earlier"],
            dimensions=["orders.status", "orders.ordered_at__day"],
        )
    )
    assert found["revenue_rolling_7_days"] == {
        "kind": "window",
        "window": "7 days",
        "days": 7,
        "time_dimension": "orders.ordered_at__day",
        "query_grain": "day",
        "filters_applied": "time_after",
    }
    assert found["order_count_7_days_earlier"]["shifts"] == [
        {"metric": "order_count_7_days_earlier", "input": "order_count", "offset": "7 days"}
    ]


def test_time_block_is_null_for_metrics_without_time_semantics(graph):
    """Simple, ratio and derived metrics without an offset, and measures, have no time block;
    an offset metric has one even when built outside a query (DEC-37)."""
    from cosmos_semantic.evidence import EvidenceBuilder

    builder = EvidenceBuilder(graph)
    request = QueryRequest(metrics=["revenue"], dimensions=["orders.ordered_at__month"])
    for name in ("revenue", "average_order_value", "customer_count"):
        assert builder.time(name, request) is None
    assert builder.time("revenue_prev_month", request)["shifts"] == [
        {"metric": "revenue_prev_month", "input": "revenue", "offset": "1 month"}
    ]


def test_filters_applied_matches_the_planner(graph, duckdb_connector, tmp_path):
    """`filters_applied` (DEC-37) is what the planner does. `before`: a filter on the time
    dimension also removes the earlier periods an offset reads. `time_after` (DEC-34): a
    rolling window still reads the days before the filtered range; a filter shape the planner
    cannot split (here `NOT`) keeps `before` and cuts the window."""
    service = make_service(graph, duckdb_connector, tmp_path)

    def rows(metrics, dimension, filters=()):
        request = QueryRequest(metrics=metrics, dimensions=[dimension], filters=list(filters))
        result = service.query(request, Principal(channel="api"))
        times = {m["name"]: m["time"] for m in result["evidence"]["metrics"]}
        return result["rows"], times

    def key(value):
        return None if value is None else str(value)[:10]  # the period's first day

    def since(day):
        return [f"{{orders.ordered_at}} >= DATE '{day}'"]

    # Offset: a month whose previous month has revenue gets NULL once the filter starts there.
    month_col = "orders.ordered_at__month"
    unfiltered, _ = rows(["revenue", "revenue_prev_month"], month_col)
    month = next(
        key(r["orders__ordered_at__month"])
        for r in unfiltered
        if r["revenue_prev_month"] is not None
    )
    filtered, times = rows(["revenue", "revenue_prev_month"], month_col, since(month))
    assert times["revenue_prev_month"]["filters_applied"] == "before"
    first = next(r for r in filtered if key(r["orders__ordered_at__month"]) == month)
    assert first["revenue"] is not None
    assert first["revenue_prev_month"] is None

    # Window: a day whose 7-day window reaches earlier revenue keeps its whole window once the
    # filter starts there, and sums only its own day under a filter that cannot be split.
    day_col = "orders.ordered_at__day"
    unfiltered, times = rows(["revenue", "revenue_rolling_7_days"], day_col)
    assert times["revenue_rolling_7_days"]["filters_applied"] == "time_after"
    full = next(
        r
        for r in unfiltered
        if r["orders__ordered_at__day"] is not None
        and r["revenue"] is not None
        and r["revenue_rolling_7_days"] > r["revenue"] + 0.005
    )
    day = key(full["orders__ordered_at__day"])
    filtered, times = rows(["revenue", "revenue_rolling_7_days"], day_col, since(day))
    assert times["revenue_rolling_7_days"]["filters_applied"] == "time_after"
    first = next(r for r in filtered if key(r["orders__ordered_at__day"]) == day)
    assert first["revenue_rolling_7_days"] == pytest.approx(full["revenue_rolling_7_days"])
    assert first["revenue"] == pytest.approx(full["revenue"])

    unsplit = [f"NOT ({{orders.ordered_at}} < DATE '{day}')"]
    filtered, times = rows(["revenue", "revenue_rolling_7_days"], day_col, unsplit)
    assert times["revenue_rolling_7_days"]["filters_applied"] == "before"
    first = next(r for r in filtered if key(r["orders__ordered_at__day"]) == day)
    assert first["revenue_rolling_7_days"] == pytest.approx(first["revenue"])
