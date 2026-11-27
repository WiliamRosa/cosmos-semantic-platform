"""Export to Databricks metric views (DEC-42): the YAML's structure, what fails closed, and the
same numbers as the planner. On DuckDB each metric view is evaluated by a small emulator (the
view's measures grouped by a dimension over the source LEFT JOINed to its joins); on Databricks
the views are created and queried with ``MEASURE()``, skipped without Databricks credentials."""

from __future__ import annotations

import decimal
import math
import os
import uuid

import pytest
import sqlglot
import yaml
from sqlglot import exp
from typer.testing import CliRunner

from cosmos_semantic.cli import app
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import MetricLossError, PolicyLossError
from cosmos_semantic.interchange.databricks import export_databricks, metric_views
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import Planner, QueryRequest
from cosmos_semantic.spec import Project

from .conftest import EXAMPLE

LOSS_FLAGS = {"allow_policy_loss": True, "allow_metric_loss": True}
# Metrics the export refuses in the example: running totals, rolling windows, offsets (and
# the growth over an offset), and metrics spanning models.
REFUSED = {
    "customers_rolling_30_days",
    "revenue_year_to_date",
    "revenue_month_to_date",
    "max_order_value_year_to_date",
    "revenue_rolling_7_days",
    "smallest_order_rolling_7_days",
    "revenue_rolling_3_months",
    "order_count_rolling_12_months",
    "largest_order_rolling_3_months",
    "revenue_prev_month",
    "revenue_mom_growth",
    "revenue_yoy_growth",
    "order_count_7_days_earlier",
    "collection_gap",
    "items_per_order",
}
# Per view, the dimensions the numbers are compared by: none (the total), one of the model's
# own and one of a joined model.
GROUPS = {
    "orders": [None, ("status", "orders.status"), ("customers_segment", "customers.segment")],
    "order_items": [
        None,
        ("orders_status", "orders.status"),
        ("products_category", "products.category"),
    ],
    "payments": [None, ("method", "payments.method"), ("orders_status", "orders.status")],
    "customers": [None, ("segment", "customers.segment")],
    "products": [None, ("category", "products.category")],
}


def _ungoverned(project: Project) -> Project:
    """The project without row filters, as the export (with --allow-policy-loss) writes it."""
    data = project.model_dump(mode="json", exclude_none=True)
    for model in data["semantic_models"]:
        model["row_filters"] = []
    return Project.model_validate(data)


@pytest.fixture(scope="module")
def project():
    return load_project(EXAMPLE)


def test_structure(project):
    views, warnings = metric_views(project, catalog="main", **LOSS_FLAGS)
    assert list(views) == ["customers", "products", "orders", "order_items", "payments"]
    orders = views["orders"]
    assert orders["version"] == 0.1
    assert orders["source"] == "main.public.orders"
    assert orders["joins"] == [
        {
            "name": "customers",
            "source": "main.public.customers",
            "on": "source.customer_id = customers.customer_id",
        }
    ]
    dims = {d["name"]: d["expr"] for d in orders["dimensions"]}
    # Bare columns of the view's own model are written `source.<col>`: `customer_id` is a
    # column of `customers` too.
    assert dims["has_discount"] == "source.discount > 0"
    assert dims["customers_segment"] == "customers.segment"
    measures = {m["name"]: m["expr"] for m in orders["measures"]}
    assert measures["order_count"] == "COUNT(*)"
    assert measures["buyers"] == "COUNT(DISTINCT source.customer_id)"
    assert measures["completed_revenue"] == (
        "SUM(CASE WHEN source.status = 'completed' THEN source.amount END)"
    )
    assert measures["average_order_value"] == (
        "CAST(SUM(source.amount) AS DOUBLE) / NULLIF(COUNT(*), 0)"
    )
    assert measures["net_revenue"] == "(SUM(source.amount) - SUM(source.discount))"
    assert measures["orders_per_buyer"] == (
        "(CAST(COUNT(*) AS DOUBLE) / NULLIF(COUNT(DISTINCT source.customer_id), 0))"
    )
    avg = {m["name"]: m["expr"] for m in views["order_items"]["measures"]}["avg_quantity"]
    assert avg == "AVG(CAST(source.quantity AS DOUBLE))"
    exported = {m["name"] for v in views.values() for m in v["measures"]}
    assert not exported & REFUSED
    names = {m.name for m in project.metrics} | {
        x.name for m in project.semantic_models for x in m.measures
    }
    assert exported == names - REFUSED
    joined = "\n".join(warnings)
    for name in REFUSED:
        assert f"metrics.{name}: " in joined
    assert "metrics.collection_gap: it spans models (orders, payments)" in joined
    assert "metrics.revenue_prev_month: it reads an input at an earlier period" in joined
    assert "semantic_models.customers.row_filters.by_region: not exported to Databricks" in joined
    assert "semantic_models.customers.dimensions.email: its sensitivity is not exported" in joined
    assert "semantic_models.order_items: the dimensions of customers are reached" in joined
    assert "source: the tables" not in joined
    # Without a catalog, the two-part names are written as they are, with a warning.
    _, warnings = metric_views(project, **LOSS_FLAGS)
    assert any(w.startswith("source: the tables public.customers") for w in warnings)


