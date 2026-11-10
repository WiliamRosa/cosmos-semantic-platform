"""Importers: the imported model returns the same numbers as the native model on the same data."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from cosmos_semantic.cli import app
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import PolicyLossError
from cosmos_semantic.expressions import REF_RE
from cosmos_semantic.interchange import dump_project, import_model
from cosmos_semantic.interchange.ossie import VENDOR, export_ossie
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.reference import load_cases, run_case
from cosmos_semantic.spec import Project

from .conftest import CASES, EXAMPLE

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

QUERIES = [
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
@pytest.mark.parametrize("source", SOURCES)
def test_imported_model_matches_native(kind, path, options, source, graph, request):
    connector = request.getfixturevalue(source)
    other, _ = imported(kind, path, **options)
    for query, principal in QUERIES:
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


# -- Ossie export (first slice: models, entities, dimensions, measures and metrics) -----------

LATER_SLICE = ("glossary", "business_rules", "user_context", "teams", "systems", "processes")
REFERENCE = load_cases(CASES) + load_cases(CASES.with_name("queries_advanced.yml"))
# Metrics kept only in the Cosmos extension, and why.
EXTENSION_ONLY = {
    "collection_gap": "it spans datasets",
    "items_per_order": "it spans datasets",
    "revenue_year_to_date": "it is cumulative",
    "revenue_month_to_date": "it is cumulative",
}


def _exported_slice(project: Project) -> dict:
    """The example as this slice exports it: no row filters, sensitivity or later members."""
    data = project.model_dump(mode="json")
    for key in LATER_SLICE:
        data[key] = Project.model_fields[key].default
    for model in data["semantic_models"]:
        model["row_filters"] = []
        for dim in model["dimensions"]:
            dim["sensitivity"] = None
    return data


def _round_trip(project: Project, tmp_path: Path, fmt: str = "yaml"):
    exported = export_ossie(project, name="ecommerce", fmt=fmt, allow_policy_loss=True)
    path = tmp_path / f"ecommerce.{fmt}"
    path.write_text(exported.text, encoding="utf-8")
    return exported, import_model("ossie", path)


def _cosmos_extension(doc: dict) -> dict:
    return json.loads(
        next(e for e in doc["custom_extensions"] if e["vendor_name"] == VENDOR)["data"]
    )


def _uses_rule(case, rules: set[str]) -> bool:
    refs = REF_RE.findall(" ".join(case.request.filters)) + case.request.dimensions
    names = [r.split("__")[0] for r in refs]
    return any(r.startswith("rule.") or r.split(".")[-1] in rules for r in names)


def _numbers_cases(project: Project) -> list:
    """Reference queries this slice must answer: no user (row filters and sensitivity are not
    exported yet) and no business rules."""
    rules = {r.name for r in project.business_rules}
    return [c for c in REFERENCE if c.question is None and not c.user and not _uses_rule(c, rules)]


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_ossie_export_round_trip(graph, tmp_path, fmt):
    exported, back = _round_trip(graph.project, tmp_path, fmt)
    want = _exported_slice(graph.project)
    got = back.project.model_dump(mode="json")
    # Docs Rule 4: exactly the same authoring model, member by member.
    assert got["semantic_models"] == want["semantic_models"]
    assert got["metrics"] == want["metrics"]
    assert got == want
    # The re-imported model is a valid v1 model on its own.
    compile_project(Project.model_validate(yaml.safe_load(dump_project(back.project))))
    # Nothing outside the slice is dropped silently, on export ...
    joined = "\n".join(exported.warnings)
    for key in LATER_SLICE:
        assert f"{key}: not exported to Ossie yet" in joined
    assert "semantic_models.customers.row_filters: by_region" in joined
    for dim in ("email", "tax_id", "health_plan"):
        assert f"semantic_models.customers.dimensions.{dim}: the sensitivity" in joined
    for name, reason in EXTENSION_ONLY.items():
        assert f"metrics.{name}: {reason}" in joined
    assert sum("only in the" in w for w in exported.warnings) == len(EXTENSION_ONLY)
    assert len(exported.warnings) == 14
    # ... nor on a later import, through the marker in the extension.
    assert back.warnings == [
        "row_filters: the export omitted customers.by_region; this model has no row-level security",
        "sensitivity: the export omitted the personal-data marking of customers.email, "
        "customers.tax_id, customers.health_plan; mark these dimensions again before agents "
        "use them",
        "glossary: the export omitted 6 item(s)",
        "business_rules: the export omitted 3 item(s)",
        "user_context: the export omitted 1 item(s)",
        "teams: the export omitted 4 item(s)",
        "systems: the export omitted 3 item(s)",
        "processes: the export omitted 2 item(s)",
    ]


def test_ossie_export_omitted_marker_has_names_and_counts_only(graph):
    doc = yaml.safe_load(export_ossie(graph.project, allow_policy_loss=True).text)
    assert _cosmos_extension(doc)["omitted"] == {
        "row_filters": ["customers.by_region"],
        "sensitivity": ["customers.email", "customers.tax_id", "customers.health_plan"],
        "glossary": 6,
        "business_rules": 3,
        "user_context": 1,
        "teams": 4,
        "systems": 3,
        "processes": 2,
    }


def test_ossie_export_fails_closed_on_policy_loss(graph):
    with pytest.raises(PolicyLossError) as exc:
        export_ossie(graph.project)
    assert str(exc.value).startswith(
        "1 row filter and 3 sensitive dimensions would NOT be exported"
    )
    assert "--allow-policy-loss" in str(exc.value)
    # Without row filters or sensitivity there is nothing to refuse, and no marker for them.
    data = _exported_slice(graph.project)
    result = export_ossie(Project.model_validate(data))
    assert "omitted" not in _cosmos_extension(yaml.safe_load(result.text))
    assert {w.split(":")[0].removeprefix("metrics.") for w in result.warnings} == set(
        EXTENSION_ONLY
    )


def _with_test_metrics(project: Project) -> Project:
    """The example plus a filtered count, a filtered count_distinct and a filtered avg."""
    data = _exported_slice(project)
    payments = next(m for m in data["semantic_models"] if m["name"] == "payments")
    payments["measures"].append({"name": "avg_paid", "agg": "avg", "expr": "value"})
    data["metrics"] += [
        {"name": "completed_orders", "type": "simple", "measure": "order_count"},
        {"name": "completed_buyers", "type": "simple", "measure": "buyers"},
        {"name": "avg_card_payment", "type": "simple", "measure": "avg_paid"},
    ]
    data["metrics"][-3]["filter"] = "{status} = 'completed'"
    data["metrics"][-2]["filter"] = "{orders.status} = 'completed'"
    data["metrics"][-1]["filter"] = "{method} = 'card'"
    return Project.model_validate(data)


def test_ossie_export_native_document(graph):
    exported = export_ossie(_with_test_metrics(graph.project), name="ecommerce")
    doc = yaml.safe_load(exported.text)
    assert doc["version"] == "0.2.0.dev0" and doc["name"] == "ecommerce"
    datasets = {d["name"]: d for d in doc["datasets"]}
    assert set(datasets) == {m.name for m in graph.project.semantic_models}
    assert datasets["orders"]["source"] == "public.orders"
    assert datasets["orders"]["primary_key"] == ["order_id"]
    fields = {f["name"]: f for f in datasets["orders"]["fields"]}
    assert fields["ordered_at"]["dimension"] == {"is_time": True}
    assert fields["has_discount"]["expression"]["dialects"] == [
        {"dialect": "OSSIE_SQL_2026", "expression": "discount > 0"}
    ]
    assert "dimension" not in fields["customer_id"]
    relationships = {(r["from"], r["to"]): r for r in doc["relationships"]}
    assert relationships[("payments", "orders")]["from_columns"] == ["order_id"]
    assert len(relationships) == sum(len(e) for e in graph.edges.values())

    native: dict[str, str] = {}
    for dataset in doc["datasets"]:
        for metric in dataset.get("metrics", []):
            (dialect,) = metric["expression"]["dialects"]
            assert dialect["dialect"] == "OSSIE_SQL_2026"
            native[metric["name"]] = dialect["expression"]
    # Written as the planner computes them.
    assert native["avg_quantity"] == "AVG(CAST(quantity AS DOUBLE))"
    assert native["avg_card_payment"] == (
        "AVG(CAST(CASE WHEN method = 'card' THEN value END AS DOUBLE))"
    )
    assert native["completed_revenue"] == "SUM(CASE WHEN status = 'completed' THEN amount END)"
    assert native["completed_orders"] == "COUNT(CASE WHEN status = 'completed' THEN 1 END)"
    assert native["completed_buyers"] == (
        "COUNT(DISTINCT CASE WHEN status = 'completed' THEN customer_id END)"
    )
    assert native["average_order_value"] == "CAST(SUM(amount) AS DOUBLE) / NULLIF(COUNT(*), 0)"
    assert native["net_revenue"] == "(SUM(amount)) - (SUM(discount))"
    assert native["orders_per_buyer"] == (
        "CAST((COUNT(*)) AS DOUBLE) / NULLIF((COUNT(DISTINCT customer_id)), 0)"
    )
    # Metrics spanning datasets or cumulative: absent from the native part, kept in the
    # extension.
    assert doc["metrics"] == []
    assert not set(EXTENSION_ONLY) & set(native)
    kept = {m["name"] for m in _cosmos_extension(doc)["project"]["metrics"]}
    assert set(EXTENSION_ONLY) <= kept


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_export_same_numbers(graph, tmp_path, source, request):
    """The exported and re-imported model returns the hand-written SQL's numbers, as the
    native model does (tests/test_reference.py), on every database available."""
    connector = request.getfixturevalue(source)
    _, back = _round_trip(graph.project, tmp_path)
    other = compile_project(back.project)
    cases = _numbers_cases(graph.project)
    assert len(cases) == 27
    for case in cases:
        result = run_case(case, other, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_export_native_part_same_numbers(graph, tmp_path, source, request):
    """A Cosmos import of the native part alone, without the Cosmos extension, returns the
    same numbers for every metric the document expresses."""
    connector = request.getfixturevalue(source)
    project = _with_test_metrics(graph.project)
    full = compile_project(project)
    doc = yaml.safe_load(export_ossie(project).text)
    doc["custom_extensions"] = [e for e in doc["custom_extensions"] if e["vendor_name"] != VENDOR]
    path = tmp_path / "native.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    native, warnings = imported("ossie", path)
    assert warnings == []
    names = [m["name"] for d in doc["datasets"] for m in d.get("metrics", [])]
    assert len(names) == 21
    for name in names:
        for dims in ([], ["orders.status"]):
            query = QueryRequest(metrics=[name], dimensions=dims)
            if dims and full.measure_owner.get(name) in ("customers", "products", "sellers"):
                continue  # those models do not reach orders
            same(rows(full, connector, query), rows(native, connector, query))


def test_ossie_export_states_each_reason(graph):
    data = _exported_slice(graph.project)
    data["metrics"] += [
        {
            "name": "midwest_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{customers.region} = 'Midwest'",
        },
        {
            "name": "recent_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{orders.ordered_at__month} >= DATE '2026-01-01'",
        },
        {"name": "my_revenue", "type": "simple", "measure": "revenue", "filter": "$user.id = 1"},
        {"name": "raw_gap", "type": "derived", "expr": "{revenue} - amount"},
        {
            "name": "gap_share",
            "type": "ratio",
            "numerator": "collection_gap",
            "denominator": "revenue",
        },
    ]
    warnings = export_ossie(Project.model_validate(data)).warnings
    reasons = {w.split(": ", 1)[0]: w.split(": ", 1)[1] for w in warnings}
    assert reasons["metrics.midwest_revenue"].startswith(
        "its filter uses `customers.region` from another dataset (customers)"
    )
    assert reasons["metrics.recent_revenue"].startswith(
        "its filter uses a time grain (`orders.ordered_at__month`)"
    )
    assert reasons["metrics.my_revenue"].startswith("its filter uses $user")
    assert reasons["metrics.raw_gap"].startswith("its expression uses bare columns (amount)")
    assert reasons["metrics.gap_share"].startswith("it spans datasets")
    with pytest.raises(ValueError):
        export_ossie(graph.project, fmt="xml", allow_policy_loss=True)


def test_cli_export_ossie(tmp_path):
    runner = CliRunner()
    refused = runner.invoke(app, ["export", "ossie", "-m", str(EXAMPLE)])
    assert refused.exit_code == 1
    assert refused.stdout == ""
    assert "1 row filter and 3 sensitive dimensions would NOT be exported" in refused.stderr
    result = runner.invoke(app, ["export", "ossie", "-m", str(EXAMPLE), "--allow-policy-loss"])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(result.stdout)
    assert doc["name"] == "ecommerce" and doc["datasets"]
    assert "warning:" not in result.stdout
    assert "warning: glossary: not exported to Ossie yet" in result.stderr
    out = tmp_path / "out" / "model.json"
    result = runner.invoke(
        app,
        [
            "export",
            "ossie",
            "-m",
            str(EXAMPLE),
            "-o",
            str(out),
            "-f",
            "json",
            "--allow-policy-loss",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "14 warnings" in result.stdout
    assert "semantic_models.customers.row_filters" in result.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["name"] == "ecommerce"
    assert import_model("ossie", out).project.semantic_models
