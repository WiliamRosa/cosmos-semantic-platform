"""Importers: the imported model returns the same numbers as the native model on the same data."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.interchange import dump_project, import_model
from cosmos_semantic.interchange.ossie import VENDOR
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.spec import Project

FIXTURES = Path(__file__).parent / "fixtures"
SUL = Principal(user={"region": "Midwest"})


def rows(graph, connector, request: QueryRequest, principal=None):
    sql = (
        Planner(graph, connector.dialect, connector.config.schema_map).plan(request, principal).sql
    )
    out = connector.execute(sql).to_pylist()
    return sorted(
        (
            tuple(round(float(v), 4) if isinstance(v, int | float) else v for v in r.values())
            for r in out
        ),
        key=str,
    )


def same(a, b):
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        for u, v in zip(x, y, strict=True):
            if isinstance(u, float):
                assert math.isclose(u, v, rel_tol=1e-6), (x, y)
            else:
                assert u == v, (x, y)


def imported(kind: str, path: Path, **options):
    result = import_model(kind, path, **options)
    # The generated YAML must be a valid v1 model on its own.
    text = dump_project(result.project)
    project = Project.model_validate(yaml.safe_load(text))
    return compile_project(project), result.warnings


CASES = [
    (QueryRequest(metrics=["revenue", "order_count"], dimensions=["orders.status"]), None),
    (QueryRequest(metrics=["completed_revenue"], dimensions=["orders.ordered_at__month"]), None),
    (QueryRequest(metrics=["average_order_value"], dimensions=["orders.status"]), None),
]


@pytest.mark.parametrize(
    "kind,path,options",
    [
        ("cube", FIXTURES / "cube", {}),
        ("dbt", FIXTURES / "dbt_legacy", {"schema": "public"}),
        ("dbt", FIXTURES / "dbt_latest", {"schema": "public"}),
    ],
    ids=["cube", "dbt-legacy", "dbt-latest"],
)
@pytest.mark.parametrize(
    "source",
    [
        "duckdb_connector",
        "postgres_connector",
        "sqlserver_connector",
        "databricks_connector",
        "snowflake_connector",
        "mysql_connector",
        "mariadb_connector",
        "oracle_connector",
    ],
)
def test_imported_model_matches_native(kind, path, options, source, graph, request):
    connector = request.getfixturevalue(source)
    other, _ = imported(kind, path, **options)
    for query, principal in CASES:
        same(rows(graph, connector, query, principal), rows(other, connector, query))


def test_cube_joins_and_row_level_equivalence(graph, duckdb_connector):
    other, warnings = imported("cube", FIXTURES / "cube")
    native = rows(
        graph,
        duckdb_connector,
        QueryRequest(metrics=["revenue", "customer_count"], dimensions=["customers.segment"]),
        SUL,
    )
    cube = rows(
        other,
        duckdb_connector,
        QueryRequest(
            metrics=["revenue", "customer_count"],
            dimensions=["customers.segment"],
            filters=["{customers.region} = 'Midwest'"],
        ),
    )
    same(native, cube)
    paid = QueryRequest(metrics=["paid_value"], dimensions=["orders.status"])
    same(rows(graph, duckdb_connector, paid), rows(other, duckdb_connector, paid))
    # The segment became a business rule and works as a filter
    seg = rows(
        other,
        duckdb_connector,
        QueryRequest(metrics=["revenue"], filters=["{rule.completed_orders}"]),
    )
    done = rows(graph, duckdb_connector, QueryRequest(metrics=["completed_revenue"]))
    same(seg, done)
    joined = " ".join(warnings)
    assert "geo" in joined and "pre_aggregations" in joined and "views" in joined


def test_cube_rejects_code_and_templates(tmp_path):
    (tmp_path / "orders.js").write_text("cube(`orders`, {})", encoding="utf-8")
    (tmp_path / "t.yml").write_text(
        "cubes:\n  - name: x\n    sql_table: {{ t }}\n", encoding="utf-8"
    )
    (tmp_path / "cube.js").write_text("module.exports = {queryRewrite: () => {}}", encoding="utf-8")
    result = import_model("cube", tmp_path)
    joined = " ".join(result.warnings)
    assert "JavaScript" in joined and "template" in joined and "row_filters" in joined
    assert result.project.semantic_models == []


def test_dbt_reports_what_was_not_imported():
    _, legacy = imported("dbt", FIXTURES / "dbt_legacy", schema="public")
    assert any("percentile" in w for w in legacy)
    assert any("offset_window" in w for w in legacy)
    _, latest = imported("dbt", FIXTURES / "dbt_latest", schema="public")
    assert any("conversion" in w for w in latest)


def test_dbt_jinja_filter_becomes_reference(graph, duckdb_connector):
    other, _ = imported("dbt", FIXTURES / "dbt_legacy", schema="public")
    assert other.metrics["midwest_revenue"].filter == "{customers.region} = 'Midwest'"
    native = rows(
        graph,
        duckdb_connector,
        QueryRequest(metrics=["revenue"], dimensions=["customers.region"]),
        SUL,
    )
    dbt = rows(other, duckdb_connector, QueryRequest(metrics=["midwest_revenue"]))
    assert math.isclose(native[0][1], dbt[0][0], rel_tol=1e-6)


def test_ossie_import(graph, duckdb_connector):
    other, warnings = imported("ossie", FIXTURES / "ossie" / "ecommerce.yaml")
    assert not warnings
    for metric in ("revenue", "order_count", "average_order_value"):
        request = QueryRequest(metrics=[metric], dimensions=["orders.status"])
        same(rows(graph, duckdb_connector, request), rows(other, duckdb_connector, request))
    per_customer = rows(other, duckdb_connector, QueryRequest(metrics=["revenue_per_customer"]))
    total = rows(graph, duckdb_connector, QueryRequest(metrics=["revenue"]))[0][0]
    count = duckdb_connector.execute("SELECT COUNT(*) AS n FROM public.customers").to_pylist()[0][
        "n"
    ]
    assert math.isclose(per_customer[0][0], total / count, rel_tol=1e-6)
    assert other.project.glossary[0].alt_labels["pt"] == ["região"]


def test_ossie_restores_cosmos_projects_without_loss(tmp_path, graph):
    doc = {
        "version": "0.2.0.dev0",
        "name": "x",
        "datasets": [],
        "custom_extensions": [
            {
                "vendor_name": VENDOR,
                "data": json.dumps({"project": graph.project.model_dump(mode="json")}),
            }
        ],
    }
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    result = import_model("ossie", path)
    assert result.project.model_dump(mode="json") == graph.project.model_dump(mode="json")