def test_text_is_one_document_per_view(project):
    result = export_databricks(project, **LOSS_FLAGS)
    docs = list(yaml.safe_load_all(result.text))
    assert [d["source"] for d in docs] == [
        f"public.{m}" for m in ("customers", "products", "orders", "order_items", "payments")
    ]
    assert result.text.startswith("# Metric view for the model `customers`\nversion: 0.1\n")
    # `on` is quoted, so a YAML 1.1 reader keeps it a key rather than the boolean true.
    assert "  'on': source.customer_id = customers.customer_id\n" in result.text


def test_fails_closed(project):
    with pytest.raises(PolicyLossError, match="row filter.*customers.by_region"):
        export_databricks(project, allow_metric_loss=True)
    with pytest.raises(MetricLossError, match="would NOT be exported.*revenue_year_to_date"):
        export_databricks(project, allow_policy_loss=True)


def test_filter_refusals(project):
    data = _ungoverned(project).model_dump(mode="json", exclude_none=True)
    data["metrics"] += [
        {"name": "by_segment", "type": "simple", "measure": "revenue",
         "filter": "{customers.segment} = 'Corporate'"},
        {"name": "by_month", "type": "simple", "measure": "revenue",
         "filter": "{ordered_at__month} = DATE '2024-01-01'"},
        {"name": "by_rule", "type": "simple", "measure": "revenue",
         "filter": "{rule.order_size_band} = 'Large'"},
        {"name": "by_band", "type": "simple", "measure": "revenue",
         "filter": "{order_size_band} = 'Large'"},
        {"name": "by_band_qualified", "type": "simple", "measure": "revenue",
         "filter": "{orders.order_size_band} = 'Large'"},
        {"name": "active", "type": "simple", "measure": "customer_count",
         "filter": "{active_customer}"},
        {"name": "missing_rule", "type": "simple", "measure": "revenue",
         "filter": "{rule.completed_orders}"},
        {"name": "last_30_days", "type": "simple", "measure": "revenue",
         "filter": "{ordered_at} >= CURRENT_DATE - INTERVAL '30' DAY"},
        {"name": "bare", "type": "derived", "expr": "{revenue} + amount"},
        {"name": "uses_refused", "type": "ratio", "numerator": "by_segment",
         "denominator": "order_count"},
        {"name": "fine", "type": "ratio", "numerator": "revenue", "denominator": "order_count",
         "filter": "{has_discount}"},
    ]  # fmt: skip
    # A model dimension with the name the export gives a joined one.
    orders = next(m for m in data["semantic_models"] if m["name"] == "orders")
    orders["dimensions"].append({"name": "customers_segment", "type": "categorical"})
    views, warnings = metric_views(
        Project.model_validate(data), allow_policy_loss=True, allow_metric_loss=True
    )
    joined = "\n".join(warnings)
    # A business rule is a dimension of its models too; under every spelling it is refused,
    # since the planner reads the version in effect on rules_as_of with the clock at as_of.
    for name, ref in [
        ("by_rule", "rule.order_size_band"),
        ("by_band", "order_size_band"),
        ("by_band_qualified", "orders.order_size_band"),
    ]:
        assert f"metrics.{name}: its filter uses the business rule `order_size_band` (`{ref}`)" in (
            joined
        )
    assert (
        "metrics.active: its filter uses the business rule `active_customer` (`active_customer`)"
        in joined
    )
    assert "metrics.missing_rule: its filter reference `rule.completed_orders` does not" in joined
    exported = {m["name"] for v in views.values() for m in v["measures"]}
    assert not exported & {"by_rule", "by_band", "by_band_qualified", "active", "missing_rule"}
    # A filter reading the clock is exported, with a warning.
    assert "metrics.last_30_days: its filter reads the clock" in joined
    assert "last_30_days" in exported
    assert (
        "semantic_models.orders: the dimension `customers_segment` has the name of one of the "
        "model's own dimensions in its metric view; dimension skipped"
    ) in joined
    segment = [d for d in views["orders"]["dimensions"] if d["name"] == "customers_segment"]
    assert segment == [{"name": "customers_segment", "expr": "source.customers_segment"}]
    assert "metrics.by_segment: its filter uses `customers.segment` from another model" in joined
    assert "metrics.by_month: its filter uses a time grain" in joined
    assert "metrics.bare: its expression uses bare columns (amount)" in joined
    assert "metrics.uses_refused: it uses `by_segment`, which is not exported" in joined
    fine = {m["name"]: m["expr"] for m in views["orders"]["measures"]}["fine"]
    assert fine == (
        "CAST(SUM(CASE WHEN source.discount > 0 THEN source.amount END) AS DOUBLE) / "
        "NULLIF(COUNT(CASE WHEN source.discount > 0 THEN 1 END), 0)"
    )


def test_cli_export_databricks(tmp_path):
    runner = CliRunner()
    refused = runner.invoke(app, ["export", "databricks", "-m", str(EXAMPLE)])
    assert refused.exit_code == 1
    assert refused.stdout == ""
    assert "1 row filter (customers.by_region)" in refused.stderr
    flags = ["--allow-policy-loss", "--allow-metric-loss", "--catalog", "main"]
    result = runner.invoke(app, ["export", "databricks", "-m", str(EXAMPLE), *flags])
    assert result.exit_code == 0, result.output
    assert "warning:" not in result.stdout
    assert len(list(yaml.safe_load_all(result.stdout))) == 5
    assert "warning: metrics.revenue_year_to_date: it is a running total" in result.stderr
    out = tmp_path / "views" / "metric_views.yml"
    result = runner.invoke(
        app, ["export", "databricks", "-m", str(EXAMPLE), "-o", str(out), *flags]
    )
    assert result.exit_code == 0, result.output
    assert "Databricks metric views written to" in result.stdout
    assert yaml.safe_load_all(out.read_text(encoding="utf-8"))


# -- same numbers --------------------------------------------------------------------------


def _norm(rows: list[tuple]) -> list[tuple]:
    def value(v):
        if isinstance(v, decimal.Decimal | int) and not isinstance(v, bool):
            return float(v)
        return v

    return sorted((tuple(value(v) for v in r) for r in rows), key=str)


def _same(a: list[tuple], b: list[tuple], what: str) -> None:
    assert len(a) == len(b), what
    for x, y in zip(a, b, strict=True):
        for u, v in zip(x, y, strict=True):
            if isinstance(u, float) and isinstance(v, float):
                assert math.isclose(u, v, rel_tol=1e-9, abs_tol=1e-9), (what, x, y)
            else:
                assert u == v, (what, x, y)


def _planner_rows(graph, connector, metric: str, dim: str | None) -> list[tuple]:
    request = QueryRequest(metrics=[metric], dimensions=[dim] if dim else [])
    sql = Planner(graph, connector.dialect, connector.config.schema_map).plan(request).sql
    return [tuple(r.values()) for r in connector.execute(sql).to_pylist()]


def _emulated_sql(view: dict, measure: str, dim: str | None) -> str:
    """The metric view query ``SELECT dim, MEASURE(measure) ... GROUP BY ALL`` as plain SQL:
    bare columns belong to the source, joins are LEFT OUTER, as in Databricks."""

    def own(text: str) -> exp.Expression:
        tree = sqlglot.parse_one(text, read="databricks")
        return tree.transform(
            lambda n: (
                exp.column(n.this.copy(), table="source")
                if isinstance(n, exp.Column) and not n.table
                else n
            )
        )

    dims = {d["name"]: d["expr"] for d in view["dimensions"]}
    measures = {m["name"]: m["expr"] for m in view["measures"]}
    select = [own(measures[measure]).as_("m")]
    if dim:
        select.insert(0, own(dims[dim]).as_("d"))
    query = exp.select(*select).from_(exp.to_table(view["source"]).as_("source"))
    for join in view.get("joins", []):
        query = query.join(
            exp.to_table(join["source"]).as_(join["name"]),
            on=sqlglot.parse_one(join["on"], read="databricks"),
            join_type="left",
        )
    if dim:
        query = query.group_by(exp.column("d"))
    return query.sql(dialect="duckdb")


def _compare(views: dict, graph, connector, run) -> int:
    checked = 0
    for model, groups in GROUPS.items():
        view = views[model]
        for measure in (m["name"] for m in view["measures"]):
            for group in groups:
                name, ref = group if group else (None, None)
                _same(
                    _norm(run(model, view, measure, name)),
                    _norm(_planner_rows(graph, connector, measure, ref)),
                    f"{model}.{measure} by {ref}",
                )
                checked += 1
    return checked


def test_same_numbers_duckdb(project, duckdb_connector):
    """Each exported measure, evaluated as a metric view evaluates it, returns the planner's
    numbers in total, by the model's own dimension and by a joined model's dimension."""
    plain = _ungoverned(project)
    views, _ = metric_views(plain, **LOSS_FLAGS)

    def run(model, view, measure, dim):
        rows = duckdb_connector.execute(_emulated_sql(view, measure, dim)).to_pylist()
        return [tuple(r.values()) for r in rows]

    assert _compare(views, compile_project(plain), duckdb_connector, run) == 57


def test_same_numbers_databricks(project, databricks_connector):
    """The exported YAML, created as metric views on Databricks and queried with
    ``MEASURE()``, returns the planner's numbers on the same warehouse."""
    catalog = os.environ.get("DATABRICKS_CATALOG", "cosmos_demo")
    plain = _ungoverned(project)
    views, _ = metric_views(plain, catalog=catalog, **LOSS_FLAGS)
    suffix = uuid.uuid4().hex[:8]
    names = {m: f"{catalog}.public.cosmos_mv_{m}_{suffix}" for m in views}
    cursor = databricks_connector._con.cursor()
    try:
        for model, view in views.items():
            body = yaml.safe_dump(view, sort_keys=False, allow_unicode=True)
            cursor.execute(
                f"CREATE OR REPLACE VIEW {names[model]} WITH METRICS LANGUAGE YAML AS $$\n{body}$$"
            )

        def run(model, view, measure, dim):
            sql = (
                f"SELECT `{dim}`, MEASURE(`{measure}`) FROM {names[model]} GROUP BY ALL"
                if dim
                else f"SELECT MEASURE(`{measure}`) FROM {names[model]}"
            )
            cursor.execute(sql)
            return [tuple(r) for r in cursor.fetchall()]

        assert _compare(views, compile_project(plain), databricks_connector, run) == 57
    finally:
        for name in names.values():
            cursor.execute(f"DROP VIEW IF EXISTS {name}")
        cursor.close()
